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
                     evidence_age=None, procs_known=True, quiet_s=None,
                     block_grace_s=None, orphan_grace_s=None):
    """Base session status from observable signals (before limit/handoff
    overrides). `delegated` counts every piece of work this session is waiting
    on itself: the CLI's own pending workflows and background agents, plus the
    background tool_uses it launched and has not been notified back about —
    `transcripts.parse_session_tail` owns the last of those and says why the
    first two are not enough on their own. Returns (status, tool_running).

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

    THREE clocks remain, and they answer three different questions with three
    different costs. Each is None here and falls back to `working_s`, which is
    what every caller but `scan_sessions` wants; each carries its measured
    value, its distribution and its misfire rate beside its key in config.py,
    and none of them is `working_s` wearing a new name any more:

      `block_grace_s` — an unresolved tool_use is a tool still RUNNING for
        this long, then it is you it is waiting for (■ BLOCKED). Reached only
        when the branches above have not already explained the silence, and
        dead code entirely under --dangerously-skip-permissions.
      `orphan_grace_s` — a fresh write with NO observed process is "we have
        not seen it yet" for this long, then the session is over. The
        dangerous one: ENDED feeds worktree-FREE feeds dispatch targeting.
      `quiet_s` — the LAST branch: unexplained silence before a live agent
        stops being WORKING. Reached only when every explanation above it has
        been ruled out — no pending tool, no delegated workflow or background
        agent, no live shell, no observed turn end — so it covers exactly "it
        has gone quiet and nothing says why". (It was called `thinking_s`
        while it was still a placeholder defaulting to 90.)
    """
    pend = pending_tools or []
    age = age_s if evidence_age is None else evidence_age
    quiet_s = working_s if quiet_s is None else quiet_s
    block_grace_s = working_s if block_grace_s is None else block_grace_s
    orphan_grace_s = working_s if orphan_grace_s is None else orphan_grace_s

    if not procs_known:                      # ps/lsof failed wholesale: never
        return "unknown", False              # claim ENDED, never claim FREE

    if alive and "AskUserQuestion" in pend:  # the question is ON DISK
        return "needs_input", False
    if alive and delegated:                  # awaiting work it launched itself
        return "working", False              # — a workflow, a background agent,
                                             # a backgrounded tool. The harness
                                             # resumes the session when they
                                             # report back, so it is not the
                                             # user's turn.
    if alive and shells:                     # a Bash shell is still running —
        return "working", True               # backgrounded ones leave the
                                             # transcript idle until they exit
    if alive and pend:
        # "awaiting approval" and "tool still running" are the same bytes on
        # disk. Under --dangerously-skip-permissions there is nothing to
        # approve; otherwise hold WORKING until the silence outlasts genuine
        # tool-run silence before calling it BLOCKED. A RUNNING Bash never
        # gets here — the `shells` branch above answers it, because the Bash
        # tool wraps every command in a live child shell. One awaiting
        # APPROVAL has no such shell, which is what this branch is really for.
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
        # process yet" (a lagging proc-table read) — not "ended". Measured, a
        # just-exec'd agent is NOT this case: `ps` and lsof have it 2.1–3.0 s
        # before its transcript's first byte exists. What the grace really
        # covers is a probe that came back empty, and config.py says why that
        # keeps it at 90 rather than at the ~0 the lag itself would buy.
        return ("working", False) if age < orphan_grace_s else ("ended", False)
    if age < quiet_s:
        return "working", False              # decay, LAST
    return "waiting", False


# How much attention a status asks for — lower is LOUDER. The ranking is the
# same one `collect_state` sorts sessions by; it is repeated here rather than
# imported because this module owns policy and imports nothing.
LOUDER = {"needs_input": 0, "limit": 1, "blocked": 2, "working": 3,
          "waiting": 4, "ended": 5}
FLICKER_DWELL_S = 3.0


def settle(prev, proposed, now, since, dwell_s=FLICKER_DWELL_S):
    """Asymmetric hysteresis on a published status (ENGINE.md §6.3(a)).
    Returns `(status, since)` — the status to publish and when it was ADOPTED.

    Flicker is worse than lag: a board that oscillates ● WORKING → ◆ YOUR TURN
    → ● WORKING summons the user for nothing, and a board that cries wolf gets
    ignored. So escalation toward MORE attention publishes on the sweep that
    sees it, always, with no clock consulted; de-escalation may only publish
    once the current status has stood for `dwell_s`. The cost of the rule is
    bounded by construction: it can never delay bad news, only good news, and
    only by `dwell_s`.

    `since` is the clock of the last ADOPTION and is deliberately NOT refreshed
    while a status merely persists. ENGINE.md's sketch refreshes it on the
    equal-status path, which under the hot cadence (`hot_s` 0.15 s) turns the
    dwell into a 0–3 s sawtooth: `since` is bumped every dwell_s, so how long a
    real de-escalation waits depends on where in that cycle it lands. Keeping
    `since` fixed makes the rule what it says it is — a status must stand for
    `dwell_s` before it may quieten — and makes the wait deterministic. It also
    means the dwell does not stack on top of `quiet_s`: a session that has been
    WORKING for the whole quiet window crosses at `quiet_s` exactly, because by
    then `now - since` is `quiet_s`, far past the dwell.

    An unranked status (`unknown`, from a wholesale `ps`/`lsof` failure, or
    anything a later version adds) is treated as louder than everything: it
    publishes at once and is never suppressed. Never hold back a status this
    table cannot reason about.
    """
    if prev is None:
        return proposed, now
    if proposed == prev:
        return prev, since                       # nothing adopted, nothing moves
    if LOUDER.get(proposed, -1) < LOUDER.get(prev, -1):
        return proposed, now                     # escalate instantly, always
    if now - since < dwell_s:
        return prev, since                       # de-escalation must dwell
    return proposed, now


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
