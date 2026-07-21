# orchestr for iOS — delivery roadmap

**Status:** planning. Nothing here is built yet.
**Scope:** taking orchestr from "a Python file that serves a dark board on loopback" to "a native SwiftUI app that can do everything the board can, over Tailscale, from a phone."

This document only **sequences**. The *what* and *why* live in the sibling specifications:
`API.md` (the wire contract — authoritative on every path, field and shape), `ARCHITECTURE.md`
(server internals, collector, security), `UX.md` (screens, flows, visual system) and
`IOS-APP.md` (client engineering). Supporting: `VERIFIED-FACTS.md`, `ENGINE.md`,
`FRESHNESS.md`, `adr/0001`–`0008`. Where they disagree, the conflict is named in §Decisions
rather than silently resolved.

> **Endpoint names below are aliases.** This document writes `/api/v2/…`; the shipping prefix
> is **`/api/v1/…`** (`API.md` §0 — the new surface is v1 because it is the first *versioned*
> API; the unversioned `/api/state` etc. are frozen legacy). Specifically: `/api/v2/stream` →
> `GET /api/v1/stream`, `/api/v2/state` → `GET /api/v1/state`, `/api/v2/map` →
> `GET /api/v1/topology`, `/api/hello` → `GET /api/v1/health` + `GET /api/v1/meta`,
> `capabilities[]` → `features[]`, `POST /api/kill` →
> `POST /api/v1/agents/{ag_id}/kill`. `API.md` §0.1 is the full table.

---

## 0. Read this first

Three things shape everything below.

**One: the phone is not the first beneficiary.** Milestones 1 and 2 make the *existing desktop
board* materially better — the CSRF hole closes, and `/api/state` goes from a measured **2.18 s
cold** to a cache read. Both ship without an iPhone anywhere near the project. That is deliberate:
it front-loads the work that is hardest to undo and gets value out before the first line of Swift.

**Two: the $99 Apple Developer account gates less than you'd think.** You can build and run the
app on your own iPhone with a *free* personal team. What the money buys is push, App Groups,
Keychain sharing and TestFlight — i.e. everything from M9 onward. See §Apple account gating.
Do not spend it in month one.

**Three: this is a multi-month programme, not a weekend.** Honest calibration below. The
"minimum useful cut" (§Cut lines) gets you a phone that shows the fleet and lets you reply to a
blocked agent, and it is roughly half the total.

### Effort calibration

| Size | Focused days | Meaning |
|---|---|---|
| **S** | 1–3 | one sitting to a long day |
| **M** | 4–8 | a week of real work |
| **L** | 9–20 | two to four weeks; should have a mid-point you could stop at |
| **XL** | 20+ | too big — split it |

"Focused days" means uninterrupted implementation days, not calendar days. The whole programme is
**≈ 120 focused days**. At two focused days a week that is over a year; at four, about seven
months. If that number is wrong it is wrong low, because it excludes the SwiftUI + Swift 6 strict
concurrency learning curve if you have not shipped one before.

### Dependency shape

```
M0 verify & decide
 └─ M1 server hardening ──┬─ M2 collector ──┬─ M5 realtime ──┬─ M8 push (ntfy) ── M9 push (APNs) ── M11 activities/widgets
                          │                 │                │
                          └─ M3 auth+TLS ───┴─ M4 read-only app ─┬─ M6 talk to an agent
                                                                └─ M7 finish & dispatch
                                                                   M10 branch map (late, optional)
                                                                   M12 v1 hardening
```

M1, M2 and M3 are all independently shippable and can be reordered against each other. M1 first is
recommended only because it closes a live vulnerability.

---

## Milestone 0 — Verify and decide

**Goal:** find out, in a week, which of this plan's load-bearing assumptions are false — before
any of them has code built on top.

Nothing here is production code. Every item is a spike, a measurement, or a decision. Several of
these have already been partly measured by the track work; M0 is where they get confirmed on the
actual devices in the actual configuration.

### Spikes (write throwaway code, keep the numbers)

| # | Spike | Why it is here | Falsifies |
|---|---|---|---|
| **S1** | **stdlib APNs.** Generate a P-256 key, sign a JWT header+claims with `/usr/bin/openssl dgst -sha256 -sign`, convert the DER `SEQUENCE{r,s}` to raw `r‖s` (32 bytes each, sign-pad stripped, left-padded), POST to `api.sandbox.push.apple.com` with `curl --http2`. | The entire push strategy rests on this and Python's stdlib has **no HTTP/2 client and no ECDSA**. | M8b, M9, M11 |
| **S2** | **Reach the Mac from the iPhone.** Tailscale up on both, `tailscale cert <magicdns>`, wrap the listener in `ssl`, load `https://<name>.ts.net:4242/api/state` in mobile Safari **and** from a 20-line URLSession test app. | ATS refuses cleartext, and Tailscale's `100.64.0.0/10` is CGNAT — **not** covered by `NSAllowsLocalNetworking`. If HTTPS-on-tailnet does not work, the whole transport plan changes. | M3, M4, everything |
| **S3** | **SSE through the tunnel.** Hold an SSE connection from the phone for 10 minutes on cellular with the screen locked; count frames received and reconnects. | SSE is the basis of M5's battery claims. Carrier CGNAT often forces a DERP relay; NAT timeouts vary. | M5 |
| **S4** | **Unreachability taxonomy.** With Tailscale up: (a) Mac asleep, (b) Mac awake + orchestr stopped, (c) Tailscale off on the phone. Record what `URLError`/errno each produces through the NE tunnel. | The app promises four distinct diagnoses. If they collapse to one timeout, the copy must change rather than lie. | M4 |
| **S5** | **Free provisioning limits.** Make a throwaway app on a personal team; try to add Push Notifications, App Groups, Keychain Sharing. Note what Xcode refuses. | Determines exactly when the $99 must be spent. Xcode's behaviour here has changed between releases. | §Apple gating |
| **S6** | **Local-network prompt.** Does iOS show the local-network permission dialog for a `100.64/10` destination routed over `utun`? | Decides whether `NSLocalNetworkUsageDescription` is required or is a wasted scary prompt on first run. | M4 |

