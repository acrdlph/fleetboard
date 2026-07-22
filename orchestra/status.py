"""orchestra.status — the policy layer: what a session's signals MEAN.

Pure functions, no imports, no clock of their own, no disk, no subprocess.
Everything they need arrives as an argument, which is why the whole status
table is unit-testable and why tests/characterize.py can pin thousands of
input/output pairs against them without standing anything in.

The collectors upstream (transcripts, procs) say what is on disk and which
processes are alive; the observer downstream joins the two and decorates the
cards. This module is the seam in between, and the ORDER of the rules here is
the contract — see `classify_session`.
"""


def classify_session(age_s, alive, pending_tools, delegated,
                     skip_perms, working_s, shells=0, *, turn_ended=False,
                     evidence_age=None, procs_known=True, thinking_s=None,
                     block_grace_s=None, orphan_grace_s=None):
    """Base session status from observable signals (before limit/handoff
    overrides). `delegated` counts pending workflows + background agents.
    Returns (status, tool_running).

    ORDER IS THE CONTRACT: nothing is decided by a clock before the evidence
    on disk has been read. The old ladder tested `age_s < working_s` first,
    so a question already written to the transcript was reported as WORKING
    until the 90 s window expired — and if it was answered inside that window,
    NEEDS ANSWER was never shown at all.

    `turn_ended` is the CLI's own end-of-turn marker (`system`/`turn_duration`)
    seen AFTER the agent's last word — see `transcripts.parse_session_tail`.
    Measured on the real corpus, 66 of 79 in-window sessions (84 %) carry it,
    so for five sessions in six this function no longer waits out `working_s`
    to admit the agent stopped — median lateness removed, the full 90 s. It is
    deliberately weaker than every positive sign of work above it — see the
    branch itself.
    """
    pend = pending_tools or []
    age = age_s if evidence_age is None else evidence_age
    thinking_s = working_s if thinking_s is None else thinking_s
    block_grace_s = working_s if block_grace_s is None else block_grace_s
    # Defaults to working_s so this layer is provably behaviour-identical
    # apart from the two intended fixes. Tightening it (10 s is the target)
    # flips a live-but-unpaired session to ENDED, which feeds worktree-FREE
    # and therefore dispatch targeting — it needs reliable process detection
    # behind it, so it lands as its own step, not smuggled in here.
    orphan_grace_s = working_s if orphan_grace_s is None else orphan_grace_s

    if not procs_known:                      # ps/lsof failed wholesale: never
        return "unknown", False              # claim ENDED, never claim FREE

    if alive and "AskUserQuestion" in pend:  # the question is ON DISK
        return "needs_input", False
    if alive and delegated:                  # awaiting its own workflows or
        return "working", False              # background agents — not the
                                             # user's turn
    if alive and shells:                     # a Bash shell is still running —
        return "working", True               # backgrounded ones leave the
                                             # transcript idle until they exit
    if alive and pend:
        # "awaiting approval" and "tool still running" are the same bytes on
        # disk. Under --dangerously-skip-permissions there is nothing to
        # approve; otherwise hold WORKING until the silence outlasts genuine
        # tool-run silence before calling it BLOCKED.
        if skip_perms or age < block_grace_s:
            return "working", True
        return "blocked", False
    if alive and turn_ended:
        # The CLI wrote its own end-of-turn marker after the agent's last word.
        # OBSERVED, so no clock is consulted: today this session reads WORKING
        # for the rest of the 90 s window and the user is told to come back
        # late. It sits BELOW every positive sign of work above — delegated
        # workflows/background agents, a live background shell, an unresolved
        # tool_use — because a turn can end while any of those keep running,
        # and "the agent is still busy" beats "the turn closed". Above the
        # clock, below the evidence.
        return "waiting", False
    if not alive:
        # A fresh write with no observed process is "we have not seen the
        # process yet" (a just-exec'd agent, or a lagging proc-table read) —
        # not "ended". This rule was implicit in the old first branch; it is
        # now named, bounded, and testable.
        return ("working", False) if age < orphan_grace_s else ("ended", False)
    if age < thinking_s:
        return "working", False              # decay, LAST
    return "waiting", False


def closeout_step(paired_status, any_working, sent, now, nudge_after_s=60):
    """Step two of ✓ finish (✕ close) when the landing won't verify: decide,
    from THIS worktree's live session, whether to refuse, send the user to
    ✉ chat, or type one nudge at the idle agent. Pure (no subprocess/tmux) so
    the whole table is unit-testable.

    The deadlock this breaks: an agent finished its closeout but left one dirty
    file it judged "another session's in-flight work" — and no other session
    was live, so nothing would ever converge. ✕ close refused forever while the
    agent idled forever. A nudge that names the leftovers and says they are
    this agent's to judge is the only exit.

    Ordering is the point. Any working session (it might be mid-closeout)
    refuses before anything else, so we never type over live work. A stuck
    agent goes to chat rather than get a nudge typed across its open dialog.
    Only a plainly idle ('waiting') agent, briefed at least nudge_after_s ago
    (the anti-double-type guard against rapid clicks), gets nudged. A scan that
    couldn't pair any session with the live pid returns None here and we refuse
    — never type into a terminal we can't classify.
    """
    if any_working:
        return "refuse"
    if paired_status in ("needs_input", "blocked"):
        return "chat"
    if paired_status == "waiting":
        return "nudge" if now - sent >= nudge_after_s else "refuse"
    return "refuse"


def card_availability(st, has_live):
    """Card-level triage from its sessions' statuses (post handoff filter)."""
    if not (has_live or "working" in st):
        return "free"          # safe to point a new agent here
    if any(k in st for k in ("needs_input", "blocked")):
        return "attention"     # hard blocker — needs you
    if "waiting" in st and "working" not in st:
        return "attention"     # everyone parked at the prompt — needs direction
    if "limit" in st and "working" not in st:
        # out of juice, not out of instructions — nothing you can do until the
        # limit resets, so it is NOT "needs you". (This card-level "waiting" is
        # unrelated to the session status "waiting" = idle at the prompt.)
        return "waiting"
    return "busy"              # something is actively working
