# ADR 0004 — Settle the contract once; build the backend first; iOS last

**Date:** 2026-07-21 · **Status:** Accepted

## Context

Two symmetric traps were identified:

1. **Build iOS first** against today's `/api/state`, then re-architect the backend → the Swift
   client gets rewritten too.
2. **Refactor the backend first** without knowing what a phone needs → the backend gets
   rewritten when the phone arrives. Deltas, idempotency keys, durable resource ids and an
   event stream exist *because* a phone on a flaky tailnet needs them; a backend rebuilt for
   the browser alone will not grow them naturally.

The user's framing: *"If we build the iOS app now and then we change the architecture, then we
have to build both again."*

## Decision

Neither "backend first" nor "mobile first" — **contract first**:

> Design the contract once, knowing both clients. Implement the backend first. Prove it in the
> browser. Then the iOS app is a client of a settled contract, not the driver of another rewrite.

Concretely:

- **Phase A** — design. `ENGINE.md`, `FRESHNESS.md`, `ARCHITECTURE.md`, `API.md` agree before
  any code. Mobile UX and iOS specs (`UX.md`, `IOS-APP.md`) are produced *now* as requirements
  input, then held.
- **Phase B** — implement the backend in shippable layers, each visible in the browser.
- **Phase C** — build iOS against the settled, already-proven contract.

## Consequences

- The mobile design work is not deferred, only its *implementation* is. It functions as a
  requirements document for the backend.
- The delta/event format is the load-bearing interface: the browser consumes it over SSE, the
  APNs pipeline is derived from it, and the Swift client reconciles against it. Getting it right
  once is the entire point of this sequencing.
- Phase B delivers user-visible value on its own — the board gets dramatically faster — so the
  sequencing costs nothing even if iOS were never built.

## Alternatives rejected

- **Parallel tracks.** Building iOS alongside the backend refactor would mean chasing a moving
  contract. Rejected as the exact failure the user named.