**S1 caveat, stated plainly:** with no paid account you can prove the *mechanism* (curl speaks
HTTP/2 to Apple; openssl signs; DER→JOSE round-trips against a hand-written verifier) but not the
*credential path* (that Apple accepts your team's JWT and delivers to a device). A `403
InvalidProviderToken` is the expected and correct success signal for the unpaid spike.

### Confirmed on this machine already

```
curl 8.7.1 ... nghttp2/1.67.1      → HTTP/2 available
/usr/bin/openssl LibreSSL 3.3.6    → dgst -sha256 -sign works on PKCS#8 .p8
macOS 26.2, Xcode 26.6             → Swift 6 / iOS 26 SDK available
tailscale 100.113.110.31           → installed, MagicDNS achills-macbook-pro.tail1205d9.ts.net
                                   → BackendState is "Stopped" — turn it on for S2
142 tests, 9.7 s, OK               → the suite you must not break
```

⚠ **`/usr/bin/openssl` is LibreSSL, not OpenSSL.** For APNs signing that is fine (verified). If you
end up generating a *self-signed* cert instead of using `tailscale cert`, LibreSSL's
`req -newkey ec` emits **explicit curve parameters**, which breaks SPKI pinning — you must use
`ecparam -name prime256v1 -param_enc named_curve` as a separate step. Another reason to prefer
`tailscale cert`.

### Decisions that must be closed in M0

See §Decisions needed from you. **D1 (TLS), D2 (realtime transport), D4 (deployment target)
block M1.** The rest can be closed later but get cheaper the earlier they're settled.

**Effort:** S (2–4 days including a day of Tailscale/Xcode setup friction)
**Unblocks:** literally everything
**You can now:** state with evidence whether the plan's four riskiest assumptions hold.

---

## Milestone 1 — Server hardening

**Goal:** make the HTTP layer safe to expose to a second client, and close the CSRF hole that is
live today on pure loopback.

This ships alone. No phone, no new endpoints, no visible UI change.

### Backend

- **`Content-Type: application/json` required on every POST.** Today `do_POST` never inspects it,
  so a CORS *simple request* (`text/plain`) from any website you visit fires `/api/dispatch` —
  which spawns `claude --dangerously-skip-permissions`. This is a working remote-code-execution
  path today, on a loopback-only install, and it is the single highest-severity item in the whole
  programme.
- **`Host` header allowlist** (kills DNS rebinding of the read endpoints), **`Origin` allowlist on
  writes**, `do_OPTIONS` → `405` with no CORS headers.
- **`protocol_version = "HTTP/1.1"` — plus `Handler.timeout = 30`.** `BaseHTTPRequestHandler.timeout`
  defaults to `None`; keep-alive without a timeout leaks one pinned thread per idle connection,
  forever. This is three lines, not one.
- **Restructure `do_POST` so the body is drained unconditionally before any parse can fail.** Under
  keep-alive an undrained body is parsed as the next request line and desynchronises the
  connection. Bound `Content-Length` (reject > 1 MB and reject negative — `read(-1)` currently
  hangs). `411` on `Transfer-Encoding: chunked`. `try/except` around the dispatcher so
  `POST /api/send {"pid":"abc"}` returns JSON instead of dumping a traceback and dropping the socket.
- **`do_HEAD`**, gzip when `Accept-Encoding` allows and the body exceeds 1 KB (measured 36,326 →
  9,202 B, 3.95×), `Vary: Accept-Encoding`.
- **Two independent bug fixes while you're in here:** atomic `save_resumes` (tmp + `os.replace` —
  today a crash mid-write silently loses every armed auto-resume), and the `--` sentinel before
  user text in `tmux send-keys -l` / `set-buffer` (verified on tmux 3.6a: a message starting with
  `-` exits 1 with `unknown flag`).
- One audit log line per mutation. There is no request log at all today (`log_message` is `pass`).

### iOS

None.

### Acceptance

1. From a scratch HTML file served on `file://`, `fetch('http://127.0.0.1:4242/api/dispatch', {method:'POST', headers:{'Content-Type':'text/plain'}, body:'{"mission":"x"}'})` → **403**, and no agent launches. Before this milestone it launches one.
2. `curl -H 'Host: evil.example' http://127.0.0.1:4242/api/state` → **403**.
3. `curl -X POST -H 'Content-Type: application/json' -d '{"pid":"abc","text":"hi"}' .../api/send` → a JSON error, not a closed connection.
4. Pipeline two POSTs on one keep-alive connection, the first with a malformed `Content-Length` — the second is answered correctly.
5. Open an idle keep-alive connection and confirm the server closes it within 35 s.
6. `./start.sh`, use the board normally for ten minutes: nothing has changed visually and nothing is broken.
7. `python3 -m unittest discover -s tests` — 142 existing tests plus new ones, green.

**Effort:** M (5–7 days)
**Unblocks:** every subsequent milestone. Nothing should bind beyond loopback before this lands.
**You can now:** browse the web with orchestr running without a website being able to launch an agent on your Mac.

---

## Milestone 2 — The collector

**Goal:** one background thread observes the fleet; every client reads a warm snapshot; git stops
being 78 % of the cost.

### Backend

- **A state bus.** One producer thread is the only caller of `collect_state()`. `cached_state()`
  becomes a read (plus a `fresh=True` path that actually waits for a new tick, so the finish flow's
  current `_cache["t"] = 0.0` invalidation is preserved rather than downgraded).
- **The tick is demand-driven and can stop completely.** With no subscriber, no request in 60 s and
  no push device registered, the producer blocks on an Event and costs exactly zero — the property
  a lazy tool has today and must not lose.
- **Parallel + cached git.** `git_info` is a measured **1,581 ms of ~1,700 ms** for 9 worktrees, 4
  serial subprocesses each. Fan out with a `ThreadPoolExecutor`; cache ref-derived facts on a
  gitdir mtime signature. ⚠ **8 of the 9 live worktrees have `.git` as a *file*** (`gitdir: …`), so
  the signature must resolve the real gitdir and `commondir` or it is a constant tuple forever.
  ⚠ **Never cache `dirty`** — `git status --porcelain` is a working-tree question that no `.git`
  mtime answers, and `_pick_defaults` sorts dispatch targets by it.
- **Single-flight for `_topo` and `_limits`.** Both use the same unlocked check-then-compute
  pattern; topology is ~90 forks / 3.07 s and `map.html` polls at exactly its TTL, so it always
  misses. Raise `TOPO_TTL_S` above the poll interval.
- **`active_at` (absolute epoch) on every session.** One line, from a value `scan_sessions` already
  computes. **This is the prerequisite for M5** — without it every delta is 100 % noise, because
  `age_s` is a function of when you asked.
- **`started_at` on live procs**, parsed from `ps`'s three `etime` shapes (`15:02`, `12:43:46`,
  `2-03:14:22`). Note macOS `ps` has no `etimes` column; write the parser and unit-test all three.
