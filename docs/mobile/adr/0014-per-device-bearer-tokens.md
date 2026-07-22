# ADR 0014 — Per-device bearer tokens, checked in one place

**Date:** 2026-07-22 · **Status:** Accepted — **shipped** (`orchestra/auth.py`)
· Referenced by [ADR 0013](0013-plain-http-over-the-tailnet.md) ("its design is ADR 0014")

## Context

Until this change the server had **no authentication of any kind**. `127.0.0.1` was the entire
security model, and it worked — but it is also a ceiling: the board cannot leave the machine,
which is the whole point of the programme (ADR 0001, ADR 0004).

Be concrete about what is behind the door, because it decides how much this is allowed to cost:

- the full text of every prompt you have typed and every reply an agent gave (`/api/chat`,
  and the session topics on `/api/state`);
- a **keyboard** attached to terminals running `--dangerously-skip-permissions` (`/api/send`),
  which will act on whatever arrives;
- a launcher that spends real usage (`/api/dispatch`) and schedules unattended resumes.

ADR 0013 already decided where the budget goes: not TLS — WireGuard has encrypted the link, and
the realistic attacker inside a tailnet is a **compromised or shared device**, which TLS does
nothing about — but tokens. This ADR is those tokens.

## Decision

**A per-device bearer token, stored hashed, checked in exactly one place, with loopback trusted.**

### The rule

> **Loopback is trusted. Everything else must present a valid token. A credential that is
> presented is always checked, wherever it came from. And a page from another site is not
> loopback, whatever the socket says.**

It is one sentence in one module docstring, and every clause is a test class in
`tests/test_auth.py`.

**Loopback is trusted** because a process that can open a socket to `127.0.0.1` is already
running as you on this Mac, and can read `~/.claude*/projects` directly with no server involved.
API.md §2.6 lists that as *unclosable*; a token there guards a door in a wall that is not
there. It is also what keeps the existing browser working with no changes at all — the board has
no token and never will.

**A presented credential is always checked**, even from loopback, so the trust cannot be used as
a laundry: a revoked device stays revoked even if its requests arrive from `127.0.0.1`, which is
what a proxy in front of the server would do to them (`tailscale serve` makes every peer
loopback — API.md §2.7).

**A page from another site is not loopback.** This is the clause that was nearly missed, and it
is what makes the first clause safe to write down. A website you merely *visit* also speaks from
127.0.0.1 — through your browser, wearing the board's trust — and `POST /api/send` types into an
agent. That is CSRF, it predates this work, and it is closed by two lines:

| guard | what it stops |
|---|---|
| every mutation must send `Content-Type: application/json` | a "simple" cross-origin request needs no preflight; JSON makes one mandatory, and this server answers the `OPTIONS` with a refusal and no `Access-Control-Allow-Origin`, so the browser never sends the POST. `do_POST` had never looked at the media type — it just `json.loads`ed the body |
| a cross-site `Origin` is `403` | belt and braces for the same attack, and free |

All six of the board's own POSTs already sent the JSON header, so this cost nothing. A
hand-written `curl -X POST` now needs `-H 'Content-Type: application/json'`.

### The token

`orc1_<devid>_<secret>` — API.md §2.1, unchanged: `devid` is 8 hex characters and public (it is
the audit identifier and the registry key), the secret is `secrets.token_urlsafe(32)`, 256 bits.

- **Stored as `sha256(whole token)`**, so `devices.json` is not itself a credential — copying it
  yields a list of labels, not a way in. Mode `0600`, written atomically.
- Compared with **`hmac.compare_digest`**, never `==`.
- Generated with **`secrets`**, never `random`.
- The registry is memoed on the file's `(mtime_ns, size, ino)`, so a `--revoke-device` run in
  **another process** reaches a running server within one `stat`.
- `last_seen` is written at most once a minute per device. Writing it per request would be a
  disk write per SSE keepalive — METHOD.md §6, the rule about making things continuous.

### The check, in one place

