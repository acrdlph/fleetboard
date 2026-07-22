# orchestra HTTP API — v1

**Status:** specification. This document is the contract. Where it disagrees with
the `orchestra/` package at HEAD, the code is wrong and must be changed; where it
disagrees with an earlier design note, this document wins.

**Audience:** the Swift engineer writing the iOS client, and the Python engineer
implementing the server. Both should be able to work from this file alone.

**Scope:** everything under `/api/v1/`. The pre-existing unversioned paths (`/api/state`,
`/api/send`, …) are frozen and documented in §16 for reference and migration only.

---

## 0. Reconciliation notes (read once)

Three design tracks produced overlapping proposals. This document unifies them. If you
have read those notes, here is what changed:

| Concept | Track proposals | **Canonical here** |
|---|---|---|
| Version prefix | `/api/v1/…`, `/api/v1/…` | **`/api/v1/…`** everywhere |
| Idempotency header | `Idempotency-Key`, `X-Orchestra-Op-Id`, body `op_id` | **`Idempotency-Key`** header (+ `Idempotency-Issued-At`) |
| Cursor | `?since=<int>&epoch=<hex>`, `"<epoch>:<seq>"` | **`?since=<epoch>:<seq>`**, one opaque token |
| Delta encoding | entity upsert/remove lists, field-addressed ops | **field-addressed ops** (`{p,f,v,x}`), one format for SSE *and* long-poll |
| Async work | `job-HHMMSS-N`, `op_…` | **`op_…`**, at `/api/v1/ops/{op_id}`, mirrored into the delta address space as `j/<op_id>` |
| Push registration | `/api/push/register`, `/api/v1/devices` | **`/api/v1/devices/self/push`** (a paired device registers its own push endpoint) |
| Event feed | `/api/events` | **`/api/v1/events`** |

Everything else is additive.

### 0.1 Alias table for the sibling documents

`UX.md`, `ARCHITECTURE.md` and `ROADMAP.md` were written against earlier drafts and still
use older spellings. They are **aliases, not alternatives** — this column is the contract.

| Written elsewhere as | Canonical here |
|---|---|
| `/api/hello`, `/api/health` | **`GET /api/v1/health`** (unauthenticated liveness + skew) and **`GET /api/v1/meta`** (`read`; features, budgets, device). The `config{}` block UX §3.1.5/§3.5 needs lives in `meta`. |
| `capabilities[]`, `caps[]` | **`features[]`** (§9.1, §9.2) |
| `/api/events`, `/api/v1/stream` | **`GET /api/v1/stream`** (SSE) |
| `/api/v1/state`, `/api/state` | **`GET /api/v1/state`** |
| `/api/v1/map` | **`GET /api/v1/topology`** |
| `POST /api/send {pid,text}` | **`POST /api/v1/sessions/{sid}/messages`** (identity-addressed; `agents/{ag_id}/messages` for the deliberate cross-agent case) |
| `POST /api/kill {session}` | **`POST /api/v1/agents/{ag_id}/kill`** |
| `POST /api/finish {worktree}` | **`POST /api/v1/worktrees/{wid}/finish`** |
| `POST /api/dispatch` | **`POST /api/v1/dispatches`** |
| `POST /api/reserve` | **`PUT /api/v1/accounts/{label}/reserve`** |
| `/api/resume/schedule`, `/api/resume/cancel` | **`PUT` / `DELETE /api/v1/sessions/{sid}/resume`** |
| `/api/devices`, `/api/push/register`, `/api/activities` | **`POST /api/v1/devices/self/push`** and §9.23–9.24 |
| `job-HHMMSS-N`, `intent_id`, `/api/intents/{key}` | **`op_…`** at **`/api/v1/ops/{op_id}`** |
| `active_at`, `evidence_at`, `last_write_at`, `age_s` | **`activity_at`** (absolute epoch float) |
| `sid` | **`session.id`**; `wid` is `wt_<12 hex>`, never the worktree name |
| `transitions[]` riding on a state frame | **`GET /api/v1/events`** — a durable, replayable log, not an ephemeral array |
| `topology.unmapped`, `topology_skipped` | **`topology.dropped[]`** with `reason` |
| `fork_ts` / `tip_ts` / `trunk_ts` | **`fork_at` / `tip_at` / `trunk_at`** |
| `session_count` | **`sessions_total` / `sessions_shown`** |
| `pid_certain` | **`agent_certain`** |

### 0.2 Assumed elsewhere, **not yet defined here**

These are open gaps, not aliases. Each is a claim another document makes about the wire that
this contract does not currently honour. Either this document grows the field or that
document loses the feature — do not implement against the assumption.

| Assumed by | Field / behaviour | Status |
|---|---|---|
| `UX.md` §3.1.2, §9.4, §9.6 | five-valued `availability` (`needs_you`, `your_turn`, `busy`, `limited`, `free`) driving the board's sections and badges | **§10.2 ships the legacy four** (`free`, `attention`, `waiting`, `busy`). `attention` conflates `needs_input`/`blocked` with `waiting`, which is the exact defect UX §3.1.2 exists to fix. **Adopt the five values or UX §3.1 does not build.** |
| `UX.md` §3.1.4, §3.10 | `confidence`, `why`, `evidence_source`, `provisional`, `liveness` per session | not defined. `status: "unknown"` (§10.1) covers only the `liveness` case. |
| `UX.md` §3.3.1 | `terminal.attribution` ∈ `certain`\|`ambiguous`\|`guess`\|`none` + `why` | not defined. §9.3 ships two-valued `agent_certain`, which UX argues is insufficient because two agents on one account in one worktree both report `true`. |
| `UX.md` §3.1.4 | `topic`, `last_user`, `last_assistant`, `subagent_said` on the **board** payload | §9.3 ships only `headline` (80 chars). The four fields are detail-endpoint only. **UX's session-row anatomy needs either the fields or a redesign to one line.** |
| `UX.md` §5.3, §5.7, §5.8 | `axis.s`, `axis.anchor_age_s`, `commits_capped`, `commits_oldest_ts`, `base_ts`, `role` | not defined in §9.7. `role` is derivable client-side from `ahead`/`behind`; the other four are not. |
| `UX.md` §4.7, §7.2 | `POST /api/pasteboard` (write the attach command to the Mac's clipboard) | not defined anywhere. |
| `UX.md` §8.5 | `GET /api/notify/body` for NSE enrichment | the NSE enriches from **`GET /api/v1/events/{id}`** (§9.22). |

---

## 1. Transport, encoding, and HTTP semantics

### 1.1 Base URL

```
https://<magicdns-name>:4242            e.g. https://achills-macbook-pro.tail1205d9.ts.net:4242
https://<tailnet-ipv4>:4242             e.g. https://100.113.110.31:4242
https://[<tailnet-ipv6>]:4242           e.g. https://[fd7a:115c:a1e0::b03a:6e20]:4242
http://127.0.0.1:4242                   loopback only, plaintext, browser board only
```

- The tailnet listeners are **HTTPS with a self-signed P-256 certificate**. The client
  pins `base64url(sha256(SPKI-DER))[:16]`, delivered out-of-band by the pairing QR. See §3.6.
- The loopback listener is **plaintext HTTP** and serves the desktop HTML board. It never
  serves HTML on the tailnet listeners.
- `0.0.0.0` is refused at startup. The server binds loopback unconditionally plus, when
  configured, exactly the tailnet IPv4 and IPv6 addresses.

### 1.2 HTTP version and connection reuse

- `HTTP/1.1` with keep-alive. The server sets an idle socket timeout of 30 s and closes
  idle connections; clients must tolerate a mid-idle close and reconnect.
- Any response with status ≥ 400 carries `Connection: close`. The server drains any
  declared request body before responding so a pooled connection is never desynchronised.
- `Transfer-Encoding: chunked` request bodies are **rejected with `411 Length Required`**.
  On iOS this means: always set `URLRequest.httpBody`. Never `httpBodyStream`, never
  `uploadTask(withStreamedRequest:)` — both force chunked and will 411.
- Request bodies are capped at **1 MiB**. A negative or non-integer `Content-Length` is
  `400 bad_content_length`; an oversized one is `413 body_too_large`.

### 1.3 Content types

- Request bodies on `POST`/`PUT`: `Content-Type: application/json; charset=utf-8`, **required**.
  Anything else is `403 content_type_required`. (This is the CSRF control — a browser
  cannot send `application/json` cross-origin without a preflight, and the preflight is
  refused.)
- Responses: `application/json; charset=utf-8`, except `GET /api/v1/stream`
  (`text/event-stream; charset=utf-8`).
- All JSON is UTF-8. The server does **not** escape non-ASCII (`ensure_ascii=False`);
  bodies contain glyphs like `▲ ■ ◆ ⛔ ⏱ ✓ ✗ ⌁ ◇ ●` and `·`.

### 1.4 Compression

- The server gzips any JSON response whose uncompressed body exceeds 1024 bytes when the
  request carries `Accept-Encoding: gzip`. Gzipped responses set `Content-Encoding: gzip`
  and `Vary: Accept-Encoding`.
- **`text/event-stream` is never gzipped**, regardless of `Accept-Encoding`. URLSession
  adds `Accept-Encoding` automatically; the server must exclude the stream unconditionally
  or incremental delivery breaks.

### 1.5 Methods

`GET`, `HEAD`, `POST`, `PUT`, `DELETE` are implemented. `OPTIONS` returns
`405 Method Not Allowed` with `Allow: GET, HEAD, POST, PUT, DELETE` and **no CORS headers**
— this is deliberate and is what blocks browser-origin writes.

`HEAD` is supported on every `GET` route and returns identical headers with no body.
`HEAD /api/v1/health` is the cheapest liveness probe and is the recommended tunnel-warmer.

### 1.6 Response headers present on every `/api/v1/` response

| Header | Meaning |
|---|---|
| `Orchestra-Api` | `1.0` — the API contract version |
| `Orchestra-Request-Id` | `req_<8 hex>`, echoed in the error envelope, printed beside any server traceback |
| `Cache-Control` | `no-store` |
| `Orchestra-Epoch` | current epoch (see §6), on state-bearing responses |
| `Orchestra-Seq` | current sequence number, on state-bearing responses |
| `ETag` | weak validator, on cacheable reads |
| `Deprecation` / `Link` | on legacy `/api/*` responses only (§16) |

### 1.7 Rate limits

Token buckets, evaluated **before** authentication for the per-IP bucket and after it for
the per-device buckets.

| Bucket | Sustained | Burst | Applies to |
|---|---|---|---|
| `ip` | 60 / 60 s | 30 | every request from one source IP, including unauthenticated ones |
| `read` | 400 / 60 s | 150 | any `read`-scope route, per device. The loopback board is exempt. |
| `act` | 30 / 60 s | 10 | any `act`- or `admin`-scope route, per device |
| `refresh` | 4 / 3600 s | 2 | `POST /api/v1/limits/refresh` |
| `dispatch_hour` | 15 / 3600 s | — | global, derived from `dispatch.log.jsonl`, survives restart |
| `dispatch_day` | 60 / 86400 s | — | global, same source |

Exceeding any bucket returns `429 rate_limited` with a `Retry-After` header (seconds) and
`error.detail.retry_after_s`. Long-poll and SSE connections consume one `read` token at
open, not per frame.

Current consumption is exposed in `GET /api/v1/meta` and in the `srv` delta entity, so a
client can grey out a launch button rather than spend a biometric prompt on a request that
is guaranteed to 429.

---

## 2. Authentication

> **As built, 2026-07-22 ([ADR 0014](adr/0014-per-device-bearer-tokens.md), `orchestra/auth.py`).**
> This section describes `/api/v1`, which does not exist yet. What ships today, on the legacy
> unversioned surface, is the **subset that is not optional**: §2.1's token format verbatim
> (`orc1_<devid>_<secret>`, stored as sha256, `hmac.compare_digest`); one check in
> `Handler.parse_request` that no route can be dispatched past; **loopback trusted** and
> everything else authenticated; §2.4's exempt list reduced to `GET /api/health` alone (there is
> no `POST /api/pair` yet, so there is no second open door); the `Origin` rule of §2.3 step 3
> plus a `Content-Type: application/json` requirement on mutations, which together close CSRF
> from the local browser; a per-IP failure budget in place of §1.7's full bucket table; and an
> audit log of every mutation and every refusal.
>
> **Not built:** scopes (§2.2 — one token grants everything, and a `read` token that cannot act
> is not the product yet), the `Host` allowlist (§2.3 step 2), tailnet whois (step 8), lockdown
> (step 7), idempotency (step 10), per-device buckets. Each is listed with its reason in ADR
> 0014; none of them was the missing one.

> **Updated 2026-07-22 — pairing and the device routes SHIP** ([ADR 0015](adr/0015-pairing-and-the-tailnet-bind.md),
> `orchestra/pairing.py`, `orchestra/qr.py`, `orchestra/tailnet.py`). `/api/v1` now exists, and
> these five routes are what is on it. §3 below is rewritten to match what was built; §3.5 and
> §3.6 (certificate pinning, ATS) are struck through, because ADR 0013 removed TLS from this
> server and there is no certificate to pin.
>
> The exempt list is now **two** routes — `GET /api/health` and `POST /api/v1/pair` — and
> "exempt" means *no token required*, not *no checks*: the `Origin` and `Content-Type` guards
> still run on both.
>
> `admin` in §2.5's scope map is implemented **without scopes**, as a stronger rule that needs
> none: **device management answers to this machine holding no token.** A valid device token
> presented to any route under `/api/v1/devices` is `403 admin_local_only` — see §2.5a.

### 2.1 Token format

```
orc1_<devid>_<secret>
      ^8 hex  ^43 chars (secrets.token_urlsafe(32) = 256 bits)
```

Example: `orc1_9f3ab21c_kQ7bN2xR4vLpZ8mT1wY6cH0jF5sA3dG9eU2iO7qK1nM`

- `devid` is public and is the audit-log identifier.
- The server stores only `sha256(full_token)`; a leaked registry grants nothing.
- Presented **only** as `Authorization: Bearer orc1_…`. Never a query parameter, never a
  cookie (both would reintroduce CSRF and leak into logs).
- Non-ASCII in the `Authorization` value is `401`, not a 500.

### 2.2 Scopes

Every device is issued **two** tokens at pairing: one `read`, one `act`. Scopes are ordered
`admin ⊃ act ⊃ read`.

| Scope | Grants | iOS storage |
|---|---|---|
| `read` | all reads, the SSE stream, self-service device endpoints | Keychain, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, access group `com.acrdlph.orchestra.shared` (the notification-service extension needs it while the device is locked), **no biometric ACL** |
| `act` | everything that types at, launches, or kills an agent | Keychain, `kSecAttrAccessibleWhenUnlockedThisDeviceOnly` + `SecAccessControl([.biometryCurrentSet, .or, .devicePasscode])`, **not** in the access group |
| `admin` | device management, reserve edits, config-adjacent writes | **the desktop board only. Phones are never issued `admin`** — a stolen phone must not be able to revoke the Mac's ability to revoke it. |

The `read` token must work with no user present (background refresh, notification
enrichment), which is exactly why it cannot also act. The `.or .devicePasscode` fallback on
the `act` ACL is mandatory: a biometry-only ACL fails `SecItemAdd` outright on a device
with no enrolled biometry, blocking pairing entirely.

Presenting a `read` token to an `act` route is `403 scope_insufficient` with
`error.detail.have`/`error.detail.need`.

### 2.3 The request guard, in order

Every `/api/v1/` request passes these checks in this exact order. The first failure wins.

1. **Per-IP rate bucket** — before authentication, before any audit write. `429`.
2. **`Host` allowlist** — the `Host` header (lowercased, with port) must be one of the
   bound addresses, `localhost`, `127.0.0.1`, `[::1]`, or a configured MagicDNS name.
   Otherwise `403 host_not_allowed`. This kills DNS rebinding and also blocks
   `tailscale serve`/`funnel` fronting, deliberately (see §2.7).
3. **`Origin` allowlist** — if `Origin` is present it must be one of the server's own
   origins, else `403 origin_not_allowed`. `Sec-Fetch-Site: cross-site|same-site` is also
   `403`.
4. **Peer range** — the tailnet listeners accept only `100.64.0.0/10` and
   `fd7a:115c:a1e0::/48`; the loopback listener accepts only `127.0.0.1`/`::1`.
   Otherwise `403 peer_not_permitted`.
5. **Route resolution** — exact `(METHOD, path-without-query)` match. There is **no prefix
   routing**. An unmatched path is `404 route_not_found`.
6. **Token** — unless the route's scope is `null`. `401 unauthorized` on absent, malformed,
   unknown or revoked. Scope ladder → `403 scope_insufficient`.
7. **Lockdown** — while lockdown is active (§9.5), only `GET /api/v1/health` and
   `GET /api/v1/state` are permitted for paired devices; everything else is
   `403 lockdown_active` carrying `detail.lockdown_until`.
8. **Tailnet identity** — on the tailnet listeners, `tailscale whois <peer-ip>` must yield
   a `LoginName` in the configured allowlist (which defaults to the login captured at first
   pairing). `403 identity_not_permitted`. A `whois` failure is not fatal; it falls back to
   token-only and records `"whois": null` in the audit log.
9. **Per-device rate bucket** — `429`.
10. **Idempotency** (mutations only, §4).
11. **Preconditions** (`expect`, §5).

### 2.4 Routes that require no token

| Route | Why |
|---|---|
| `GET /api/v1/health` | version-skew diagnosis must work before you have a token |
| `POST /api/v1/pair` | it is how you get a token; guarded by the pairing code, per-IP attempt limits and a 120 s window |

Everything else requires a token, including `GET /api/v1/meta`.

**Exempt means no TOKEN is required. It does not mean no checks.** Both of these still face the
`Origin` rule and, for the mutation, the `Content-Type: application/json` requirement of §2.3
step 3 — without which a page you are merely visiting could post a pairing claim from your
browser with no preflight to stop it. As built the list is exactly
`{("GET", "/api/health"), ("POST", "/api/v1/pair")}` and it is pinned whole by a test, so
growing it is a deliberate edit rather than a drift.

### 2.5 Scope map (complete)

| Method + path | Scope |
|---|---|
| `GET /api/v1/health` | — |
| `POST /api/v1/pair` | — |
| `GET /api/v1/meta` | read |
| `GET /api/v1/state` | read |
| `GET /api/v1/stream` | read |
| `GET /api/v1/worktrees/{wid}` | read |
| `GET /api/v1/agents` | read |
| `GET /api/v1/topology` | read |
| `GET /api/v1/limits` | read |
| `GET /api/v1/sessions/{sid}/messages` | read |
| `GET /api/v1/dispatches` | read |
| `GET /api/v1/ops`, `GET /api/v1/ops/{op_id}` | read |
| `GET /api/v1/events`, `/api/v1/events/{id}`, `/api/v1/events/open` | read |
| `POST /api/v1/devices/self/push` | read |
| `POST /api/v1/devices/self/settings` | read |
| `POST /api/v1/devices/self/reissue-act` | read |
| `POST /api/v1/push/mute`, `POST /api/v1/push/snooze` | read |
| `POST /api/v1/push/test` | read |
| `POST /api/v1/sessions/{sid}/messages` | **act** |
| `POST /api/v1/agents/{ag_id}/messages` | **act** |
| `POST /api/v1/agents/{ag_id}/kill` | **act** |
| `POST /api/v1/agents/{ag_id}/focus` | **act** |
| `POST /api/v1/dispatches` | **act** |
| `POST /api/v1/worktrees/{wid}/finish` | **act** |
| `PUT /api/v1/sessions/{sid}/resume` | **act** |
| `DELETE /api/v1/sessions/{sid}/resume` | **act** |
| `POST /api/v1/limits/refresh` | **act** |
| `POST /api/v1/ops/{op_id}/cancel` | **act** |
| `PUT /api/v1/accounts/{label}/reserve` | **admin** |
| `GET /api/v1/devices` | **admin** |
| `POST /api/v1/devices/pair/open` | **admin** |
| `POST /api/v1/devices/{id}/revoke` | **admin** |
| `POST /api/v1/devices/{id}/approve-act` | **admin** |
| `POST /api/v1/devices/lockdown`, `/unlock` | **admin** |