- **Prime the limits cache at boot** on a daemon thread. Today `collect_state` reads a cache only
  `/api/limits` fills, so on a cold server every limit-parked agent classifies as `waiting` — i.e.
  mis-triaged as *needing you*, the loudest state in the product.
- Demo/real payload parity as a **test**, not a note (`demo_state()` is missing 5 session fields
  real state always has). This is a prerequisite for using `--demo` as an app fixture.

### iOS

None.

### Acceptance

1. `time curl -s localhost:4242/api/state > /dev/null` twice in a row — second call under 50 ms. Before: 2.18 s and 4.5 s.
2. Open four browser tabs on the board. `ps aux | grep -c '[g]it '` sampled repeatedly shows no more git processes than with one tab.
3. Close every browser tab, wait two minutes, run `ps` — no git subprocesses are being spawned at all.
4. `touch` a file in a worktree; the dirty count updates within one tick.
5. Restart orchestr with no browser open, immediately `curl /api/state | python3 -c "..."` — sessions on an exhausted account already report `status: "limit"`.
6. The board feels instant.

**Effort:** L (10–14 days)
**Unblocks:** M5 (deltas need `active_at`), M8 (edge detection is impossible without continuous observation — with the phone asleep and no browser open, nothing currently looks at the fleet at all).
**You can now:** run the desktop board with three tabs open and not hear the fan.

---

## Milestone 3 — Auth, TLS and pairing

**Goal:** orchestr can safely listen on the tailnet, and a device proves who it is.

### Backend

- **Move mutable secret state out of the repo directory** to `~/Library/Application Support/orchestr/` (0700). `~/Downloads/orchestr` is commonly cloud-synced and Time-Machined, and it will hold a TLS private key.
- **Per-device bearer tokens.** `devices.json` storing only `sha256(token)`; `Authorization: Bearer`; loopback exempt so the desktop board is untouched. Scopes (`read` / `act` / `admin`) so a phone's background-readable token cannot dispatch.
- **TLS.** `tailscale cert <magicdns>` + `ssl.SSLContext.wrap_socket` on the listener (~12 lines, stdlib). Dual-bind: loopback stays plain HTTP so `start.sh`'s hardcoded `http://127.0.0.1:$PORT` keeps working.
- **`GET /api/hello`** — hostname, user, version, `capabilities[]`, and the config values the client needs to render honest empty states (`roots`, `pattern`, `session_window_h`, `exclude_accounts`, `reserve_percent`). `capabilities[]` is the version-skew mechanism; it replaces the hand-written *"the server predates auto-resume"* string in `index.html`.
- **Pairing.** `./start.sh --pair` prints a short-lived (120 s, single-use) code plus the MagicDNS host and the cert fingerprint. **Ship manual entry first.** The ASCII/QR encoder is a realistic 250–400 lines of Reed-Solomon and mask selection — treat it as an optional add-on inside this milestone, not a prerequisite.
- **Refuse to bind beyond loopback without a token**, as a hard `sys.exit`, not the current advisory stderr warning that `start.sh` redirects into `/tmp/orchestr.log` where nobody sees it.

### iOS

None yet — but this is where you *could* write the 20-line URLSession probe from S2 into a throwaway app to confirm the token round-trips.

### Acceptance

1. `./start.sh` with no token — board works on loopback exactly as before.
2. `./start.sh --tailnet` with no token — refuses to start, with a message naming the fix.
3. `./start.sh --tailnet --token …` — `curl https://<magicdns>:4242/api/state` returns **401**; with `-H "Authorization: Bearer …"` returns **200**. No `-k` flag needed (the cert is publicly trusted).
4. Mobile Safari on the iPhone, over Tailscale, loads `https://<magicdns>:4242/` with no certificate warning.
5. `./start.sh --devices` lists paired devices; `--revoke <id>` takes effect within a second on the running server.
6. Kill and restart orchestr — paired devices survive.

**Effort:** M–L (8–12 days; L if the QR encoder is in scope)
**Unblocks:** M4. Nothing may reach the phone before this.
**You can now:** open your own board from your phone's browser, over Tailscale, with a real padlock.

---

## Milestone 4 — The read-only app

**Goal:** see your fleet on your phone. No writes of any kind.

**This is the first milestone that delivers the actual product.** Everything before it was
groundwork; from here the value is visible on the device in your pocket.

### Backend

- Nothing new required. **M4 deliberately polls the existing `/api/state`** every 5 s.

  *Judgement call:* after M2 that endpoint is a ~2 ms cache read, so the fork storm is gone and the
  only cost is 36 KB per poll — fine on wifi, expensive on cellular. Shipping against the v1 payload
  gets the phone working weeks earlier, and it lets the Swift `Codable` layer be validated against
  **real** data before the wire format changes underneath it. M5 then swaps the transport behind
  one protocol and the improvement is measurable as a before/after.

- One small addition: server-side truncation of `git.commit.subject` (the only completely
  untruncated string in the payload; 107 chars observed live, and it will wreck a phone row).

### iOS

