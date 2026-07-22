# ADR 0015 — Pairing by QR, and a bind that cannot be got wrong

**Date:** 2026-07-22 · **Status:** Accepted — **shipped** (`orchestra/pairing.py`,
`orchestra/qr.py`, `orchestra/tailnet.py`)
· Completes what [ADR 0014](0014-per-device-bearer-tokens.md) deferred ("Pairing (QR + code) —
tokens are minted at a shell and carried by hand")
· Scoped by [ADR 0013](0013-plain-http-over-the-tailnet.md) (no TLS) and
[ADR 0009](0009-api-v1.md) (the versioned surface is `/api/v1`)

## Context

ADR 0014 built the credential and deliberately did not build the way to install it. A token is
`orc1_` plus eight hex plus forty-three base64url characters, printed once, at a shell. Getting
one onto a phone meant typing it — once, with no feedback, on a phone keyboard, into a field
where a single wrong character is indistinguishable from a wrong token. That is the kind of
flow people complete by pasting the token into a notes app first, which turns a
carefully-hashed 256-bit secret into a plaintext file that syncs.

Two other things were left open and turn out to be the same piece of work:

- **the tailnet address was a magic number.** `--host 100.113.110.31`, pasted from a document.
  One keystroke from `0.0.0.0` in muscle memory, and Tailscale can reassign it.
- **`0.0.0.0` was refused outright**, with no way to say "yes, I mean it" — which is a rule
  people work around by editing the source.

## Decision

**A QR carries a short-lived ticket, not a credential; the address is detected, not typed; and
device management answers to the Mac and to no token at all.**

### The QR carries a pairing code

`POST /api/v1/devices/pair/open` mints an 8-character Crockford base32 code, valid **120 s**,
**single use**, and renders `orc://p?h=<host>[&p=<port>]&c=<code>` as an SVG QR. The phone
presents the code to `POST /api/v1/pair` and gets a token back.

The indirection is the whole security argument. **A QR on a screen is visible to the room** —
to the person behind you, to a video call you forgot was sharing, to a screenshot in a bug
report six months from now. A token in that picture is a permanent credential to a server that
can type into terminals running `--dangerously-skip-permissions`. A pairing code in that picture
is worthless 120 seconds later, and worthless immediately if anybody has already used it.

Crockford base32 (no `I`, `L`, `O`, `U`) because the manual fallback is a human reading a screen,
and `I`/`1`, `O`/`0`, `L`/`1` is where that goes wrong. Normalisation is generous about form —
case, spaces, dashes, and the four ambiguous glyphs folded — and not at all generous about value:
`hmac.compare_digest` over SHA-256.

**The window lives in memory.** It dies with the process, and that is correct rather than a
limitation: a door that reopens by itself after a restart is a door nobody closed.

### The QR encoder is written here, and proved elsewhere

Zero dependencies is a real constraint on this project, so `qrcode` was not an option and
`orchestra/qr.py` is ~400 lines of ISO/IEC 18004 — byte mode, EC level M, versions 1–10, and it
**raises** rather than truncating a payload that does not fit.

The reason this gets its own paragraph in an ADR is the failure mode. **A QR encoder that is
wrong produces a picture that looks completely correct**: three finder patterns, a plausible
speckle, the right dimensions, and no camera on earth can read it. There is no crash and no red
test. METHOD.md §3 is about exactly this, so the encoder is not trusted on the basis that its
output looks like a QR code. It is verified three ways, none of which is a second reading of the
same file:

1. **against Apple's `CIQRCodeGenerator`** — a third-party implementation of the same spec —
   compared **module for module**, 14 payloads across versions 1, 3, 4, 5, 6, 7, 8, 9, 10 and
   all four EC levels;
2. **through Vision's `VNDetectBarcodesRequest`**, which must return the exact string that went
   in — 23 payloads, including the SVG the live server actually served;
3. **Reed–Solomon syndromes** in the unit suite, which is a property of the code rather than a
   re-run of the encoder, plus golden matrices recorded from a run that (1) and (2) both passed.

That was not ceremony. The reference comparison found three real defects that looked fine: the
version-information block is required from **version 7** (not 10, as first written), its BCH
generator is **thirteen** bits (`0x1F25`) and had been written with a spare nibble, and the
format information is placed **most-significant bit first** — reversed, every one of 1,681 data
modules was correct and the code was unreadable.

Two things the harnesses could not see, measured rather than assumed: removing the quiet zone
leaves the external checks completely green (Vision reads a margin-less code out of a clean
synthetic PNG — the margin is for a camera pointed at a busy screen, which nothing here
reproduces), and a corrupted RS generator does **not** decode, contradicting the guess that
error correction would absorb it.

### `admin` is the absence of a token, from this machine

API.md §2.5 puts device management behind an `admin` scope and says phones are never issued it.
Scopes are still deferred (ADR 0014 declined to half-build the ladder), so `admin` is implemented
as a rule that needs no ladder and is strictly stronger:

> **Device management answers to this machine, holding no token.**

No token grants it — not a valid one, not one presented from loopback. A stolen phone cannot
list devices, cannot open a pairing window, and cannot revoke the device that would have revoked
**it**. A stranger with no token still gets `401` rather than `403`, because authentication is
decided first and an unauthenticated peer should learn nothing about which routes are special.
A `403 admin_local_only` costs nothing from the failure budget: the caller is a known device
making a legitimate mistake, and charging it would let a buggy app lock its own phone out.

Matched by prefix at a segment boundary (`/api/v1/devices`, `/api/v1/devices/…`, not
`/api/v1/devices-of-others`), where `EXEMPT` matches exactly. The asymmetry is deliberate and
one-directional: too-eager matching here is a refusal, never a hole.

### Exempt means no token, not no checks

`POST /api/v1/pair` is the second and last exempt route. Adding it exposed a real hole for the
ten minutes it existed: `auth.check` returned from the exempt branch **before** the cross-site
guards ran, so a page you were merely visiting could have posted a `text/plain` pairing claim
from your browser with no preflight to stop it. It could only have guessed at the code, but
"it would probably fail anyway" is not how rule 6 reads. The exempt branch now runs
`browser_guards` and still sits above the failure budget, which is what `/api/health` needs.

### The bind

| you type | you get |
|---|---|
| nothing | `127.0.0.1` |
| `--tailnet` | the Tailscale address, **detected**; refuses to start if Tailscale is not up, saying which of three situations it is |
| `--host <addr>` | that address; `0.0.0.0` is still refused, and the message now names `--tailnet` |
| `--bind-every-interface` | `0.0.0.0`, said out loud. Cannot be set from the config file |

**No bind beyond loopback succeeds with no device registered**, including the wide one.

Detection is a **bind**, not a parse: candidates come from `ifconfig` and `tailscale ip -4`, and
each is then actually bound on port 0 and thrown away. METHOD.md §2 — the question is "will
`Server(...)` succeed", and an address on a down interface passes every proxy for that question
and fails the real one.

### Two listeners, and this was found by driving it

A non-loopback bind also starts a listener on `127.0.0.1`. This is not a convenience. Driving a
real `--tailnet` board showed a server bound only to `100.113.110.31` is **broken in two ways
that no unit test binding `127.0.0.1` can see**:

1. the board's own bookmark is `ConnectionRefused` — nothing is listening on loopback;
2. a request the Mac sends to *its own tailnet address* arrives with a source address of
   `100.113.110.31`, which is not loopback and never can be. So the `admin` rule above locked
   the person at the keyboard out of device management entirely. **The security rule was right
   and the topology made it unusable.**

The tailnet listener carries the phone; the loopback listener carries the board. API.md §2.3
had assumed this split all along, writing about "the tailnet listeners" and "the loopback
listener" separately; it just had never been built.

## Consequences

- **The first device is still minted at a shell**, and the docs say so rather than implying
  otherwise. The tailnet bind refuses with nobody registered, and pairing needs the phone to
  reach the server, so the bootstrap is `--add-device` once; every device after that is a QR.
  Closing that needs a mode that binds the tailnet with nobody registered — exactly the silent
  wide exposure ADR 0013 forbids — so it is an open item, not a rushed feature.
- A pairing attempt writes **two** audit lines and they are not a duplicate: `allow` from the
  door (`auth.check`, the unskippable seam, which has no token to judge) and `paired`/`refuse`
  from pairing, which knows what actually happened. The second outcome is spelled `paired`
  rather than `allow` so the log never reads `allow` beside a refusal of the same request.
- `/api/v1` exists, with five routes on it. They answer with **real status codes** rather than
  this server's in-payload `{"ok": false}` convention, because a Swift client written from
  API.md has to branch on 409 vs 403 vs 429 before parsing anything.
- 78 new tests (700 total), 27 mutations applied and 27 caught after two rounds; the
  characterization payload is unchanged, which is correct — none of this touches `/api/state`.
- `orchestra/qr.py` is reusable and has no orchestra dependencies. It is a leaf.

## Alternatives rejected

| option | why |
|---|---|
| **Put the token in the QR** | a photograph of the screen becomes a permanent credential. The indirection costs one round trip and is the entire point |
| **A `qrcode` dependency** | zero dependencies is a real constraint (README, Conventions) and trading it requires its own ADR. A QR encoder is bounded, fully specified work that can be paid for once |
| **Trust the encoder because the picture looks right** | this is the specific thing METHOD.md §3 exists to prevent, and the reference comparison found three defects that all looked right |
| **A `--pair` CLI flag** | it would open a window, draw a QR and exit — and the window dies with the process, so it would print a picture of a code that was already dead. The phone would get `pairing_not_open` and the user could not tell that from a network problem. Headless boxes `curl` the same route the page uses |
| **Six digits, as API.md §3 originally said** | 8 Crockford characters is 40 bits against 20, costs the user nothing extra to read aloud, and removes the four glyphs that get misread |
| **A longer window** | 120 s is long enough to unlock a phone and open a camera. The window is the thing that makes a photograph safe, so it is the last number to relax |
| **Persist the pairing window** | a restart would reopen a door nobody closed. In-memory is a property, not a shortcut |
| **Let a token grant `admin` if it is the only device** | "the rule relaxes when you are alone" is a rule nobody can reason about, and it is exactly the state a stolen first phone would be in |
| **Ask the user for the tailnet address** | it is a magic number that goes stale, and its most likely typo (`0.0.0.0`) is the dangerous one |
| **Bind only the tailnet, and reach admin over ssh** | it takes the board away from the browser it lives in, to solve a problem a second listener solves for four lines |