### 2.5a `admin` without scopes — the local-only rule

Scopes (§2.2) are still not built. Rather than invent half of the ladder for one route, the
`admin` rows of §2.5 are implemented as a rule that needs no ladder at all and is strictly
stronger than the one they describe:

> **Device management answers to this machine, holding no token.**

| caller | `/api/v1/devices…` |
|---|---|
| loopback, no `Authorization` header (the board) | allowed |
| tailnet, no token | `401 unauthorized` — authentication is decided first, so a stranger learns nothing about which routes are special |
| tailnet, **valid** token | `403 admin_local_only` |
| loopback, **valid** token | `403 admin_local_only` — the rule is the absence of a token, not the address, so a proxy that makes every peer loopback (§2.7) does not open it |

A `403 admin_local_only` does **not** spend from the per-IP failure budget: the caller is a
known device making a legitimate mistake, and charging it would let a buggy app lock its own
phone out of the API it is entitled to.

Matched by **prefix** on `/api/v1/devices`, at a path-segment boundary — `/api/v1/devices`,
`/api/v1/devices/…`, but not `/api/v1/devices-of-others`. The asymmetry against §2.4's exact
matching is one-directional and in the safe direction: a path this list matches too eagerly is
a refusal, never a hole. `POST /api/v1/pair` is deliberately **not** under this prefix.

When scopes do arrive, this becomes `scope == admin` and nothing else about the surface changes.

### 2.5b The bind, and the two listeners

`--tailnet` detects this machine's Tailscale address rather than asking for it, and **refuses to
start** if Tailscale is not up, saying which of the three situations it is (not installed / not
up / will not bind). `--host <addr>` still takes an explicit address. `0.0.0.0` is refused
through `--host` whatever the registry says; the escape hatch is the differently-named
`--bind-every-interface`, which cannot be set from the config file. **No bind beyond loopback
succeeds with no device registered**, including the wide one.

A non-loopback bind starts a **second listener on `127.0.0.1`**, and this is load-bearing rather
than convenient. A server bound only to `100.113.110.31` is not listening on loopback, so the
board's own pages are refused — and a request the Mac sends to its own tailnet address arrives
with a source address of `100.113.110.31`, which is not loopback and never can be, so §2.5a
would lock the person at the keyboard out of device management entirely. The tailnet listener
carries the phone; the loopback listener carries the board.

### 2.6 What the tailnet does and does not protect

Stated plainly so nobody over-trusts it:

- **Closed:** the public internet; passive interception (WireGuard); other people's devices
  and `tailscale share`d nodes (login allowlist + per-device tokens); browser CSRF and DNS
  rebinding (`Content-Type` + `Origin` + `Host`); a lost phone (revoke, lockdown, 30-day
  dormancy auto-revoke).
- **Not closed:** another process running as *you* on the Mac (unclosable — it can read
  anything you can read); the owner's own other Macs on the tailnet (they present the same
  `LoginName`; their only barrier is not holding a token); a rooted phone defeating
  biometry (client-side biometry is advisory — the controls that hold are server-side:
  scopes, rate limits, audit, revoke).
- **Half closed:** a stolen *unlocked* phone. Acting is gated behind biometry; reading is
  not, by design, so such a phone can read transcripts until you revoke it or trigger
  lockdown.

### 2.7 `tailscale serve` is blocked on purpose

A `tailscale serve`-proxied request arrives with `Host: <node>.ts.net` (no port) and from
loopback. That `Host` is not in the allowlist, so it is `403`. If you deliberately want to
front orchestra with `serve`, you must add the host to `auth.extra_hosts` **and** switch the
board to nonce authentication — otherwise you publish the desktop `admin` token to every
tailnet node. Do not do half of it.

---

## 3. Pairing

> **Built, 2026-07-22** — `orchestra/pairing.py`, `orchestra/qr.py`. This section was written
> against the TLS design that [ADR 0013](adr/0013-plain-http-over-the-tailnet.md) replaced, and
> has been rewritten to describe what ships. The differences from the original draft, so that
> anyone holding the old version knows what moved:
>
> | was | is | why |
> |---|---|---|
> | `f=<spki pin>` in the QR | **gone** | there is no TLS and therefore no certificate to pin (ADR 0013). A pin field would be a field the client must ignore |
> | two tokens per device (`read`, `act`) | **one** `token` | scopes are deferred whole (§2.2, ADR 0014). Returning the same string twice under two names would be a lie about what exists |
> | `server.spki`, `server.cert_not_after` | **absent**, plus `server.tls: false` | sending them as nulls invites a client to pin against nothing |
> | §3.5 certificate pinning, §3.6 ATS | **struck through** below | same reason |

### 3.1 Flow

```
Mac — the board at http://127.0.0.1:4242/pair            iPhone
──────────────────────────────────────────────           ──────
1. open /pair (loopback only — §2.5a)
2. POST /api/v1/devices/pair/open
   → 8-char Crockford code, 120 s, single use
   → QR as SVG, rendered from the same string
3. the QR is on screen                                   4. camera scans it
                                                         5. POST /api/v1/pair {code,label,platform}
6. peer range → window → attempts → code
7. mints ONE token, writes the registry,
   closes the window
                                                         8. stores the token in the Keychain
9. the page sees `pairing.claimed` and says so           10. the token authenticates every route
```

**Bootstrapping the first device is different**, and the docs should not pretend otherwise. The
tailnet bind refuses to start with no device registered (§2.5b), and pairing needs the phone to
be able to reach the server — so the *first* device is minted at a shell with
`python3 -m orchestra --add-device <label>` and its token carried by hand. Every device after
that pairs with the QR. Closing that gap needs a bootstrap mode that binds the tailnet with
nobody registered, which is exactly the silent wide exposure ADR 0013 forbids; it is listed as
an open item rather than done badly.

### 3.2 `POST /api/v1/devices/pair/open`

**Auth:** local-only (§2.5a). **Idempotency:** not required; each call replaces any open window,
so a user who clicks twice has exactly one live code and it is the one on the screen.

Request body: `{}` or omitted. `Content-Type: application/json` is **required** (§2.3 step 3).

Response `200`:

```json
{
  "ok": true,
  "code": "N1T3-8XY5",
  "url": "orc://p?h=100.113.110.31&p=4299&c=N1T38XY5",
  "svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 37 37\" …></svg>",
  "qr_version": 3,
  "expires_at": 1784741006.05,
  "expires_in": 120.0,
  "manual": { "host": "100.113.110.31", "port": 4299, "code": "N1T3-8XY5" }
}
```

- `svg` is generated from **the same string** as `url`, by one function, so the picture and the
  manual fields cannot disagree.
- `h` is where the **phone** should connect, which is not necessarily what the server bound: a
  loopback-bound board advertises the detected tailnet address, because a QR saying `127.0.0.1`
  sends the phone to its own web server.
- `p` is omitted from `url` when the port is 4242.
- The pairing **window lives in memory** and dies with the process. A restart closes every open
  window, which is correct: a door that reopens by itself is a door nobody closed.

QR payload grammar:

```
orc://p?h=<host>[&p=<port>]&c=<8-char code>
```

Register `orc://` in `CFBundleURLTypes` so the system camera can launch the app directly.

The encoder is `orchestra/qr.py` — stdlib only, byte mode, EC level **M**, versions 1–10, and it
**raises** rather than truncating a payload that will not fit. It is verified against Apple's
`CIQRCodeGenerator` module for module and decoded back by Vision (`tests/qr_ref.py`).

### 3.3 `POST /api/v1/pair`

**Auth:** none — this is the bootstrap, and it is the second and last entry in §2.4's exempt
list. Exempt means *no token required*; the `Origin` and `Content-Type` guards still apply.
**Idempotency:** not applicable — the code is single-use, so a replay is `409 pairing_not_open`.

Request:

```json
{
  "code": "n1t3-8xy5",
  "label": "Achill's iPhone",
  "platform": "ios",
  "app_version": "1.0 (14)"
}
```

- `code` is normalised on both sides before comparison: strip whitespace, `-` and `_`,
  uppercase, then Crockford-fold `I`→`1`, `L`→`1`, `O`→`0`, `U`→`V`. `n1t3-8xy5`, `N1T3 8XY5`
  and `N1T38XY5` all match, and so does a `1` a user read off the screen and typed as `I`.
  Comparison is `hmac.compare_digest` over SHA-256, never `==`.
- `label` is truncated to 40 characters, `platform` to 16. A device that sends no label still
  gets one (`"ios (100.64.0.9)"`), because an empty row in the device list is a row you cannot
  act on.
- `app_version` is accepted and currently ignored.

Response `200` — **the only time a token is ever returned**:

```json
{
  "ok": true,
  "device_id": "33ba5d99",
  "label": "Achill's iPhone",
  "token": "orc1_33ba5d99_vpV-s_r-_THb68qjJbDrj6b5i_VK4rSUwDRC2ScMEas",
  "server": {
    "host": "100.113.110.31",
    "port": 4299,
    "hostname": "MacBookPro",
    "api": "1",
    "tls": false
  }
}
```

Note the secret half of the token contains `_`. Split it with **maxsplit=2** or roughly half of
all valid tokens will be rejected by your client.

Errors:

| Status | `error` | Cause |
|---|---|---|
| 403 | `peer_not_permitted` | the claimant is neither on loopback nor in `100.64.0.0/10`. Checked **before** the code, so a stranger cannot tell a wrong code from a closed window and cannot spend the real phone's attempts |
| 409 | `pairing_not_open` | no window open, or it expired, or the code was already claimed — **one answer for all three**, because they are the same fact from the claimant's side and distinguishing them would say whether somebody else just paired |
| 409 | `pairing_code_wrong` | code mismatch; the per-IP attempt counter and the server-wide failure budget were both incremented |
| 429 | `pairing_attempts` | more than 5 attempts from this source IP within the window |
| 429 | `pairing_locked` | 25 attempts in aggregate; the window is closed and locked for 600 s |
| 400 | `pairing_bad_request` | the body was not a JSON object |
| 415 | `content_type_required` | the mutation did not announce `application/json` (§2.3) |

Attempt counting is **per source IP**, so one hostile peer cannot lock the legitimate device out.
The aggregate cap is what closes the window against many peers at once.

### 3.4 Manual fallback — compare, do not type

If the camera fails, the user types **host + port + the 8-char code**. All three are short, the
code is case-insensitive, and the alphabet is Crockford base32 — `I`, `L`, `O` and `U` do not
occur, which is the whole reason this path is usable.

There is no pin to compare (ADR 0013). Be honest in the UI: on this transport, both the QR and
the manual path rest on **being inside the tailnet**, and the token is what identifies the
device afterwards.

### 3.5 ~~Certificate pinning (client)~~ — **does not apply**

~~SPKI pinning, `p256SPKIPrefix`, `SecTrustEvaluateWithError`.~~ ADR 0013 removed TLS from this
server: there is no certificate, no SPKI, and nothing to pin. The original text is kept in git
history rather than here, because a client written from a struck-through section is a client
that fails closed against a certificate that will never be presented.

### 3.6 ~~ATS~~ — **plain HTTP, scoped exception**

ADR 0013: the app carries an `NSExceptionDomains` entry for the tailnet address, and is personal
(TestFlight / sideload), never App Store. `NSAllowsLocalNetworking` does **not** help —
Tailscale's `100.64.0.0/10` is RFC 6598 CGNAT, not RFC 1918.

`NSLocalNetworkUsageDescription`: **verify on device before adding it.** Traffic over the
Tailscale `utun` interface is generally not subject to the local-network gate, and adding the
key spends a scary permission prompt for nothing.

### 3.7 Device management

All three are local-only (§2.5a) and all three are written to the audit log — including the
`GET`, because it is the inventory of every credential to this machine and, unlike `/api/state`,
nothing polls it.

#### `GET /api/v1/devices`

```json
{
  "ok": true,
  "devices": [
    { "id": "33ba5d99", "label": "Achill's iPhone",
      "created": 1784741006.05, "last_seen": 1784741014.63, "revoked": null }
  ],
  "pairing": { "open": false, "claimed": "33ba5d99", "claimed_at": 1784741006.05 }
}
```

Newest first. The token hash is never included. `pairing` is one of
`{"open": false}` · `{"open": true, "expires_at", "expires_in", "attempts"}` ·
`{"open": false, "expired": true}` · `{"open": false, "claimed", "claimed_at"}` — and **never
carries the code or its hash in any of those shapes**, since the page polls it.

`last_seen` is written at most once a minute per device, so it is accurate to the minute and
costs one write a minute per active device rather than one per request.

#### `POST /api/v1/devices/{id}/revoke`

`200 {"ok": true, "device": {…, "revoked": 1784741023.86}}`, or `404 device_unknown`.

The path is **parsed exactly**: `/api/v1/devices/<id>` with no verb, or with any other verb, is
a `404` that changes nothing. Revocation is immediate — a running server sees a revocation made
in another process within one `stat`, because the registry memo is keyed on the file's
`(mtime_ns, size, ino)` — and it is irreversible. Pairing again mints a new token.

#### `POST /api/v1/devices/pair/close`

`200 {"ok": true, "pairing": {"open": false}}`. Shuts an open window early; idempotent.

---

## 4. Idempotency

### 4.1 The header

Every mutating request (`POST`, `PUT`, `DELETE` under `/api/v1/`) **must** carry:

```
Idempotency-Key: 8b1e5f2a-3c47-4d19-9e02-71ac5f0b2d38
Idempotency-Issued-At: 1784636700.12
```

- `Idempotency-Key` is a client-generated UUID, minted **once at the moment the user
  commits** (the second tap of a two-step confirm, the launch button, the notification
  action), and reused across every retry of that same intent. Never regenerate on retry.
- `Idempotency-Issued-At` is the client's wall clock, **skew-corrected** against the
  server's `at` (§6.5), as a float epoch. It bounds how long a request may sit in a
  background queue before the server refuses it.
- A missing key is `400 idempotency_key_required`. A missing `Idempotency-Issued-At` is
  permitted but the server then cannot expire the request; clients must send it.

### 4.2 Why this is not optional

`POST /api/v1/dispatches` and the `dispatch` mode of `POST /api/v1/worktrees/{wid}/finish`
launch a tmux session whose name embeds `%H%M%S`, so any retry ≥ 1 second later launches a
**second** agent in the same worktree — two agents merging and pushing the same branch, two
accounts burned. Compounding it, the state snapshot is up to 12 s old and a new agent takes
~30 s to register as busy, so a fast retry's auto-picker re-selects the same "free"
worktree.

A background `URLSession` defaults `timeoutIntervalForResource` to **seven days** and
retries across reboots. A dispatch handed to the background daemon during a tailnet outage
can land 40 minutes later. Hence server-side expiry, not just client-side care.

### 4.3 Fingerprint

```
fingerprint = sha1(METHOD "\n" PATH "\n" canonical_json(body minus "expect"))
```

`expect` — and only `expect` — is stripped. It is an assertion about the client's *view*,
which legitimately advances between the original request and its retry. Everything that
changes what the server does is fingerprinted: `mission`, `worktree_id`, `account`,
`model`, `effort`, `force_model`, `text`, `verify`, `step`, `delay_s`, `due_at`, `percent`,
`reason`.

Corollary: re-POSTing a dispatch with `force_model: true` changes the fingerprint and
therefore **requires a new `Idempotency-Key`**. Reusing the old one correctly returns
`422 idempotency_key_reused`.

### 4.4 Write-ahead reservation

The reservation is persisted to disk **before** any side effect, tagged with the server's
`boot_id`. There is no "abandon" path: a handler exception records a terminal error under
the key, so a retry replays the failure verbatim rather than re-executing.

### 4.5 Replay matrix (exact)

| Situation | Response | Headers |
|---|---|---|
| key unseen, fingerprint new | execute normally | `Idempotent-Replay: false` |
| key `in_flight`, **same boot**, same fingerprint | `409 operation_in_flight`, `detail.op_id`, `retriable: true`, `Retry-After: 1`. **Never blocks.** | — |
| key `in_flight`, **different boot** | `409 operation_indeterminate`, `detail.op_id`, `retriable: false`. Message: *"the server restarted while this was running — check the fleet before retrying; a mission may already be live"*. **Never re-executes.** | — |
| key `done`, same fingerprint | the stored status and the stored body, **byte-identical** | `Idempotent-Replay: true` |
| key `done`, different fingerprint | `422 idempotency_key_reused` | — |
| `Idempotency-Issued-At` older than 900 s | `409 operation_expired`, `retriable: false` | — |
| key older than 24 h | treated as unseen | `Idempotent-Replay: false` |

**A stored `done` replay short-circuits before `expect` is evaluated.** It must, since the
stored body is returned unchanged.

### 4.6 Resource locks (the second guard)

Idempotency stops a *retry*. It does not stop a double-tap producing two distinct keys, nor
a phone and a browser acting at once.

| Operation | Lock | Held for |
|---|---|---|
| `dispatch` | `worktree:<wid>` — **resolved synchronously in the accept path, before the 202** | the op's lifetime |
| `finish` (all modes, including the headless `dispatch` mode) | `worktree:<wid>` | the op's lifetime |
| `chat_send`, `kill` | `agent:<ag_id>` | the op's lifetime |
| any tmux buffer paste | a global buffer lock | the set-buffer/paste-buffer pair |

Locks are **non-blocking**: a second actor gets `409 worktree_busy` (or `409 agent_busy`)
with `detail.blocking_op_id`, never a queue. A queued finish that fires 40 s later would
type a closeout brief at an agent that has moved on.

Auto-pick dispatch (`worktree_id: null`) resolves its target under a global pick lock and
subtracts already-reserved worktrees from the free list, so two concurrent auto-dispatches
can never select the same worktree.

---

## 5. Preconditions — the `expect` object

### 5.1 Where it is mandatory

`expect` is **required** (`400 expect_required` without it) on every route that types at or
kills an agent:

- `POST /api/v1/sessions/{sid}/messages`
- `POST /api/v1/agents/{ag_id}/messages`
- `POST /api/v1/agents/{ag_id}/kill`
- `POST /api/v1/worktrees/{wid}/finish`

It is **optional** on `POST /api/v1/dispatches` (which does not address an existing agent)
and on `PUT /api/v1/sessions/{sid}/resume`.

### 5.2 Shape

```json
"expect": {
  "cursor": "9f2c1a04:4711",
  "agent_id": "ag_7c21f0a9b3de",
  "pid": 41234,
  "card_rev": "7b21e4de",
  "closeout_sent_at": null
}
```

### 5.3 Evaluation order and meaning

1. `agent_id` absent from the current snapshot → **`409 agent_gone`**.
2. `agent_id` present but its live `pid` differs → **`409 agent_moved`**, with
   `detail.current_pid`.
3. `card_rev` mismatch → **`409 card_changed`**, with a human-readable `detail.changed`
   array and `detail.was` / `detail.now`.
4. `closeout_sent_at` mismatch (finish only) → **`409 finish_step_mismatch`**, with the
   current value.
5. `cursor` — **advisory only.** The snapshot ring reaches roughly two minutes; a phone
   backgrounded for an hour always carries an older cursor, so this check cannot fire in
   the scenario it is most often assumed to cover. It is recorded on the operation so you
   can tell that the user acted on a stale view. **`agent_id` + `pid` + `card_rev` are the
   real guards** and they work at any staleness.

### 5.4 `card_rev`

A digest over exactly the facts an action depends on:

```
card_rev = blake2b_8(repr((
    availability,
    sorted((sid, status, handed_to or "") for each session),
    sorted(pid for each live proc),
    bool(closeout_sent_at),
)))
```

Deliberately **excluded**: `age_s`/`activity_at`, `topic`, `last_assistant`, `git.dirty`,
`cpu`, `etime`. Include those and every action 409s spuriously within one tick, users learn
to hammer "do it anyway", and the guard becomes worse than none.

`card_rev` is a field of the `w/<wid>` delta entity, so the client always holds the current
value without an extra fetch.

