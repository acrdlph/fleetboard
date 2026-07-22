# ADR 0011 — Where `ENGINE.md` and measurement disagree, measurement wins

**Date:** 2026-07-22 · **Status:** Accepted

## Context

`ENGINE.md` is the design authority for the observer work. Implementing steps 0–2 and 5 against it
has now produced **six** places where following it literally would have shipped a defect, each
caught only because the number was measured rather than assumed:

| § | the document says | measured reality |
|---|---|---|
| §9 step 1 | rebuild a detached label as `detached@<branch.oid[:9]>` | git's abbreviation length is **per-repository** — 8 chars in `ConfidAi5/repo`, 9 in the other eight worktrees. The slice renders `detached@6dcc53338` where git renders `detached@6dcc5333`. Detached heads now spend one extra `rev-parse`. |
| §9 step 1 | move the subagent `rglob` **after** the window check | That walk is exactly what rescues a session whose main transcript is idle while a Workflow writes under `<session-id>/` — the advertised ⚙ feature. Skipping it makes such sessions **vanish from the board**, silently. The walk stayed complete and got cheap instead (`os.scandir`, 146 ms → 84 ms). |
| §2.5 | `git_s = 5.0`, a re-probe cadence **within** a sweep (i.e. a memo) | There cannot be a memo. `dirty` is the working tree and no cheap stat detects an edit there — `.git/index` mtime does not, because it moves when *git* writes the index, which `--no-optional-locks` exists to prevent. Implemented as a **between-sweep cadence** at 15.0. |
| §6.3 | the `working_s` work lands **after** hooks (§7 ranks hooks above every other signal) | The CLI already writes its own end-of-turn marker, and it is present after the agent's last word in **66 of 79** in-window sessions (**84 %**). Hooks would improve the residual 16 %, not the majority path — so the majority path shipped first, off `system`/`turn_duration`, with `working_s` untouched at 90. Note this is *not* the threshold change §6.3 sequences: no timer moved. What lands is §7's rank 3 (precise file writes) displacing rank 4 (mtime heuristics) for 84 % of sessions, which §7 itself says is the right order. |

| §6.2 | `QUIET_S = 25.0`, "25 s is generous for it" | Measured over **14,006** unexplained mid-turn gaps in the 79 in-window transcripts, 25 misfires on **5.80 %** of them — one genuine thinking pause in 17 mistaken for the end of a turn — and it sits *below* the p95 (27.7 s) of the very population it exists to tolerate. 95 % of those misfires recover inside two minutes, so the rate is not a late correction, it is the ● WORKING → ◆ YOUR TURN → ● WORKING oscillation §6.3 opens by forbidding. Shipped at **45** (2.71 %), between the p95 and the p99 (72.7 s). |
| §6.3(a) | `settle()`'s final `return proposed, now` also runs when `proposed == prev` | That re-stamps `since` on every sweep that merely *agrees*, so under the hot cadence (`hot_s = 0.15`) the dwell becomes a 0–3 s sawtooth and how long a real de-escalation waits depends on where in the cycle it lands. `since` is now the clock of the last **adoption** and does not move while a status persists — which also means the dwell does not stack on `quiet_s`: the board says ◆ YOUR TURN at 45 s, not 48. Pinned by `test_the_dwell_does_not_stack_on_top_of_the_quiet_timer` and `test_an_unchanged_status_never_restamps_its_clock`. |

A seventh, softer case, on cadence rather than status: §2.5 specifies `idle_s = 1.0`. At the measured 1.68 CPU-s per sweep that
is **164 % of one core, continuously** — not aggressive, unreachable. It became affordable only
after the sweep got cheap, and even then costs 43 %.

## Decision

**Measurement supersedes `ENGINE.md`.** Where they conflict, the measured result wins, the
divergence is recorded *in the code* next to the constant or function it governs, and the
reasoning travels with it.

`ENGINE.md` remains the authority on **structure** — the decomposition, the state model,
versioning semantics, the seam rules, the mandatory mitigations for statefulness. Its judgement
there has been consistently good. What it cannot be trusted on is **numbers and platform
behaviour**, because it was written from reasoning rather than from this machine.

Concretely:

- A constant that diverges from the document carries a comment giving the measurement, the unit,
  and what would have to change for the document's value to become reachable. `IDLE_S` and
  `GIT_S` already do.
- CPU is measured with `resource.getrusage(RUSAGE_SELF) + RUSAGE_CHILDREN`, never wall time and
  never `ps -o time` on the parent. Both understate this workload by roughly 2×, for different
  reasons: the sweep is a parallel fan-out, and most of its cost is billed to `git`/`ps`
  children. An earlier revision made exactly this mistake and reported 17 % where the truth was
  55 %.
- A performance claim without a measurement is not a claim.

## Consequences

- `ENGINE.md` is *not* rewritten to match. It is a design record, and rewriting it would erase
  the evidence that these questions were considered. This ADR is the index of where it is stale.
- Anyone reading `ENGINE.md` must read this ADR alongside it. The pointer is in
  `docs/mobile/README.md` and at the head of `ENGINE.md`.
- The precedent is already set in code: `aaff6e4` put corrected numbers beside `IDLE_S` and left
  the document alone.

## Known boundary, recorded rather than fixed

The transcript memo keys on `(st_size, st_mtime_ns)` plus `(st_dev, st_ino)` identity. It can be
defeated by an in-place rewrite that preserves **all four** — same size, byte-identical
nanosecond mtime, same inode. Verified: restoring mtime with `os.utime(..., ns=...)` makes the
memo serve stale content.

This is adversarial rather than realistic — Claude Code transcripts are append-only, so size
always moves — and the 60 s cold reconcile bypasses the memo and counts any disagreement as
`drift`. It is recorded because the boundary of a cache should be written down, not discovered.

Note the first attempt to demonstrate this **failed** and looked like proof of safety:
`os.utime` with float seconds cannot restore the nanosecond component. A negative result from a
test that could not have succeeded is not evidence.

## Alternatives rejected

- **Rewrite `ENGINE.md` to match the implementation.** Destroys the record of what was
  considered and why, and invites the same errors to be re-derived later.
- **Treat the document as binding.** Would have shipped five defects, three of them silent — a
  board that flickered on one thinking pause in 17 among them.
