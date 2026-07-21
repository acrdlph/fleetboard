# ADR 0003 — Push via APNs, driven from stdlib Python

**Date:** 2026-07-21 · **Status:** Accepted

## Context

The phone's core job is to tell the user something *when they are not looking* — an agent needs
an answer, a limit reset, a mission landed. That requires push. The user confirmed they hold a
paid Apple Developer account, which is what makes APNs (and Live Activities, widgets and
TestFlight) available at all.

The complication: orchestra's identity is "zero dependencies — one python3 stdlib file". APNs
requires an ES256-signed JWT and an HTTP/2 POST. Python's stdlib provides **neither** ECDSA
signing nor an HTTP/2 client.

## Decision

Use **APNs**, driven from stdlib Python by shelling out to two binaries that ship with macOS:

- **`openssl`** (3.6.2 verified present) for the ES256 signature over the JWT signing input.
- **`curl --http2`** (8.7.1 with nghttp2 1.67.1 verified linked in) for the POST to
  `api.push.apple.com`.

The notification layer is written behind a **pluggable sink interface** so an alternative
delivery path (e.g. `ntfy.sh`) remains a config change rather than a rewrite.

## Consequences

- Zero-dependency status is preserved in letter and mostly in spirit — no pip install, but two
  new subprocess dependencies on tools macOS already ships.
- **The sharp edge:** `openssl` emits a **DER-encoded** signature; JOSE requires **raw `r||s`,
  64 bytes**. That conversion is mandatory and is the single most likely thing to be wrong.
  It must be covered by a unit test with a known-good vector before anything is built on it.
- Linux would need its own path for both pieces. macOS is the primary platform.
- APNs sandbox vs production environments, token rotation, and `410 Unregistered` handling all
  become server concerns.

## Alternatives rejected

| option | why rejected |
|---|---|
| Add PyJWT + httpx | Simplest correct implementation, but breaks the zero-dependency identity outright for a feature that has a working stdlib path. Kept documented as the escape hatch if the openssl route proves fragile. |
| ntfy.sh only | Needs no Apple account, but requires a second app on the phone, sends alert text to a third party, and cannot drive Live Activities. Retained as a fallback sink, not the primary. |
| No push in v1 | Fastest, but guts the premise — the user would still be checking their desk. |
