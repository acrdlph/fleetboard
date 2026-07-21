#!/usr/bin/env python3
"""orchestra — local mission control for parallel Claude Code agents.

Watches your git worktrees, your Claude Code home directories (multi-account
setups included), and live `claude` processes; serves three views on
http://127.0.0.1:4242 — the board (who's working / who needs you / which
worktree is free), the map (real git topology of every branch), and limits
(per-account usage via cclimits) — plus a click-only control plane: chat with
any agent, resume a limit-stuck one when its session limit resets, dispatch
new tmux-hosted agents into free worktrees, and finish a done mission (an
agent lands the branch; the worktree goes free).

Watching is read-only and touches nothing. Acting (chat/resume/dispatch/
finish) happens only on an explicit request — dispatch spends account usage,
and finish hands a closeout brief to an agent that merges and pushes. When
the branch has already landed, finish skips the agent: it parks the worktree
back on the trunk itself (switch + pull — the one provably-safe case where
the board runs git write commands). Zero dependencies — python3 stdlib only.

    python3 -m orchestra --root ~/code
    python3 -m orchestra --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > orchestra.config.json (next to this
script, else cwd) > defaults. See README.md.
"""

import getpass
import json
import os
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler

from . import config, shell, status, gitrepo, procs, transcripts, limits, terminal

# ---- public surface (facade). Re-exported so tests, tools and
# tests/characterize.py can keep saying `orchestra.<name>`. DEMO and
# CONFIG_PATH are deliberately NOT re-exported: they are rebound at runtime,
# so a facade copy would go stale and `orchestra.DEMO = True` would be a patch
# that lies. Reach them as `orchestra.config.DEMO`.
from .config import CFG, HOME, HERE, load_config, account_label
from .shell import run
from .status import classify_session, closeout_step, card_availability
from .gitrepo import (munge, match_worktree, discover_worktrees, git_info,
                      _base_ref, branch_topology, demo_topology,
                      cached_topology, TOPO_TTL_S, _topo)
from .procs import (claude_processes, pair_sessions_with_procs, shell_children,
                    _pid_cwds, _pid_config_dirs, _host_of, _tmux_pane_map)
from .transcripts import (claude_homes, _read_chunk, _clean, _real_prompt,
                          session_topic, last_assistant_text, find_last_user,
                          parse_session_tail, scan_sessions,
                          TAIL_BYTES, HEAD_BYTES)
from .limits import (cached_limits, account_reserve, _model_remaining,
                     model_candidates, set_reserve, limits_by_account,
                     demo_limits, _limit_active_until, _cclimits_bin,
                     LIMITS_TTL_S, _limits)
from .terminal import focus_process, send_to_process, _osa_escape

STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}


# ---------------------------------------------------------------- collectors

