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
agent never gets the brief typed at it twice. The flag dies with the terminal,
so a fresh mission never inherits it.

`_park_on_trunk` is the exception to "watching touches nothing": when the
branch has already landed and the tree is clean, two git commands don't need
an agent — the provably-safe case.
"""

from . import shell


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


def _reachable(p):
    return bool(p.get("tmux_target")) or (
        p.get("host") in ("Terminal", "iTerm2") and p.get("tty"))