`server.Handler.parse_request`, and nothing else. `handle_one_request` calls `parse_request`
*before* it looks up `do_<METHOD>`, so a route cannot forget the check, cannot opt out, and
cannot be added unguarded — including a `do_PUT` that does not exist yet, and including
`/api/events`, which returns early from `do_GET` and would have slipped past a guard written at
the top of the `elif` chain. A method the handler does not implement is refused *before* it is
told it is not implemented.

The alternative — a decorator per `do_*`, or a first line in both — is one invisible edit away
from being wrong.

The order inside `auth.check` is load-bearing and each step says why in the docstring: exempt
route → loopback-with-no-credential → failure budget → token shape → identity → revocation →
the browser guards. Two of those placements are the interesting ones:

- **loopback-with-no-credential is decided before the budget is consulted**, so the local
  browser can never be throttled — it presents nothing, so it cannot fail, so it never spends
  from a bucket, so no flood from anywhere else can lock the board out of its own server.
- **the browser guards run last, on both allow paths.** After authentication, because a
  stranger's POST deserves "you are not authenticated" rather than a lecture about media types;
  before anything is permitted, because the request they stop arrives with a *good* identity.

### Exempt routes — the complete list

| route | why |
|---|---|
| `GET /api/health` | it is the route you need *before* you have a token: it distinguishes "the Mac is asleep" from "my token was revoked" from "wrong address". A health check that requires the credential being diagnosed can only report a tautology. It carries `{ok, service, api, time}` — no worktrees, no counts, no hostname, no device list, nothing that varies with what the fleet is doing. The clock is the point: every other route's timestamps are unreadable to a client whose own clock is wrong |

**Nothing else**, and the two candidates that were rejected are worth recording:

- **the static pages** (`/`, `/stream.js`, `/map`, `/limits`, `/guide`) carry no transcript text
  and their source is public — but a browser cannot attach an `Authorization` header to a
  top-level navigation, so exempting them would load a shell whose every fetch then fails, which
  is a worse failure than being refused at the door. ADR 0002 puts a *native* client on the
  phone. They stay shut, and the tailnet sees exactly one open route.
- **`GET /api/state`** is "just a summary" until you read it: session topics are the first line
  of what you typed. It reads transcript text. Shut.

Matching is **exact** on `(method, path-without-query)` while the router matches by prefix. The
asymmetry is deliberate and one-directional: `/api/healthcheck-for-free` is a 401, not a hole.

### The audit log

`audit.log.jsonl`, `0600`, one JSON object per line: `at`, `peer`, `device`, `label`, `method`,
`path`, `outcome`, `code`. Every **mutation that is allowed** (every non-GET, plus `GET
/api/focus`, which raises a window and steals the keyboard) and every **refusal, on any route**.

- **Not the body.** `/api/send` carries the text typed at an agent and `/api/dispatch` a mission
  brief; logging either would make this file a second copy of exactly the asset the tokens
  exist to protect.
- **Not reads.** The board polls `/api/state`; logging reads would write a line every few
  seconds forever and bury the eleven lines that matter. A stolen token that only ever *reads*
  leaves no evidence here — a known gap, and the honest way to close it is a counter in `meta`,
  not a log nobody can grep.
- **It records the request, not the outcome.** The seam is before the route by construction —
  that is what makes it unskippable — and a second write afterwards would be a second thing to
  forget. What it proves is that somebody *asked*.

### The failure budget

Ten refusals per minute per source IP, token bucket, refilling. It is **not** what stops a token
being guessed — nothing guesses 256 bits from `secrets`; 10/min across a long weekend is 36,000
of 2²⁵⁶. What it buys:

- the audit log cannot be flooded by an unauthenticated peer (≤10 lines/min/IP), so evidence of
  a real theft stays findable beside it — and the refusal path is the expensive one, 99.6 µs
  because it writes to disk;
- a sha256 per attempt cannot be turned into CPU load;
- and the one that will matter: the pairing code of API.md §3 is six digits and will inherit
  this bucket.

Only failures spend from it; a success empties it (whoever holds a valid token can already do
everything a token permits, so charging them for a typo protects nothing and locks out the one
device trying to reconnect); the cross-site guards spend nothing (nobody is guessing). Entries
that have refilled are dropped on every touch, so the table is proportional to the peers
*actively failing*, with a hard cap above which a new address is treated as exhausted — the
failure direction is a refusal, never an allow.

