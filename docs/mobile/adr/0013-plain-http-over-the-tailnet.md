# ADR 0013 — Plain HTTP over the tailnet, with a scoped ATS exception

**Date:** 2026-07-22 · **Status:** Accepted · **Supersedes** the TLS choice sketched in `ARCHITECTURE.md` §5

## Context

The phone reaches the server at the Mac's tailnet address (`100.113.110.31`, ADR 0001). Two
things then collide:

1. **WireGuard already encrypts the link.** Tailscale gives mutual authentication and
   confidentiality between devices. TLS on top secures a channel that is already secure.
2. **iOS App Transport Security blocks plain HTTP by default**, and the usual escape hatch does
   not apply: `NSAllowsLocalNetworking` covers `.local` and link-local, **not** the `100.64/10`
   CGNAT range Tailscale hands out. So plain HTTP to a tailnet address needs an explicit
   exception.

`ARCHITECTURE.md` proposed self-signed TLS with SPKI pinning in the app. That is defensible but
carries real cost: certificate generation, renewal, a trust store on the phone, pinning logic
that fails closed and strands the user, and a second failure mode whenever anything changes.

## Decision

**Plain HTTP over the tailnet.** No TLS on the server. The iOS app carries an ATS exception
scoped as narrowly as iOS permits, and the app is personal (TestFlight / sideload), never
App Store, so the exception attracts no review consequence.

Security comes from three layers that are *not* TLS:

| layer | what it stops |
|---|---|
| **Tailscale/WireGuard** | anyone not on the tailnet; passive interception on any network in between |
| **Per-device bearer token** | a compromised or shared tailnet device driving the fleet |
| **Loopback-by-default bind** | the server is not reachable at all until the user opts in |

## Why TLS does not earn its place here

The threat model inside a tailnet is **device compromise**, not wire interception — and TLS does
nothing about device compromise. A hostile device on the tailnet completes a TLS handshake
exactly as happily as a friendly one. What actually stops it is the bearer token, which is
required regardless of transport.

Self-signed TLS would therefore add certificate lifecycle management in exchange for defending
against an attacker who is already excluded by WireGuard.

## Consequences

- The server binds the **tailnet interface specifically**, never `0.0.0.0`, and refuses to bind
  beyond loopback unless a token is configured. Silent wide exposure must be impossible.
- The token is the whole of authentication, so it must be per-device, revocable, and compared in
  constant time. Its design is ADR 0014.
- **This decision is scoped to the tailnet.** If the server is ever exposed through Cloudflare
  Tunnel, a LAN bind, or anything reachable without WireGuard, TLS stops being optional and this
  ADR must be superseded. The bind logic should make that transition loud rather than silent.
- The ATS exception is a one-line `Info.plist` entry, versus a certificate pipeline. If the
  transport assumption ever changes, adding TLS later is additive — nothing here forecloses it.

## Alternatives rejected

| option | why |
|---|---|
| **Self-signed TLS + SPKI pinning** (`ARCHITECTURE.md` §5) | Defends against an attacker WireGuard already excludes, at the cost of cert generation, rotation, pinning that fails closed, and a second thing to debug when the phone cannot connect. Revisit only if the transport assumption changes. |
| **`tailscale serve` / `tailscale cert`** | Gives real certs with no manual management and is genuinely attractive — but it fronts the server with a proxy, which conflicts with binding a known interface and with the Host allowlist, and it makes the server's reachability depend on a second daemon's configuration. Worth revisiting if cert management ever becomes desirable for another reason. |
| **`NSAllowsArbitraryLoads`** | Works, but disables ATS process-wide rather than for one host. A scoped `NSExceptionDomains` entry says what is actually intended. |