def collect_state():
    now = time.time()
    worktrees = gitrepo.discover_worktrees()
    # `all_procs`, not `procs`: a local of that name would shadow the module.
    all_procs = procs.claude_processes()
    sessions = transcripts.scan_sessions(worktrees, all_procs, now)

    # An agent parked at the prompt on an exhausted account isn't "your turn" —
    # it's out of juice. Joined from the cclimits cache (populated lazily by
    # /api/limits; never fetched on the state path).
    acct_limits = limits.limits_by_account()
    limit_re = re.compile(r"out of usage credits|(reached|hit) your .{0,30}limit", re.I)
    rank = {"needs_input": 0, "limit": 1, "blocked": 2, "working": 3, "waiting": 4, "ended": 5}
    for ss in sessions.values():
        for s in ss:
            if s["status"] not in ("needs_input", "blocked", "waiting"):
                continue
            al = acct_limits.get(s["account"])
            smodel = (s["model"] or "").lower()
            lim = None
            if al and al["exhausted"]:
                # account-wide cap (session / umbrella weekly) — bites every model here
                lim = {"worst": al["worst"], "group": al["group"],
                       "resets_in": al["resets_in"], "resets_at": al["resets_at"]}
            elif al:
                # a model-scoped cap only strands a session running THAT model
                hit = next((sx for sx in al.get("scoped_exhausted", [])
                            if (sx["label"] or "").lower() in smodel), None)
                if hit:
                    lim = {"worst": hit["label"], "group": hit["group"],
                           "resets_in": hit["resets_in"], "resets_at": hit["resets_at"]}
            if lim:
                s["status"] = "limit"
                s["limit"] = lim
            elif limit_re.search(s["last_assistant"] or ""):
                # the CLI wrote its limit notice into the transcript —
                # trust it even when the cclimits cache is cold/stale
                s["status"] = "limit"
                s["limit"] = {"worst": None, "group": None, "resets_in": None, "resets_at": None}

    # Handoff awareness: a limit-hit session whose worktree has a FRESHER live
    # session (typically another account continuing from a handoff doc) is no
    # longer the actionable one — annotate the succession and stop treating
    # the stranded session as needing attention.
    for ss in sessions.values():
        alive = [s for s in ss if s["status"] in ("working", "waiting", "needs_input", "blocked")]
        for s in ss:
            if s["status"] == "limit":
                succ = [a for a in alive if a["age_s"] < s["age_s"]]
                if succ:
                    s["handed_to"] = min(succ, key=lambda a: a["age_s"])["account"]
        ss.sort(key=lambda s: (4.5 if s.get("handed_to") else rank[s["status"]], s["age_s"]))

    def _attention_statuses(ss):
        return [s["status"] for s in ss
                if not (s["status"] == "limit" and s.get("handed_to"))]

    cards = []
    for w in worktrees:
        ss = sessions.get(w["path"], [])
        live = [p for p in all_procs if p.get("cwd") and
                (p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/"))]
        cards.append({
            **w,
            "git": gitrepo.git_info(w["git"]),
            "sessions": ss,
            "live_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                            "tty": p["tty"], "host": p["host"],
                            "account": p.get("account"),
                            "tmux": p.get("tmux_target"),
                            "reachable": bool(p.get("tmux_target") or
                                              (p["host"] in ("Terminal", "iTerm2") and p["tty"])),
                            "subdir": os.path.relpath(p["cwd"], w["path"])
                            if p["cwd"] != w["path"] else None} for p in live],
        })

    for c in cards:
        c["availability"] = status.card_availability(
            _attention_statuses(c["sessions"]), bool(c["live_procs"]))
        # two-step finish: while a closeout brief is with this card's live
        # agent, the button reads ✕ close. The flag dies with the terminal,
        # so a card never offers to close an agent that no longer exists.
        ts = _closeouts.get(c["name"])
        if ts:
            if c["live_procs"]:
                c["closeout_sent"] = ts
            else:
                _closeouts.pop(c["name"], None)

    matched = {p["pid"] for c in cards for p in c["live_procs"]}
    other = [p for p in all_procs if p["pid"] not in matched]

    def severity(c):
        st = _attention_statuses(c["sessions"])
        if "needs_input" in st: return 0
        if "blocked" in st: return 1
        if "waiting" in st and "working" not in st: return 2
        if "working" in st: return 3
        if "limit" in st: return 4   # un-actionable — parked behind the busy ones
        return 5
    cards.sort(key=lambda c: (severity(c), c["name"].lower()))

    counts = {"working": 0, "needs_input": 0, "limit": 0, "blocked": 0, "waiting": 0, "ended": 0}
    for c in cards:
        for s in c["sessions"]:
            if s["status"] == "limit" and s.get("handed_to"):
                continue  # informational — work already continues elsewhere
            counts[s["status"]] += 1
    return {
        "generated_at": now,
        "hostname": os.uname().nodename,
        "user": getpass.getuser(),
        "counts": counts,
        "free_worktrees": [c["name"] for c in cards if c["availability"] == "free"],
        "worktrees": cards,
        "other_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                         "tty": p["tty"], "host": p["host"],
                         "cwd": p.get("cwd")} for p in other],
    }




# --------------------------------------------------------------- demo state