### The bind

`python3 -m orchestra --host <tailnet-ip>` now **refuses to start** unless a device is
registered, and refuses `0.0.0.0` outright whatever the registry says. It used to print a
warning to stderr and bind anyway, which is the worst of both — it told you, in a stream nobody
reads, and did the thing. ADR 0013: *silent wide exposure must be impossible.*

### Administration is a shell, not a route

`--add-device LABEL`, `--list-devices`, `--revoke-device ID`. API.md §2.5 puts every device
route behind `admin` and never issues `admin` to a phone, for the reason that a device which can
revoke devices can revoke the device that would have revoked **it**. On this machine, at a
shell, is a boundary the network cannot cross. The token is printed once and is unrecoverable.

## What is deliberately NOT built

| deferred | why now is not the time |
|---|---|
| **Scopes** (`read`/`act`/`admin`, API.md §2.2) | one token grants everything. A phone that can read but not act is not the product, so the split buys nothing yet — and a half-built scope ladder is worse than an honest absence, because it invites the belief that a `read` token is safe to hand out |
| **Pairing** (QR + code, API.md §3) | tokens are minted at a shell and carried by hand. The pairing flow needs a second unauthenticated route, guarded by an attempt limit and a 120 s window; it can wait until there is a phone to walk through it |
| **Host allowlist** (API.md §2.3 step 2) | DNS rebinding is not closed: `evil.com` resolved to 127.0.0.1 sends `Origin: http://evil.com` and `Host: evil.com`, which agree with each other. The JSON/preflight rule blocks the *mutation* half of that attack, and closing the rest needs the bound address plumbed into the check |
| **Side-effecting GETs** | `GET /api/focus` raises a window and `GET /api/limits?refresh=1` shells out, so both are reachable cross-origin by an `<img>` or a no-cors `fetch`. Annoying, not dangerous — and `/api/v1` already makes focus a POST |
| **Tailnet whois, lockdown, idempotency, per-device buckets** | steps 8, 7, 10 of API.md §2.3. Each is real; none of them was *the missing one* |
| **Token rotation / expiry, audit rotation** | revoke-and-remint is the rotation story today, and the audit log is append-only forever (~150 B a mutation, i.e. a human click rate) |

## Consequences

- The board is unchanged and unaware. Every page and every click still works over loopback with
  no token — verified live, and pinned by `TestTheBrowserStillWorks`.
- A `curl -X POST` against the API needs `-H 'Content-Type: application/json'`.
- **No route can be added unguarded.** `tests/test_auth.py::TestEveryRouteIsGuarded` reads the
  routes back out of `server.py`'s own AST — every string literal beginning with `/` inside a
  `do_*` method — and requires each to be exempt-by-list or to refuse a stranger over a real
  socket. A route added tomorrow is in that test tomorrow, and a hard-coded list would have gone
  stale on precisely the day it was needed.
- The check costs 1.8 µs on the board's path and 14.9 µs on a phone's, against a `/api/state`
  that answers in ~800 µs. A stream pays it once, at open.
- 95 new tests; 40 mutations applied and all 40 caught.

## Alternatives rejected

| option | why |
|---|---|
| **One shared token** | cannot be revoked per device, and the audit log could only ever say "somebody" |
| **Token in a query parameter or cookie** | a cookie reintroduces CSRF as a first-class feature; a query parameter leaks into every log, shell history and `Referer`. API.md §2.1 already forbids both |
| **Store the token in the clear** | then the registry file *is* the credential, and every backup of this machine is a copy of it |
| **Loopback must authenticate too** | it would break the existing browser today for a wall that is not there (an attacker who can reach `127.0.0.1` can read the transcripts directly). Available as `auth_trust_loopback: false` for the day a proxy fronts the server, which is the only case where it earns its cost |
| **A decorator per route** | the check must be impossible to forget; a decorator is exactly the thing that gets forgotten |
| **Scopes now** | see above — deferred whole, not half-built |