- Xcode project + a local `OrchestrKit` package: `Core` (pure, nonisolated) / `API` (actor client) / `Store` (`@MainActor @Observable`) / `UI`. Zero third-party dependencies, matching the project's identity.
- `Codable` for the whole payload, written against the **conditional-keys-are-absent-not-null** reality (`limit`, `handed_to`, `tool_running`, `bg_shell`, `closeout_sent`).
- Pairing screen (manual host + code; camera QR if M3 shipped it), Keychain storage, `/api/hello` version gate.
- **Board:** severity-sectioned list, status glyph + word + colour on every row, the triage headline, sessions inline with the desktop's exact line order.
- **Worktree detail**, **chat transcript read-only** (the composer is replaced by a read-only note), **limits** read-only.
- **Connection & staleness states**: live / lagging / stale / offline / Mac-asleep, with the S4 taxonomy driving the copy.
- **The re-sort hold.** The board must never reorder under a finger, and the desktop's `pointerenter` mechanism does not port. Freeze order from first touch until an explicit apply (a `⌗ N updates` pill, pull-to-refresh, tab tap or foreground), plus a short tap shield after any applied reorder.
- Accessibility from day one, not bolted on: one accessibility element per row, custom actions, Dynamic Type, no colour-only status.

### Acceptance

1. Pair the phone, over cellular, away from home. The board appears within ~5 s.
2. The counts and statuses match what the desktop board shows at the same moment.
3. Scroll the list while a poll lands — nothing moves under your thumb.
4. Tap a session → read the last 40 turns of the conversation.
5. Airplane mode → the board dims and says something true and specific ("can't reach studio-mac"), and does **not** silently show hour-old data as if it were live.
6. Close the app for an hour, reopen — you get the cached board marked "as of 14m ago" and then live data, never a spinner on blank.
7. VoiceOver: swipe through the board and hear one coherent sentence per row.

**Effort:** L (15–20 days — this is the largest single milestone)
**Unblocks:** M5, M6, M7, M10.
**You can now:** stand in a queue and know whether anything needs you.

---

## Milestone 5 — Realtime and battery

**Goal:** the board updates itself, and a day of use costs kilobytes rather than tens of megabytes.

### Backend

- **Canonical projection + field-addressed diffs.** Measured against the live 9-worktree / 33-session fleet: a real 5-second window is **131 bytes**, and **47 bytes** when nothing happened, against 36,326 for a full poll. That is a ~277× reduction and it exists *only* because `active_at` (M2) removed the one field that changes every tick regardless of events.
- **Cursor = `epoch:seq`**, where `seq` advances only when the projection actually changed — so `since == seq` is a zero-byte freshness proof, and a restart is unambiguously discontinuous.
- **`GET /api/v2/stream`** (SSE) with a bounded replay history, heartbeats carrying cadence and collector health, keepalive comments for dead-peer detection, and per-device subscriber eviction on reconnect.
- **`GET /api/v2/state`** with `?since=` (delta), `?wait=` (long-poll) and an ETag — this *is* the fallback ladder, so the work is not wasted if SSE proves unreliable in S3.
- Gzip the snapshot. **Never** the deltas (gzip measurably *expands* a 47-byte frame) and never the stream (it breaks incremental delivery).
- Wake detection: a monotonic-vs-wall-clock jump means the Mac slept — new epoch, resync everyone, and suppress event derivation for two ticks so opening the lid does not produce a burst about three-hour-old transitions.

### iOS