def demo_state():
    """Fictional data with the exact shape of collect_state(), for screenshots."""
    now = time.time()

    seq = [0]

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None, sid=None):
        seq[0] += 1
        return {"id": "demo0000", "sid": sid or f"demo-{seq[0]}",
                "account": acct, "status": status, "age_s": age,
                "cwd": "/demo", "subdir": subdir, "branch": None, "model": model,
                "pending_tools": pend or [], "topic": topic, "last_assistant": said}

    def card(name, avail, branch, dirty, ahead, behind, cts, subject, sessions, pids):
        procs = [{"pid": p, "cpu": 4.2, "etime": "02:14:33", "subdir": None,
                  "tty": f"ttys{p % 1000:03d}", "host": "Terminal",
                  "account": None, "tmux": None, "reachable": True} for p in pids]
        # mirror the real pairing: each live session owns one terminal, and the
        # process advertises that session's account
        live = [s for s in sessions if s["status"] != "ended"]
        for s, p in zip(live, procs):
            s["pid"], s["pid_certain"] = p["pid"], True
            p["account"] = s["account"]
        return {"name": name, "path": "/demo/" + name, "git_root": "",
                "git": {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
                        "commit": {"hash": "a1b2c3d", "ts": int(now - cts), "subject": subject}},
                "sessions": sessions, "availability": avail, "live_procs": procs}

    cards = [
        card("orbital-api", "attention", "feat/webhook-retries", 12, 3, 0, 1800,
             "feat(webhooks): exponential backoff with jitter", [
                 sess("needs_input", "work", "fable-5", 340,
                      "Add retry logic to the webhook dispatcher with dead-letter support",
                      "Should failed deliveries older than 24h go to the dead-letter queue or be dropped? I've laid out both options.",
                      pend=["AskUserQuestion"]),
                 sess("ended", "work", "opus-4-8", 9100,
                      "Profile the webhook worker under load", None)], [41234]),
        card("orbital-web", "attention", "fix/checkout-race", 3, 1, 0, 5400,
             "fix(cart): serialize checkout mutations", [
                 dict(sess("limit", "work", "opus-4-8", 3900,
                      "The checkout button double-fires on slow connections — find and fix the race",
                      "I'll continue once usage is available again.",
                      sid="demo-limit-1"),
                      limit={"worst": "Session", "group": "session",
                             "resets_in": 7560, "resets_at": now + 7560}),
                 sess("waiting", "personal", "fable-5", 2100,
                      "Audit the cart telemetry events for double-counting",
                      "Fixed and verified — the mutation is now idempotent and the test suite passes. Ready for review.")], [41567]),
        card("kepler-worker", "busy", "perf/batch-inserts", 7, 0, 0, 600,
             "perf(db): batch event inserts, 40x fewer round-trips", [
                 sess("working", "work", "opus-4-8", 15,
                      "Migrate the event pipeline to batched COPY inserts",
                      "Running the benchmark suite against the staging database now.")], [42901]),
        card("voyager-cli", "free", "main", 0, 0, 0, 86400 * 2,
             "chore: release v0.4.1", [], []),
        card("lander-docs", "free", "docs/quickstart", 2, None, None, 86400,
             "docs: rewrite quickstart around the new init flow", [], []),
    ]
    return {
        "generated_at": now, "hostname": "starbase", "user": "you",
        "counts": {"working": 1, "needs_input": 1, "limit": 1, "blocked": 0, "waiting": 1, "ended": 1},
        "free_worktrees": ["voyager-cli", "lander-docs"],
        "worktrees": cards,
        "other_procs": [{"pid": 40001, "cpu": 1.1, "etime": "15:02", "cwd": "/demo/scratch"}],
    }


def cached_state():
    if config.DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()
        _cache["t"] = now
    return _cache["state"]


# ------------------------------------------------------------- finish

# The full closeout brief, for a branch that still needs landing. ✓ finish
# hands it to an agent: the live one if a terminal exists, a freshly
# dispatched one if not. (Once a branch HAS landed, finish stops delegating —
# see SLIM_CLOSEOUT_TEXT and _park_on_trunk below.)
CLOSEOUT_TEXT = (
    "Close out this worktree now: "
    "1) wait for (or stop) any background agents and workflows you started; "
    "2) commit remaining meaningful work — drop scratch files; "
    "3) land the branch: merge {trunk} into it, resolve any conflicts — but "
    "if a conflict needs real judgment about the code, stop and report it "
    "instead of guessing — push the branch, then push it to the trunk and "
    "verify with `git merge-base --is-ancestor HEAD {trunk}`; "
    "4) switch this worktree to the trunk branch and pull, so it starts the "
    "next mission clean; "
    "5) reply with a one-line summary of what landed."
)

# The slim brief for a branch that already landed (HEAD is an ancestor of the
# trunk): nothing merged gets re-checked — only whatever is actually left.
SLIM_CLOSEOUT_TEXT = (
    "This worktree's branch has already landed on {trunk} — do not re-merge, "
    "re-push, or re-verify any of that. Close out only what's left: "
    "1) stop (or wait for) any background agents and workflows you started; "
    "2) drop scratch files; if meaningful uncommitted work remains, commit it "
    "and land it on {trunk} like a normal closeout; "
    "3) switch this worktree to the trunk branch and pull, so it starts the "
    "next mission clean; "
    "4) reply with one line saying what, if anything, was left to do."
)

# Follow-up typed at a live agent when step two (✕ close) still can't verify a
# clean landing AND nothing else is working in this worktree — so the leftover
# files are this agent's call, not "another session's in-flight work" it can
# quietly ignore. That misjudgment is the exact deadlock this breaks: without
# the nudge the agent idles forever and ✕ close refuses forever, because no
# other session exists to ever converge the tree. {files} is up to five raw
# `git status --porcelain` lines (blank when the branch simply hasn't landed).
CLOSEOUT_NUDGE_TEXT = (
    "Step two of the closeout still can't verify a clean landing: {left}. "
    "{files}"
    "No other session is working in this worktree, so these files are yours to "
    "judge — none of them is another session's in-flight work. Commit and land "
    "anything meaningful, drop scratch, and leave the tree clean on {trunk}. If "
    "any of it genuinely needs the user's decision, stop and ask explicitly — "
    "don't just leave it dirty."
)

