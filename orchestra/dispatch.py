"""orchestra.dispatch — launching new agents, and typing at the ones we launch.

Dispatch is the act layer's other half: ✓ finish ends a mission, dispatch
starts one. It picks deterministically — the cleanest FREE worktree, the
account with the most headroom that can actually run the chosen model — and
refuses to choose the model or the effort for you. When no account clears its
reserve for that model, it comes back with a needs_decision instead of quietly
spending someone else's usage.

The launch itself is tmux on its own socket (`FLEET_SOCK`), so the fleet never
collides with the user's own tmux server. Getting the brief INTO the CLI is
the fiddly part: the composer's paste heuristic chops a rapid send-keys burst
into '[Pasted text #N]' chips that swallow the Enter, so `deliver_text` pastes
atomically via a tmux buffer and then presses Enter until `kickoff_sent`
proves the composer let go of it.

Every launch appends a line to `DISPATCH_LOG` — the audit trail, with the
author's original words next to the brief the agent actually got. Jobs run on
background threads; `_jobs` holds their progress so the browser can poll
`dispatch_status` without holding an HTTP request open.

`closeout_shell` lives here rather than in `finish`, where its prose belongs:
it is the tmux command a DISPATCH runs, `_run_dispatch` is its only caller,
and keeping it here is what breaks the finish↔dispatch import cycle (ADR 0010,
'cycles'). It takes the brief as a parameter and touches no CLOSEOUT_* text,
so it carries nothing with it.

DISPATCH_LOG is rebound at runtime (tests point it at a temp file), so it is
deliberately NOT re-exported by the facade — reach it as `dispatch.DISPATCH_LOG`.
"""

import json
import re
import shlex
import threading
import time

from . import config, shell, gitrepo, transcripts, limits, observer


# --------------------------------------------------------------- dispatch

FLEET_SOCK = "fleet"
DISPATCH_LOG = config.HERE / "dispatch.log.jsonl"