### 5.5 Client obligations around `expect`

- Capture `card_rev` at **press-down**, not at request time, so the value reflects what the
  user was looking at.
- Separately, a card whose grid position changed within the last **700 ms** must swallow the
  first tap and show *"ConfidAI moved — tap again"*. `card_rev` guards staleness; it does
  **not** guard mis-targeting, because after a re-sort the rev is perfectly fresh — for the
  wrong card.
- The confirmation sheet must name the target worktree explicitly.
- **Never auto-retry a 409.** A 409 means a human's mental model diverged from reality; only
  a human closes that gap. Present `detail.changed` verbatim with a single "do it anyway"
  that resubmits with the fresh rev, a fresh `Idempotency-Key`, and `expect.force: true`.

---

## 6. Cursors, epochs, versions, ETags

### 6.1 Cursor

```
cursor := "<epoch>:<seq>"          e.g.  "9f2c1a04:4711"
```

- **`epoch`** — 8 lowercase hex chars, regenerated on every process start **and** whenever
  the server detects that the Mac slept (a wall-clock jump exceeding the monotonic elapsed
  time by > 60 s). An epoch change means *"discontinuity: everything you hold may be
  wrong"*.
- **`seq`** — a monotonically increasing integer that **advances only when the canonical
  projection actually changed**. Therefore `since == seq` is a zero-byte proof of
  freshness, and an idle fleet costs a 304 or an empty long-poll return.

A cursor is opaque to the client except that it must be stored, echoed, and compared for
equality. Never parse it to derive time.

### 6.2 Freshness, split into two independent signals

This distinction is load-bearing; conflating them produces either a board that dims itself
when a fleet is merely quiet, or a board that shows green while the collector is wedged.

| Signal | Source | Meaning |
|---|---|---|
| **connection liveness** | arrival of *any* frame or response — including SSE heartbeats and keepalive comments | the socket is alive |
| **data recency** | `at` (the collector's `generated_at`), which advances on **every** tick regardless of whether anything changed | the data is current |
| **collector health** | `collector_ok` in heartbeats and in `srv` | the collector is not stuck |

### 6.3 Client freshness state machine

| State | Condition | Presentation |
|---|---|---|
| `live` | frame within `hb × 1.6 + 5 s`, `at` within `3 × tick + 10 s`, `collector_ok` | green dot, no chrome |
| `lagging` | frames fine, `at` behind, or one missed heartbeat | amber dot + "data as of 34s ago". **No functional change.** |
| `collector_stuck` | `collector_ok: false`, or `at` behind by > 90 s | amber bar naming the cause. Actuation disabled. |
| `stale` | no frame for `hb × 3 + 10 s` | board dims to 55 %, amber bar "not live — reconnecting". Actuating controls **disabled with the reason printed on the control**, never hidden (hiding reflows under the thumb). |
| `offline` | 3 failed reconnects, or `NWPath` unsatisfied | red bar "can't reach `<host>` — is Tailscale on?" + Retry |
| `mac_asleep` | `NWPath` satisfied but connect refused/timed out, or `wake_gap > 120` in the last `hello` | "your Mac appears to be asleep — nothing is running and no alerts will arrive" |

**Rendering rule that falls out of absolute timestamps:**

> **Ages keep ticking while stale. Statuses dim.**

An age derives from an absolute `activity_at`, so "8m ago" stays literally true with a dead
stream and degrades in the safe direction — counting up toward "we don't know". A `WORKING`
badge on a four-minute-old snapshot is a lie and gets dimmed.

### 6.4 ETag

Cacheable reads carry a weak ETag. It is computed over the **stable projection only** —
never over the raw body, which contains `at` and other per-tick values and could therefore
never match.

```
ETag: W/"9f2c1a04:4711"                  state
ETag: W/"1a2b3c4d5e6f7081"               worktree detail (content hash)
ETag: W/"9d4db7b2:412"                   chat (sid : message count)
```

`If-None-Match` matching returns `304 Not Modified` with the ETag, `Orchestra-Epoch`,
`Orchestra-Seq` and no body.

Worktree-detail ETags are **content hashes with volatile fields stripped** (`cpu`, `etime`,
`uptime_s`, `first_seen_at` are excluded from the hash), so an open card that is genuinely
unchanged costs a 304 across arbitrarily many ticks.

### 6.5 Clock skew

The client maintains `skew = server_at − local_now`, as a 5-sample median over the `at`
field of every response and frame. Every displayed age, every countdown
(`limit.resets_at`, resume `due_at`), and `Idempotency-Issued-At` uses it.

If `|skew| > 120 s`, surface it once: *"your phone's clock is 3 minutes behind the Mac —
times may look wrong."* Do not paper over it; a wrong reset countdown makes the resume
button lie.

---

## 7. The delta protocol

One encoding, used by both the SSE stream and the long-poll/conditional forms of
`GET /api/v1/state`. Learn it once.

### 7.1 Address space

```
counts                      the six-key tally                        LEAF
free                        array of free worktree ids               LEAF
order                       array of worktree ids, server sort order LEAF
other                       array of unwatched claude processes      LEAF
srv                         server-level scalars                     descend
w/<wid>                     worktree card scalars                    descend
w/<wid>/git                 git dict                                 descend
w/<wid>/p                   live_procs array                         LEAF
w/<wid>/order               array of session ids, server sort order  LEAF
w/<wid>/s/<sid>             one session                              descend
acct/<label>                one account's headroom summary           descend
r/<wid>|<sid>               one auto-resume schedule                 descend
j/<op_id>                   one operation                            descend
```

- **`wid`** is `wt_` + 12 hex of `sha1(abspath)`. It is **not** the worktree name:
  `discover_worktrees` dedupes by path, so two roots each holding a `ConfidAI` directory
  produce two cards with the same name, which would silently overwrite in a name-keyed map.
  The `name` and `path` ride as fields of `w/<wid>`.
- **`sid`** is the full transcript UUID, not the 8-char display id.
- Account labels are orchestra's `fb_label` (`"main"`, `"account8"`), never cclimits' `slug`
  — for the default home they differ (`slug: "default"` vs `fb_label: "main"`), and
  `session.account` carries the label.
- **`order` is explicit, not inferred from array position.** The server re-sorts cards by
  `(severity, name.lower())` and sessions by `(4.5 if handed_to else rank[status], age)`.
  That handed-off weight is a subtle, load-bearing decision; reimplementing it client-side
  would make the phone and the board disagree on the same fleet.

### 7.2 Op grammar

```json
{"p": "w/3f9a11c2/s/0bc2125a", "f": "status", "v": "needs_input"}   set field
{"p": "counts", "v": {"working": 3, "needs_input": 2, ...}}         replace entity
{"p": "w/3f9a11c2/s/0bc2125a", "x": 1}                              delete entity
{"p": "w/3f9a11c2/git", "f": "ahead", "x": 1}                       delete field
```

- `p` is always an entity address. `f` is an optional field within it. `x: 1` deletes.
- A set with no `f` replaces the entity **wholesale**.
- Descent is **declared per family** in §7.1, not inferred. `LEAF` families are always
  replaced whole; `descend` families are diffed one level deep. Nested values (e.g.
  `session.limit`, `session.pending_tools`) are replaced whole because they are small and
  their internal churn is correlated.

### 7.3 Apply algorithm (client)

```
for op in frame.ops:
    kver[op.p] += 1
    if op.f is None and op.x: mirror.remove(op.p)
    elif op.f is None:        mirror[op.p] = op.v
    elif op.x:                mirror[op.p].remove(op.f)
    else:                     mirror[op.p][op.f] = op.v
cursor = "<frame.epoch>:<frame.seq>"
```

Then rebuild the typed view. **Apply into a non-observed mirror and publish once**: under
`@Observable`, mutating an observed property per op invalidates dependent views per op and
any coalescing window throttles nothing.

Publish immediately for attention-raising frames; otherwise coalesce with a ~600 ms window.

### 7.4 Divergence detection

The server maintains a per-entity version counter and ships `dg`, a digest over
`sorted(f"{path}:{version}")`. The client maintains the identical map purely from applied
ops — no float serialisation is involved, so language repr differences cannot cause a false
mismatch. A mismatch **forces a full resync** and must be surfaced in diagnostics: silently
ignoring an unknown path is how a field added server-side becomes a permanently wrong value
on the phone with no error anywhere.

### 7.5 Fields excluded from the delta domain, and why

`generated_at`, `age_s`, `cpu`, `etime`, `id` (the 8-char display session id),
`ops[].updated_at`, `limit.resets_in`.

Measured on a live 9-worktree / 33-session fleet: four consecutive 5-second polls showed
**100 % of cards "changing" every tick, with `age_s` on all 33 sessions as the only
difference**. `age_s` is `now − mtime`: a function of when you asked, not of what happened.
Removing it took a real 5-second delta from **32,106 bytes to 131 bytes**, and to **47
bytes** when nothing happened.

The v1 payloads therefore carry **absolute epochs** — `activity_at`, `resets_at`,
`first_seen_at`, `started_at` — and the client derives every relative string locally
against skew-corrected time. `uptime_s` is parsed server-side from `ps`'s three `etime`
forms (`15:02`, `12:43:46`, `2-03:14:22`); the client never parses `etime`.

**Never compute `Date() - age_s`.** There is no absolute activity time in the legacy
payload at all, the snapshot can be seconds old, and the collect takes over a second.

---

## 8. Error envelope

### 8.1 Shape

Every non-2xx `/api/v1/` response — without exception, including `404` — is JSON:

```json
{
  "error": {
    "code": "agent_moved",
    "message": "that terminal is now a different process — refresh the board before sending",
    "detail": {
      "agent_id": "ag_7c21f0a9b3de",
      "expected_pid": 41234,
      "current_pid": 52118
    },
    "retriable": false,
    "request_id": "req_5f2a91c0"
  }
}
```

| Field | Type | Contract |
|---|---|---|
| `code` | String | machine-readable, stable within a major version. Unknown codes must be tolerated. |
| `message` | String | **user-facing prose, shown verbatim.** Often carries the remediation inline. Never parsed. |
| `detail` | Object | code-specific; may be `{}`. Never assume a key exists. |
| `retriable` | Bool | the **only** signal a retry policy may read, alongside the status code |
| `request_id` | String | echoed in `Orchestra-Request-Id`, printed beside any server traceback |

The prose is a product asset. `"couldn't reach Terminal — Automation permission? (ttys004)"`
and `"reopened in tmux but 'continue' never reached the conversation — attach and type it:
tmux -L fleet attach -t resume-x-143201"` are the actual copy. Show them.

### 8.2 Client retry policy

| Status | Policy |
|---|---|
| 2xx | — |
| 304 | not an error |
| 400, 403, 404, 410, 411, 413, 422 | **never retry.** A bug or a state problem. |
| 401 | **never retry.** Enter the re-pair flow. |
| 409 | retry **only** if `retriable: true`, after `Retry-After`, and only with the **same** `Idempotency-Key` |
| 429 | back off using `Retry-After`; jittered exponential thereafter |
| 5xx, transport error, timeout | retry with the **same** `Idempotency-Key`, capped exponential backoff with jitter (500 ms → 30 s), reset only after a successful response |

### 8.3 Complete error-code reference

| Status | `code` | `retriable` | Meaning |
|---|---|---|---|
| 400 | `bad_request` | no | malformed body |
| 400 | `bad_query` | no | a query parameter is not the declared type or is out of range; `detail.field` |
| 400 | `bad_content_length` | no | negative or non-integer `Content-Length` |
| 400 | `idempotency_key_required` | no | mutation without `Idempotency-Key` |
| 400 | `expect_required` | no | agent-touching route without `expect` |
| 400 | `model_and_effort_required` | no | dispatch without both; routing is deterministic and nothing is guessed |
| 400 | `empty_message` | no | message text empty after normalisation |
| 401 | `unauthorized` | no | absent / malformed / unknown / revoked token |
| 403 | `scope_insufficient` | no | `detail.have`, `detail.need` |
| 403 | `host_not_allowed` | no | `Host` header not in the allowlist (DNS rebinding, or a proxy fronting) |
| 403 | `origin_not_allowed` | no | cross-origin write refused |
| 403 | `content_type_required` | no | mutation not `application/json` |
| 403 | `peer_not_permitted` | no | source IP outside the listener's permitted range |
| 403 | `identity_not_permitted` | no | `tailscale whois` login not in `tailnet_allow_logins` |
| 403 | `lockdown_active` | no | `detail.lockdown_until`; only health and state are readable |
| 403 | `demo_mode` | no | the server is running `--demo`; actuation is disabled |
| 404 | `route_not_found` | no | no exact `(method, path)` match |
| 404 | `worktree_not_found` | no | unknown `wid` |
| 404 | `session_not_found` | no | unknown `sid` |
| 404 | `agent_not_found` | no | unknown `ag_id` |
| 404 | `op_not_found` | no | unknown op id, and it is not in `ops.jsonl` either — it never existed |
| 404 | `device_not_found` | no | unknown device id |
| 404 | `account_not_found` | no | unknown account label |
| 409 | `operation_in_flight` | **yes** | same key, same boot, still running; `detail.op_id` |
| 409 | `operation_indeterminate` | no | same key, **different boot** — the server restarted mid-operation. Check the fleet before retrying. |
| 409 | `operation_expired` | no | `Idempotency-Issued-At` older than 900 s |
| 409 | `worktree_busy` | **yes** | another op holds this worktree; `detail.blocking_op_id` |
| 409 | `agent_busy` | **yes** | another op holds this agent |
| 409 | `agent_gone` | no | `expect.agent_id` is not in the current snapshot |
| 409 | `agent_moved` | no | the agent's pid changed; `detail.current_pid` |
| 409 | `card_changed` | no | `expect.card_rev` mismatch; `detail.changed[]`, `detail.was`, `detail.now` |
| 409 | `finish_step_mismatch` | no | `expect.closeout_sent_at` mismatch; a phone and a browser disagree about the step |
| 409 | `resume_firing` | no | the schedule is executing; cancel is not an abort |
| 409 | `limits_not_primed` | **yes** | account headroom is not known yet |
| 409 | `no_free_worktree` | **yes** | auto-pick found nothing free |
| 409 | `pairing_not_open` | no | no window, expired, or already claimed |
| 409 | `pairing_code_wrong` | no | wrong code |
| 410 | `op_expired` | no | the op existed but is past the 24 h retention |
| 410 | `transcript_pruned` | no | the transcript file is gone |
| 410 | `cursor_invalid` | no | a chat cursor points past EOF or into a rotated file |
| 411 | `length_required` | no | chunked request body |
| 413 | `body_too_large` | no | over 1 MiB |
| 422 | `idempotency_key_reused` | no | same key, different fingerprint |
| 422 | `resume_time_unknown` | no | no reset time is known; supply an explicit `due_at`. `detail.reason`. |
| 422 | `invalid_model` | no | model not in the enum |
| 422 | `invalid_effort` | no | effort not in the enum |
| 422 | `invalid_percent` | no | reserve percent not an integer in 0–95 |
| 422 | `apns_unavailable` | no | `curl` with HTTP/2 or `openssl` is missing; `detail.missing` |
| 422 | `push_token_invalid` | no | the device token is not 64–200 hex characters |
| 429 | `rate_limited` | **yes** | `detail.retry_after_s`, `detail.bucket` |
| 429 | `budget_exhausted` | **yes** | dispatch hour/day cap; `detail.bucket`, `detail.used`, `detail.cap`, `detail.resets_in` |
| 429 | `pairing_attempts` / `pairing_locked` | no | pairing brute-force guards |
| 500 | `internal` | no | unhandled; a traceback with `request_id` was printed on the Mac |
| 503 | `state_not_ready` | **yes** | the first collect has not landed. Should never be the normal boot path. |
| 503 | `too_many_waiters` | **yes** | long-poll / stream capacity reached; `detail.retry_after_ms` is jittered |
| 503 | `ops_saturated` | **yes** | the operation worker pool queue is full |
| 504 | `cclimits_timeout` | **yes** | the limits subprocess timed out |
| 504 | `upstream_timeout` | **yes** | some other subprocess timed out |

---

## 9. Endpoints

Throughout, examples use one coherent fleet:

- host `achills-macbook-pro`, user `achill`, epoch `9f2c1a04`
- `wt_3f9a2b1c7d04` = `ConfidAI-ci-cleanup` (attention)
- `wt_88bc4d1e0a72` = `orbital-web` (free)
- `wt_9911aabb2233` = `ConfidAI3` (waiting on a limit)
- sessions `9d4db7b2-…` (needs_input, account8, opus-4-8), `0c77aa19-…` (limit, handed off),
  `4f2c88e1-…` (limit, resume armed)
- agent `ag_7c21f0a9b3de`, op `op_9c3a1b7f20e4d5a1`
- "now" ≈ `1784636700`

---

### 9.1 `GET /api/v1/health`

**Auth:** none. **Scope:** —. **Query:** none. **Idempotency:** n/a (safe).

Runs **no collector**. This is the true liveness probe and the tunnel warmer; it must never
be slow.

`200`:

```json
{
  "ok": true,
  "service": "orchestra",
  "version": "1.0.0",
  "api": "1.0",
  "build": "2026.07.21+9f3c1a",
  "min_client_build": 1,
  "boot_id": "7d21ac90bb14",
  "epoch": "9f2c1a04",
  "started_at": 1784630001.2,
  "uptime_s": 6699.1,
  "at": 1784636700.3,
  "state_ready": true,
  "last_tick_at": 1784636698.9,
  "collector_ok": true,
  "on_battery": false,
  "sleep_prevented": true,
  "mode": "live",
  "cert_not_after": 1855123200,
  "paired_devices": 2,
  "features": ["delta", "stream", "longpoll", "gzip", "idempotency", "ops",
               "events", "chat_cursor", "chat_raw", "chat_multiline_tmux",
               "agent_kill", "receipts", "push_ntfy"]
}
```

| Status | When |
|---|---|
| 200 | always, once the listener is up |
| 403 | `host_not_allowed` / `peer_not_permitted` — the guard runs even here |
| 429 | `rate_limited` (per-IP bucket) |

Client use:

- `min_client_build` > the app's build → render a blocking *"update the app"* screen.
- `api` major mismatch → *"update orchestra on your Mac"*.
- `state_ready: false` → expect `503 state_not_ready` on state reads; retry in 2 s.
- `cert_not_after` within 30 days → warn.
- `sleep_prevented: false` on a laptop with push configured → warn that notifications will
  stop when the lid closes.

---

### 9.2 `GET /api/v1/meta`

**Auth:** Bearer, `read`. **Query:** none.

Capability discovery and budget introspection. Poll on foreground, not on a timer.

`200`:

```json
{
  "api": "1.0",
  "version": "1.0.0",
  "build": "2026.07.21+9f3c1a",
  "epoch": "9f2c1a04",
  "boot_id": "7d21ac90bb14",
  "started_at": 1784630001.2,
  "mode": "live",
  "min_client_build": 1,
  "server": {"hostname": "achills-macbook-pro", "user": "achill"},
  "device": {
    "id": "9f3ab21c",
    "label": "Achill's iPhone",
    "scope": "act",
    "act_reissue_pending": false,
    "push": {"backend": "apns", "registered": true,
             "last_push_at": 1784636400.0, "last_push_code": "200"}
  },
  "features": ["delta", "stream", "longpoll", "gzip", "idempotency", "ops",
               "events", "chat_cursor", "chat_raw", "chat_multiline_tmux",
               "agent_kill", "receipts", "push_ntfy"],
  "limits": {
    "max_wait_s": 30,
    "max_waiters": 32,
    "max_streams_per_device": 1,
    "max_connections": 64,
    "max_body_bytes": 1048576,
    "idempotency_ttl_s": 86400,
    "op_issued_at_ttl_s": 900,
    "snapshot_ring": 64,
    "snapshot_reach_s": 180,
    "ops_retention_s": 86400,
    "ops_workers": 6,
    "ops_max_queued": 12,
    "chat_max_limit": 200,
    "message_max_chars": 4000
  },
  "budget": {
    "read":          {"tokens": 148.0, "burst": 150, "per_min": 400},
    "act":           {"tokens": 8.2,   "burst": 10,  "per_min": 30},
    "refresh":       {"tokens": 3.0,   "burst": 2,   "per_hour": 4},
    "dispatch_hour": {"used": 4, "cap": 15, "resets_in": 2210},
    "dispatch_day":  {"used": 9, "cap": 60, "resets_in": 51840}
  },
  "collect": {
    "last_ms": 1541,
    "tick_s": 10.0,
    "duty_cycle": 0.15,
    "mode": "hot",
    "worktrees": 9,
    "sessions_scanned": 44,
    "sessions_served": 31,
    "limits_primed": true,
    "limits_fetched_at": 1784636467.6
  },
  "lockdown_until": 0,
  "legacy_hits": {"/api/state": 4192, "/api/chat": 88, "/api/dispatch": 3}
}
```

**API compatibility contract** — stated because HTML skew is impossible but phone skew is
inevitable (a user who has not updated orchestra in three months while the App Store
auto-updates the app is the default case):

> Within a major version the server **may** add fields, add endpoints, and add enum values
> in fields documented as open (`status`, `availability`, `mode`, `flags`, `kind`,
> `error.code`, `event.type`). Clients **must** ignore unknown fields and **must** map
> unknown enum values to a documented fallback (`status` → `waiting`; `availability` →
> `busy`; `error.code` → generic, driven by `retriable`). Removing a field, retyping a
> field, or changing a status code requires a major bump and a new path prefix.

| Status | When |
|---|---|
| 200 | success |
| 401 / 403 | auth |
| 429 | rate limited |

---

### 9.3 `GET /api/v1/state`

**Auth:** Bearer, `read`. **Idempotency:** n/a.

The board. Serves three shapes from one route.

**Query parameters**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `since` | cursor `"<epoch>:<seq>"` | — | request a delta from this cursor |
| `wait` | int seconds, 0–30 | `0` | long-poll: hold until the cursor advances or the deadline passes |

**Headers:** `If-None-Match: W/"<epoch>:<seq>"` is honoured and is equivalent to `since`.

**Resolution:**

| Condition | Result |
|---|---|
| no `since`, no `If-None-Match` | `200`, `kind: "full"` |
| `since` epoch ≠ current epoch | `200`, `kind: "full"` — the epoch changed, everything is discontinuous |
| `since` seq == current seq, `wait=0` | `304 Not Modified`, no body |
| `since` seq == current seq, `wait>0` | hold; on advance `200 kind:"delta"`, on timeout `304` |
| `since` seq older than the ring | `200`, `kind: "full"` |
| `since` seq in the ring | `200`, `kind: "delta"` |
| `since` seq > current seq | `200`, `kind: "full"` — a client from the future (restart with a lower seq) |

**A stale `since` is never an error.**

#### `kind: "full"` — `200`

```json
{
  "kind": "full",
  "epoch": "9f2c1a04",
  "seq": 4711,
  "cursor": "9f2c1a04:4711",
  "at": 1784636692.641,
  "collected_ms": 1541,
  "dg": "a41f0c93",
  "tick_s": 10.0,
  "server": {"hostname": "achills-macbook-pro", "user": "achill",
             "mode": "live", "api": "1.0"},
  "srv": {
    "limits_primed": true,
    "limits_fetched_at": 1784636467.641,
    "limits_available": true,
    "collector_ok": true,
    "on_battery": false,
    "last_tick_at": 1784636692.641,
    "wake_gap": 0.0,
    "lockdown_until": 0,
    "dispatch_budget": {"hour_used": 4, "hour_cap": 15,
                        "day_used": 9, "day_cap": 60}
  },
  "counts": {"working": 3, "needs_input": 2, "limit": 3,
             "blocked": 0, "waiting": 1, "ended": 19},
  "free": ["wt_88bc4d1e0a72"],
  "order": ["wt_3f9a2b1c7d04", "wt_9911aabb2233", "wt_88bc4d1e0a72"],
  "worktrees": [
    {
      "id": "wt_3f9a2b1c7d04",
      "name": "ConfidAI-ci-cleanup",
      "path": "/Users/achill/Downloads/ConfidAI-ci-cleanup",
      "availability": "attention",
      "severity": 0,
      "card_rev": "7b21e4de",
      "detail_etag": "W/\"1a2b3c4d5e6f7081\"",
      "closeout_sent_at": null,
      "agents_n": 1,
      "reachable_agents_n": 1,
      "sessions_total": 9,
      "sessions_shown": 6,
      "git": {
        "branch": "feat/ci-cleanup",
        "dirty": 12,
        "ahead": 3,
        "behind": null,
        "commit_hash": "a1b2c3d4",
        "commit_at": 1784600011,
        "commit_subject_short": "ci: drop the legacy matrix and pin tmux for the tmux…",
        "stale": false
      },
      "session_order": ["9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
                        "0c77aa19-55b2-4d31-9f0e-2ab4c8d71133"],
      "sessions": [
        {
          "id": "9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
          "account": "account8",
          "status": "needs_input",
          "activity_at": 1784636651.641,
          "model": "opus-4-8",
          "agent_id": "ag_7c21f0a9b3de",
          "agent_certain": true,
          "reachable": true,
          "multiline_ok": true,
          "flags": ["tool_running"],
          "pending_tools": ["AskUserQuestion"],
          "handed_to": null,
          "limit": null,
          "resume_id": null,
          "headline": "Should I drop the legacy workflow file or keep it behind a flag?"
        },
        {
          "id": "0c77aa19-55b2-4d31-9f0e-2ab4c8d71133",
          "account": "main",
          "status": "limit",
          "activity_at": 1784627380.641,
          "model": "fable-5",
          "agent_id": "ag_11ffee0099aa",
          "agent_certain": false,
          "reachable": true,
          "multiline_ok": true,
          "flags": [],
          "pending_tools": [],
          "handed_to": "account8",
          "limit": {"worst": "Fable", "group": "weekly",
                    "resets_at": 1784645999.294, "known": true},
          "resume_id": null,
          "headline": "Handed the migration to account8 — nothing further here."
        }
      ]
    },
    {
      "id": "wt_9911aabb2233",
      "name": "ConfidAI3",
      "path": "/Users/achill/Downloads/ConfidAI3",
      "availability": "waiting",
      "severity": 4,
      "card_rev": "c40a19f2",
      "detail_etag": "W/\"77aa0913cc1e4b20\"",
      "closeout_sent_at": null,
      "agents_n": 1,
      "reachable_agents_n": 1,
      "sessions_total": 3,
      "sessions_shown": 3,
      "git": {"branch": "feat/limit-retry", "dirty": 0, "ahead": 1, "behind": 2030,
              "commit_hash": "77aa0913", "commit_at": 1784599001,
              "commit_subject_short": "wip: retry on weekly cap", "stale": false},
      "session_order": ["4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f"],
      "sessions": [
        {
          "id": "4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
          "account": "account8",
          "status": "limit",
          "activity_at": 1784627140.0,
          "model": "fable-5",
          "agent_id": "ag_5501ba7d0c31",
          "agent_certain": true,
          "reachable": true,
          "multiline_ok": true,
          "flags": [],
          "pending_tools": [],
          "handed_to": null,
          "limit": {"worst": "Fable", "group": "weekly",
                    "resets_at": 1784645999.517, "known": true},
          "resume_id": "wt_9911aabb2233|4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
          "headline": "You've hit your weekly limit for Fable."
        }
      ]
    },
    {
      "id": "wt_88bc4d1e0a72",
      "name": "orbital-web",
      "path": "/Users/achill/Downloads/orbital-web",
      "availability": "free",
      "severity": 5,
      "card_rev": "0e11c882",
      "detail_etag": "W/\"0e11c882aa430cd7\"",
      "closeout_sent_at": null,
      "agents_n": 0,
      "reachable_agents_n": 0,
      "sessions_total": 2,
      "sessions_shown": 2,
      "git": {"branch": "main", "dirty": 0, "ahead": 0, "behind": 0,
              "commit_hash": "5f0c22aa", "commit_at": 1784560000,
              "commit_subject_short": "chore: bump lockfile", "stale": false},
      "session_order": [],
      "sessions": []
    }
  ],
  "accounts": [
    {"label": "main", "headroom": 18.0, "exhausted": false,
     "reserve_percent": 20, "reserve_blocked": true,
     "worst": "Weekly", "group": "weekly", "resets_at": 1784780400.0,
     "known": true, "fresh": true},
    {"label": "account8", "headroom": 62.0, "exhausted": false,
     "reserve_percent": 0, "reserve_blocked": false,
     "worst": "Fable", "group": "weekly", "resets_at": 1784645999.294,
     "known": true, "fresh": true}
  ],
  "other": [
    {"pid": 51221, "uptime_s": 8123, "tty": "ttys011",
     "host": "Cursor", "cwd": "/Users/achill/scratch"}
  ],
  "resumes": [
    {
      "id": "wt_9911aabb2233|4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
      "worktree_id": "wt_9911aabb2233",
      "worktree": "ConfidAI3",
      "session_id": "4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
      "account": "account8",
      "model": "fable-5",
      "status": "pending",
      "delay_s": 60.0,
      "resets_at": 1784645999.517,
      "due_at": 1784646059.517,
      "created_at": 1784587543.5,
      "attempts": 0,
      "firing_since": null,
      "fired_at": null,
      "message": null
    }
  ],
  "ops": [
    {"id": "op_9c3a1b7f20e4d5a1", "kind": "finish", "status": "running",
     "target": {"worktree_id": "wt_3f9a2b1c7d04"},
     "progress_n": 2, "created_at": 1784636700.1}
  ]
}
```

**Field notes**

- `headline` is a hard **80-char** cap, chosen as `last_assistant or topic`, ellipsised with
  `…` (U+2026). The full free text (`topic` 140, `last_user` 140, `last_assistant` 240,
  `subagent_said` 240) lives only in the detail endpoint — those four fields are ~48 % of
  the legacy payload and change only when the agent speaks.
- `flags` is **always an array**, never absent, never `false`-valued keys. In the legacy
  payload `tool_running`, `bg_shell` and `subagents_active` were present only when true.
- `limit.known: false` makes the all-null limit object explicit. `status: "limit"` with an
  unknown reset time is a real and common state (the transcript-regex fallback fires when
  the CLI wrote a limit notice but the cclimits cache is cold). Render *"limited, reset
  time unknown"* and route the user to an explicit-time resume.
- `handed_to` non-null means **do not alert and do not treat as actionable** — work already
  continued on another account. Such sessions are excluded from `counts`, excluded from card
  severity, and sorted between `waiting` and `ended`.
- `agent_certain: false` means the session↔process pairing came from a freshness fallback,
  not an account match. Render it as a guess (the board uses a dashed chip) and refuse to
  send to it from a notification (§9.13).
- `sessions_total` vs `sessions_shown` exposes the silent per-card truncation
  (`max_sessions`, default 6). Render "and 3 more".
- `git.stale: true` means the git facts are older than twice their refresh interval;
  `dirty` in particular is refreshed on a timer and may lag by up to ~15 s.
- `commit_subject_short` is truncated to 72 chars. Commit subjects are the only untruncated
  strings in the legacy payload.

#### `kind: "delta"` — `200`

```json
{
  "kind": "delta",
  "epoch": "9f2c1a04",
  "base": 4711,
  "seq": 4713,
  "cursor": "9f2c1a04:4713",
  "at": 1784636703.2,
  "collected_ms": 388,
  "dg": "b7710e42",
  "ops": [
    {"p": "w/3f9a2b1c7d04/s/9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
     "f": "status", "v": "working"},
    {"p": "w/3f9a2b1c7d04/s/9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
     "f": "activity_at", "v": 1784636702.9},
    {"p": "w/3f9a2b1c7d04", "f": "card_rev", "v": "1cc8e40b"},
    {"p": "w/3f9a2b1c7d04", "f": "availability", "v": "busy"},
    {"p": "counts", "v": {"working": 4, "needs_input": 1, "limit": 3,
                          "blocked": 0, "waiting": 1, "ended": 19}},
    {"p": "order", "v": ["wt_9911aabb2233", "wt_3f9a2b1c7d04", "wt_88bc4d1e0a72"]},
    {"p": "j/op_9c3a1b7f20e4d5a1", "f": "progress_n", "v": 4}
  ]
}
```

#### `304 Not Modified`

```
HTTP/1.1 304 Not Modified
ETag: W/"9f2c1a04:4711"
Orchestra-Epoch: 9f2c1a04
Orchestra-Seq: 4711
Orchestra-Api: 1.0
```

| Status | When |
|---|---|
| 200 | full or delta |
| 304 | cursor current (immediately, or after `wait` elapsed) |
| 400 | `bad_query` — `wait` out of 0–30, or a malformed cursor |
| 401 / 403 | auth, lockdown |
| 429 | rate limited |
| 503 | `state_not_ready` (first collect not landed), `too_many_waiters` |

`too_many_waiters` carries a **jittered** `Retry-After` and `detail.retry_after_ms`.
Overflow must shed load; returning an immediate 304 instead would turn the 33rd client into
a hot reconnect loop.

---

### 9.4 `GET /api/v1/stream` — Server-Sent Events

**Auth:** Bearer, `read`. **Idempotency:** n/a.

The primary transport. One connection replaces polling entirely.

**Query parameters**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `since` | cursor | — | replay from here if it is in the ring |
| `sub` | string, ≤ 64 chars | required | stable per-install subscriber id |
| `low` | `0`\|`1` | `0` | Low Data / Low Power mode: slower server tick, longer heartbeat, no keepalive comments |

**Response headers**

```
HTTP/1.1 200 OK
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
X-Accel-Buffering: no
Connection: close
Orchestra-Api: 1.0
```

There is **no `Content-Encoding`**, ever, even with `Accept-Encoding: gzip`.

**`sub` semantics:** reconnecting with the same `sub` **evicts** the previous subscriber
(which receives `event: bye` with `reason: "evicted"`) and is exempt from the concurrency
cap. This makes reconnect idempotent and makes stream exhaustion by your own phone
arithmetically impossible.

#### Frame types

**`hello`** — always first.

```
event: hello
id: 4711
data: {"epoch":"9f2c1a04","seq":4711,"cursor":"9f2c1a04:4711","at":1784636692.641,
       "dg":"a41f0c93","tick":10.0,"hb":25.0,"sub":"e7c1a2","server":"orchestra/1",
       "wake_gap":0.0,"collector_ok":true,"max_age_s":300,
       "caps":["delta","gzip","idempotency","ops","events","chatafter","push"]}

```

**`delta`** — identical `ops` grammar to §7.2.

```
event: delta
id: 4713
data: {"epoch":"9f2c1a04","seq":4713,"at":1784636703.2,"dg":"b7710e42","ops":[
        {"p":"w/3f9a2b1c7d04/s/9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520","f":"status","v":"working"},
        {"p":"w/3f9a2b1c7d04","f":"card_rev","v":"1cc8e40b"},
        {"p":"counts","v":{"working":4,"needs_input":1,"limit":3,"blocked":0,"waiting":1,"ended":19}}]}

```

**`hb`** — heartbeat, every `hb` seconds regardless of whether anything changed. It carries
the current cadence so a client that connected during a busy period widens its window when
the server slows down.

```
event: hb
id: 4713
data: {"epoch":"9f2c1a04","seq":4713,"at":1784636728.0,"dg":"b7710e42",
       "tick":10.0,"hb":25.0,"collector_ok":true,"wake_gap":0.0,"on_battery":false}

```

**`resync`** — discard local state and refetch `GET /api/v1/state` with no `since`.

```
event: resync
id: 4900
data: {"epoch":"9f2c1a04","seq":4900,"reason":"cursor_too_old"}

```

`reason` ∈ `cursor_too_old` | `epoch_changed` | `history_empty` | `slow_consumer` |
`digest_mismatch` | `unaddressable`.

**`bye`** — a clean close. Reconnect **immediately, with no backoff**.

```
event: bye
data: {"reason":"max_age"}

```

`reason` ∈ `max_age` (the 300 s cap) | `evicted` (same `sub` reconnected) | `shutdown`.

**Keepalive comment** — every 5 s, suppressed when `low=1`.

```
:

```

Three bytes, invisible to any conformant SSE parser. Its only job is to fill a black-holed
peer's send buffer so the write fails in seconds rather than after the kernel's full
retransmit ladder.

#### Client rules

- `timeoutIntervalForRequest` on the stream's session must be **≥ 3 × `hb`** (75 s at the
  25 s default) — for a streaming response this is an inter-packet inactivity timeout, and
  the heartbeat is what holds it open.