# worktree name -> epoch seconds when a closeout brief was typed at its live
# agent. Step two of ✓ finish: while this is set (and the terminal lives) the
# board shows ✕ close instead — which only verifies the landing and /exits;
# it never re-types the brief into a mid-closeout agent. The flag dies with
# the terminal, so a fresh mission never inherits it.
_closeouts = {}


def _park_on_trunk(git_root, trunk):
    """Landed and clean: park the worktree on the trunk branch right here —
    two git commands don't need an agent. Returns None if the switch fails so
    the caller can hand it to an agent instead."""
    branch = trunk.split("/", 1)[-1]
    if shell.run(["git", "switch", branch], cwd=git_root, timeout=30)[0] != 0:
        return None
    pulled = shell.run(["git", "pull", "--ff-only", "--quiet"], cwd=git_root,
                       timeout=60)[0] == 0
    return {"ok": True, "mode": "parked", "message":
            f"already landed — parked on {branch}, no agent needed"
            + ("" if pulled else " (pull failed; next dispatch refreshes)")}


def closeout_shell(home, model, brief, trunk):
    """The tmux command for a one-shot closeout: run claude headless, then let
    git itself verify the landing. Verified clean -> exit, the tmux session
    dies, the card reads FREE with no second ✓ finish. Anything else -> resume
    the conversation interactively, so a failed closeout parks as needs-you
    instead of masquerading as free."""
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


def _reachable(p):
    return bool(p.get("tmux_target")) or (
        p.get("host") in ("Terminal", "iTerm2") and p.get("tty"))


