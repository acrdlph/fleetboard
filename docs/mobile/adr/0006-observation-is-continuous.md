# ADR 0006 — Observation becomes continuous and client-independent

**Date:** 2026-07-21 · **Status:** Accepted

## Context

`cached_state()` (orchestra.py:1000) is **lazy**: state is computed only when a client requests
it, cached for `STATE_TTL_S = 4.0`, then discarded. State exists because a browser asked for it.

This is the structural blocker for push. The entire purpose of a notification is to tell the
user something **when they are not looking at the board**. Under a lazy model, if no client is
connected, nothing computes, no state change is detected, and no notification can ever fire.

This is not a latency problem to be tuned. It is an impossibility.

Separately, the lazy model is slow: `collect_state()` measures **1641 ms**, of which
**1277 ms (78 %)** is a git subprocess storm — five git processes per worktree, 45 spawns for
nine worktrees. Stacked with the 4 s cache and the 5 s browser poll, a status change takes up
to **~10.6 s** to reach the screen.

## Decision

Observation moves to a **continuous background loop that runs whether or not any client is
connected**, maintaining an authoritative, versioned in-memory model of the fleet.

Two properties follow, and both matter independently:

1. **Client-independent** — push becomes possible at all.
2. **Stateful** — the model *accumulates* rather than being re-derived from scratch each time.
   This is what fixes the status quality problem, not merely the speed problem. A stateless
   collector can only ask *"is this file's mtime within `working_s = 90`?"* — coarse, because
   it is the only question available to it. A stateful engine that was watching when the write
   happened knows something strictly better: *"this session wrote 2.3 s ago."*

## Consequences

- Enables push, sub-second board updates, and a tightened status model — one change, three wins.
- **Statefulness introduces drift risk.** Stateless re-derivation is self-healing: every
  collection is a fresh consistent read, so bugs cannot accumulate. A stateful engine can
  silently diverge and stay wrong. Mandatory mitigations — periodic full reconciliation,
  transcript truncation/rotation/compaction detection, laptop sleep/wake handling, missed-event
  detection, memory bounds — are specified in `ENGINE.md` and are **not optional**. A design
  that is fast but silently wrong is worse than today's.
- Idle CPU cost rises from ~zero to a continuous loop. Mitigated by making the loop event-driven
  (kqueue) rather than sweeping, and by the collector optimisations that cut 1641 ms to a small
  fraction of it.
- The 90 s `working_s` window can shrink — **but carefully.** Part of that window covers poll
  granularity (removable); part covers genuine silence while an agent thinks (**not**
  removable). Conflating them would make the board flicker, which is worse than lag. The
  anti-flicker rule is specified in `ENGINE.md`.

## Alternatives rejected

- **Keep lazy collection, just make it faster.** Fixes the 10.6 s lag for an open browser and is
  far cheaper — but cannot deliver push at all, and leaves `working_s = 90` untouched because
  precise write timestamps are unavailable to a stateless collector. The cheap collector work is
  still done (it is step 1 of the migration), it is simply not sufficient.
