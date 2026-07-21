# ADR 0005 — Push to clients with SSE on the existing ThreadingHTTPServer

**Date:** 2026-07-21 · **Status:** Accepted (verified empirically)

## Context

The board polls `/api/state` every 5 s. A phone cannot do that — battery, bytes, and a tailnet
that drops as the device moves between wifi and LTE. Clients need to be *pushed* to.

The gating risk: orchestra runs on `ThreadingHTTPServer`, which dedicates **one thread per
request**. An SSE connection is long-lived, so every subscriber pins a thread for its entire
lifetime. If dead clients never released their threads, this would force a rewrite onto
`asyncio` or `selectors` — a large change to a 2300-line file, and a serious cost.

## Decision

**Server-Sent Events over the existing `ThreadingHTTPServer`.** No server foundation rewrite.

Verified by direct measurement (12 concurrent SSE subscribers, then killed rudely by closing
sockets without a FIN handshake, simulating the phone dropping off the tailnet):

```
12 SSE clients open                    → 14 threads alive (1/client + main + accept)
broadcast → first-client latency       → 0.45–0.68 ms
normal GET while 12 streams held open  → 21.2 ms (not starved)
after 12 rude disconnects              → 0 subscribers, 2 threads   ← fully reclaimed
```

Working pattern: per-subscriber queue + `threading.Condition`; the handler blocks on
`cv.wait(timeout=25)` and emits a `: keepalive` comment frame on timeout.

## Consequences

- The largest architectural risk in the programme is retired. Everything downstream — the delta
  protocol, the browser's move off polling, the phone's live updates — rests on this.
- **Two mandatory mitigations:**
  1. Override `handle_error()` on the server. A dropped SSE client raises
     `ConnectionResetError` and `socketserver` prints a full traceback to stderr by default;
     without this, every tailnet blip spams the log.
  2. Impose a hard subscriber cap. Thread-per-client is fine at fleet scale (a browser in a few
     tabs plus a phone) but is not unbounded — reject beyond a configured maximum rather than
     degrading silently.
- SSE is one-directional (server → client). Client actions remain ordinary POSTs. This is a
  fit, not a limitation.
- SSE reconnection semantics (`Last-Event-ID`) give the resume-from-cursor behaviour the delta
  protocol needs, for free.

## Alternatives rejected

| option | why rejected |
|---|---|
| WebSockets | Bidirectional, but **not implementable in python stdlib** — the framing protocol would have to be hand-rolled or a dependency added. SSE gives everything needed here. |
| Long-polling | Works, but wastes a request round-trip per update and complicates the cursor logic. SSE is strictly better and equally stdlib-friendly. |
| Faster polling only | Does not solve the "no client attached" case that push requires (see ADR 0006), and burns phone battery. |
| Rewrite onto asyncio | Would have been forced if the measurement had failed. It did not. Large change, no longer justified. |