def start_finish(wt_name):
    """One button, tiered by what's actually left to do:
    live agent -> type a brief at it — the slim one if the branch already
    landed, the full closeout otherwise; everything landed and an agent
    idling -> type /exit; no terminal + landed + clean -> park on the trunk
    right here, no agent; anything else -> launch a one-shot closeout agent
    (headless; frees the card itself, or parks as needs-you if the landing
    doesn't verify)."""
    if config.DEMO:
        return {"ok": False, "message": "demo mode — nothing to finish"}
    wt = next((w for w in gitrepo.discover_worktrees() if w["name"] == wt_name), None)
    if not wt:
        return {"ok": False, "message": f"unknown worktree '{wt_name}'"}
    path, git_root = wt["path"], wt["git"]
    trunk = gitrepo._base_ref(git_root)
    if not trunk:
        return {"ok": False, "message": "no trunk ref found for this repo"}
    shell.run(["git", "fetch", "--quiet", "origin"], cwd=git_root, timeout=30)
    landed = shell.run(["git", "merge-base", "--is-ancestor", "HEAD", trunk],
                       cwd=git_root)[0] == 0
    porcelain = [l for l in shell.run(["git", "status", "--porcelain"],
                                      cwd=git_root)[1].splitlines() if l.strip()]
    mine = [p for p in procs.claude_processes() if p.get("cwd")
            and (p["cwd"] == path or p["cwd"].startswith(path + os.sep))]
    live = next((p for p in mine if _reachable(p)), None)
    if live:
        if landed and not porcelain:
            res = terminal.send_to_process(live["pid"], "/exit")
            if res["ok"]:
                _closeouts.pop(wt_name, None)
                _cache["t"] = 0.0    # button reverts on the next poll
            return {"ok": res["ok"], "mode": "exit", "message":
                    "already landed — sent /exit to close the terminal"
                    if res["ok"] else res["message"]}
        sent = _closeouts.get(wt_name)
        if sent:
            # step two (✕ close), but the landing still doesn't verify. What to
            # do isn't a fixed refusal — it depends on THIS worktree's live
            # session. The observed deadlock: the agent finished its closeout
            # but left one dirty file it took for another session's in-flight
            # work; nothing else was live, so ✕ close refused forever while the
            # agent idled forever. classify the session and nudge it out of that.
            left = (f"{len(porcelain)} leftover file(s)" if landed
                    else f"branch not landed on {trunk}")
            files = porcelain[:5]          # ≤5 raw lines, for the agent + UI
            now = time.time()
            sessions = transcripts.scan_sessions([wt], mine, now).get(path, [])
            paired = next((s for s in sessions
                           if s.get("pid") == live["pid"]), None)
            any_working = any(s.get("status") == "working" for s in sessions)
            step = status.closeout_step(paired["status"] if paired else None,
                                        any_working, sent, now)
            if step == "nudge":
                # idle agent, nothing else working, briefed ≥60s ago: type the
                # specifics so it stops treating the leftovers as untouchable.
                block = "\n".join(files)
                if len(porcelain) > len(files):
                    block += f"\n… and {len(porcelain) - len(files)} more"
                nudge = CLOSEOUT_NUDGE_TEXT.format(
                    left=left, trunk=trunk, files=(block + "\n") if block else "")
                res = terminal.send_to_process(live["pid"], nudge)
                if not res["ok"]:
                    return {"ok": False, "mode": "nudge", "message": res["message"]}
                _closeouts[wt_name] = time.time()   # restart the "sent Xm ago"
                _cache["t"] = 0.0                    # clock + re-arm the 60s guard
                # `left`/`files` ride along so the card note can say what's
                # blocked without parsing the human message
                return {"ok": True, "mode": "nudge", "left": left,
                        **({"files": files} if files else {}), "message":
                        f"closeout had stalled — sent the agent the specifics "
                        f"({left}); ✕ close works once it reports clean"}
            # otherwise refuse, but hand the frontend the specifics too: `left`
            # (short reason), `files` (≤5 porcelain lines, only when any), and
            # `sent` (the epoch it was briefed). mode "pending" is a plain
            # refusal; mode "chat" is a DISTINCT mode meaning the agent is stuck
            # on a question/approval — a typed nudge would collide with its open
            # dialog, so the frontend must route the user to ✉ chat instead.
            extra = {"left": left, "sent": sent}
            if files:
                extra["files"] = files
            if step == "chat":
                return {"ok": False, "mode": "chat", **extra, "message":
                        f"can't close yet — {left}, and the agent is stuck on a "
                        "question or approval. Answer it in ✉ chat — a typed "
                        "nudge would collide with its open dialog. ✕ close works "
                        "once the landing verifies."}
            mins = int((now - sent) // 60)
            ago = f"{mins}m ago" if mins else "under a minute ago"
            return {"ok": False, "mode": "pending", **extra, "message":
                    f"can't close yet — {left}. The closeout brief went to "
                    f"the agent {ago}; if it looks stuck, ✉ chat with it. "
                    "✕ close works once the landing verifies."}
        brief = (SLIM_CLOSEOUT_TEXT if landed else CLOSEOUT_TEXT)
        res = terminal.send_to_process(live["pid"], brief.format(trunk=trunk))
        if not res["ok"]:
            return {"ok": False, "mode": "slim" if landed else "brief",
                    "message": res["message"]}
        _closeouts[wt_name] = time.time()
        _cache["t"] = 0.0            # show ✕ close on the next poll, not in 4s
        return {"ok": True, "mode": "slim" if landed else "brief", "message":
                ("already landed — slim brief sent (tidy scratch and park, "
                 "no re-merge)" if landed else
                 "closeout brief sent to the live agent")
                + " — when it reports done, ✕ close verifies the landing "
                  "and closes the terminal"}
    _closeouts.pop(wt_name, None)    # no live agent — the two-step is moot
    if mine:
        return {"ok": False, "message":
                "a live process exists but its terminal can't be scripted — "
                "finish from that terminal, or close it and ✓ finish again"}
    if landed and not porcelain:
        branch = shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=git_root)[1].strip()
        if branch == trunk.split("/", 1)[-1]:
            return {"ok": True, "mode": "noop",
                    "message": "already landed and clean — nothing to finish"}
        parked = _park_on_trunk(git_root, trunk)
        if parked:
            return parked
        # the switch itself failed — fall through and let an agent sort it out
    # any leftover file — even untracked scratch — goes to an agent: whether
    # it's droppable is a judgment call, not ours. haiku is enough for the
    # mechanical run, a landed branch gets the slim brief so nothing already
    # merged is re-checked, and a failed landing escalates itself (see
    # closeout_shell's rescue line)
    brief = (SLIM_CLOSEOUT_TEXT if landed else CLOSEOUT_TEXT).format(trunk=trunk)
    out = start_dispatch(brief, worktree=wt_name,
                         model="haiku", closeout_trunk=trunk)
    out.setdefault("ok", True)
    out["mode"] = "dispatch"
    out.setdefault("message",
                   "no live terminal — launched a one-shot closeout agent; "
                   "the card frees itself once the landing verifies")
    return out


# ------------------------------------------------------------- chat reader