- Use a **dedicated** `URLSessionConfiguration` for the stream. Sharing the app's session
  means a shorter request timeout silently kills it.
- `waitsForConnectivity = false`; the app manages reconnect itself.
- Reconnect backoff: `0, 1, 2, 4, 8, 15, 30, 60` s, ±25 % jitter, **reset only after
  `hello` arrives** (not on TCP connect, or a server that accepts then rejects yields a hot
  loop). A single-flight flag ensures at most one attempt is ever in flight.
- Reconnect immediately on `bye`. Debounce `NWPathMonitor` changes by 500 ms and dedupe on
  `(status, interface types)`. `scenePhase → .active` reconnect is idempotent: if a frame
  arrived within `hb`, do nothing.
- `401`/`403` → do **not** retry; enter the re-pair flow. A token problem is not a network
  problem.
- `404` on the stream route → the server predates it; drop to the fallback ladder and do
  not re-probe for 10 minutes.
- Tear down only on `scenePhase == .background`, debounced 2 s. `.inactive` is transient
  (Control Center, the app switcher, an incoming call banner, screen lock) and must not
  drop the connection. Cancel **both** the consuming `Task` and the retained
  `URLSessionDataTask`.
- Do **not** hold a `beginBackgroundTask` assertion for a stream or a poll.

#### Fallback ladder

| Tier | Transport | Enter when | Leave when |
|---|---|---|---|
| 1 | SSE `/api/v1/stream` | default | 2 consecutive failures to receive `hello`; buffering detected over ≥ 5 samples; `404` |
| 2 | `GET /api/v1/state?since=&wait=25` | tier 1 exited | `404`, or 3 consecutive transport errors |
| 3 | `GET /api/v1/state?since=` on the cadence table below | tier 2 exited | `404` |
| 4 | legacy `GET /api/state` full poll at 2× cadence | the server predates v1 | — |

Tier-3/4 foreground cadence:

| Fleet state | Wi-Fi | Cellular | Low Power |
|---|---|---|---|
| something working | 5 s | 10 s | 20 s |
| attention only | 10 s | 15 s | 30 s |
| all idle | 30 s | 45 s | 60 s |
| backgrounded | **none** | **none** | **none** |

**Low Data Mode keeps the stream** (with `low=1`) and suppresses discretionary fetches
instead. Switching off the stream in favour of a 60 s snapshot poll is roughly 13× *more*
cellular data, which inverts the user's stated intent.

| Status | When |
|---|---|
| 200 | stream opened |
| 400 | `bad_query` — `sub` missing or too long |
| 401 / 403 | auth, lockdown |
| 429 | rate limited |
| 503 | `too_many_waiters` |

---

### 9.5 `GET /api/v1/worktrees/{wid}`

**Auth:** Bearer, `read`. **Query:** none. **Headers:** `If-None-Match` honoured.

Everything the summary omits: full free text, per-agent process facts, per-session
`cwd`/`branch`/`pid`.

`200`:

