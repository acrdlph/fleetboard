"""orchestra.finish — the closeout: an agent lands the branch, the card frees.

✓ finish is the one button that ends a mission. It does not merge anything
itself; it hands an agent a brief and lets the agent do the work, because
landing a branch is judgment (conflicts, scratch files, half-done work) and
the board refuses to guess. This module owns the briefs, the flag that makes
finish a visible two-step, and the single case where the board *does* run git
write commands itself.

Three texts, tiered by what is actually left. `CLOSEOUT_TEXT` is the full
brief for a branch that still needs landing. `SLIM_CLOSEOUT_TEXT` is for a
branch that already landed — nothing merged gets re-checked. `CLOSEOUT_NUDGE_TEXT`
is the follow-up for the one observed deadlock: an agent that finished its
closeout but left a file it took for another session's in-flight work, so it
idles forever while ✕ close refuses forever.

`_closeouts` is the two-step: worktree name -> when its live agent was briefed.
While it is set the card shows ✕ close instead of ✓ finish, so a mid-closeout
agent never gets the brief typed at it twice. A card with no live procs never
renders it, so it dies with the terminal as far as the board shows, and this
module reaps the entry itself — on its next action, or by `CLOSEOUT_TTL_S`.

`_park_on_trunk` is the exception to "watching touches nothing": when the
branch has already landed and the tree is clean, two git commands don't need
an agent — the provably-safe case.

`start_finish` is the button itself: it reads the worktree (gitrepo, procs,
transcripts), asks status which step of the two-step it is on, and acts
through terminal or dispatch. That makes finish the top of the act layer —
it imports observe, never the other way round. observer needs one thing back
(it READS `_closeouts` to decide which button a card shows — it never writes
it) and it takes it through a function-local import, so this module's imports
stay a one-way DAG. See ADR 0010, 'cycles'.
"""

import os
import time

from . import (config, shell, status, gitrepo, procs, transcripts, terminal,
               observer, dispatch)


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
# it never re-types the brief into a mid-closeout agent. A card with no live
# procs never renders the flag, so it dies with the terminal as far as anyone
# can see; this module reaps the entry itself.
_closeouts = {}

# An hour-old brief is not a two-step in progress any more — it is an agent
# that never converged, or a terminal that has been gone for most of an hour.
# Past this the flag can only mislead (a fresh mission in that worktree greeted
# by ✕ close and its refusal), so it is dropped and the card goes back to
# ✓ finish, which is the honest offer at that point.
CLOSEOUT_TTL_S = 3600.0


def _prune_closeouts(now=None):
    """Drop closeout flags too old to mean anything. Called on this module's
    next action, because reaping is a MUTATION and only a mutation path may do
    it (ENGINE.md §2.5) — `collect_state` used to pop stale entries as a side
    effect of being looked at, which under the perpetual sweep would run on a
    schedule nobody requested."""
    now = time.time() if now is None else now
    for name in [n for n, ts in _closeouts.items() if now - ts > CLOSEOUT_TTL_S]:
        _closeouts.pop(name, None)


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
    _prune_closeouts()   # every press cleans the whole map, not just this card
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
                observer._cache["t"] = 0.0    # button reverts on the next poll
                observer.nudge("finish/exit")  # …and the sweep sees it now
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
                observer._cache["t"] = 0.0           # clock + re-arm the 60s guard
                observer.nudge("finish/nudge")
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
        observer._cache["t"] = 0.0   # show ✕ close on the next poll, not in 4s
        observer.nudge("finish/brief")
        return {"ok": True, "mode": "slim" if landed else "brief", "message":
                ("already landed — slim brief sent (tidy scratch and park, "
                 "no re-merge)" if landed else
                 "closeout brief sent to the live agent")
                + " — when it reports done, ✕ close verifies the landing "
                  "and closes the terminal"}
    _closeouts.pop(wt_name, None)   # no live agent — the two-step is moot
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
            # The one path where the board itself moves git — switch + pull.
            # It KNOWS the branch changed under it, so it says so: without this
            # the card serves the old branch until git's own clock comes round
            # (GIT_S, up to 15s), and this is the single mutation the observer
            # could never have inferred any sooner.
            observer._cache["t"] = 0.0
            observer.nudge("finish/park")
            return parked
        # the switch itself failed — fall through and let an agent sort it out
    # any leftover file — even untracked scratch — goes to an agent: whether
    # it's droppable is a judgment call, not ours. haiku is enough for the
    # mechanical run, a landed branch gets the slim brief so nothing already
    # merged is re-checked, and a failed landing escalates itself (see
    # closeout_shell's rescue line)
    brief = (SLIM_CLOSEOUT_TEXT if landed
             else CLOSEOUT_TEXT).format(trunk=trunk)
    out = dispatch.start_dispatch(brief, worktree=wt_name,
                                  model="haiku", closeout_trunk=trunk)
    out.setdefault("ok", True)
    out["mode"] = "dispatch"
    out.setdefault("message",
                   "no live terminal — launched a one-shot closeout agent; "
                   "the card frees itself once the landing verifies")
    return out