def read_chat(account, sid, limit=40):
    """Last conversation turns of a session, from its transcript."""
    home = next((h for h in transcripts.claude_homes()
                 if config.account_label(h) == account), None)
    if not home:
        return {"ok": False, "error": f"unknown account {account}"}
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    if not fp:
        return {"ok": False, "error": "transcript not found"}
    msgs = []
    for line in transcripts._read_chunk(fp, 512 * 1024, from_end=True).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain") or e.get("isMeta"):
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if e.get("type") == "user":
            texts = [c] if isinstance(c, str) else [
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
            for t in texts:
                if transcripts._real_prompt(t):
                    msgs.append({"role": "you", "text": transcripts._clean(t, 900),
                                 "ts": e.get("timestamp")})
        elif e.get("type") == "assistant" and isinstance(c, list):
            parts = [b["text"] for b in c
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
            if parts:
                msgs.append({"role": "agent", "text": transcripts._clean(" ".join(parts), 900),
                             "ts": e.get("timestamp")})
    return {"ok": True, "messages": msgs[-limit:]}


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
    state = cached_state()
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


# -------------------------------------------------------- scheduled resumes

# A limit-stuck agent needs exactly one keystroke ("continue") typed at it
# once its limit resets — but resets land at 3am, or days out on a weekly cap.
# Arming a schedule hands that keystroke to the board: at the armed time it
# verifies the limit really lifted (re-arming for the next reset if not), then
# types the resume message into the session's own terminal — or, when no
# terminal can be scripted (Cursor/VS Code, or the window is gone), relaunches
# the conversation in a fleet tmux session via `claude --resume`.
#
# Schedules survive both the browser and this server: every mutation is
# persisted to resume.schedule.json, and pending entries whose time passed
# while the server was down fire on the first loop pass after boot.

RESUME_STATE = config.HERE / "resume.schedule.json"
RESUME_POLL_S = 5.0
RESUME_MAX_ATTEMPTS = 10       # re-arms on "still limited" before giving up
_resumes = {}                  # "worktree|sid" -> schedule dict
_resumes_lock = threading.Lock()


def save_resumes():
    with _resumes_lock:
        snap = json.dumps({"schedules": list(_resumes.values())}, indent=1)
    try:
        RESUME_STATE.write_text(snap + "\n")
    except OSError:
        pass


def load_resumes():
    try:
        data = json.loads(RESUME_STATE.read_text())
    except (OSError, ValueError):
        return
    with _resumes_lock:
        for r in data.get("schedules", []):
            if r.get("worktree") and r.get("sid"):
                _resumes[f"{r['worktree']}|{r['sid']}"] = r


def _resume_set(key, **updates):
    with _resumes_lock:
        r = _resumes.get(key)
        if r:
            r.update(updates)
    save_resumes()


def resume_public():
    """The schedules, shaped for the board (rides along on /api/state)."""
    if config.DEMO:
        return demo_resumes()
    with _resumes_lock:
        return {k: dict(r) for k, r in _resumes.items()}


def demo_resumes():
    return {"orbital-web|demo-limit-1": {
        "worktree": "orbital-web", "sid": "demo-limit-1", "account": "work",
        "model": "opus-4-8", "delay_s": 60, "status": "pending",
        "due_at": time.time() + 7620, "attempts": 0, "message": None}}


def schedule_resume(worktree, sid, account, model=None, delay_s=None,
                    resets_at=None, due_at=None):
    """Arm (or re-arm) an auto-resume. The due time is `due_at` when given
    (the user picked an exact time), else the limit reset + delay. Refuses —
    asking for an exact time — when no reset timestamp is known."""
    if config.DEMO:
        return {"ok": False, "message": "demo mode — nothing to schedule"}
    if not (worktree and sid and account):
        return {"ok": False, "message": "need worktree, sid and account"}
    now = time.time()
    try:
        delay = float(delay_s if delay_s is not None
                      else config.CFG.get("resume_delay_s", 60))
    except (TypeError, ValueError):
        return {"ok": False, "message": "delay must be a number of seconds"}
    delay = max(0.0, min(86400.0, delay))
    if due_at is not None:
        try:
            due = float(due_at)
        except (TypeError, ValueError):
            return {"ok": False, "message": "bad due time"}
    else:
        if resets_at is None:
            # the client normally sends the reset it displays; recompute as a
            # fallback so the API stands on its own
            al = limits.limits_by_account().get(account) or {}
            resets_at = al.get("resets_at") if al.get("exhausted") else None
            if resets_at is None:
                resets_at = min((sx["resets_at"] for sx in
                                 al.get("scoped_exhausted", [])
                                 if sx.get("resets_at")), default=None)
        try:
            resets_at = float(resets_at) if resets_at is not None else None
        except (TypeError, ValueError):
            resets_at = None
        if resets_at is None:
            return {"ok": False, "need_time": True, "message":
                    "no known reset time for this limit — pick an exact time"}
        due = resets_at + delay
    due = max(now + 5, due)   # a reset already past fires on the next pass
    key = f"{worktree}|{sid}"
    with _resumes_lock:
        _resumes[key] = {
            "worktree": worktree, "sid": sid, "account": account,
            "model": model, "delay_s": delay, "resets_at": resets_at,
            "due_at": due, "created_at": now, "attempts": 0,
            "status": "pending", "message": None}
    save_resumes()
    return {"ok": True, "due_at": due, "message":
            "auto-resume armed for " + time.strftime("%H:%M", time.localtime(due))}


def cancel_resume(worktree, sid):
    key = f"{worktree}|{sid}"
    with _resumes_lock:
        found = _resumes.pop(key, None)
    save_resumes()
    return {"ok": bool(found), "message":
            "auto-resume disarmed" if found else "nothing armed for this session"}


def _session_on_board(state, worktree, sid):
    """(session, its own live proc) for a schedule key, from board state."""
    card = next((w for w in state["worktrees"] if w["name"] == worktree), None)
    if not card:
        return None, None
    s = next((x for x in card["sessions"] if x.get("sid") == sid), None)
    proc = None
    if s and s.get("pid"):
        proc = next((p for p in card["live_procs"] if p["pid"] == s["pid"]), None)
    return s, proc


RESUME_READY_S = 420.0   # --resume on a fat session auto-compacts for minutes


def _wait_composer_idle(name, timeout_s):
    """Block until the reopened CLI can provably receive input: the composer
    idle on two consecutive looks. One look lies — the CLI idles for a beat
    between finishing its reload and starting the auto-compact."""
    deadline = time.time() + timeout_s
    streak = 0
    while time.time() < deadline:
        _, pane = shell.run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        streak = streak + 1 if composer_idle(pane) else 0
        if streak >= 2:
            return True
        time.sleep(3)
    return False


def _proven_in_transcript(fp, offset, text, timeout_s=20.0):
    """True once the session file gains a user entry carrying `text` beyond
    `offset` — receipt at the source, not read off the screen."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(fp, "rb") as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            return False
        for line in chunk.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "user":
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(x.get("text", "") for x in content
                                   if isinstance(x, dict))
            if isinstance(content, str) and text in content:
                return True
        time.sleep(2)
    return False


def _tmux_resume(worktree, cwd, home, sid):
    """No terminal to type into — reopen the conversation in a fleet tmux
    session (claude --resume <sid>) and send it the resume message there.

    Reopening is the easy half. A fat transcript makes the CLI reload for
    tens of seconds and then auto-compact for minutes, and a message pasted
    into either phase vanishes — while the bare composer it leaves behind
    reads as delivered. So the send waits out reload and compaction, and the
    only accepted receipt is the session file gaining the message; anything
    less retries, then reports failure with the attach command."""
    name = ("resume-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree).strip("-").lower()
            + time.strftime("-%H%M%S"))
    shell_cmd = (f"export CLAUDE_CONFIG_DIR={shlex.quote(str(home))}\n"
                 f"exec claude --dangerously-skip-permissions --resume {shlex.quote(sid)}\n")
    rc, out = shell.run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                         "-c", cwd, shell_cmd])
    if rc != 0:
        return {"ok": False,
                "message": f"tmux failed: {out or 'is tmux installed?'}"}
    attach = f"tmux -L {FLEET_SOCK} attach -t {name}"
    where = f"no scriptable terminal — resumed in tmux · {attach}"
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    msg = config.CFG.get("resume_message", "continue")
    _wait_composer_idle(name, RESUME_READY_S)
    for attempt in range(3):
        if attempt:   # the last paste vanished — let the CLI settle, try again
            _wait_composer_idle(name, 90.0)
        try:
            offset = fp.stat().st_size if fp else 0
        except OSError:
            fp, offset = None, 0
        sent = deliver_text(name, msg)
        if fp and _proven_in_transcript(fp, offset, msg):
            return {"ok": True, "message": where}
        if not fp and sent:
            return {"ok": True, "message":
                    where + " · ⚠ transcript not found — send unproven"}
    return {"ok": False, "message":
            f"reopened in tmux but '{msg}' never reached the conversation — "
            f"attach and type it: {attach}"}


def fire_resume(key):
    """The armed moment. Decision order: already moved on -> done; limit still
    binds -> re-arm for the fresh reset; else type the resume message into the
    session's OWN terminal, or reopen the session in tmux. Unlike the manual
    button, this never borrows another session's terminal — unattended, a
    'continue' typed at the wrong agent is an injected instruction, while the
    tmux fallback targets the sid exactly."""
    with _resumes_lock:
        r = dict(_resumes.get(key) or {})
    if not r or r.get("status") != "pending":
        return
    now = time.time()
    if config.DEMO:
        return _resume_set(key, status="failed", fired_at=now,
                           message="demo mode")
    worktree, sid, account = r["worktree"], r["sid"], r["account"]

    state = cached_state()
    s, proc = _session_on_board(state, worktree, sid)
    if s and s.get("handed_to"):
        return _resume_set(key, status="done", fired_at=now, message=
                           f"work already continued by [{s['handed_to']}] — nothing sent")
    if s and s["status"] != "limit":
        return _resume_set(key, status="done", fired_at=now, message=
                           f"session is {s['status']} — no resume needed")

    until = limits._limit_active_until(account, r.get("model"), now)
    if until:
        attempts = int(r.get("attempts", 0)) + 1
        if attempts >= RESUME_MAX_ATTEMPTS:
            return _resume_set(key, status="failed", fired_at=now, message=
                               f"still limited after {attempts} checks — gave up")
        return _resume_set(key, due_at=until + float(r.get("delay_s") or 60),
                           attempts=attempts, message=
                           "still limited — re-armed for the next reset")

    msg = config.CFG.get("resume_message", "continue")
    if proc and proc.get("reachable"):
        res = terminal.send_to_process(proc["pid"], msg)
        if res.get("ok"):
            return _resume_set(key, status="done", fired_at=now,
                               message=f"sent '{msg}' — {res['message']}")
    wt = next((w for w in gitrepo.discover_worktrees() if w["name"] == worktree), None)
    home = next((h for h in transcripts.claude_homes()
                 if config.account_label(h) == account), None)
    if not wt or not home:
        return _resume_set(key, status="failed", fired_at=now, message=
                           "worktree or account no longer known — nothing sent")
    out = _tmux_resume(worktree, (s or {}).get("cwd") or wt["path"], home, sid)
    if out["ok"] and proc:
        # the session's old window survives it — a frozen pre-resume view
        out["message"] += (f" · the old {proc.get('host') or 'terminal'} window"
                           " now shows a stale view — close it, don't type into it")
    return _resume_set(key, status="done" if out["ok"] else "failed",
                       fired_at=now, message=out["message"])


def resume_loop():
    """Daemon: fire due schedules; prune finished ones after a day."""
    while True:
        time.sleep(RESUME_POLL_S)
        now = time.time()
        with _resumes_lock:
            due = [k for k, r in _resumes.items()
                   if r.get("status") == "pending" and r.get("due_at", 0) <= now]
        for k in due:
            try:
                fire_resume(k)
            except Exception as e:   # a broken fire must not kill the loop
                _resume_set(k, status="failed", fired_at=now,
                            message=f"internal error: {e}")
        with _resumes_lock:
            stale = [k for k, r in _resumes.items()
                     if r.get("status") in ("done", "failed")
                     and now - r.get("fired_at", now) > 86400]
            for k in stale:
                del _resumes[k]
        if stale:
            save_resumes()


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            # schedules ride along so the board needs no second fetch
            body = json.dumps({**cached_state(),
                               "resumes": resume_public()}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/focus"):
            m = re.search(r"pid=(\d+)", self.path)
            result = terminal.focus_process(int(m.group(1))) if m else {"ok": False, "message": "missing pid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/topology"):
            body = json.dumps(gitrepo.cached_topology()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/limits"):
            body = json.dumps(limits.cached_limits(refresh="refresh=1" in self.path)).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatchlog"):
            body = json.dumps(read_dispatch_log()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatch/status"):
            m = re.search(r"job=([\w-]+)", self.path)
            body = json.dumps(dispatch_status(m.group(1)) if m
                              else {"ok": False, "error": "no job"}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/chat"):
            qa = re.search(r"account=([^&]+)", self.path)
            qs = re.search(r"sid=([0-9a-fA-F-]+)", self.path)
            result = read_chat(qa.group(1), qs.group(1)) if qa and qs else \
                {"ok": False, "error": "need account & sid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] in ("/", "/index", "/index.html"):
            body = (config.HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/map"):
            body = (config.HERE / "map.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/limits"):
            body = (config.HERE / "limits.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/guide"):
            body = (config.HERE / "guide.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, OSError):
            payload = {}
        if self.path.startswith("/api/reserve"):
            result = limits.set_reserve(payload.get("account"), payload.get("percent"))
        elif self.path.startswith("/api/resume/schedule"):
            result = schedule_resume(
                payload.get("worktree"), payload.get("sid"),
                payload.get("account"), model=payload.get("model"),
                delay_s=payload.get("delay_s"),
                resets_at=payload.get("resets_at"), due_at=payload.get("due_at"))
        elif self.path.startswith("/api/resume/cancel"):
            result = cancel_resume(payload.get("worktree"), payload.get("sid"))
        elif self.path.startswith("/api/send"):
            result = terminal.send_to_process(int(payload.get("pid") or 0), payload.get("text") or "")
        elif self.path.startswith("/api/finish"):
            result = start_finish(payload.get("worktree") or "")
        elif self.path.startswith("/api/dispatch"):
            result = start_dispatch(
                payload.get("mission"), payload.get("worktree") or None,
                payload.get("account") or None,
                payload.get("model") or None, payload.get("effort") or None,
                bool(payload.get("force_model")))
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