```json
{
  "epoch": "9f2c1a04",
  "seq": 4713,
  "at": 1784636703.2,
  "id": "wt_3f9a2b1c7d04",
  "name": "ConfidAI-ci-cleanup",
  "path": "/Users/achill/Downloads/ConfidAI-ci-cleanup",
  "availability": "attention",
  "card_rev": "1cc8e40b",
  "closeout_sent_at": null,
  "trunk": "origin/main",
  "git": {
    "branch": "feat/ci-cleanup",
    "dirty": 12,
    "ahead": 3,
    "behind": null,
    "stale": false,
    "commit": {
      "hash": "a1b2c3d4",
      "at": 1784600011,
      "subject": "ci: drop the legacy matrix and pin tmux for the tmux tests so loaded runners stop flaking",
      "subject_short": "ci: drop the legacy matrix and pin tmux for the tmux…"
    }
  },
  "sessions": [
    {
      "id": "9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
      "account": "account8",
      "status": "needs_input",
      "activity_at": 1784636651.641,
      "model": "opus-4-8",
      "cwd": "/Users/achill/Downloads/ConfidAI-ci-cleanup/repo",
      "subdir": "repo",
      "branch": "feat/ci-cleanup",
      "agent_id": "ag_7c21f0a9b3de",
      "agent_certain": true,
      "reachable": true,
      "multiline_ok": true,
      "pid": 41234,
      "flags": ["tool_running"],
      "pending_tools": ["AskUserQuestion"],
      "pending_workflows": 0,
      "pending_bg_agents": 0,
      "pending_bg_tools": 0,
      "topic": "clean up the CI matrix so tmux tests stop flaking on loaded runners",
      "last_user": "keep the 3.11 job, drop 3.10",
      "last_assistant": "Should I drop the legacy workflow file or keep it behind a flag? Dropping it removes the 3.10 job entirely; keeping it behind a flag means…",
      "subagent_said": null,
      "handed_to": null,
      "limit": null,
      "resume_id": null
    }
  ],
  "agents": [
    {
      "id": "ag_7c21f0a9b3de",
      "handle": "tmux:fleet:mission-confidai-ci-cleanup-121030:0.0",
      "kind": "tmux",
      "host": "tmux -L fleet",
      "tmux_sock": "fleet",
      "tmux_target": "mission-confidai-ci-cleanup-121030:0.0",
      "tty": null,
      "account": "account8",
      "reachable": true,
      "multiline_ok": true,
      "pid": 41234,
      "cpu": 3.4,
      "uptime_s": 45826,
      "etime": "12:43:46",
      "subdir": null,
      "first_seen_at": 1784590866.0,
      "claims": ["9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520"]
    }
  ]
}
```

- `multiline_ok` is `true` only for tmux-hosted agents, where delivery uses a bracketed
  paste buffer and newlines survive. For AppleScript hosts (Terminal.app, iTerm2) the server
  collapses all newlines to single spaces, so the composer must disable multi-line input
  rather than silently mangle it.
- `agents[].kind` ∈ `tmux` | `terminal` | `iterm` | `editor` | `other`. Do **not** switch on
  `host`: it embeds the tmux socket name (`"tmux -L fleet"`) and is not an enum.
- `uptime_s` is parsed server-side. `etime` is included verbatim for display only.

| Status | When |
|---|---|
| 200 | success |
| 304 | `If-None-Match` matched the content hash |
| 401 / 403 | auth, lockdown |
| 404 | `worktree_not_found` |
| 429 | rate limited |
| 503 | `state_not_ready` |

---

### 9.6 `GET /api/v1/agents`

**Auth:** Bearer, `read`. **Query:** `include_unwatched` = `0`|`1`, default `1`.

A flat list of every live `claude` process, including those whose cwd matched no watched
worktree. Used by the "other live agents" section and to resolve an `ag_id` from a push
payload.

`200`:

```json
{
  "epoch": "9f2c1a04",
  "seq": 4713,
  "at": 1784636703.2,
  "agents": [
    {"id": "ag_7c21f0a9b3de", "worktree_id": "wt_3f9a2b1c7d04",
     "handle": "tmux:fleet:mission-confidai-ci-cleanup-121030:0.0",
     "kind": "tmux", "host": "tmux -L fleet", "tty": null, "account": "account8",
     "reachable": true, "multiline_ok": true, "pid": 41234, "cpu": 3.4,
     "uptime_s": 45826, "first_seen_at": 1784590866.0,
     "claims": ["9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520"]},
    {"id": "ag_a0c19b7734ef", "worktree_id": null,
     "handle": "tty:ttys011:1784628577", "kind": "editor", "host": "Cursor",
     "tty": "ttys011", "account": null, "reachable": false, "multiline_ok": false,
     "pid": 51221, "cpu": 0.2, "uptime_s": 8123, "first_seen_at": 1784628577.0,
     "claims": []}
  ]
}
```

**`ag_id` derivation** (documented so the client can reason about stability):

```
handle = "tmux:<sock or 'default'>:<session:win.pane>"     when a tmux target exists
       | "tty:<tty>:<int(first_seen_at)>"                  when a tty exists
       | "pid:<pid>:<int(first_seen_at)>"                  otherwise
ag_id  = "ag_" + sha1(handle)[:12]
```

A tmux handle survives pid churn inside a pane. A tty handle is salted with `first_seen_at`
because macOS aggressively reuses tty device names — a bare `tty:ttys004` would be exactly
as reusable as a pid, just on a slower clock.

| Status | 200, 401, 403, 429, 503 |
|---|---|

---

### 9.7 `GET /api/v1/topology`

**Auth:** Bearer, `read`. **Query:** `wait` (int 0–30, long-poll on change).

The branch map. Expensive server-side (~90 git subprocesses); cached with a 45 s TTL and
invalidated when a git tick produced any `w/*/git` change.

**Fetch on sheet open, then only on explicit pull-to-refresh.** Never on a timer from a
phone.

`200`:

```json
{
  "epoch": "9f2c1a04",
  "seq": 4713,
  "at": 1784636703.2,
  "fetched_at": 1784636650.0,
  "groups": [
    {
      "repo": "ConfidAI",
      "base": "origin/main",
      "trunk_at": 1784577000,
      "trunk_commits": [1784577000, 1784570100, 1784562220],
      "branches": [
        {
          "worktree_id": "wt_3f9a2b1c7d04",
          "worktree": "ConfidAI-ci-cleanup",
          "branch": "feat/ci-cleanup",
          "fork_at": 1784330000,
          "tip_at": 1784600011,
          "ahead": 3,
          "behind": 0,
          "dirty": 12,
          "hash": "a1b2c3d4",
          "subject_short": "ci: drop the legacy matrix and pin tmux for the tmux…",
          "commits": [1784600011, 1784591200, 1784588000]
        },
        {
          "worktree_id": "wt_9911aabb2233",
          "worktree": "ConfidAI3",
          "branch": "feat/limit-retry",
          "fork_at": 1784200000,
          "tip_at": 1784599001,
          "ahead": 1,
          "behind": 2030,
          "dirty": 0,
          "hash": "77aa0913",
          "subject_short": "wip: retry on weekly cap",
          "commits": [1784599001]
        }
      ]
    }
  ],
  "dropped": [
    {"worktree_id": "wt_88bc4d1e0a72", "worktree": "orbital-web",
     "reason": "no_merge_base"}
  ]
}
```

- `dropped` is new in v1 and matters: the legacy topology endpoint **silently omits** any
  worktree whose base ref or merge-base lookup fails, so `topology.groups[].branches` can be
  shorter than the state worktree list and the two cannot be zipped. `reason` ∈
  `no_base_ref` | `no_merge_base` | `bad_timestamps`.
- `trunk_commits` and `commits` are capped at 40 entries, newest first.
- `branch` is `"?"` when `git branch --show-current` is empty (detached HEAD).
- `behind` can be enormous (2030 above) for an orphaned or unrelated branch.
- All timestamps are integer epoch seconds.

| Status | 200, 304 (`If-None-Match`), 401, 403, 429, 503, 504 (`upstream_timeout`) |
|---|---|

---

### 9.8 `GET /api/v1/limits`

**Auth:** Bearer, `read`. **Query:** none.

Never blocks on the network. Serves the cached cclimits result, which a dedicated
server thread refreshes on demand. To force a refetch use §9.9.

`200`:

```json
{
  "available": true,
  "fetched_at": 1784636467.641,
  "fresh": true,
  "stale_after": 1784638267.641,
  "error": null,
  "accounts": [
    {
      "label": "main",
      "slug": "default",
      "email": "achillr@gmail.com",
      "plan": "max",
      "config_dir": "/Users/achill/.claude",
      "ok": true,
      "known": true,
      "fresh": true,
      "error": null,
      "headroom_percent": 18.0,
      "reserve_percent": 20,
      "reserve_blocked": true,
      "exhausted": false,
      "limits": [
        {"label": "Session", "group": "session", "percent": 21.0,
         "remaining_percent": 79.0, "model_scoped": false, "exhausted_now": false,
         "resets_at": 1784640067.6, "resets_in_s": 3600.0},
        {"label": "Weekly", "group": "weekly", "percent": 82.0,
         "remaining_percent": 18.0, "model_scoped": false, "exhausted_now": false,
         "resets_at": 1784780400.0, "resets_in_s": 143932.4}
      ]
    },
    {
      "label": "account8",
      "slug": "account2",
      "email": "achill+8@example.com",
      "plan": "max",
      "config_dir": "/Users/achill/.claude-account8",
      "ok": true,
      "known": true,
      "fresh": true,
      "error": null,
      "headroom_percent": 62.0,
      "reserve_percent": 0,
      "reserve_blocked": false,
      "exhausted": false,
      "limits": [
        {"label": "Fable", "group": "weekly", "percent": 100.0,
         "remaining_percent": 0.0, "model_scoped": true, "exhausted_now": true,
         "resets_at": 1784645999.294, "resets_in_s": 9531.65}
      ]
    },
    {
      "label": "work",
      "slug": "work",
      "email": null,
      "plan": "pro",
      "config_dir": "/Users/achill/.claude-work",
      "ok": false,
      "known": false,
      "fresh": false,
      "error": "token refresh failed (401)",
      "headroom_percent": null,
      "reserve_percent": 0,
      "reserve_blocked": false,
      "exhausted": false,
      "limits": []
    }
  ]
}
```

**Critical semantics:**

- **`label` is the join key** into `session.account` and into every write endpoint. `slug` is
  cclimits' own identifier and is display-only; for the default home they differ
  (`slug: "default"` vs `label: "main"`).
- **All timestamps are float epochs.** The legacy endpoint returns ISO-8601 strings here and
  float epochs elsewhere under the *same field names*; v1 normalises everything to floats.
- **A failed account is present with `ok: false, known: false`, not omitted.** The legacy
  code drops it, which makes a limit-stuck session look merely `waiting` — the mis-alert
  this API exists to prevent. Any client rule that says "it's your turn" must first check
  that the session's account is `known` **and** `fresh`.
- **`model_scoped: true` caps do not block the account** — they strand only sessions running
  that model, matched by substring containment of the label in the model name. Never collapse
  `exhausted` and `scoped exhausted` into one flag.
- `resets_in_s` is relative to `fetched_at`, not to now. Compute the deadline as
  `fetched_at + resets_in_s` (or just use `resets_at`) and drive a live countdown against
  the wall clock.
- `email` is a real account email address. It is behind `read` scope; treat it as sensitive.

| Status | When |
|---|---|
| 200 | success, including a stale-but-usable cache (`fresh: false`) |
| 401 / 403 | auth, lockdown |
| 429 | rate limited |
| 503 | `state_not_ready` — cclimits has never succeeded and there is no cache; `error` is set and `accounts` is `[]` |

---

### 9.9 `POST /api/v1/limits/refresh`

**Auth:** Bearer, `act`. **Idempotency:** required. **Rate:** `refresh` bucket, 4/hour.

Forces a real network refetch. Returns an **operation**, never blocking — the underlying
subprocess has a 90 s timeout, well past iOS's default 60 s request timeout.

Request: `{}`

`202`:

```
Location: /api/v1/ops/op_44b90e1c7fa22d10
Idempotent-Replay: false
```
```json
{"op": {"id": "op_44b90e1c7fa22d10", "kind": "limits_refresh", "status": "queued",
        "target": {}, "progress": [], "result": null, "error": null,
        "created_at": 1784636710.0, "updated_at": 1784636710.0,
        "completed_at": null}}
```

Terminal `result`: `{"accounts": 8, "fetched_at": 1784636790.2, "changed": true}`.
Terminal `error.code` on failure: `cclimits_timeout`, `cclimits_missing`, `cclimits_failed`.

Concurrent refreshes share one subprocess (single-flight); the second caller receives the
first's op id via idempotency or a `409 operation_in_flight` if the key differs.

| Status | 202, 400, 401, 403, 409, 422, 429, 503 |
|---|---|

---

### 9.10 `PUT /api/v1/accounts/{label}/reserve`

**Auth:** Bearer, **`admin`**. **Idempotency:** required.

Sets the headroom percentage kept free from **auto**-dispatch. Writes
`orchestra.config.json` (whole-file read-modify-write under a lock, tmp + atomic replace),
touching only the `reserve_percent` key.

Request:

```json
{"percent": 20}
```

- `percent` must be an **integer** 0–95. `20.5` is `422 invalid_percent`.
- **`percent: 0` deletes the key**, after which the `"*"` wildcard default (if configured)
  applies. There is no way to express "explicitly zero" while a wildcard is set; the
  response's `effective` field tells you what actually resolved.

`200`:

```json
{"label": "main", "percent": 20, "effective": 20, "wildcard": 0,
 "reserve_blocked": true, "headroom_percent": 18.0}
```

The file is written **before** `CFG` is mutated, so a failed write leaves memory and disk
consistent.

| Status | When |
|---|---|
| 200 | success |
| 400 / 422 | `bad_request`, `invalid_percent` |
| 401 / 403 | auth (phones never hold `admin`) |
| 404 | `account_not_found` |
| 409 | idempotency conflicts |
| 500 | `internal` — config write failed; the in-memory value is unchanged |

---

### 9.11 `GET /api/v1/sessions/{sid}/messages`

**Auth:** Bearer, `read`. **Headers:** `If-None-Match` honoured.

**Query parameters**

| Param | Type | Default | Meaning |
|---|---|---|---|
| `limit` | int 1–200 | 40 | how many messages |
| `before` | cursor | — | page backwards from this message (exclusive) |
| `after` | cursor | — | page forwards from this message (exclusive) |
| `format` | `clean` \| `raw` | `clean` | see below |

`before` and `after` are mutually exclusive; supplying both is `400 bad_query`.

`200`:

```json
{
  "session_id": "9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
  "account": "account8",
  "worktree_id": "wt_3f9a2b1c7d04",
  "format": "clean",
  "messages": [
    {"cursor": "eyJvIjozNjcwMDE2LCJpIjoyLCJnIjo3NzMxfQ",
     "role": "you", "at": 1784636334.076,
     "text": "keep the 3.11 job, drop 3.10", "truncated": false},
    {"cursor": "eyJvIjozNjcwNDIyLCJpIjowLCJnIjo3NzMxfQ",
     "role": "agent", "at": 1784636341.201,
     "text": "Should I drop the legacy workflow file or keep it behind a flag? Dropping it removes the 3.10 job entirely…",
     "truncated": false}
  ],
  "has_more_before": true,
  "next_before": "eyJvIjozNjcwMDE2LCJpIjoyLCJnIjo3NzMxfQ",
  "next_after": "eyJvIjo0MTk0MzA0LCJpIjoxNCwiZyI6NzczMX0"
}
```

**Cursor:** base64url of `{"o": <byte offset of the JSONL line>, "i": <index within the
line>, "g": <low bits of the file inode>}`. Transcripts are append-only, so offsets remain
valid. There is **no absolute sequence number**: computing one would require reading the
file from byte 0, and these files reach many megabytes.

If a cursor's offset exceeds the file size, or its inode marker does not match, the file was
truncated or rotated → **`410 cursor_invalid`**. Reset the thread; do not silently render
wrong messages.

**`format`:**

| Value | Behaviour |
|---|---|
| `clean` | ANSI stripped, `<tags>` under 80 chars stripped, **all whitespace including newlines collapsed to single spaces**, truncated at 900 chars with `…`. This is what the desktop board shows. Code blocks and lists arrive as one run-on line. |
| `raw` | newlines preserved, 4000-char cap, `truncated` set when clipped. Use this for a phone chat view; `clean` destroys structure irrecoverably. |

`role` is exactly `"you"` | `"agent"`. Tool calls and tool results are not included.
Sidechain and meta entries are filtered, as are slash-command stubs, `Caveat:` prefixes and
`<system-reminder>` machine text.

**Session resolution** does **not** go through the board projection. The server maintains a
full `sid → (account, transcript path)` index built from every Claude home before the
per-card `max_sessions` truncation, with a live filesystem glob as a fallback. A session
that scrolled off its card is still readable — and, critically, its armed auto-resume is
still cancellable.

| Status | When |
|---|---|
| 200 | success |
| 304 | `If-None-Match` matched `W/"<sid>:<total>"` |
| 400 | `bad_query` — `limit` out of range, both `before` and `after`, bad `format` |
| 401 / 403 | auth, lockdown |
| 404 | `session_not_found` — no such sid in any home |
| 410 | `transcript_pruned` (file gone), `cursor_invalid` |
| 429 | rate limited |

---

### 9.12 `POST /api/v1/sessions/{sid}/messages` — send to an agent

**Auth:** Bearer, **`act`**. **Idempotency:** required. **`expect`:** required.

This is the flagship mobile action and the most dangerous call in the API. It types text
into a live agent's terminal. Dispatched agents run `claude --dangerously-skip-permissions`,
so the text is an instruction that will be executed without any approval prompt.

Request:

```json
{
  "text": "-- revert that, keep the flag behind an env var",
  "verify": true,
  "expect": {
    "cursor": "9f2c1a04:4713",
    "agent_id": "ag_7c21f0a9b3de",
    "pid": 41234
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `text` | String | yes | ≤ 4000 chars. Empty after normalisation → `400 empty_message`. |
| `verify` | Bool | no, default `true` | poll the session transcript for the text after delivery |
| `expect` | Object | **yes** | §5 |

`202`:

```
Location: /api/v1/ops/op_2f8a41c009bb7e35
Idempotent-Replay: false
```
```json
{"op": {"id": "op_2f8a41c009bb7e35", "kind": "chat_send", "status": "queued",
        "target": {"session_id": "9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
                   "agent_id": "ag_7c21f0a9b3de", "worktree_id": "wt_3f9a2b1c7d04"},
        "progress": [], "result": null, "error": null,
        "created_at": 1784636742.1, "updated_at": 1784636742.1, "completed_at": null}}
```

Terminal `result` on success:

```json
{"delivered": true, "proven": true, "via": "tmux",
 "delivered_at": 1784636744.8, "pid": 41234,
 "message": "sent — confirmed in the transcript"}