- SSE client with a reconnect state machine (backoff + jitter, reset only after `hello`, single-flight so a wifi↔LTE handoff doesn't fire five attempts).
- Apply layer: ops land in a non-observed mirror; exactly one assignment publishes to the observed model, so six changes in a tick are one view invalidation.
- **Clock skew correction** — moving from relative `age_s` to absolute `active_at` means a phone 40 s off shows wrong ages everywhere. Median of samples from `hello`/`hb`, applied to every age and countdown.
- Freshness split into *connection liveness* (any frame arrived) and *data recency* (the collector's timestamp advanced), so an idle fleet stays green and a wedged collector goes amber.
- Diagnostics screen (five taps on the version row): bytes/hour, connections/hour, reconnect causes, RTT, skew.

### Acceptance

1. Leave the app foregrounded on an idle fleet for 10 minutes. Diagnostics reports well under 100 KB.
2. Answer a question at the Mac; the phone's board reflects it within ~3 s with no poll.
3. Walk out of wifi range onto cellular — one reconnect, board stays correct, no visible gap.
4. Idle fleet, zero changes for five minutes — the connection indicator stays **live** the whole time (this is the regression that matters most).
5. Sleep the Mac for an hour, wake it — the phone reconnects and says the Mac was asleep, and you do **not** get a burst of stale notifications later in M8.
6. Compare a day's cellular data usage against M4. Expect roughly two orders of magnitude.

**Effort:** L (12–16 days)
**Unblocks:** M8 (shares the transition detector), M11.
**You can now:** leave the app open all day without watching your battery or data allowance.

---

## Milestone 6 — Talk to an agent

**Goal:** reply to a blocked agent, and manage limits, from the phone. The low-blast-radius writes only.

### Backend

- **`/api/send` addressed by `{account, sid}`, not `pid`.** Today pid is the only addressing and the server verifies only that *some* claude process holds it, so a phone restored from background can type an instruction into a different agent. The server re-resolves the session→process pairing at send time and refuses on mismatch.
- **A real delivery receipt.** Route the send through `deliver_text` (bracketed paste, avoiding the `[Pasted text #N]` chip failure) and prove arrival with `_proven_in_transcript`. Both functions already exist in the file and `/api/send` uses neither. Today `ok: true` means "tmux accepted keystrokes", which is not the same thing.
- **Idempotency keys** on every mutation, with the response cached and replayed for a repeat key.
- `parse_qs` for query parameters (fixes the never-decoded `account=` regex).
- Chat `?after=` / `?before=` with an ETag, so an unchanged transcript costs a 304 rather than 6 KB.
- `firing` status + `started_at` on resume schedules, so the phone can tell "armed" from "firing for the last nine minutes".

### iOS

- Chat composer with `◌ → ✓ typed → ✓✓ delivered` states, and a distinct queued state for a message typed mid-turn.
- Newlines collapse **as you type**, because the server collapses them anyway — WYSIWYG or nothing.
- Manual `▶ resume` (session-cap limits only, disabled with a reason until the reset).
- `⏱ auto-resume` sheet: delay presets, exact-time picker, arm / re-arm / disarm. Handle `need_time` (a limit with no known reset is normal, not an error).
- Reserve slider on the limits screen with optimistic update and inline rollback.

### Acceptance

1. Get a blocked agent to ask a question. From the phone, reply. The reply appears in the desktop board's chat drawer and the agent proceeds.
2. Send a message beginning with `-` — it works (this fails on the server today).
3. Send while the agent is mid-turn — the bubble says *queued*, not *failed*.
4. Put the phone in airplane mode mid-send — the failure is explicit and the draft survives.
5. Arm an auto-resume from the phone; watch it fire on the Mac at the stated time.
6. Adjust a reserve percentage; confirm `orchestr.config.json` changed and the desktop limits page agrees.

**Effort:** L (9–13 days)
**Unblocks:** M9's notification actions.
**You can now:** unblock an agent from a café without opening a laptop. This is the point at which the app pays for itself.

---

## Milestone 7 — Finish and dispatch

**Goal:** the two dangerous writes, made safe enough to hand to a thumb.

### Backend

- **Server-side per-worktree finish lock** plus **request-id replay**. A client-side guard cannot cover an app relaunch, a second device, or the desktop board open on the same Mac — and in `dispatch` mode a duplicate launches a *second* headless agent merging and pushing the same branch.
- **Jobify `/api/finish`.** It currently runs `git fetch` (30 s) + `claude_processes()` twice (≤26 s each) + osascript (10 s) synchronously and can exceed iOS's 60 s default timeout — and a client timeout followed by a retry *is* the double-fire scenario. Return a job id immediately and stream progress.
- **`try/except` around `_run_dispatch`.** Today an unexpected exception strands a job at `done:false, result:null` forever and the client polls until the heat death of the universe.
- **`POST /api/kill {session}`** → `tmux kill-session`. There is currently no way to stop a mission from anywhere, let alone a phone.
- Server-side expiry on idempotency records, so a request that a background URLSession lands 40 minutes later is *refused*, not re-executed.

### iOS

- Finish as a **confirmation sheet**, never a swipe and never a bare tap. It names the worktree, the dirty count, and which of the four server tiers is likely — including that the `dispatch` tier launches a headless agent that merges and pushes.
- The full two-step `✓ finish` → `✕ close`, driven by `closeout_sent`. The desktop map's one-step variant is not reproduced; one state machine for both surfaces.
- The `mode: "pending"` refusal is a **persistent row that re-verifies itself and dissolves when it clears**, not a 6-second toast.
- Mission composer: model and effort have no defaults (mirroring the server's own refusal), auto-pick preview that matches `_pick_defaults` exactly, the headroom-decision dialog, draft autosave.
- Reconciliation: on timeout, poll the dispatch log matched on the idempotency key for ~25 s and say *"lost track"*, never *"failed"*.

### Acceptance

1. Dispatch a mission from the phone; watch `①…⑤ ✓ launched` and then see the agent appear on the desktop board ~30 s later.
2. Tap Launch twice in quick succession. **Exactly one agent exists.** Verify with `tmux -L fleet ls`.
3. Finish a worktree with a live agent: the brief is typed, the button flips to `✕ close`, and pressing it early gives a refusal that explains what is unverified.
4. Kill orchestr mid-finish and restart it. The phone reconciles to a definite answer rather than spinning.
5. Kill a mission from the phone; the tmux session is gone.
6. Try to finish the same worktree from the phone and the desktop board simultaneously — the second gets a clear "already running" answer.

**Effort:** L (9–13 days)
**Unblocks:** nothing downstream depends on it, which is why it can slip.
**You can now:** run the entire loop — dispatch, watch, answer, close out — without a keyboard.

---

## Milestone 8 — Push, via ntfy

**Goal:** the phone buzzes when an agent needs you. **No Apple Developer account required.**

Splitting push in two is the single most valuable sequencing decision in this roadmap: **ntfy
proves the whole event pipeline for free**, and only the last mile costs $99.

### Backend

- **Transition detection** off the collector's consecutive snapshots (M2 made this possible; before it, nothing observes the fleet when the browser is closed and the phone is asleep).
- The event taxonomy, with debounce. Two rules that are not optional:
  - **`limit` with `handed_to` set generates nothing** — it means the work already continued on another account, and the server already excludes it from counts and severity.
  - **`waiting` is off by default.** It occurs at the end of every single turn; pushing it fires dozens of times a day and gets the whole channel muted, taking the alerts that matter with it.
- Rate limiting (per-key cooldown, a global ceiling, then a digest) and quiet hours evaluated in the *device's* timezone.
- **Pluggable sink interface** with an ntfy implementation — a single `urllib.request` POST to `https://ntfy.sh/<topic>`, using the **JSON publish endpoint** (every one of our notification titles is non-ASCII, and HTTP header values are not UTF-8).
- Device registry with per-device preferences (which event types, quiet hours) — delivered pushes cannot be filtered on-device, so this must be server state or the toggles are decorative.

### iOS

- Settings screen: choose sink, per-event toggles, quiet hours, **"send a test notification"**, and a **"last push delivered: 4m ago"** row. Push that silently stopped after a restore is otherwise indistinguishable from a quiet fleet, and users discover it a week late.

### Acceptance

1. Install the ntfy app, subscribe to your topic. Close orchestr's app entirely. Lock the phone.
2. On the Mac, get an agent into `needs_input`. **The phone buzzes within ~15 s** with the worktree name and the question.
3. Answer it at the Mac. No second notification arrives about the same episode.
4. Trigger six things at once — you get one digest, not six buzzes.
5. Set quiet hours; confirm nothing arrives inside them and a digest arrives after.
6. Hand an agent off to another account (`handed_to`) — **no** notification.

**Effort:** M (6–8 days)
**Unblocks:** M9 (which is then just a second sink behind the same interface).
**You can now:** stop checking. The Mac tells you.

---

## Milestone 9 — Push, via APNs

**Goal:** native notifications with actions, badges, and a reply field on the lock screen.
**This is the first milestone that requires the $99.**

### Backend

- APNs sink behind the M8 interface: ES256 JWT via `openssl` (cached 40 min — Apple rate-limits regeneration), HTTP/2 POST via `curl`, both through the existing `run()` seam so nothing new enters the import graph.
- The DER→JOSE conversion from S1, with test vectors covering a short `r`, a high-bit-set `s`, and a truncated buffer.
- Correct headers: `apns-push-type`, `apns-priority`, `apns-collapse-id`, and **`apns-expiration` as an absolute epoch** (a duration means "expired in 1970" and Apple makes one attempt with no store-and-forward).
- `410 Unregistered` prunes the token; `403 ExpiredProviderToken` forces exactly one re-sign and retry.
- Badge decrements are pushed too, or the badge sticks at 2 forever after you clear attention at the Mac.

### iOS

- APNs registration, re-posted on every foreground (tokens rotate on reinstall and restore).
- `interruption-level: time-sensitive` for `needs_input` / `blocked` — without the Time Sensitive entitlement, every alert is suppressed by Sleep Focus, which is precisely the 2 a.m. case the product exists for, failing invisibly.
- **`UNTextInputNotificationAction` — reply from the lock screen.** This is the highest-value single feature in the programme: it collapses the whole round trip. It must refetch state immediately before sending and report failure as a follow-up local notification (a notification action handler cannot present inline UI).
- Notification Service Extension for body enrichment, with the token stored `kSecAttrAccessibleAfterFirstUnlock` in a shared access group — the default accessibility is unreadable on a locked device, which is the entire scenario.
- Thread grouping by worktree.

### Acceptance

1. Phone locked, app closed. Agent asks a question. Notification arrives with a **Reply** field.
2. Reply from the lock screen without unlocking. The agent receives it. A confirmation notification follows.
3. Turn on Sleep Focus. A `needs_input` notification still breaks through; a `dispatch_done` does not.
4. Delete and reinstall the app; push still works after the next launch (token rotation handled).
5. The badge count matches the number of agents needing you, and goes to zero when you clear them at the Mac.

**Effort:** M (6–9 days, assuming S1 de-risked the mechanism)
**Unblocks:** M11.
**You can now:** answer an agent from your lock screen in four seconds.

---

## Milestone 10 — The branch map

**Goal:** see fork ages, divergence and merge collisions on the phone.

Deliberately late, and **explicitly optional**. The map's own design track concluded it answers
reflective questions that are mostly laptop decisions. Its genuinely unique payload is small: the
fork timestamp, and which worktrees share a trunk (which matters because finishing two on the same
trunk concurrently collides — and `/api/state` cannot tell you that).

**Instrument M4 for "map opened from phone" and let the data decide whether to build this.**

### Backend

- `GET /api/v2/map` — topology joined with board status server-side, raw float epochs, per-group clamped log axis parameters, plus `role` (diverged / stale / parked) and the `unmapped` worktrees the current code silently drops.
- Ship `base_ts` so a per-clone stale `behind` count can be marked rather than presented as fact.

### iOS

- A ranked **row list**, not an SVG port. One row per worktree with a 20 pt fork strip on a shared axis, one inline age label per row, tap → detail sheet. This eliminates the desktop's label-collision packer, its 4–13 px hit targets and its hard 900 px width floor in one move.
- Shared finish machine with the board.

### Acceptance

1. Open the map tab. Every worktree is on screen or one scroll away, each readable on its own.
2. The worktree that forked five months ago is visibly separated from the eight that forked this week — without doing arithmetic on a column of numbers.
3. Tap a row → hash, subject, ahead/behind/dirty, fork age, sessions, and the same `✓ finish` as the board.
4. A worktree with no trunk ref appears in a "not comparable" section with the reason, instead of vanishing.

**Effort:** M (7–10 days)
**Unblocks:** nothing.
**You can now:** decide what to land next without opening a terminal.

---

## Milestone 11 — Live Activities and widgets

**Goal:** glanceable without opening anything.

### Backend

- ActivityKit push route (`apns-push-type: liveactivity`, its own topic suffix) and push-to-start token storage.

### iOS

- **One Live Activity: the limit-reset countdown.** Rendered with `Text(timerInterval:)`, so it self-updates on-device and consumes **zero** pushes for its whole life. Started only when the reset is under ~6 hours out — iOS ends an activity after roughly 8 hours and a weekly cap can reset days away, so a long-horizon activity would silently vanish and read as "the schedule was lost".
- **Dispatch progress is *not* a Live Activity.** It is a 10–20 s job, often invisible on a device without a Dynamic Island, and driving `①…⑤` costs six pushes against a scarce budget for something you are not looking at. One terminal alert instead.
- Widgets: attention count (small), top three cards (medium), nearest reset (Lock Screen rectangular). **Every widget prints "as of HH:MM" and replaces the number with a dash past 10 minutes** — a widget confidently showing `0 need you` from stale data is the worst failure available.
- Widgets must be designed for **tinted mode**, which forces monochrome and erases the five-hue code exactly the way colour blindness does. Symbol + word only.
- Freshness comes from the Notification Service Extension writing counts into the App Group and reloading timelines, not from background refresh (which is opportunistic and disabled in Low Power Mode).

### Acceptance

1. An agent hits a weekly limit resetting in two hours. A Lock Screen countdown appears and ticks down without the app running.
2. Add the medium widget. It shows the three cards that matter and updates when a notification arrives.
3. Turn on a tinted home screen — the widget is still readable and still says which state each card is in.
4. Force-quit the app and leave it overnight. The widget shows a dash and "can't reach", not a stale zero.

**Effort:** M (6–9 days)
**Unblocks:** nothing.
**You can now:** glance at the Lock Screen instead of opening the app.

---

## Milestone 12 — v1 hardening

**Goal:** the things that make it a product rather than a demo.

### Work

- **Accessibility audit.** `performAccessibilityAudit()` on every screen in CI; VoiceOver pass on the full flow; Dynamic Type at AX5 on a 320 pt width with the longest worktree name; contrast ratios asserted as tests rather than documented in comments.
- **Light theme ("Daylight")** if D7 goes that way — and note that the out-of-process surfaces (notification banners, widgets, Lock Screen) render in the OS appearance regardless, so a light status palette must exist either way.
- Snapshot tests hosted in a real `UIWindow` (not `ImageRenderer`, which does not render `List` correctly and would produce stable, green, meaningless PNGs).
- A contract artifact generated from the Python side and asserted from both suites, so a change to `scan_sessions` turns something red instead of breaking an app in the field.
- Log rotation for `dispatch.log.jsonl` and the new ops/events logs (all currently append-only and unbounded).
- `launchd` LaunchAgent with `KeepAlive` so orchestr survives a crash and a reboot. There is no service manager at all today.
- Onboarding polish, empty states, the manual rendered natively.

### Acceptance

1. Navigate the entire app with VoiceOver only, and complete a dispatch.
2. Set text to the largest accessibility size — nothing is clipped or unreachable.
3. Reboot the Mac; orchestr is running when you next check the phone.
4. Use the app in direct sunlight and be able to read it.

**Effort:** M (6–10 days)
**You can now:** hand it to someone else.

---

## Decisions needed from you

Each has a recommendation. **D1, D2 and D4 block M1.**

| # | Decision | Options | Recommendation |
|---|---|---|---|
| **D1** | **How TLS is terminated** | (a) `tailscale cert` + `ssl.wrap_socket` in-process · (b) `tailscale serve` proxying loopback · (c) self-signed + SPKI pinning in the app | **(a).** Publicly-trusted cert on a real hostname ⇒ ATS passes with zero exceptions and no pinning code. (b) makes every request arrive from loopback, which breaks the peer-identity check and the Host allowlist. (c) needs a pin-rotation story and hits LibreSSL's explicit-curve-parameters trap. Cost of (a): the tailnet needs HTTPS enabled in the admin console — **confirm in S2**. |
| **D2** | **Realtime transport** | SSE · long-poll · WebSockets | **SSE, with the long-poll endpoint as a tested fallback.** SSE has been measured working under this project's exact `ThreadingHTTPServer` shape. WebSockets need ~150 lines of hand-rolled RFC 6455 for a one-directional channel. The long-poll fallback is not wasted work — it is also the tier-2 path when SSE proves unreliable on a relayed cellular route. |
| **D3** | **When to spend the $99** | now · at M8 · at M9 | **At the start of M8.** M0's spike proves the mechanism with a fake key; M4–M8 all run on free personal provisioning. Buying at M8 means the App ID, capabilities and provisioning profile are ready when M9 starts, without a week of certificate archaeology blocking you. If cash-conscious, M9 is the true hard gate. |
| **D4** | **iOS deployment target** | 18.0 · 26.0 | **26.0.** This is a single-operator tool on your own devices; you are on macOS 26.2 with Xcode 26.6. Targeting 26 costs nothing here and buys the bottom tab accessory (which fixes the unreachable-thumb-zone problem), Icon Composer, Control Center controls and `onScrollPhaseChange` ergonomics. Fall back to 18 only if your iPhone is not on 26 — **check this in M0**. |
| **D5** | **Split `orchestr.py` into a package** | now · before M2 · after M7 · never | **Open, and the two documents disagree.** This roadmap says *after M7* — the split is a 20-file diff that relocates `resume.schedule.json` (silently losing every armed schedule) and must not be indistinguishable from a behavioural change. `ARCHITECTURE.md` §4.1/§8 says *before the collector and before auth* (its migration step 5a, gated on `git diff --stat` showing zero changed lines inside function bodies), on the grounds that landing ~1,740 new lines of auth, idempotency, bus and push into a single 2,300-line module puts the security boundary in the middle of a file that also holds AppleScript templates, with `py_compile` as the entire static-analysis budget. **Both are defensible and the choice is real:** deferring means M2 and M3 are written twice-over into a file you then split; splitting first means the riskiest mechanical change lands before any of the work that would justify it. If you split, do it as ARCHITECTURE's **two commits** (pure `git mv` + import rewiring, then everything else) and ship `paths.py`'s `migrate_stray_state()` in the same PR. |
| **D6** | **Keep ntfy after APNs ships?** | keep · drop | **Keep.** It is ~30 lines behind an interface you are building anyway, it is the only path for anyone without a developer account, and it is a working fallback when your APNs key expires. |
| **D7** | **Light theme** | dark-only · ship Daylight | **Ship Daylight**, but in M12. You need a light status palette regardless — notification banners, widgets and the Lock Screen render in the OS appearance and `.preferredColorScheme` does not reach them. Once those colours exist, a full light theme is nearly free via the Asset Catalog. |
| **D8** | **Mac sleep policy** | do nothing · `caffeinate` · LaunchAgent + Energy Saver | **LaunchAgent with `KeepAlive`, plus documenting Energy Saver's "prevent sleeping when the display is off".** A closed lid stops the collector, the resume loop and every notification — and the app cannot tell you, because the thing that would tell you is asleep. This is a change to your machine's behaviour, so it is your call. |
| **D9** | **Build the branch map at all** | yes at M10 · defer · drop | **Instrument in M4, decide after M8.** Its own design track argues it is the least-opened view and answers laptop-shaped questions. Its one irreplaceable fact — which worktrees share a trunk — could be a single line on the board instead. |
| **D10** | **API versioning** | evolve `/api/*` · additive `/api/v2/*` | **Additive.** `index.html` is a real client you use daily; breaking it strands your desktop. Freeze v1 bodies, add v2, delete v1 only once the HTML migrates. |

---

## What the paid Apple Developer account gates

Verify the "free" column empirically in **S5** — Xcode's free-provisioning restrictions have moved
between releases and the authoritative answer is what your Xcode says today.

| Capability | Free personal team | Paid ($99/yr) | Needed by |
|---|---|---|---|
| Build & run on your own iPhone | ✅ — **7-day expiry**, re-sign weekly, max 3 apps per device | ✅ 1 year | M4 |
| Custom URL scheme (`orchestr://`) | ✅ | ✅ | M4 |
| ntfy notifications | ✅ (Apple is not involved) | ✅ | M8 |
| **Push Notifications (APNs)** | ❌ | ✅ | **M9** |
| **Time Sensitive Notifications entitlement** | ❌ | ✅ | **M9** |
| **App Groups** (shared container) | ❌ | ✅ | **M9** (NSE), M11 (widgets) |
| **Keychain Sharing** (access group) | ❌ | ✅ | **M9** (NSE reading the token on a locked device) |
| Notification Service Extension | target builds, but useless without the two rows above | ✅ | M9 |
| Widgets | extension builds; timeline data needs the App Group | ✅ | M11 |
| Live Activities (local start) | likely ✅ | ✅ | M11 |
| Live Activities (**push**-to-start / push updates) | ❌ | ✅ | M11 |
| Control Center controls | likely ✅ | ✅ | M11 |
| TestFlight | ❌ | ✅ | never required — you can install directly |
| App Store distribution | ❌ | ✅ | **out of scope** (see below) |

**Timing:** the account is not needed until M8/M9 — realistically **three to four months in**. The
7-day re-signing during M4–M8 is annoying (reconnect the phone and rebuild each week) but not
blocking. Critical Alerts are excluded entirely: they need a separate Apple approval that is not
plausibly granted for a developer tool, so `time-sensitive` is the ceiling and Sleep Focus is
handled that way.

---

## Explicitly out of scope for v1

Named so nobody has to guess, and so the estimates above mean something.

**Platform**
- iPad, landscape on iPhone, macOS, watchOS, visionOS
- App Store distribution (a self-hosted server with a per-user APNs key cannot be App Store distributed anyway)
- Anyone other than you running it — no multi-user, no sharing, no onboarding for a stranger
- Localisation

**Function**
- Aggregating multiple Macs into one board (pair several, view one at a time)
- Editing `orchestr.config.json` from the phone beyond the reserve percentage. That file is a code-execution sink (`cclimits_cmd` is executed; `resume_message` is typed at an agent) — treat write access to it as equivalent to shell access
- A terminal emulator, or attaching to tmux from the phone
- `⌖ focus` **as a primary affordance**. It is a GET with a side effect that opens a *new* Terminal window per call, and its payoff is on a screen you are not looking at. Demoted, not deleted: `API.md` §9.15 keeps it as `POST /api/v1/agents/{ag_id}/focus` (a POST, so nothing retries it) and `UX.md` §4.7/§7.2 surfaces it only in the Session Info sheet and the long-press *On studio-mac* submenu, rate-limited to one call per agent per 60 s and never retried. The **primary** action is "send the attach command to the Mac's clipboard" — which needs a server endpoint that `API.md` does not yet define (§0.2)
- Git operations beyond `finish` — no branch creation, no manual merge, no conflict resolution
- Creating or removing worktrees
- An offline write queue. A merge-and-push that fires an hour late is worse than one that never fires
- *(not out of scope — M6 ships the chat cursor.* An earlier draft listed "chat scrollback beyond 40 turns" here while M6's own backend list ships `?after=`/`?before=` with an ETag, and `API.md` §9.11 specifies it with a `chat_max_limit` of 200. The **UI** for infinite scroll-back is what M6 defers: v1 shows the `— earliest of N loaded turns —` marker and pulls older pages only once `features[]` advertises `chat_cursor`.*)
- Commit-level zoom on the branch map
- A dispatch-progress Live Activity

**Engineering**
- Rewriting `index.html` / `map.html` for mobile browsers. The desktop board stays a desktop board
- Deleting the v1 API. It ships alongside v2 indefinitely
- Any third-party Swift package. Zero dependencies on both sides, matching the project's identity
- Any `pip install`. CI has no install step and Python 3.11 is the floor

---

## Cut lines

If the programme has to stop somewhere, stop at one of these — each is a coherent product.

| Cut | Milestones | Focused days | What you get |
|---|---|---|---|
| **Read-only** | M0–M4 | ~40 | See the fleet and read conversations from anywhere. No writes, no push. |
| **Minimum useful** ⭐ | + M6, M8 | ~55 | Reply to a blocked agent, and get told when one needs you. **This is where the app becomes something you rely on.** |
| **Full control** | + M5, M7 | ~80 | Everything the desktop board does, with realtime and affordable cellular. |
| **Native feel** | + M9, M11 | ~95 | Lock-screen reply, badges, widgets, countdowns. Needs the $99. |
| **Complete** | + M10, M12 | ~120 | The map, accessibility, and a v1 you'd show someone. |

⭐ Note that the minimum useful cut **skips M5**, so it polls at 5 s and costs real cellular data.
That is a deliberate trade: get the value, measure how much you actually use it, then decide
whether the realtime work is worth two more weeks.

---

## Where this will slip, honestly

- **Tailscale is currently `Stopped` on this Mac.** S2 may surface that HTTPS certs are not enabled on the tailnet, which is an admin-console toggle plus a re-issue. Budget a day.
- **Swift 6 strict concurrency** is the most likely source of unplanned days in M4. `@Observable` is not `Sendable`; every store must be `@MainActor`; anything crossing to the actor client must be a value type. Getting this wrong produces compile errors that look like language bugs.
- **Free provisioning's 7-day expiry** during M4–M8 means the app stops launching roughly weekly. It is friction, not a blocker, but it will cost an hour here and there and it makes "leave it running for a week and see" experiments awkward.
- **The collector rewrite (M2) touches `classify_session`'s ordering**, which is load-bearing and subtle (the current order is a deliberate fix — evidence on disk is read before any clock is consulted). Regressions there show up as wrong statuses, which is the worst possible failure mode for a triage tool. Lean hard on the existing 142 tests and add more before touching it.
- **`_closeouts` and `_jobs` are memory-only and lost on restart**, and `start.sh` restarts the server on every invocation. Any milestone that assumes job state survives is wrong until it is persisted (M7).
- **The APNs DER→JOSE conversion** is the single most likely thing to be silently wrong. A malformed signature returns `403 InvalidProviderToken` with no further explanation, which is indistinguishable from a wrong Key ID. Ship it with test vectors from day one (S1).
