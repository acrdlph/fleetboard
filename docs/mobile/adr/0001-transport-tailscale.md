# ADR 0001 — Reach the server over Tailscale, not the public internet

**Date:** 2026-07-21 · **Status:** Accepted

## Context

orchestra binds `127.0.0.1:4242`. The README warns that binding wider "serves your transcript
text to the network" — and it undersells the risk: the server also *types into live terminals*
and dispatches agents running `--dangerously-skip-permissions`. A phone client needs to reach
it from outside the machine.

## Decision

The phone reaches the Mac over a **Tailscale (WireGuard) tailnet**, at the Mac's `100.x.y.z`
address. The server binds the tailnet interface specifically — not `0.0.0.0`.

## Consequences

- Works anywhere the phone has connectivity: LTE, coffee shop wifi. No port forwarding.
- Zero public attack surface. WireGuard provides transport encryption, so TLS inside the
  tailnet is a defence-in-depth question rather than a confidentiality requirement (see the
  auth design in `ARCHITECTURE.md`).
- Requires Tailscale installed on both devices.
- A tailnet is *not* a trust boundary on its own — a compromised or shared tailnet device can
  still reach the server. Token auth is therefore still mandatory, not optional. See ADR 0007.
- The server must detect the tailnet interface, and must refuse to bind beyond loopback when
  no auth is configured.

## Alternatives rejected

| option | why rejected |
|---|---|
| LAN-only (`0.0.0.0` + Bonjour) | Dead the moment the user leaves the house — defeats the "away from the desk" premise entirely. |
| Cloudflare Tunnel / ngrok | Prompt and agent-reply text would transit a third party, and a public hostname is a permanent attack surface for a service that can execute terminal input. |
| Custom relay server | All the exposure of a tunnel plus infrastructure to run and secure. No benefit at single-user scale. |