```

`proven: false` with `delivered: true` still settles `succeeded`, with
`"message": "sent via tmux — ⚠ not yet visible in the transcript"`. Honest, not fatal.

**Server obligations** (each one closes a verified bug):

1. **Address by session, never by pid.** The server resolves session → agent from the
   current snapshot. `expect.agent_id` + `expect.pid` must both match, so a phone restored
   from background with a cached board cannot type into a different agent.
2. **Refuse uncertain pairings.** If the session's `agent_certain` is `false`, refuse with
   `409 agent_uncertain` unless the caller passes `"allow_uncertain": true` (which the
   desktop board may, because a human is looking at the card). A "continue" typed at the
   wrong agent is an injected instruction.
3. **Insert a `--` sentinel** before user text in every `tmux send-keys -l` and
   `tmux set-buffer`. Verified on tmux 3.6a: `send-keys -t T -l "-n foo"` exits 1 with
   `command send-keys: unknown flag -n`. Any message whose first token starts with `-`
   fails today.
4. **Use a per-operation tmux buffer**, on the agent's own socket, with the
   `set-buffer` + `paste-buffer` pair held under a global lock. A single shared buffer name
   races across per-agent locks: A sets, B overwrites, A pastes B's instruction into A's
   agent.
5. **Prove receipt** by polling the session `.jsonl` past a recorded offset for a `user`
   entry containing the text. `ok: true` must not mean "tmux accepted keystrokes" — a long
   payload sent via `send-keys -l` can be chopped by the CLI's paste heuristic into
   `[Pasted text #N]` chips that swallow the Enter.
6. **Newlines** survive only on tmux hosts. For AppleScript hosts the server collapses them;
   the client must have disabled multi-line input already via `multiline_ok`.

**Client obligations for notification replies:**

- Write the draft to a shared App Group container **before** any network call.
- Send via a background `URLSession` with `timeoutIntervalForResource = 300` (inside the
  900 s op TTL), never the 7-day default.
- On terminal failure post a local notification carrying the draft:
  *"✗ reply not delivered · ConfidAI-ci-cleanup — tap to retry"*.
- Reconcile the outbox on every foreground against `GET /api/v1/ops/{op_id}`.
- Every notification action that actuates must carry `.authenticationRequired` so a
  pocket-tap cannot type into a live agent from a locked screen.

| Status | When |
|---|---|
| 202 | accepted |
| 200 | idempotent replay of a completed send |
| 400 | `idempotency_key_required`, `expect_required`, `empty_message`, `bad_request` |
| 401 / 403 | auth, lockdown, `demo_mode` |
| 404 | `session_not_found` |
| 409 | `agent_gone`, `agent_moved`, `agent_uncertain`, `agent_busy`, `operation_in_flight`, `operation_indeterminate`, `operation_expired` |
| 422 | `idempotency_key_reused` |
| 429 | rate limited |
| 503 | `state_not_ready`, `ops_saturated` |

---

### 9.13 `POST /api/v1/agents/{ag_id}/messages`

**Auth:** Bearer, **`act`**. **Idempotency:** required. **`expect`:** required.

Identical body and semantics to §9.12, but addressed by terminal rather than by session.
Use it only for a live process that no session claims (`claims: []` in §9.6) — the board
renders these as loose terminal chips. Prefer the session-addressed form everywhere else.

`expect` must carry `agent_id` and `pid`; `session_id` is absent from `target`.

---

### 9.14 `POST /api/v1/agents/{ag_id}/kill`

**Auth:** Bearer, **`act`**. **Idempotency:** required. **`expect`:** required.

New in v1 — the legacy API has no kill of any kind, so a mission dispatched by accident
from a phone could not be stopped from the phone.

Request:

```json
{"reason": "dispatched into the wrong worktree",
 "expect": {"agent_id": "ag_7c21f0a9b3de", "pid": 41234}}
```

Runs `tmux -L <sock> kill-session -t <session>` for tmux-hosted agents. For non-tmux hosts
the op fails with `error.code = "agent_not_killable"` and a message naming the host.

`202` with an op of kind `kill`. Terminal `result`:

```json
{"killed": true, "tmux_session": "mission-confidai-ci-cleanup-121030",
 "message": "killed mission-confidai-ci-cleanup-121030"}
```

| Status | 202, 200 (replay), 400, 401, 403, 404, 409, 422, 429, 503 |
|---|---|

---

### 9.15 `POST /api/v1/agents/{ag_id}/focus`

**Auth:** Bearer, **`act`**. **Idempotency:** required.

Raises the agent's terminal window on the Mac. **A `POST`, not a `GET`, because it has a
real side effect**: for tmux-hosted agents it opens a **brand-new Terminal.app window**
attached to the session on every call. Never prefetch it, never retry it speculatively.

**Exclude this from the iOS surface.** Focusing a window on a Mac you are not looking at is
meaningless from a phone, and any retry spams the desktop. It exists so the board and any
future macOS client share one route table.

Request: `{}`

`200`:

```json
{"ok": true, "message": "opened Terminal attached to mission-confidai-ci-cleanup-121030 (Ctrl-b d to detach)"}
```

| Status | 200, 400, 401, 403, 404 (`agent_not_found`), 429 |
|---|---|

---

### 9.16 `POST /api/v1/dispatches` — launch a mission

**Auth:** Bearer, **`act`**. **Idempotency:** required. **`expect`:** optional.
**Budget:** `dispatch_hour` and `dispatch_day`.

Request:

```json
{
  "mission": "Harden the CI matrix: pin tmux, drop the 3.10 job, and make the tmux tests skip rather than fail on loaded runners. Commit as you go.",
  "worktree_id": null,
  "account": "account8",
  "model": "opus",
  "effort": "xhigh",
  "force_model": false
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `mission` | String | yes | reaches the agent **verbatim**. Empty → `400 bad_request`. |
| `worktree_id` | String \| null | no | `null` = auto-pick the cleanest free worktree |
| `account` | String \| null | no | `null` = auto-pick the account with the most model headroom. An explicit account **bypasses `exclude_accounts`**. |
| `model` | enum | **yes** | `fable` \| `opus` \| `sonnet` \| `haiku`. Routing is deterministic; nothing is guessed. |
| `effort` | enum | **yes** | `high` \| `xhigh` \| `max` \| `ultracode` |
| `force_model` | Bool | no, default `false` | bypass the reserve/headroom check |

`202` — note the target is **already resolved**, even for auto-pick:

```
Location: /api/v1/ops/op_9c3a1b7f20e4d5a1
Idempotent-Replay: false
```
```json
{"op": {
  "id": "op_9c3a1b7f20e4d5a1",
  "kind": "dispatch",
  "status": "queued",
  "target": {"worktree_id": "wt_88bc4d1e0a72", "worktree": "orbital-web",
             "account": "account8", "model": "opus", "effort": "xhigh"},
  "progress": [],
  "result": null,
  "error": null,
  "created_at": 1784636700.1,
  "updated_at": 1784636700.1,
  "completed_at": null,
  "idempotency_key": "8b1e5f2a-3c47-4d19-9e02-71ac5f0b2d38"
}}
```

**Auto-pick resolves synchronously, inside the accept path, under a global pick lock, and
the resolved worktree is reserved for the op's lifetime.** The picker subtracts already
reserved worktrees from the free list, so two concurrent auto-dispatches can never select
the same target — which matters because a new agent takes ~30 s to register as busy.

Echoing the resolved worktree and account back is deliberate: for an auto-pick the user
never chose a worktree, so "the free list changed" is not a decision they can meaningfully
veto after typing a mission. Knowing where it went beats vetoing where it goes. Surface it
in the confirmation: *"launched on orbital-web · [account8]"*.

**Headroom block.** If no account clears the model's reserve and `force_model` is false, the
op settles `failed`:

```json
{"op": {"id": "op_9c3a1b7f20e4d5a1", "kind": "dispatch", "status": "failed",
  "error": {
    "code": "model_headroom_blocked",
    "message": "No opus headroom on any account — best is [work] at 12% left, below its 20% reserve.",
    "detail": {"model": "opus", "can_opus": true, "opus_account": "spare",
               "opus_left": 88, "retry_with": {"force_model": true}},
    "retriable": false}}}
```

The client offers three choices, matching the desktop board: *start with Opus on
`opus_account`* / *⚑ use `model` anyway* / *cancel*. Both re-submissions change the
fingerprint and therefore **need a new `Idempotency-Key`**.

**Progress lines** (§9.19 for the polling shape). Each has a stable machine `code` and the
board's exact prose:

| `code` | `text` |
|---|---|
| `dispatch.picked` | `① picked → orbital-web · [account8] (cleanest free worktree · most model headroom)` |
| `dispatch.tmux_created` | `② creating tmux session mission-orbital-web-121030…` |
| `dispatch.booting` | `③ booting claude…` |
| `dispatch.effort_set` | `④ setting effort xhigh…` |
| `dispatch.effort_confirmed` | `  effort confirmed ✓` |
| `dispatch.effort_unconfirmed` | `  effort UNCONFIRMED ⚠` |
| `dispatch.kickoff_sending` | `⑤ sending kickoff brief…` |
| `dispatch.kickoff_sent` | `  kickoff sent ✓` |
| `dispatch.kickoff_unconfirmed` | `  kickoff UNCONFIRMED ⚠ — attach and press Enter` |
| `dispatch.launched` | `✓ launched` |

Lines beginning with two spaces are sub-lines; render them indented and muted.

Terminal `result` on success:

```json
{
  "worktree_id": "wt_88bc4d1e0a72",
  "worktree": "orbital-web",
  "tmux_session": "mission-orbital-web-121030",
  "account": "account8",
  "model": "opus",
  "effort": "xhigh",
  "effort_confirmed": true,
  "kickoff_sent": true,
  "attach": "tmux -L fleet attach -t mission-orbital-web-121030",
  "message": "launched mission-orbital-web-121030 in orbital-web on [account8] · opus · effort xhigh ✓"
}
```

Wall-clock budget: ~6 s boot + ~3 s effort settle + up to 3 delivery retries ⇒ **10–20 s**
typical. A client-side deadline of 90 s is mandatory.

| Status | When |
|---|---|
| 202 | accepted |
| 200 | idempotent replay |
| 400 | `idempotency_key_required`, `model_and_effort_required`, `bad_request` |
| 401 / 403 | auth, lockdown, `demo_mode` |
| 404 | `worktree_not_found` (explicit `worktree_id`), `account_not_found` |
| 409 | `worktree_busy`, `no_free_worktree`, `limits_not_primed`, `operation_in_flight`, `operation_indeterminate`, `operation_expired` |
| 422 | `invalid_model`, `invalid_effort`, `idempotency_key_reused` |
| 429 | `rate_limited`, `budget_exhausted` |
| 503 | `state_not_ready`, `ops_saturated` |

---

### 9.17 `GET /api/v1/dispatches` — the dispatch log

**Auth:** Bearer, `read`.

| Param | Type | Default | Meaning |
|---|---|---|---|
| `limit` | int 1–100 | 25 | newest first |
| `before` | float epoch | — | page backwards |
| `full` | `0`\|`1` | `0` | include untruncated `mission` and `kickoff` |

The durable record of every dispatch, and **the reconciliation source of truth** when an op
id is lost. After a network timeout, poll this for ~20 s matching on `mission_head` before
offering "retry" — the log line is written only after the tmux session exists (~10–16 s
post-request) and is never written when tmux itself failed.

`200`:

```json
{
  "entries": [
    {
      "at": 1784636713.2,
      "op_id": "op_9c3a1b7f20e4d5a1",
      "tmux_session": "mission-orbital-web-121030",
      "worktree_id": "wt_88bc4d1e0a72",
      "worktree": "orbital-web",
      "account": "account8",
      "model": "opus",
      "effort": "xhigh",
      "closeout": false,
      "alive": true,
      "mission_head": "Harden the CI matrix: pin tmux, drop the 3.10 job, and make the tmux tests skip rather…",
      "mission_chars": 143
    },
    {
      "at": 1784620011.0,
      "op_id": null,
      "tmux_session": "closeout-confidai3-093331",
      "worktree_id": "wt_9911aabb2233",
      "worktree": "ConfidAI3",
      "account": "account8",
      "model": "haiku",
      "effort": null,
      "closeout": true,
      "alive": false,
      "mission_head": "Close out this worktree. Settle background work, commit what matters, land the branch…",
      "mission_chars": 812
    }
  ],
  "has_more": true,
  "next_before": 1784620011.0
}
```

- `at` is a **float epoch**. The legacy log stores a timezone-naive local string with
  second resolution, which cannot be localised on a phone; v1 converts using the server's
  timezone and reports UTC epochs.
- `mission_head` is capped at 120 chars by default. `full=1` adds `mission` and `kickoff`
  untruncated — the legacy endpoint always returns both, which makes a 24-entry response as
  large as the entire board payload, and both contain verbatim user prose.
- `alive` is computed at read time from `tmux -L fleet list-sessions`.
- `op_id` is `null` for entries written before v1 or by a path that had no operation.

| Status | 200, 400, 401, 403, 429 |
|---|---|

---

### 9.18 `POST /api/v1/worktrees/{wid}/finish` — the two-step closeout

**Auth:** Bearer, **`act`**. **Idempotency:** required. **`expect`:** required.

The most subtle endpoint in the API. Read this whole section before implementing either
side.

#### The state machine

```
                       step="brief"                      step="close"
  ┌──────────┐    ────────────────────▶   ┌───────────┐  ──────────────▶  ┌────────┐
  │ ✓ finish │     server types the       │  ✕ close  │   verify + /exit  │  free  │
  │ (step 1) │     closeout brief at      │ (step 2)  │                   │        │
  └──────────┘     the live agent         └───────────┘                   └────────┘
                   closeout_sent_at set     mode:"pending" if the
                   on the card              landing does not verify
```

- The **arm/confirm** interaction is entirely client-side (the desktop board uses a 6 s
  window). The server has no arm concept. iOS must implement its own confirm gate.
- `closeout_sent_at` on the worktree summary is what flips the button from *✓ finish* to
  *✕ close*. It is **process-memory + a small persisted file**, rehydrated at boot only when
  the same agent is still present; otherwise the gate resets to step one, which is the safe
  direction.

#### Request

```json
{
  "step": "brief",
  "expect": {
    "cursor": "9f2c1a04:4713",
    "card_rev": "1cc8e40b",
    "closeout_sent_at": null
  }
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `step` | `"brief"` \| `"close"` | yes | making the intent explicit lets the server refuse a mis-sequenced call rather than silently re-typing a ~600-char brief |
| `expect.card_rev` | String | yes | |
| `expect.closeout_sent_at` | float \| null | yes | mismatch → `409 finish_step_mismatch`, so a phone and a browser cannot fight over the step |

#### Response

`202` with an op of kind `finish`. Terminal `result` always carries `mode`:

```json
{"op": {"id": "op_9c3a1b7f20e4d5a1", "kind": "finish", "status": "succeeded",
  "target": {"worktree_id": "wt_3f9a2b1c7d04", "worktree": "ConfidAI-ci-cleanup"},
  "result": {
    "mode": "brief",
    "ok": true,
    "worktree_id": "wt_3f9a2b1c7d04",
    "closeout_sent_at": 1784636760.4,
    "child_op_id": null,
    "message": "closeout brief sent to the agent — when it reports done, ✕ close verifies the landing and closes the terminal"
  },
  "error": null}}
```

#### `mode` reference (complete)

| `mode` | `ok` | What happened | Next |
|---|---|---|---|
| `brief` | true | a reachable live agent existed, the branch was not landed or the tree was dirty; the full 5-step closeout brief was typed. `closeout_sent_at` is now set. | show *✕ close* |
| `slim` | true | as above but the branch **was** already landed; a shorter brief was typed (settle background work, drop scratch, park on trunk — do **not** re-merge). | show *✕ close* |
| `pending` | **false** | `step: "close"` but the landing still does not verify. `message` names exactly what is unverified and how long ago the brief went out. **The brief is deliberately never re-typed.** | keep *✕ close*, offer chat |
| `exit` | true | landed and clean with a live agent; `/exit` was typed and the terminal closed | card goes free on its own |
| `parked` | true | no agent, landed, clean, but not on trunk; the server ran `git switch <trunk>` + `git pull --ff-only` | done |
| `noop` | true | already landed, clean, and on trunk. Nothing to do. | done |
| `dispatch` | true | no live terminal; a one-shot headless closeout agent was launched (`claude -p --model haiku`). `result.child_op_id` links it. | poll the child op; the card frees itself when the landing verifies |

Example `pending`:

```json
{"mode": "pending", "ok": false,
 "closeout_sent_at": 1784636760.4,
 "message": "can't close yet — 3 leftover file(s). The closeout brief went to the agent 4m ago; if it looks stuck, ✉ chat with it. ✕ close works once the landing verifies."}
```

Failure cases settle the op `failed`:

| `error.code` | Meaning |
|---|---|
| `no_trunk_ref` | no `origin/HEAD`, `origin/main`, `origin/master`, `main` or `master` resolved |
| `agent_unreachable` | a live process exists but its terminal cannot be scripted (Cursor / VS Code embeds). Message: *"finish from that terminal, or close it and ✓ finish again"* |
| `brief_send_failed` | the brief could not be delivered; `closeout_sent_at` is **not** set |

#### Why this is an operation and not a synchronous call

The legacy `POST /api/finish` runs `_base_ref` (~6 s) + `git fetch --quiet origin` (30 s
timeout) + a full process scan **twice** + AppleScript (10 s). It can exceed 60 s — exactly
iOS's default `timeoutIntervalForRequest` — and a client timeout followed by a user retry is
precisely the double-fire scenario. The worktree lock is held for the whole op, so the
`dispatch` mode cannot spawn two headless closeout agents on the same branch.

#### Known layout caveat

For the `<worktree>/repo` layout, the headless closeout tmux session is created one level
**above** the git root, so its self-verification always fails and it falls through to an
interactive rescue session. Such a card does not free itself. Surface `mode: "dispatch"`
with the caveat in the UI rather than promising the card will clear.

| Status | When |
|---|---|
| 202 | accepted |
| 200 | idempotent replay |
| 400 | `idempotency_key_required`, `expect_required`, `bad_request` (bad `step`) |
| 401 / 403 | auth, lockdown, `demo_mode` |
| 404 | `worktree_not_found` |
| 409 | `card_changed`, `finish_step_mismatch`, `worktree_busy`, `operation_in_flight`, `operation_indeterminate`, `operation_expired` |
| 422 | `idempotency_key_reused` |
| 429 | rate limited |
| 503 | `state_not_ready`, `ops_saturated` |

---

### 9.19 Operations — `GET /api/v1/ops`, `GET /api/v1/ops/{op_id}`, `POST /api/v1/ops/{op_id}/cancel`

#### `GET /api/v1/ops`

**Auth:** Bearer, `read`. **Query:** `status` (repeatable, filters), `limit` (1–100,
default 25), `since` (float epoch).

```json
{"ops": [
  {"id": "op_9c3a1b7f20e4d5a1", "kind": "finish", "status": "running",
   "target": {"worktree_id": "wt_3f9a2b1c7d04"}, "progress_n": 2,
   "created_at": 1784636700.1, "updated_at": 1784636742.0, "completed_at": null}
], "has_more": false}
```

#### `GET /api/v1/ops/{op_id}`

**Auth:** Bearer, `read`.

| Param | Type | Default | Meaning |
|---|---|---|---|
| `since_progress` | int | 0 | return only progress entries after this index |
| `wait` | int 0–30 | 0 | long-poll until progress advances or the op settles |

`200`:

```json
{"op": {
  "id": "op_9c3a1b7f20e4d5a1",
  "kind": "dispatch",
  "status": "succeeded",
  "target": {"worktree_id": "wt_88bc4d1e0a72", "worktree": "orbital-web",
             "account": "account8", "model": "opus", "effort": "xhigh"},
  "progress": [
    {"i": 0, "at": 1784636700.2, "level": "info", "code": "dispatch.picked",
     "text": "① picked → orbital-web · [account8] (cleanest free worktree · most model headroom)"},
    {"i": 1, "at": 1784636700.4, "level": "info", "code": "dispatch.tmux_created",
     "text": "② creating tmux session mission-orbital-web-121030…"},
    {"i": 2, "at": 1784636700.5, "level": "info", "code": "dispatch.booting",
     "text": "③ booting claude…"},
    {"i": 3, "at": 1784636706.6, "level": "info", "code": "dispatch.effort_set",
     "text": "④ setting effort xhigh…"},
    {"i": 4, "at": 1784636709.8, "level": "info", "code": "dispatch.effort_confirmed",
     "text": "  effort confirmed ✓"},
    {"i": 5, "at": 1784636709.9, "level": "info", "code": "dispatch.kickoff_sending",
     "text": "⑤ sending kickoff brief…"},
    {"i": 6, "at": 1784636713.1, "level": "info", "code": "dispatch.kickoff_sent",
     "text": "  kickoff sent ✓"},
    {"i": 7, "at": 1784636713.2, "level": "info", "code": "dispatch.launched",
     "text": "✓ launched"}
  ],
  "progress_n": 8,
  "result": {
    "worktree_id": "wt_88bc4d1e0a72", "worktree": "orbital-web",
    "tmux_session": "mission-orbital-web-121030", "account": "account8",
    "model": "opus", "effort": "xhigh", "effort_confirmed": true,
    "kickoff_sent": true,
    "attach": "tmux -L fleet attach -t mission-orbital-web-121030",
    "message": "launched mission-orbital-web-121030 in orbital-web on [account8] · opus · effort xhigh ✓"
  },
  "error": null,
  "created_at": 1784636700.1,
  "updated_at": 1784636713.2,
  "completed_at": 1784636713.2,
  "idempotency_key": "8b1e5f2a-3c47-4d19-9e02-71ac5f0b2d38"
}}
```

- `status` ∈ `queued` | `running` | `succeeded` | `failed` | `cancelled`.
- `level` ∈ `info` | `warn` | `error`.
- Ops are retained **24 h** and appended to `ops.jsonl`, so a lookup falls back to disk when
  memory has evicted the record. Only after both miss is `404 op_not_found` correct — and it
  then genuinely means "never existed", not "lost".
- **Boot reconciliation:** an op reloaded in `queued`/`running` has no worker behind it, so
  the server settles it `failed` with `error.code = "interrupted_by_restart"` and the message
  *"orchestra restarted while this was running — check the fleet before retrying; a mission
  may already be live"*. Its idempotency key then resolves to
  `409 operation_indeterminate`, never a silent re-execution.
- Ops are also mirrored into the delta address space at `j/<op_id>`, so a streaming client
  gets progress without polling this route at all.

`410 op_expired` means it existed and aged out. `404 op_not_found` means it never did.

#### `POST /api/v1/ops/{op_id}/cancel`

**Auth:** Bearer, `act`. **Idempotency:** required.

Best-effort. Only `queued` ops can be cancelled cleanly; a `running` op that has already
created a tmux session cannot be un-created, so cancel does **not** kill the agent — use
§9.14 for that.

Request: `{}`

`200`:

```json
{"op": {"id": "op_9c3a1b7f20e4d5a1", "kind": "dispatch", "status": "cancelled",
        "error": {"code": "cancelled", "message": "cancelled before it started",
                  "detail": {}, "retriable": false},
        "completed_at": 1784636701.0}}
```

`409 op_not_cancellable` when the op is already running past the point of no return or has
settled; `detail.status` says which.

| Status | 200, 400, 401, 403, 404, 409, 410, 429 |
|---|---|

---

### 9.20 `PUT /api/v1/sessions/{sid}/resume` — arm auto-resume

**Auth:** Bearer, **`act`**. **Idempotency:** required. Synchronous.

Arms a timer that, at the given moment, re-checks the limit and then types the configured
resume message (default `"continue"`) into **that session's own** terminal — never another
session's, because an unattended message typed at the wrong agent is an injected
instruction. If no terminal can be scripted, the conversation is reopened in a fleet tmux
session via `claude --resume` and resumed there.

Request — supply exactly one of `delay_s` or `due_at`:

```json
{"delay_s": 60}
```
```json
{"due_at": 1784646059.5}
```

| Field | Type | Notes |
|---|---|---|
| `delay_s` | number | seconds **after the known reset**, clamped to 0–86400. Default is the server's `resume_delay_s` (60). |
| `due_at` | float epoch | absolute. Floored to `now + 5`. |
| `model` | String \| null | recorded for display; optional |
| `worktree_id` | String | optional disambiguator when a sid resolves ambiguously |

`200`:

```json
{
  "resume": {
    "id": "wt_9911aabb2233|4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
    "worktree_id": "wt_9911aabb2233",
    "worktree": "ConfidAI3",
    "session_id": "4f2c88e1-7a30-4c19-9e88-1d2b3c4d5e6f",
    "account": "account8",
    "model": "fable-5",
    "status": "pending",
    "delay_s": 60.0,
    "resets_at": 1784645999.517,
    "due_at": 1784646059.517,
    "created_at": 1784636800.0,
    "attempts": 0,
    "firing_since": null,
    "fired_at": null,
    "message": null
  },
  "message": "auto-resume armed for 14:32"
}
```

**`422 resume_time_unknown`** when neither `due_at` was supplied nor a reset time is known
for the account:

```json
{"error": {"code": "resume_time_unknown",
           "message": "no known reset time for this limit — pick an exact time",
           "detail": {"reason": "limit_object_null", "account": "account8"},
           "retriable": false}}
```

`detail.reason` ∈ `limit_object_null` | `limits_not_primed` | `account_unknown`. The client
opens an exact-time picker and re-submits with `due_at` **and a new `Idempotency-Key`**.

**`firing_since`** is set at the top of the firing routine, before an up-to-90 s limits
refetch. A single fire can block for many minutes (limit recheck + waiting for the composer
to go idle + up to three delivery attempts), during which the schedule would otherwise still
read `pending` with a past `due_at` and the client could not distinguish "armed" from
"firing right now for the last nine minutes". Render `firing_since != null` as *▶
resuming…*, not as a countdown stuck at zero.

Re-arming an existing schedule **overwrites it in place** and resets `attempts`; this is
safe and idempotent by construction. The server gives up after 10 re-arms with
`status: "failed"` and `message: "still limited after 10 checks — gave up"`.

| Status | When |
|---|---|
| 200 | armed or re-armed |
| 400 | `bad_request` (both or neither of `delay_s`/`due_at`, non-numeric) |
| 401 / 403 | auth, lockdown, `demo_mode` |
| 404 | `session_not_found` |
| 409 | `resume_firing` — a fire is in progress; wait for it to settle |
| 422 | `resume_time_unknown`, `idempotency_key_reused` |
| 429 | rate limited |

---

### 9.21 `DELETE /api/v1/sessions/{sid}/resume`

**Auth:** Bearer, **`act`**. **Idempotency:** required.

`200`:

```json
{"cancelled": true, "message": "auto-resume disarmed"}
```

`404` when nothing is armed:

```json
{"error": {"code": "resume_not_armed", "message": "nothing armed for this session",
           "detail": {}, "retriable": false}}
```

**`409 resume_firing`** when the schedule is already executing. Cancel is not an abort: the
side effect (typing the message, or spawning a `claude --resume` tmux session) will still
happen and would never be reported, so refusing is the honest answer.

| Status | 200, 400, 401, 403, 404, 409, 429 |
|---|---|

---

### 9.22 Events — `GET /api/v1/events`, `/api/v1/events/{id}`, `/api/v1/events/open`

**Auth:** Bearer, `read`.

The durable side of push. Push is lossy by construction — collapse supersedes, expiration
discards, quiet hours holds, budgets defer, an offline device receives only the most recent
notification — so a client must be able to reconcile what it missed. Fetch on every
foreground and on every notification tap.

#### `GET /api/v1/events`

| Param | Type | Default | Meaning |
|---|---|---|---|
| `since` | event id | — | exclusive; omit for the newest page |
| `limit` | int 1–200 | 50 | |
| `wait` | int 0–30 | 0 | long-poll for new events |

`200`:

```json
{
  "epoch": "9f2c1a04",
  "events": [
    {
      "id": "evt-000431",
      "at": 1784636700.1,
      "type": "session.needs_answer",
      "level": "P1",
      "worktree_id": "wt_3f9a2b1c7d04",
      "worktree": "ConfidAI-ci-cleanup",
      "session_id": "9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520",
      "agent_id": "ag_7c21f0a9b3de",
      "account": "account8",
      "model": "opus-4-8",
      "dedupe_key": "session.needs_answer|9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520|3",
      "title": "▲ needs you · ConfidAI-ci-cleanup",
      "body": "opus asked a question — \"should I bump the minor or the major?\"",
      "counts": {"needs_input": 2, "blocked": 0, "limit": 3, "working": 3},
      "open": true,
      "delivery": "delivered"
    },
    {
      "id": "evt-000432",
      "at": 1784636714.3,
      "type": "dispatch.succeeded",
      "level": "P3",
      "worktree_id": "wt_88bc4d1e0a72",
      "worktree": "orbital-web",
      "op_id": "op_9c3a1b7f20e4d5a1",
      "dedupe_key": "dispatch.succeeded|op_9c3a1b7f20e4d5a1",
      "title": "⌁ launched · orbital-web",
      "body": "opus · effort xhigh · kickoff delivered ✓",
      "open": false,
      "delivery": "delivered"
    }
  ],
  "next_since": "evt-000432",
  "reset": false,
  "server_started_at": 1784630001.2
}
```

- Event ids are `evt-%06d` from a **persisted** monotonic sequence, so `since` is a total
  order and a restart never reuses an id.
- **`reset: true`** with an empty `events` array means the epoch changed or `since` is older
  than the retained window (500 entries / 24 h). Resync from state; do not attempt a replay.
- `delivery` ∈ `delivered` | `queued` | `held` (quiet hours or budget) | `unknown`
  (delivery outcome could not be determined) | `suppressed` (muted or snoozed) | `none`
  (no push sink configured).

#### `GET /api/v1/events/{id}`

Returns a single event envelope, unwrapped:

```json
{"id": "evt-000431", "at": 1784636700.1, "type": "session.needs_answer", "...": "..."}
```

This is the route the iOS Notification Service Extension calls to enrich a notification body.
Give it a **3 s client timeout** and fall back to the structural body — not 30 s, or a
sleeping tunnel delays every banner by half a minute.

`404 event_not_found` when the id is unknown or has aged out.

#### `GET /api/v1/events/open`

The reconcile route.

```json
{"open": ["session.needs_answer|9d4db7b2-3e1f-4a7c-b0d2-8f11ac9e5520|3",
          "session.blocked|1a2b3c4d-…|1"],
 "as_of": 1784636700.0,
 "badge": 2}
```

Every attention rule has a resolution half. On foreground, withdraw every delivered
notification whose `request.identifier` (which the client must set to the `dedupe_key`) is
absent from `open`, and call `setBadgeCount(badge)`.

Without this, a question answered at the Mac leaves *"▲ needs you"* on the lock screen
forever, and on a 9-worktree fleet Notification Center becomes a graveyard within a day. A
surface where nothing you see is necessarily still true is worse than no surface.

| Status | 200, 400, 401, 403, 404, 429 |
|---|---|

---

### 9.23 Push registration and preferences

#### `POST /api/v1/devices/self/push`

**Auth:** Bearer, **`read`** (deliberately — see below). **Idempotency:** required.

Registering *your own* push endpoint is not an admin operation; escalating your own scope
would be. Scoping this to `admin` would make push structurally impossible on a phone,
silently and permanently, because APNs tokens rotate on reinstall, on restore, and
occasionally on OS update.

The server writes **only** the calling device's `push` sub-object, and only these keys.

Request:

```json
{
  "backend": "apns",
  "token": "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00",
  "environment": "production",
  "topic": "com.acrdlph.orchestra",
  "app_version": "1.0 (14)",
  "tz": "America/Los_Angeles",
  "tz_offset_min": -420,
  "settings": {
    "authorization_status": "authorized",
    "time_sensitive_allowed": true,
    "critical_allowed": false,
    "alert": true, "sound": true, "badge": true,
    "low_power_mode": false
  }
}
```

| Field | Notes |
|---|---|
| `backend` | `apns` \| `ntfy` |
| `token` | APNs device token, 64–200 hex. `422 push_token_invalid` otherwise. |
| `environment` | `production` \| `sandbox`. **Read from the embedded provisioning profile's `aps-environment` at runtime**, never from `#if DEBUG` — TestFlight builds are `DEBUG=0`, and this is historically the single most common cause of "push just doesn't work". |
| `tz` | **IANA identifier**, not an offset. Quiet hours are evaluated in the phone's zone, and a fixed offset is wrong at every DST transition. Re-send on every foreground; iOS gives no background callback for timezone change. |
| `settings` | the app's resolved `UNNotificationSettings`, so the server can warn instead of pushing into a void |

`200`:

```json
{"ok": true, "backend": "apns", "environment": "production",
 "warnings": ["time_sensitive_allowed is false — P1 alerts will be suppressed by any Focus mode, including Sleep"]}
```

The server auto-heals a wrong environment: on APNs `400 BadDeviceToken` it retries once
against the other host and persists the corrected value.

Re-send this on **every app launch**. It is cheap and idempotent, and it is the only thing
that keeps a rotated token working.

#### `POST /api/v1/devices/self/settings`

**Auth:** Bearer, `read`. **Idempotency:** required.

Per-device notification preferences.

```json
{
  "quiet_hours": {"enabled": true, "from": "23:00", "to": "08:00", "allow_p1": false},
  "rules": {
    "session.needs_answer": true,
    "session.blocked": true,
    "session.your_turn": true,
    "session.idle_nudge": true,
    "session.died": true,
    "account.limit_hit": true,
    "account.limit_reset": true,
    "resume.armed": false,
    "resume.fired": true,
    "resume.failed": true,
    "dispatch.succeeded": true,
    "dispatch.failed": true,
    "dispatch.stalled": true,
    "finish.landed": true,
    "worktree.free": false,
    "session.unstable": false
  },
  "privacy": "structural",
  "nudge_min": 15
}
```

`privacy` ∈ `structural` (default — glyph, worktree, status, account; **no transcript text,
no mission prose**) | `detail` (full copy in the payload) | `silent_fetch` (structural body
plus `mutable-content`, with the extension fetching the detail over the tailnet).

`200` echoes the stored preferences.

> **Honest residual for `silent_fetch`:** it removes transcript text and mission prose. It
> does not remove `apns-collapse-id`, `thread-id`, the entity block or the category — and
> worktree names are typically client or product names. Repository identity and timing
> metadata still transit Apple. If that matters, hash worktree names with a per-install salt
> and resolve display names from the client's local cache.

#### `POST /api/v1/push/test`

**Auth:** Bearer, `read`. **Idempotency:** required.

```json
{}
```

`200`:

```json
{"ok": true, "backend": "apns", "status": 200,
 "apns_id": "B1C2D3E4-5F60-7182-93A4-B5C6D7E8F901", "reason": null}
```

or

```json
{"ok": false, "backend": "apns", "status": 403,
 "apns_id": "B1C2D3E4-…", "reason": "InvalidProviderToken"}
```

Returns the real transport status and the `apns-id`. Debugging push without an `apns-id` is
guesswork. Surface "last push delivered: 4m ago (200)" and this button in the app's settings
screen — the failure this replaces is *push silently stopped after a restore, the app looks
fine in the foreground, and there are at least two independent causes with no diagnostic*.

#### `POST /api/v1/push/mute` and `POST /api/v1/push/snooze`

**Auth:** Bearer, `read`. **Idempotency:** required.

```json
{"minutes": 120}
```
```json
{"key": "wt_3f9a2b1c7d04", "minutes": 15}
```

`200`: `{"muted_until": 1784643900.0}` / `{"key": "wt_3f9a2b1c7d04", "snoozed_until": 1784637600.0}`

`minutes: 0` clears. The one control a user reaches for at 1am — *make it stop, now, from
the phone* — must exist, and must also be a snooze action on every notification category.

| Status (all push routes) | 200, 400, 401, 403, 422, 429 |
|---|---|

---

### 9.24 Device management

#### `GET /api/v1/devices`

**Auth:** Bearer, **`admin`**.

```json
{
  "devices": [
    {"id": "9f3ab21c", "label": "Achill's iPhone", "platform": "ios",
     "app_version": "1.0 (14)", "created_at": 1784636692.6,
     "paired_from": "100.101.44.9", "paired_login": "achillr@gmail.com",
     "last_seen": 1784640001.2, "last_ip": "100.101.44.9", "last_scope": "read",
     "scopes": ["read", "act"],
     "push": {"backend": "apns", "environment": "production",
              "registered_at": 1784636700.0, "last_push_at": 1784636400.0,
              "last_push_code": "200"},
     "act_reissue_pending": false,
     "revoked_at": null, "revoked_reason": null},
    {"id": "c7e10b93", "label": "old iPhone", "platform": "ios",
     "app_version": "0.9 (3)", "created_at": 1783600000.0,
     "paired_from": "100.101.44.31", "paired_login": "achillr@gmail.com",
     "last_seen": 1783990000.0, "last_ip": "100.101.44.31", "last_scope": "read",
     "scopes": [], "push": null, "act_reissue_pending": false,
     "revoked_at": 1784380000.0, "revoked_reason": "lost"}
  ],
  "lockdown_until": 0
}
```

Token hashes are never returned.

#### `POST /api/v1/devices/{id}/revoke`

**Auth:** `admin`. **Idempotency:** required.

```json
{"reason": "left it in a taxi"}
```

`200`: `{"revoked": true, "id": "c7e10b93", "message": "revoked old iPhone"}`

Burns both token hashes and the push registration immediately. Takes effect within one
second even if the revoke was performed by a separate CLI process — the registry file is the
authority and the server stats its mtime.

**Dormancy:** any device unseen for 30 days is auto-revoked with
`revoked_reason: "dormant"`. A phone lost and not noticed would otherwise read transcripts
forever.

#### `POST /api/v1/devices/self/reissue-act`

**Auth:** Bearer, **`read`** — authenticated by the surviving read token.

`.biometryCurrentSet` invalidates on Face ID re-enrolment or on adding an Alternate
Appearance (the standard fix for glasses). Without a remote re-issue path, a user who does
that at an airport has a read-only app until they physically return to their Mac and scan a
QR inside 120 s.

Request: `{}`

`200`: `{"pending": true, "message": "approve this on your Mac's board to restore acting"}`

Returns **no token**. Rate-limited to 3/day per device. The pending state appears in
`GET /api/v1/meta` (`device.act_reissue_pending`) so the app can say "waiting for approval
on your Mac" instead of showing a dead button.

#### `POST /api/v1/devices/{id}/approve-act`

**Auth:** `admin`. **Idempotency:** required.

`200` — the new token, returned **once**:

```json
{"id": "9f3ab21c",
 "token": "orc1_9f3ab21c_zR8kM3nQ7vT1yB5cX9wL2pF6hJ0sA4dG8eU1iO5qK7b",
 "scope": "act"}
```

The board renders it as a QR carrying only the token; the phone is already paired and
pinned. The approval **is** the security property: a read token alone can never escalate.

`409 nothing_pending` when no re-issue was requested.

#### `POST /api/v1/devices/lockdown` and `/unlock`

**Auth:** `admin`. **Idempotency:** required.

```json
{"minutes": 60}
```

`200`: `{"lockdown_until": 1784640300.0}` / `{"lockdown_until": 0}`

While locked down, paired devices may reach `GET /api/v1/health` and `GET /api/v1/state`
and nothing else — no chat, no dispatch log, no acting. The desktop board is unaffected.

`lockdown_until` is always a **unix timestamp**, never a formatted wall-clock string; the
phone may be in a different timezone from the Mac.

| Status (device routes) | 200, 400, 401, 403, 404, 409, 422, 429 |
|---|---|

---

## 10. Enum reference

### 10.1 `session.status`

| Value | Meaning | Client fallback |
|---|---|---|
| `working` | the transcript (or a background subagent's) was written within the working window, **or** the agent is provably mid-turn | — |
| `needs_input` | a live process has an `AskUserQuestion` pending. **The highest-priority state.** | — |
| `blocked` | a pending tool call with no permission bypass — usually an approval prompt | — |
| `waiting` | the turn finished; the agent is idle at the prompt | — |
| `limit` | parked on an exhausted account. Overlaid on top of `needs_input`/`blocked`/`waiting`. | — |
| `ended` | no live process and the transcript is quiet | — |
| `unknown` | the process scan failed, so liveness could not be determined. **Never render as ENDED.** | — |
| *anything else* | a future value | render as `waiting` |

Board colours, for parity: `working` green, `needs_input` terracotta, `blocked` terracotta
border with amber text, `waiting` soft amber, `limit` yellow, `ended` muted at 55 % opacity.

### 10.2 `worktree.availability`

| Value | Meaning |
|---|---|
| `free` | no live process and nothing recently working. **There is no button that frees a worktree** — free is a state the board observes, not one you set. |
| `attention` | any `needs_input` or `blocked`, **or** any `waiting` with nothing working |
| `waiting` | a `limit` session is present with nothing working. Deliberately *not* "needs you". |
| `busy` | something is working |
| *anything else* | render as `busy` |

`severity` is the server's card sort key: `0` needs_input, `1` blocked, `2` waiting without
working, `3` working, `4` limit, `5` everything else. Sessions carrying `handed_to` are
excluded from this computation.

### 10.3 `session.flags`

`tool_running`, `bg_shell`, `subagents_active`, `pending_workflows`, `pending_bg_agents`,
`pending_bg_tools`. Always an array; absence means the flag is false. `pending_bg_tools` counts
background work the agent launched itself — a backgrounded Bash, a Workflow, an async Agent —
that has not yet reported back through a `<task-notification>`; the CLI's own two counts do not
see it, and it resolves its own `tool_use` immediately so `pending_tools` does not either. Any of these set means the agent is
genuinely busy even when the transcript has gone quiet — treat it as a veto on "your turn".

`turn_ended` is the one flag that argues the other way, and it is strictly weaker than all of
them: it means the CLI wrote its own end-of-turn marker after the agent's last word, so this
session's `waiting` was **observed** rather than decayed out of a timer (84 % of in-window
sessions carry it). It is already vetoed server-side by every flag above — the server never
emits `waiting` alongside them — so a client must not re-derive status from it. Its only client
use is presentation: "◆ YOUR TURN" on evidence versus on a guess.

### 10.4 `agent.kind`

`tmux`, `terminal`, `iterm`, `editor`, `other`. Only `tmux`, `terminal` and `iterm` can be
`reachable: true`. Only `tmux` has `multiline_ok: true`.

### 10.5 `model` and `effort` (dispatch inputs)

`model` ∈ `fable` | `opus` | `sonnet` | `haiku`.
`effort` ∈ `high` (simple task) | `xhigh` (research / medium) | `max` (hard feature) |
`ultracode` (hard feature, long-running).

**`session.model` in responses is a different thing entirely.** It is a raw API model id
with a `claude-` prefix stripped, and it is **not an enum**: observed values include
`opus-4-8`, `fable-5`, `haiku-4-5-20251001` (a full dated id) and the **empty string** when
no assistant turn was in the tail window. Never switch on it; render it verbatim, falling
back to `?`.

### 10.6 `op.kind` and `op.status`

`kind` ∈ `dispatch` | `finish` | `chat_send` | `kill` | `limits_refresh`.
`status` ∈ `queued` | `running` | `succeeded` | `failed` | `cancelled`.

### 10.7 `finish.mode`

`brief` | `slim` | `pending` | `exit` | `parked` | `noop` | `dispatch`. See §9.18.

### 10.8 `resume.status`

`pending` | `done` | `failed`. With `firing_since != null`, a `pending` schedule is
currently executing — render *resuming…*, not a countdown.

### 10.9 `event.type` and `event.level`

Levels: **P1** interrupts (`interruption-level: time-sensitive`, `apns-priority: 10`),
**P2** delivers actively, **P3** quiet (no sound, priority 5), **P4** passive.

| `type` | Level | Default | Fires when |
|---|---|---|---|
| `session.needs_answer` | P1 | on | a session enters `needs_input` |
| `session.blocked` | P1 | on | enters `blocked` **and** that session's own process has no permission bypass |
| `session.your_turn` | P2 | on | enters `waiting` with no busy flags and with account limits known and fresh |
| `session.idle_nudge` | P2 | on | a fired `your_turn` is still unacknowledged after `nudge_min` |
| `session.died` | P2 | on | was working/needs_input/blocked ≤ 300 s ago, now ended or absent, **and** the branch is dirty or unlanded |
| `account.limit_hit` | P2 | on | an account becomes exhausted, or a session enters `limit` **without `handed_to`** |
| `account.limit_reset` | P3 | on | an account stops being exhausted |
| `resume.armed` | P4 | **off** | a schedule appears |
| `resume.fired` | P2 | on | a schedule reaches `done` |
| `resume.failed` | P1 | on | a schedule reaches `failed` |
| `dispatch.succeeded` | P3 | on | a dispatch op settles ok |
| `dispatch.failed` | P1 | on | a dispatch op settles not-ok |
| `dispatch.stalled` | P2 | on | a dispatch op has not settled 240 s after start |
| `finish.landed` | P3 | on | `closeout_sent_at` clears **and** the card goes free |
| `worktree.free` | P4 | **off** | a card goes free without a closeout |
| `session.unstable` | P4 | off | the flap damper engaged for an entity |
| *anything else* | P3 | on | unknown type — render generically, never drop |

**`handed_to` suppresses `account.limit_hit`.** A handed-off limit means the work already
continued elsewhere; alerting on it is a false positive by construction and a channel that
cries wolf gets muted, taking the real alerts with it.

### 10.10 `error.code`

See §8.3.

### 10.11 `resync.reason` and `bye.reason`

`resync.reason` ∈ `cursor_too_old` | `epoch_changed` | `history_empty` | `slow_consumer` |
`digest_mismatch` | `unaddressable`.
`bye.reason` ∈ `max_age` | `evicted` | `shutdown`.

---

## 11. Cold start — the exact client sequence

```
1.  HEAD /api/v1/health          (fire immediately on scenePhase == .active,
                                  concurrently with app launch, to warm the tunnel)
2.  render the on-disk snapshot at once, marked "data as of 14m ago",
    with actuating controls disabled and a determinate "reconnecting…" affordance
3.  GET  /api/v1/health          → version skew, cert expiry, state_ready
4.  GET  /api/v1/state           → full snapshot; take `cursor` from the ENVELOPE
5.  GET  /api/v1/stream?since=<that cursor>&sub=<install id>
       ← the stream replays from that cursor, so nothing that happened between
         step 4 completing and step 5 opening is lost
6.  GET  /api/v1/events/open     → withdraw stale notifications, fix the badge
7.  POST /api/v1/devices/self/push  → re-register the (possibly rotated) APNs token
8.  GET  /api/v1/meta            → budgets, features, min_client_build
```

**Budget: ~2 s p50, ~5 s p95 on cellular after a long gap.** You pay TCP SYN/SYN-ACK plus
request/response (2 RTT minimum), the packet tunnel is frequently not resident so the first
connection triggers on-demand bring-up, and on carrier CGNAT the path is commonly
DERP-relayed at 100–300 ms per RTT. Step 2 is what makes that survivable.

**`GET /api/v1/limits` is not in the critical path** — v1 keeps the limits cache warm on a
dedicated thread, so `status: "limit"` appears in state without the client priming anything.
(In the legacy API it does not: the state collector reads a cache it never fills, so a
client that never calls `/api/limits` sees limit-stuck agents as `waiting` forever. That
cold-start dependency is gone in v1.)

---

## 12. Worked flows

### 12.1 Answer a question from the lock screen

```
push arrives  → category ORCH_NEEDS_ANSWER, payload carries session_id, agent_id,
                worktree_id and counts
user types    → UNTextInputNotificationAction, .authenticationRequired
app (bg)      → write the draft to the App Group container FIRST
              → GET  /api/v1/state?since=<cached cursor>        (refresh agent_id + pid)
              → POST /api/v1/sessions/{sid}/messages
                     Idempotency-Key: <uuid minted at commit>
                     Idempotency-Issued-At: <skew-corrected now>
                     {"text":"1", "verify":true,
                      "expect":{"agent_id":"ag_…","pid":41234,"cursor":"…"}}
              ← 202 {"op":{"id":"op_…"}}
              → GET  /api/v1/ops/op_…?wait=20
              ← result.proven == true   → clear the outbox entry
                result.proven == false  → keep it, show "sent, not yet confirmed"
                409 agent_moved         → local notification: "that terminal moved —
                                          open orchestra", keep the draft
```

### 12.2 Dispatch a mission with a headroom conflict

```
POST /api/v1/dispatches  Idempotency-Key: K1   {model:"opus", effort:"xhigh", …}
  ← 202 op_A
GET  /api/v1/ops/op_A?wait=30
  ← failed, error.code = model_headroom_blocked, detail.can_opus = true,
    detail.opus_account = "spare", detail.opus_left = 88

user taps "⚑ use opus anyway"
POST /api/v1/dispatches  Idempotency-Key: K2   {..., force_model:true}   ← NEW KEY
  ← 202 op_B
GET  /api/v1/ops/op_B?wait=30   (or just watch j/op_B on the stream)
  ← succeeded, result.message = "launched mission-orbital-web-121030 …"
```

### 12.3 Finish, both steps, including the refusal

```
tap 1 (arm, client-side only, 6 s window)
tap 2 → POST /api/v1/worktrees/wt_3f9a2b1c7d04/finish
          Idempotency-Key: K3
          {"step":"brief","expect":{"card_rev":"1cc8e40b","closeout_sent_at":null}}
        ← 202 op_C → succeeded, mode "brief", closeout_sent_at 1784636760.4
        button becomes "✕ close"

…four minutes pass, the agent is still tidying…

tap → POST …/finish  Idempotency-Key: K4
        {"step":"close","expect":{"card_rev":"9a20fe31",
                                  "closeout_sent_at":1784636760.4}}
      ← 202 op_D → succeeded, mode "pending", ok false
        "can't close yet — 3 leftover file(s). The closeout brief went to the agent
         4m ago; if it looks stuck, ✉ chat with it."
        button stays "✕ close"

…the agent finishes…

tap → POST …/finish  Idempotency-Key: K5  {"step":"close", …}
      ← 202 op_E → succeeded, mode "exit"
        "already landed — sent /exit to close the terminal"
        the card goes ◇ free on the next tick, by itself
```

### 12.4 Recovering a lost operation after a restart

```
POST /api/v1/dispatches  Idempotency-Key: K6   → client timeout, no response
retry, same key                                 → 409 operation_indeterminate
                                                   "the server restarted while this was
                                                    running — check the fleet before
                                                    retrying; a mission may already be live"

DO NOT retry. Instead:
GET /api/v1/dispatches?limit=10
  poll for ~20 s, matching entries on mission_head
  found  → the mission launched; adopt entry.op_id and entry.tmux_session
  absent after 20 s → nothing started; a NEW Idempotency-Key may be used
```

---

## 13. Server implementation checklist

Non-negotiable, in the order they must land:

1. `protocol_version = "HTTP/1.1"` **and** a socket timeout on the handler. Keep-alive with
   `timeout = None` pins a thread per idle connection forever, with no diagnostic because
   access logging is disabled.
2. A bounded worker pool (64 slots) that returns `503` past the cap, plus `Connection: close`
   on every error response and an unconditional body drain before any error is written.
3. `411` on `Transfer-Encoding`; a hard 1 MiB body cap; reject negative `Content-Length`
   (`read(-1)` reads until EOF and hangs).
4. `do_HEAD`, `do_PUT`, `do_DELETE`; `do_OPTIONS` → 405 with no CORS headers.
5. Exact `(METHOD, path)` routing. No `startswith`. Today `POST /api/dispatchlog` reaches
   the dispatch handler because `"/api/dispatchlog".startswith("/api/dispatch")`.
6. `urllib.parse` for every query parameter, with typed accessors. Today `/api/chat`'s
   `account` is scraped by regex and never percent-decoded.
7. gzip on JSON over 1 KiB; **never** on `text/event-stream`.
8. `try/except` around the whole request dispatcher and around every operation worker. A
   `POST` with `{"pid": "abc"}` currently raises an uncaught `ValueError`, prints a traceback
   and drops the connection.
9. One background collector thread. **No collector may run on a request thread.** Today
   there is no lock at all, so N clients missing the cache each fork ~36 git subprocesses.
10. Absolute timestamps everywhere: `activity_at`, `resets_at`, `first_seen_at`,
    `started_at`, `uptime_s`. Never a value derived from "now" inside anything that gets
    diffed.
11. `time.monotonic()` for every duration; `time.time()` only for values on the wire. On
    wake, re-baseline without emitting.
12. Atomic `tmp + os.replace + fsync` for `resume.schedule.json`, `push.state.json`,
    `devices.json`, `ops.jsonl` rotation and `orchestra.config.json`. Today
    `save_resumes()` truncates then writes and swallows `OSError`, so a crash mid-write
    silently loses every armed schedule.
13. `--` sentinel before user text in every `tmux send-keys -l` and `tmux set-buffer`.
14. Per-operation tmux buffer names, on the agent's own socket, under a global buffer lock.
15. Non-blocking per-worktree and per-agent locks that 409 rather than queue.
16. Write-ahead idempotency reservations tagged with `boot_id`; boot reconciliation settling
    orphaned ops as `interrupted_by_restart`.
17. A complete `sid → (account, path)` index built before per-card truncation, persisted,
    with a live glob fallback.
18. Failed limits accounts emitted with `ok: false, known: false`, never dropped.
19. `firing_since` on resume schedules.
20. Per-session `skip_perms_own` alongside the existing per-worktree flag.

Everything stays stdlib-only. The two external binaries the push path needs (`curl` with
HTTP/2, `openssl`) are OS-provided, are probed with `shutil.which` at registration, and
degrade to the ntfy backend when absent — the same category as the existing `git`, `ps`,
`lsof`, `tmux` and `osascript` shell-outs.

---

## 14. Swift client checklist

1. **One `URLSession` with the pinning delegate.** Ban `URLSession.shared` with a lint rule —
   it has no delegate and cannot accept the self-signed certificate, and any accidental use
   hard-fails with an opaque `NSURLErrorServerCertificateUntrusted`.
2. A **separate** session for the stream, with `timeoutIntervalForRequest ≥ 3 × hb`.
3. **Never** `httpBodyStream`. Always `httpBody`.
4. `Codable` with `decodeIfPresent` and defaults everywhere. Conditional keys are **absent**,
   not null. Unknown enum cases map to the documented fallback; unknown object keys are
   ignored.
5. **Never compute `Date() - age_s`.** v1 has no `age_s`; use `activity_at` and the skew
   correction.
6. `Idempotency-Key` minted at user-commit and reused across retries. Never regenerated on
   retry; always regenerated when the request body changes.
7. Never auto-retry a 409. Never retry a 4xx except a `retriable: true` 409.
8. `card_rev` captured at press-down, plus a 700 ms geometric shield against mis-targeting
   after a re-sort. Both, not either.
9. Freshness split into connection liveness and data recency (§6.2). Ages tick while stale;
   statuses dim.
10. One `LAContext` for the app session with
    `touchIDAuthenticationAllowableReuseDuration`, invalidated on background. Prompt
    unconditionally for dispatch and finish; ride the reuse window for send and resume.
    Never re-enter the gate on a retry.
11. Reply drafts persisted to the App Group **before** any network call.
12. Bearer token in the Keychain with `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly` in
    a shared access group — otherwise a lock-screen reply and the notification extension
    both fail silently, with no server-side error because nothing arrives.
13. Request **`.provisional`** notification authorization on first launch, so a first-run
    denial cannot permanently gut the premise.
14. Reachability ladder: Tailscale down / Mac asleep / orchestra down / pin mismatch / server
    too old are five distinct states with five distinct actions. Do not show one spinner for
    all of them.

---

## 15. Demo mode

`--demo` serves synthetic data. Every actuating route returns `403 demo_mode` with the
existing prose (`"demo mode — no live agents to talk to"`, `"demo mode — nothing to
finish"`, `"demo mode — dispatch disabled"`, `"demo mode — nothing to schedule"`), and
`meta.mode` / `health.mode` is `"demo"`.

**Contract:** demo payloads have **exactly the same key sets at every level** as live
payloads, and a CI test asserts it. (The legacy demo state omits five session fields that
real state always has, adds a bogus `git_root`, and omits `tty`/`host` from other
processes — so a `Codable` layer validated only against `--demo` crashes on real data and
vice versa.)

Demo mode also sandboxes the dispatch log and chat, which the legacy `--demo` does not:
those read real files today and are unsafe for screenshots.

---

## 16. Legacy endpoints

### 16.1 Compatibility guarantee

> The unversioned `/api/*` paths are **frozen**. Their response **bodies** are byte-stable:
> no new fields, no removed fields, no retyped fields, no new unversioned endpoints. They
> exist so the bundled HTML board keeps working without a redeploy, and they are computed
> from the same snapshot v1 serves, so the two can never disagree.
>
> The freeze covers response bodies **only**. It does **not** cover the security preamble:
> the `Host` allowlist, the `Origin` check, the `Content-Type` requirement on writes, the
> body cap and the rate limits apply to `/api/*` exactly as they do to `/api/v1/*`. That is
> non-negotiable — `POST /api/dispatch` from a `text/plain` cross-origin simple request is
> live remote code execution today, on pure loopback.
>
> Every legacy response carries `Deprecation: true` and
> `Link: </api/v1/…>; rel="successor-version"`. `GET /api/v1/meta` reports `legacy_hits`
> per path so you can see when the HTML has finished migrating. The legacy surface is
> deleted once those counters stay at zero for a week.

**Do not implement a mobile client against these.** They are documented for migration and
for reading the existing HTML.

### 16.2 Mapping

| Legacy | v1 successor | Notable differences |
|---|---|---|
| `GET /api/state` | `GET /api/v1/state` + `GET /api/v1/worktrees/{wid}` | legacy has `age_s` (relative), no absolute activity time, no ids, no `resumes` array (it is a dict keyed `"worktree\|sid"` with a literal pipe), and merges `resumes` in at the handler rather than in the collector |
| `GET /api/topology` | `GET /api/v1/topology` | legacy silently drops worktrees whose base ref or merge-base fails, with no `dropped` list |
| `GET /api/limits`, `?refresh=1` | `GET /api/v1/limits`, `POST /api/v1/limits/refresh` | legacy `refresh=1` blocks synchronously for up to 90 s, past iOS's default request timeout. Legacy `generated_at` is an ISO-8601 **string** here while `/api/state`'s is a **float** — same name, different type. Legacy `accounts[].limits[].resets_at` is an ISO-8601 string while `session.limit.resets_at` is a float epoch — same name, different type, in payloads fetched together. Legacy drops `ok: false` accounts entirely. |
| `GET /api/chat?account=&sid=` | `GET /api/v1/sessions/{sid}/messages` | legacy is hardcoded to the last 40 messages from a 512 KiB tail, has no pagination, no cursor and no `has_more`, and never percent-decodes `account` |
| `GET /api/dispatchlog` | `GET /api/v1/dispatches` | legacy returns ~36 KB for 24 entries because `mission_original` and `kickoff` are both untruncated and `kickoff` re-embeds the mission; `ts` is a timezone-naive local string; a legacy `routed` field appears on old rows and is never written now |
| `GET /api/dispatch/status?job=` | `GET /api/v1/ops/{op_id}` | legacy jobs are memory-only, capped at the last 20, and the sequence resets on restart so ids repeat. `{"ok": false, "error": "unknown job"}` means **lost**, not failed. |
| `GET /api/focus?pid=` | `POST /api/v1/agents/{ag_id}/focus` | legacy is a **GET with a side effect** that opens a new Terminal window per call |
| `POST /api/send {pid, text}` | `POST /api/v1/sessions/{sid}/messages` | legacy addresses by pid only, verifies only that *some* claude process holds that pid, has no receipt check, no idempotency, and raises an uncaught `ValueError` on a non-numeric pid |
| `POST /api/finish {worktree}` | `POST /api/v1/worktrees/{wid}/finish` | legacy is synchronous and can exceed 60 s; it has no `step` parameter, and its `dispatch` mode has **zero** double-fire protection |
| `POST /api/dispatch` | `POST /api/v1/dispatches` | legacy returns three disjoint shapes on one endpoint, all HTTP 200: `{"job": id}` on success **with no `ok` key at all**, `{ok:false, message}`, and `{ok:false, needs_decision:true, …}` |
| `POST /api/resume/schedule`, `/cancel` | `PUT` / `DELETE /api/v1/sessions/{sid}/resume` | legacy has no existence check on worktree/sid and no `firing` state |
| `POST /api/reserve` | `PUT /api/v1/accounts/{label}/reserve` | legacy mutates memory before writing the file, so a failed write leaves routing behaving as if it succeeded until restart |

### 16.3 Legacy behaviours a v1 client must never assume

- **Every legacy endpoint returns HTTP 200**, including logical errors. Only an unrouted
  path 404s, and that 404 body is **HTML**, not JSON.
- `HEAD` and `OPTIONS` return `501`.
- Routing is `str.startswith` on the **raw path including the query string**, so
  `/api/statefoo` reaches the state handler.
- There is no authentication of any kind.
- The server is HTTP/1.0 with `Connection: close` on every response, and never gzips.

v1 fixes all of these. None of them are behaviours to preserve.

---

## 17. Changelog

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-21 | initial contract |