def read_dispatch_log(limit=25):
    """Recent dispatches, newest first, each annotated with whether its tmux
    session is still alive."""
    if not DISPATCH_LOG.exists():
        return {"entries": []}
    rows = []
    try:
        for line in DISPATCH_LOG.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return {"entries": []}
    live = set()
    rc, out = shell.run(["tmux", "-L", FLEET_SOCK, "list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        live = set(out.splitlines())
    for r in rows:
        r["alive"] = r.get("session") in live
    return {"entries": rows[-limit:][::-1]}


def _pick_defaults(model=None):
    """Deterministic auto-picks, no AI in the loop: the cleanest FREE worktree,
    and the account with the most headroom that can actually run `model`
    (falling back to overall headroom when none clears its reserve — that only
    happens on a forced model, where the user already chose to push through)."""
    state = observer.cached_state()
    limits.cached_limits()  # ensure the account picker isn't working from a cold cache
    by_acct = limits.limits_by_account()   # local rename: `limits` is a module now
    free = [w for w in state["worktrees"] if w["availability"] == "free"]
    free.sort(key=lambda w: (w["git"]["dirty"] or 0))
    wt = free[0]["name"] if free else None
    acct = None
    if model:
        acct = next((c["label"] for c in limits.model_candidates(model) if c["ok"]), None)
    if acct is None:
        excl = set(config.CFG.get("exclude_accounts") or [])
        accounts = [(a, d) for a, d in by_acct.items() if d["available"] and a not in excl]
        accounts.sort(key=lambda x: -(x[1]["headroom"] or 0))
        acct = accounts[0][0] if accounts else None
    return wt, acct


_jobs = {}                 # job_id -> {progress, done, result}
_jobs_lock = threading.Lock()
_job_seq = [0]


def _log(job, line):
    with _jobs_lock:
        job["progress"].append(line)


def start_dispatch(mission, worktree=None, account=None,
                   model=None, effort=None, force_model=False,
                   closeout_trunk=None):
    """Kick a dispatch off in the background; return a job id to poll.
    Routing is deterministic — cleanest free worktree, most-headroom account
    that can run the chosen model — and choosing model + effort is the
    caller's job (only closeouts run without them). If the chosen model has
    no account with enough headroom (above reserve), return a needs_decision
    instead of launching — unless force_model.
    With closeout_trunk set the mission runs as a one-shot closeout (headless
    claude that verifies its own landing against that trunk ref)."""
    if not closeout_trunk and not (model and effort):
        return {"ok": False, "message":
                "pick a model and an effort first — routing is deterministic, "
                "nothing is chosen for you"}
    # closeouts skip the headroom dialog: ✓ finish must always just run, and
    # _pick_defaults already prefers an account with headroom for the model
    if not config.DEMO and model and not force_model and not closeout_trunk:
        limits.cached_limits()
        cands = limits.model_candidates(model, only_account=account)
        if not any(c["ok"] for c in cands):
            best = cands[0] if cands else None
            opus = limits.model_candidates("opus", only_account=account)
            best_opus = next((c for c in opus if c["ok"]), None)
            where = f"account [{account}]" if account else "any account"
            if best and best["reserve"] > 0:
                detail = (f"best is [{best['label']}] at {best['remaining']}% "
                          f"left, below its {best['reserve']}% reserve")
            elif best:
                detail = (f"best is [{best['label']}] at {best['remaining']}% — "
                          f"the {model} limit is used up")
            else:
                detail = "no readable account for this model"
            return {"ok": False, "needs_decision": True, "model": model,
                    "message": f"No {model} headroom on {where} — {detail}.",
                    "can_opus": bool(best_opus),
                    "opus_account": best_opus["label"] if best_opus else None,
                    "opus_left": best_opus["remaining"] if best_opus else None}
    _job_seq[0] += 1
    job_id = "job-" + time.strftime("%H%M%S") + f"-{_job_seq[0]}"
    job = {"progress": [], "done": False, "result": None}
    with _jobs_lock:
        _jobs[job_id] = job
        for old in list(_jobs)[:-20]:   # keep only the last 20 jobs
            del _jobs[old]
    threading.Thread(target=_run_dispatch, daemon=True, args=(
        job, mission, worktree, account, model, effort,
        closeout_trunk)).start()
    return {"job": job_id}


def kickoff_sent(pane):
    """True once the composer no longer holds the brief: the CLI is visibly
    mid-turn, or the input line at the bottom of the pane is bare again."""
    if "esc to interrupt" in pane:
        return True
    prompts = [l.strip() for l in pane.splitlines()
               if l.lstrip().startswith(("❯", ">"))]
    return bool(prompts) and prompts[-1] in ("❯", ">")


def composer_idle(pane):
    """True when the composer is bare with no turn or compaction in flight —
    the only state where a paste reliably lands in the input line. Distinct
    from kickoff_sent: there a bare prompt proves the brief LEFT the composer;
    here it must prove the CLI is ready to RECEIVE, so any activity vetoes."""
    if "esc to interrupt" in pane or "ompacting" in pane:
        return False
    prompts = [l.strip() for l in pane.splitlines()
               if l.lstrip().startswith(("❯", ">"))]
    return bool(prompts) and prompts[-1] in ("❯", ">")


def deliver_text(name, text):
    """Put `text` into a fleet tmux session's claude composer and press Enter
    until the send is proven (see kickoff_sent). Pasting atomically (bracketed,
    via a tmux buffer) sidesteps the CLI's paste heuristic, which chops a rapid
    send-keys burst into '[Pasted text #N]' chips that swallow the Enter."""
    shell.run(["tmux", "-L", FLEET_SOCK, "set-buffer", "-b", "orchestra-kickoff", text])
    shell.run(["tmux", "-L", FLEET_SOCK, "paste-buffer", "-p", "-d",
               "-b", "orchestra-kickoff", "-t", name])
    time.sleep(1)
    for _ in range(3):
        shell.run(["tmux", "-L", FLEET_SOCK, "send-keys", "-t", name, "Enter"])
        time.sleep(2)
        _, pane = shell.run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        if kickoff_sent(pane):
            return True
    return False


def closeout_shell(home, model, brief, trunk):
    """The tmux command for a one-shot closeout: run claude headless, then let
    git itself verify the landing. Verified clean -> exit, the tmux session
    dies, the card reads FREE with no second ✓ finish. Anything else -> resume
    the conversation interactively, so a failed closeout parks as needs-you
    instead of masquerading as free.

    It reads like finish's prose, but it lives here: it is the shell a
    DISPATCH runs, _run_dispatch is its only caller, and keeping it on this
    side is what stops finish and dispatch importing each other (ADR 0010)."""
    model_flag = f" --model {shlex.quote(model)}" if model else ""
    return (
        f"export CLAUDE_CONFIG_DIR={shlex.quote(str(home))}\n"
        f"claude --dangerously-skip-permissions{model_flag} -p {shlex.quote(brief)}\n"
        f"if git merge-base --is-ancestor HEAD {shlex.quote(trunk)} 2>/dev/null "
        '&& [ -z "$(git status --porcelain)" ]; then exit 0; fi\n'
        "echo; echo '⚠ closeout could not verify a clean landing — resuming the session:'\n"
        # no --model here on purpose: the rescue resumes on the account's
        # default (stronger) model, so a haiku closeout escalates on failure
        "exec claude --dangerously-skip-permissions --continue\n"
    )


def _run_dispatch(job, mission, worktree, account, model, effort,
                  closeout_trunk=None):
    def finish(result):
        with _jobs_lock:
            job["result"] = result
            job["done"] = True

    if config.DEMO:
        return finish({"ok": False, "message": "demo mode — dispatch disabled"})
    mission = (mission or "").strip()
    if not mission:
        return finish({"ok": False, "message": "empty mission"})

    if not (worktree and account):
        dw, da = _pick_defaults(model)
        worktree, account = worktree or dw, account or da
        _log(job, f"① picked → {worktree} · [{account}] "
                  "(cleanest free worktree · most model headroom)")
    if not worktree:
        return finish({"ok": False, "message": "no free worktree available"})
    if not account:
        return finish({"ok": False, "message": "every account is exhausted"})
    wt = next((w for w in gitrepo.discover_worktrees() if w["name"] == worktree), None)
    if not wt:
        return finish({"ok": False, "message": f"unknown worktree {worktree}"})
    home = next((h for h in transcripts.claude_homes()
                 if config.account_label(h) == account), None)
    if not home:
        return finish({"ok": False, "message": f"unknown account {account}"})

    if closeout_trunk:
        # One-shot closeout: no branch header (the branch IS the mission), no
        # effort dance (a headless run takes no slash commands). The wrapper
        # verifies the landing itself — see closeout_shell.
        name = ("closeout-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree)
                .strip("-").lower() + time.strftime("-%H%M%S"))
        _log(job, f"② launching one-shot closeout {name}…")
        rc, out = shell.run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                             "-c", wt["path"],
                             closeout_shell(home, model, mission, closeout_trunk)])
        if rc != 0:
            return finish({"ok": False,
                           "message": f"tmux failed: {out or 'is tmux installed?'}"})
        try:
            with open(DISPATCH_LOG, "a") as lf:
                lf.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": name,
                    "worktree": worktree, "account": account, "model": model,
                    "closeout": True, "mission_original": mission,
                    "kickoff": mission}) + "\n")
        except OSError:
            pass
        _log(job, "✓ launched")
        return finish({"ok": True, "message":
                       f"one-shot closeout running in {worktree} on [{account}] — "
                       "the card frees itself once the landing verifies; if it "
                       "can't land, the session stays open and needs you",
                       "session": name, "worktree": worktree, "account": account,
                       "attach": f"tmux -L {FLEET_SOCK} attach -t {name}"})

    # branch naming is the agent's call — it reads the mission and knows best
    header = ("If this worktree's current branch is not the right home for this "
              "work, check out an appropriately named new branch from latest "
              "origin/main first. ")
    kickoff = (header + "Commit as you go. Your mission, in the author's own "
               "words: " + mission)

    name = "mission-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree).strip("-").lower() \
           + time.strftime("-%H%M%S")
    model_flag = f" --model {shlex.quote(model)}" if model else ""
    shell_cmd = (f"CLAUDE_CONFIG_DIR={shlex.quote(str(home))} "
                 f"exec claude --dangerously-skip-permissions{model_flag}")
    _log(job, f"② creating tmux session {name}…")
    rc, out = shell.run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                         "-c", wt["path"], shell_cmd])
    if rc != 0:
        return finish({"ok": False, "message": f"tmux failed: {out or 'is tmux installed?'}"})
    brief = re.sub(r"\s*\n\s*", " ", kickoff).strip()

    def keys(*args):
        shell.run(["tmux", "-L", FLEET_SOCK, "send-keys", "-t", name] + list(args))

    _log(job, "③ booting claude…")
    time.sleep(6)
    effort_confirmed = None
    if effort:
        _log(job, f"④ setting effort {effort}…")
        keys("-l", f"/effort {effort}")
        keys("Enter")
        time.sleep(3)
        _, pane = shell.run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        effort_confirmed = "set effort level" in pane.lower()
        _log(job, "  effort " + ("confirmed ✓" if effort_confirmed else "UNCONFIRMED ⚠"))
        if not effort_confirmed:
            keys("Escape")
            time.sleep(1)
    _log(job, "⑤ sending kickoff brief…")
    kick_sent = deliver_text(name, brief)
    _log(job, "  kickoff " + ("sent ✓" if kick_sent
                              else "UNCONFIRMED ⚠ — attach and press Enter"))

    try:  # audit trail: every dispatch, with the author's original words
        with open(DISPATCH_LOG, "a") as lf:
            lf.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": name,
                "worktree": worktree, "account": account, "model": model,
                "effort": effort,
                "mission_original": mission, "kickoff": kickoff}) + "\n")
    except OSError:
        pass
    effort_note = ""
    if effort:
        effort_note = f" · effort {effort} " + ("✓" if effort_confirmed else "UNCONFIRMED")
    kick_note = "" if kick_sent else \
        " · ⚠ kickoff UNCONFIRMED — attach and press Enter"
    _log(job, "✓ launched" if kick_sent else "⚠ launched, kickoff unconfirmed")
    finish({"ok": True,
            "message": f"launched {name} in {worktree} on [{account}]"
                       + (f" · {model}" if model else "") + effort_note + kick_note,
            "session": name, "worktree": worktree, "account": account,
            "model": model, "effort": effort, "effort_confirmed": effort_confirmed,
            "kickoff_sent": kick_sent, "kickoff": kickoff,
            "attach": f"tmux -L {FLEET_SOCK} attach -t {name}"})


def dispatch_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job"}
        return {"ok": True, "progress": list(job["progress"]),
                "done": job["done"], "result": job["result"]}
