# orchestra — architecture evolution & iOS client

**Start here.** This directory holds the design for two linked pieces of work:

1. **Re-architecting orchestra's state layer** so status is fresh, accurate, and pushable.
2. **A native iOS client** that drives the fleet from a phone while sessions keep running on
   the Mac.

They are one programme, not two. The backend changes exist largely *because* a phone client is
coming — see [ADR 0004](adr/0004-backend-before-ios.md) for why they are sequenced the way they
are.

---

## Current status

| | |
|---|---|
| **Phase** | C — the iOS client, phases 1–3 shipped and verified (step 9 ◐) |
| **Shipped** | steps 0–5 + identity: the board watches on its own clock, reacts to writes, and costs 5.3% of a core. An ended turn is read off the transcript rather than waited out, and delegated work — including the background tasks the CLI's own counts miss — holds the turn open. The board is told over SSE rather than asking, and the delta envelope is closed as a class: every term that can bump the version rides every frame |
| **Next** | step 8 (APNs — blocked on a key only the owner can mint) and step 6 (hooks). **Step 9 is now ◐**: the app builds clean, runs, pairs against the real board and draws it live, and every degradation path in the brief was driven rather than reasoned about — see the step 9 row and *What remains before this is an app to rely on* below. **Step 7 is done and adversarially verified**: authentication (ADR 0014), pairing and the bind (ADR 0015) — a phone scans a QR carrying a 120-second single-use ticket, exchanges it for a token, and is listed and revocable from the board. The tailnet address is detected rather than typed, `0.0.0.0` needs a flag that says what it is, and no bind beyond loopback succeeds with no device registered. The final pass drove 60 attacks against a server really bound to `100.113.110.31` and **found two defects that review had not**: `GET /api/v1/devicesX` served the device inventory to any token holder, and a token that arrived in a query string was written to `audit.log.jsonl` in full. Both fixed, both with a test that was watched red first |
| **Tests** | 709 · characterization 5,656 cases (18 sections), unchanged — none of step 7 touches `/api/state` · the QR encoder additionally checked against Apple's `CIQRCodeGenerator` module for module and decoded back by Vision (`tests/qr_ref.py`) |
| **iOS** | 96 tests (`cd ios && swift test`, ~1 s, macOS, no simulator). `xcodebuild … clean` then `build` succeeds with no source warning — the single line the log calls a warning is `appintentsmetadataprocessor: Metadata extraction skipped. No AppIntents.framework dependency found`, which is Apple's tool noting the app declares no App Intents |
| **Last updated** | 2026-07-22 |

Design documents are being generated and reconciled. Until each is listed as **settled** below,
treat it as draft.

| document | covers | status |
|---|---|---|
| [`METHOD.md`](METHOD.md) | how to change this system without shipping a silent bug — the traps, each with the incident that found it | **settled** |
| [`TRANSCRIPT-FORMAT.md`](TRANSCRIPT-FORMAT.md) | Claude Code's on-disk format as *observed*, with counts — the reference the whole observer rests on | **settled** |
| [`VERIFIED-FACTS.md`](VERIFIED-FACTS.md) | measurements and platform capabilities, taken empirically on the dev machine | **settled** |
| [`ENGINE.md`](ENGINE.md) | the component decomposition — what knows, what tells, what does | **settled** |
| [`FRESHNESS.md`](FRESHNESS.md) | killing the status lag: collector, event-driven invalidation, the status model | **settled** |
| `ARCHITECTURE.md` | system architecture, auth, push pipeline, realtime, migration | in progress |
| `API.md` | the full HTTP contract both clients consume | in progress |
| `UX.md` | mobile information architecture, every flow, the visual system | in progress |
| `IOS-APP.md` | iOS engineering plan | in progress |
| `ROADMAP.md` | phased delivery plan | in progress |

> **`VERIFIED-FACTS.md` outranks every other document.** Its contents were measured, not
> recalled. If a design doc contradicts it, the design doc is wrong.

> **Naming note.** The *project* is `orchestra`; the *checkout directories* are still
> `~/Downloads/orchestr` and `~/Downloads/orchestr-engine`. That is deliberate — a directory
> name is invisible to git, to the config (which points at the parent, `~/Downloads`), and to
> the code. Renaming them would break the `git worktree` link (absolute paths are baked into
> `.git/worktrees/*/gitdir`) and would make Claude Code treat this as a brand-new project,
> since session and memory directories are keyed by munged cwd — the same mechanic orchestra
> itself uses to map transcripts to worktrees. Not worth it. Paths in these docs point at the
> real directories.

---

## The problem, in one screen

orchestra computes state **lazily** — only when a client asks
(`cached_state()`, now `orchestra/observer.py`). Measured on a 9-worktree fleet:

```
collect_state()          1641 ms
  git_info x9            1277 ms   78%   ← five git processes per worktree, 45 spawns
  scan_sessions           335 ms   20%   ← re-tails 128KB of every transcript, every time
  claude_processes        112 ms    7%
```

Plus a 4 s server cache and a 5 s browser poll → **~10.6 s** from a real change to pixels.

Separately, `CFG["working_s"] = 90` holds `● WORKING` for up to **90 seconds** after a session
stops. That is not lag; that is the heuristic being coarse.

> **Both are now fixed (steps 1–3 and 5).** `/api/state` is a 0.8 ms dict read off a background
> sweep, a write reaches the board in ~1 s, and the 90 s window is gone: 84 % of sessions resolve
> on the CLI's own end-of-turn marker with no clock at all, and the residual falls to
> `quiet_s = 45` — a number taken off the measured misfire table, not off a feeling. The
> paragraphs above are kept as the record of what the work started from.

**The framing that organises all of this work** — three different problems needing three
different fixes:

| class | meaning | fixed by |
|---|---|---|
| **latency** | truth changed, we have not looked yet | faster collection, then push |
| **hysteresis** | we looked, but the rule holds the old value | precise write timestamps |
| **ambiguity** | the signal cannot distinguish two states | better signals (hooks) |

Conflating them is the main way this work goes wrong. Faster polling does nothing for the
second two.

And the structural blocker: **lazy observation makes push impossible.** A notification's whole
job is to reach you when you are *not* looking — but with no client attached, nothing computes,
so nothing is ever detected. See [ADR 0006](adr/0006-observation-is-continuous.md).

---

## Decisions so far

Every load-bearing choice is recorded in [`adr/`](adr/) with its context, its consequences, and
the alternatives that were rejected and why. Read these before proposing changes — most obvious
objections have already been considered and answered there.

| # | decision | status |
|---|---|---|
| [0001](adr/0001-transport-tailscale.md) | Reach the server over Tailscale, not the public internet | accepted |
| [0002](adr/0002-client-native-swiftui.md) | The mobile client is native SwiftUI, iOS only | accepted |
| [0003](adr/0003-push-apns.md) | Push via APNs, driven from stdlib Python (openssl + curl) | accepted |
| [0004](adr/0004-backend-before-ios.md) | Settle the contract once; backend first; iOS last | accepted |
| [0005](adr/0005-sse-on-threadinghttpserver.md) | SSE on the existing ThreadingHTTPServer — no rewrite | accepted, **verified** |
| [0006](adr/0006-observation-is-continuous.md) | Observation becomes continuous and client-independent | accepted |
| [0007](adr/0007-hooks-as-first-class-signal.md) | Claude Code hooks as a first-class signal source | accepted in principle |
| [0008](adr/0008-identity-addressed-mutations.md) | Mutations addressed by durable identity, never by pid | accepted, **implemented** (`orchestra/identity.py`) |
| [0009](adr/0009-api-v1.md) | The versioned API starts at `/api/v1` | accepted |
| [0011](adr/0011-measurement-supersedes-the-design-doc.md) | Where ENGINE.md and measurement disagree, measurement wins | accepted |
| [0012](adr/0012-the-watcher-is-evidence-not-truth.md) | The kqueue watcher, built — and it is evidence, not truth | accepted, **implemented** (`orchestra/watcher.py`); supersedes ENGINE.md §10 |
| [0013](adr/0013-plain-http-over-the-tailnet.md) | Plain HTTP over the tailnet, with a scoped ATS exception | accepted; supersedes `ARCHITECTURE.md` §5's TLS |
| [0014](adr/0014-per-device-bearer-tokens.md) | Per-device bearer tokens, checked in one place | accepted, **implemented** (`orchestra/auth.py`) |
| [0015](adr/0015-pairing-and-the-tailnet-bind.md) | Pairing by QR, and a bind that cannot be got wrong | accepted, **implemented** (`orchestra/pairing.py`, `orchestra/qr.py`, `orchestra/tailnet.py`) |

---

## Development path

Each step is independently shippable and independently valuable. Steps 1–2 make the **existing
browser board** dramatically faster and carry no iOS risk at all — they would be worth doing
even if the phone client were cancelled.

| step | what | result |
|---|---|---|
| **0** ✅ | three unattended-path bugs | an armed 3am resume fired at all; one resume stopped costing 3× usage; 27/654 transcripts stopped quoting the harness back as you |
| **1** ✅ | git storm: 5 spawns → 2, parallelised | `collect_state` 1641 → 506 ms |
| **2** ✅ | publish point — background sweep, versioned immutable snapshots | `/api/state` 506 ms → **0.8 ms**; push becomes possible at all |
| **—** ✅ | make the sweep affordable: git on a 15 s cadence, transcript memo, `(pid,start)` cwd memo | 55% → 15% of a core |
| **3** ✅ | kqueue watcher — react to writes instead of sweeping | idle **5.3%** of a core; write→board ~1 s (was a 30 s cadence); 220 fds |
| **—** ✅ | identity-addressed mutations (ADR 0008) | a recycled pid is refused, not delivered to the wrong agent |
| **5** ✅ | **status model — `working_s = 90`** | phase 1: the CLI's own end-of-turn marker is read positionally and wired into `classify_session`, so **84 %** of in-window sessions resolve by observation and stop waiting out the window (median lateness removed: the full 90 s). Phase 2: the residual 16 % fall to `quiet_s = 45`, chosen off the misfire table (2.71 % against 5.80 % at ENGINE.md's 25), with `settle()` making escalation instant and de-escalation dwell 3 s. Phase 3: `delegated` stops under-counting — a tool_use that LAUNCHED background work counts until its `<task-notification>` arrives (`delegated_s = 600`), taking the end-of-turn misfire rate from **5.09 % to 4.42 %** over 904 replayed claims at no measured cost. Phase 4: the last two inherited numbers stop being `working_s` under another name — `block_grace_s = 60` (the p95–p99 band of genuine tool-run silence, 1.03 % false ■ BLOCKED against 0.82 % at 90, and free on the board's own working set), and `orphan_grace_s` stays at **90** because the measurement says the timer is standing in for a guard that does not exist. Phase 5: `age_s` leaves the wire — both clients animate from `last_write_at`, and the payload is time-invariant end to end (`_UNDIFFED_SESSION_KEYS` is now empty, so a session is diffed WHOLE and a write bumps the version where a clock tick never can). The last inherited number gets its own key: `subagent_grace_s = 180`, measured on 18,145 subagent runs where the tree writes an order of magnitude denser than a conversation (p99 27 s against 408 s) — the ⚙ stopped blinking off mid-flight on 1 run in 15 (6.57 % → 1.17 %). |
| **4** ✅ | SSE + delta protocol; retire the 5 s browser poll | **server**: `GET /api/events` streams one frame per version bump off the existing publish Condition — never a poll, so a sweep that changes nothing sends nothing. A snapshot on connect, `Last-Event-ID` resumed through `delta_since` (delta in the ring, full snapshot when unknown, too old, or *ahead* of a restarted server), `: keepalive` on idle, and a hard `sse_max_subscribers = 32`, refused with a 503 that names the cap. Measured against the real handler: bump → bytes at **32** clients p50 **1.30 ms** / p95 1.73 ms, `GET /api/state` **0.7 ms** alongside them, 34 threads and **0.21 %** of a core, and every slot and thread back after 32 rude RSTs. **browser**: ONE `EventSource` per browser, held in a `SharedWorker` (`stream.js`, served from `/stream.js`) and fanned to tabs over `BroadcastChannel` — the ceiling is the browser's 6 connections per origin, not the server. The 5 s poll is demoted to the floor, not deleted: `SharedWorker` is absent in some Safari private modes, and demo mode takes that path by design (no sweep thread, so `/api/events` answers 503 rather than promising a stream it cannot serve). Driven against the real nine-worktree fleet: snapshot on connect, then `v=5 base=4` carrying the **2** cards that changed rather than all 9, with a concurrent `GET /api/state` at **3.3 ms**. |
| **6** ⬜ | Claude Code hooks; reconcile signal sources by rank | `BLOCKED`/`YOUR TURN` become observed, not inferred |
| **7** ✅ | auth, device pairing, tailnet bind | **auth shipped** (ADR 0014, `orchestra/auth.py`): `orc1_<devid>_<secret>` per device, stored as sha256 so the registry is not a credential, compared with `hmac.compare_digest`, and checked in `Handler.parse_request` — *before* `handle_one_request` looks up `do_<METHOD>`, so no route can forget it and none added later starts out unguarded. The rule is one sentence: **loopback is trusted, everything else must present a valid token, a presented credential is always checked, and a page from another site is not loopback** — that last clause closing a CSRF hole that predates this work, since a site you merely visit also speaks from 127.0.0.1 and `POST /api/send` types at an agent. `GET /api/health` was the only exempt route and carries nothing that varies with the fleet (pairing added the second and last one, below). Every mutation and every refusal is appended to `audit.log.jsonl` (who/what/when, never the body — that would copy the asset the tokens protect); auth failures cost from a 10/min per-IP budget that the local board can never be throttled by; `--host <tailnet-ip>` now REFUSES to start with no device registered instead of warning and binding anyway. 95 tests, 40 mutations all caught, and the route-coverage test reads the routes out of `server.py`'s AST so it cannot rot. **Second half now shipped too** (ADR 0015): a QR carries a 120-second, single-use Crockford code — never the token, because a QR on a screen is visible to the room and to every screenshot of it forever — which the phone exchanges at `POST /api/v1/pair` for a fresh 256-bit secret. The encoder is stdlib (`orchestra/qr.py`, ~400 lines of ISO 18004) and is **not** trusted for looking like a QR code: it is compared to Apple's `CIQRCodeGenerator` MODULE FOR MODULE across versions 1–10 and all four EC levels, and decoded back by Vision — which found three defects that all looked perfect, including a format field placed least-significant-bit-first whose 1,681 data modules were every one correct. Device management (`/api/v1/devices`) answers to **this machine holding no token**, so a stolen phone cannot revoke the device that would have revoked it. The tailnet address is DETECTED and then actually bound to prove it (never parsed), `--host 0.0.0.0` is refused in favour of a differently-named `--bind-every-interface` that a config file cannot set, and a non-loopback bind starts a second listener on 127.0.0.1 — found by driving it, because a tailnet-only bind takes the board away from the browser AND locks the person at the keyboard out of device management, since the Mac talking to its own tailnet address is not loopback. **Left:** the Host allowlist, and the first device is still minted at a shell |
| **8** ⬜ | APNs event pipeline | alerts reach a locked phone |
| **9** ◐ | iOS client, against the settled contract | **phases 1–3 shipped and adversarially verified; it is an app you can use, not one to rely on unattended yet.** `ios/`, 53 Swift files, one target, Swift 6 language mode with `SWIFT_STRICT_CONCURRENCY = complete` and warnings-as-errors, 96 headless tests (`swift test`, ~1 s, no simulator). **Works, driven against the real board on `100.113.110.31:4269`, every screen screenshotted and looked at**: pair by QR ticket → the nine-worktree fleet with its triage headline, worktree detail, session chat, limits and per-account detail, a server/diagnostics screen; the board is TOLD, not asked — one `GET /api/events` socket, `stream.js`'s applier ported card-for-card, and an untracked file created in `ConfidAi6` moved the phone from no-Δ to `Δ1 uncommitted` and `v94 → v96` with no interaction. Mutations are wired and their refusals are the server's own sentence verbatim (`/api/send`, `/api/dispatch`, the `/api/finish` two-step, `/api/resume/*`). **The degradation paths were driven, not reasoned about**: server killed mid-stream → `reconnecting… (2) · showing data from 6s ago`, escalating to `orchestra isn't running · showing data from 31s ago` plus a banner, and it recovered by itself when the server came back *with the version reset 96 → 1*; a revoked token → `this device isn't paired · device 'iPhone 17 Pro Max' was revoked; pair again to get a new token`; an unresolvable `ts.net` name → `tailnet unreachable`; a tailnet IP with no ATS entry → `this build cannot reach that address`, which names the plist rather than blaming the Mac; background → foreground kept one socket (`1 → 0 → 1`, same pid) and resynced with `resyncs: 0`. **Stale is marked** — `staleness` is a pure function of link state and an explicit clock, the bar grows a second dated line, and a 401 clears the board rather than dimming it. **The final pass found one defect review had not**: the side fetch recorded its cadence clock only on SUCCESS, so a phone whose token had been revoked polled `/api/state` at **1 Hz forever** — 30 refusals in 26 s in `audit.log.jsonl` — which spent the server's 10/min per-IP auth budget in about a second and replaced the honest *"pair again"* sentence with *"the server said 429"*. Fixed, with the clock moved to the ATTEMPT and 401 taken off the poll entirely; three mutations watched red, and re-driven live: 3 refusals in 30 s and the right sentence on screen. **NOT shipped: push** (no APNs key exists — §8), **no undo for a launch** (`/api/kill` does not exist), **no idempotency on the wire** (two taps of Launch start two agents; the client-side lock is defeated by a second client), and **the ATS exception is a hard-coded IP literal**, so a Tailscale reassignment needs a plist edit and a rebuild |

**Why 5 before 4.** Notifications fire on status *transitions*. Building the SSE stream and the
APNs pipeline on a status model we already know is wrong means every transition changes
underneath them later, and the notifier gets rebuilt. Settle what a status means, then stream it.
Step 3 is also what makes step 5 possible: the 90 s window existed because a stateless collector
could only ask "is the mtime within 90 s?" — precise write timestamps now exist and are unused.

## What remains before this is an app the user would rely on

Written after the final verification pass of 2026-07-22, which built the app from
clean, ran it on a simulator, screenshotted **fourteen** screens and looked at
every one, and drove the whole flow — pair, fleet, worktree, chat, limits,
account, server, mission, finish, resume — against the live nine-worktree board.
Ordered by what would bite first.

| # | what | why it matters, and what it would take |
|---|---|---|
| 1 | **push does not exist** | the whole point of a phone client is to be told when you are *not* looking. Today the app knows nothing while it is in the background — `stop()` closes the socket deliberately, which is right, and leaves nothing behind. Step 8, and blocked on an APNs key only the owner can create |
| 2 | **two taps of Launch start two agents** | the server has no idempotency of any kind (open item below). The client refuses its own second tap and never auto-retries a mutation, and both of those hold — but a second phone, or the desktop board, defeats a client-side lock, and that is the case UX.md §7.1 principle 3 names. It wants `Idempotency-Key` on the server |
| 3 | **there is no undo for a launch** | `POST /api/kill` does not exist. A mission dispatched by accident is stopped at the Mac. The app does not pretend otherwise, which is honest and is not the same as safe |
| 4 | **the ATS exception is a hard-coded IP literal** | `Orchestra-Info.plist` lists `100.113.110.31` exactly. Tailscale reassigning the Mac's address, or pairing with a second Mac, means no plain-HTTP load leaves the phone at all. The app diagnoses this precisely rather than blaming the network — driven against `100.79.218.31`, it says *"this build cannot reach that address… this build's Info.plist needs an NSExceptionDomains entry for that host — see ADR 0013. Nothing on the Mac is wrong."* — but the fix is a plist edit and a rebuild, which a user cannot do. The `ts.net` wildcard entry is already there and would cover this, except that `pairing._server_facts` advertises the **bound address**, so the QR hands the phone an IP literal. Making the QR carry the MagicDNS name when one exists would close it without touching the client |
| 5 | **a revoked phone has no route back to pairing from the screen that tells it** | the failure screen now says the right sentence and offers *Try again*. Re-pairing is *Server → Unpair this device*, two taps away and unsignposted. A **Pair again** button on that screen is small and has not been built |
| 6 | **the closeout's second step has never been seen with a brief actually outstanding** | `✕ close` and the self-clearing `pending` row are built against `closeout_sent` and are decode-tested. Driving them means typing a ~600-character brief at a live agent, which this pass did not do to somebody's running work |
| 7 | **a real dispatch has never been launched from the phone** | the refusal paths and the whole job → poll → result path were driven for real against a worktree that does not exist. The success branch is modelled and decode-tested |
| 8 | **`/api/state` is polled at 5 s whenever the stream is not live** | correct as a fallback, and it is now the *only* cadence a failing fetch runs at (fixed this pass). There is still no backoff ladder on the side fetch the way there is on the stream, so a Mac that is off costs a request every five seconds for as long as the app is foregrounded |
| 9 | **one module, not IOS-APP.md §1.2's six** | directories carry the layering and no file crosses one, but nothing structurally prevents it. Splitting into real SwiftPM targets is additive |

## Open items — deliberately deferred, not forgotten

| item | why it is parked | where |
|---|---|---|
| `cpu` / `etime` are the last now-derived fields on the wire | swept for (both passes: compose twice with the world held still, and inventory every key on all ten routes). They are the ONLY fields that move on the clock alone, and both are proc readings the board draws verbatim: `cpu` is a rate with no absolute twin, `etime` has one in principle (a process start stamp) but arrives from `ps` pre-formatted. Neither reaches a status, an availability or dispatch, and `_UNDIFFED_PROC_KEYS` keeps them out of the version. The now-THRESHOLDED fields — `status`, `subagents_active`, `pending_bg_tools` and everything derived from a status — stay by design: what a threshold crossing MEANS is the payload's job, and they change on a crossing, not on a tick | `observer.py`, `procs.py` |
| `procs_known` is not merely unwired — **the `unknown` status has nowhere to land** | The guard for a wholesale `ps`/`lsof` failure — "never claim ENDED, never claim FREE" — exists in `classify_session`'s signature and in the characterization, and no call site supplies it, so a blind probe publishes ○ ENDED for every session past its orphan grace. Driven in the final verification, wiring it today does **not** work: `collect_state` raises `KeyError: 'unknown'` at `transcripts.py:955` (`rank[s["status"]]`), and the same missing case sits in `observer.py`'s `rank`, its `severity`, and its `counts` dict. Worse, the one table that does not crash gets it **backwards**: `card_availability(["unknown"], has_live=False)` returns **`"free"`** — a probe failure hands the worktree to `dispatch.auto_target`, which is the exact failure the guard was written to prevent. So this is not one call site; it is four tables with no case for `unknown`, one of which must be fixed *before* the guard is wired. It is also why `orphan_grace_s` cannot be shortened on measurement alone: with 0 observed probe failures there is no distribution to place a number in, and the timer is the only thing standing in for a guard that would not work if it fired | `status.py`, `transcripts.py`, `observer.py` |
| `card_availability`'s 18 characterization cases pin **nothing** | `characterize.py` calls it as `card_availability(sessions, procs)` — a list of session *dicts* and a list of proc *dicts* — where the function takes a list of status *strings* and a *bool*. `"working" in [{"status": "working"}]` is False, so every one of the 18 outputs is decided solely by whether `procs` is empty, and the golden currently pins `needs_input` → **`"free"`** and `working` → **`"free"`**. Production calls it correctly (`observer.py:219`) and six correctly-shaped assertions in `test_orchestra.py` cover the real ladder, so nothing is broken on the board — but the one function that gates "safe to point a new agent here" has 18 cases of the safety net pinning a degenerate call, and neither the net nor the unit tests reach `unknown`. Fixing it is a deliberate re-record and wants its own commit with mutations | `tests/characterize.py`, `status.py` |
| `flicker_dwell_s = 3.0` is the last threshold with no measurement | Every other number in `config.py` now carries its distribution and its misfire rate — `quiet_s` 45, `delegated_s` 600, `block_grace_s` 60, `orphan_grace_s` 90, `subagent_grace_s` 180, and `idle_s`/`git_s` beside their tables in `observer.py`. This one is still `ENGINE.md` §6.3(a)'s figure taken on faith, and the documents do not even agree: `FRESHNESS.md` §R4 says `MIN_DWELL_S = 2`. It is the harmless direction — the dwell can only ever delay *good* news, by at most `dwell_s`, and `settle` was fixed so it does not stack on `quiet_s` — but the population it should be chosen from (how long a de-escalation that is about to be *reversed* stands) has never been measured | `config.py`, `status.py` |
| `skip_perms` is poisoned by `claude`'s own helper processes | It is `all("--dangerously-skip-permissions" in p["cmd"])` over the worktree's procs, and `claude bg-spare` / `claude bg-pty-host` match `claude ` while carrying no such flag. Observed live on `ConfidAi7`: three procs, two real agents that both skip permissions and one `bg-spare` helper, so the whole worktree reads `skip_perms = False` and its agents become eligible for a ■ BLOCKED they can never resolve — over 25 minutes of board ticks, **every** ■ BLOCKED on the fleet came from that one worktree. The helper is also *paired to a session* by `pair_sessions_with_procs` — the same session held pid 70327 for 797 consecutive ticks, so its status was read off a 6-hour-old PTY daemon rather than an agent. Needs its own measurement of which `claude <subcommand>` forms are agents | `transcripts.py`, `procs.py` |
| `working_s` is now a fallback nothing on the board reaches | all four graces are named keys that `scan_sessions` passes explicitly, so `CFG["working_s"]` survives only as the `None` default inside `classify_session`'s signature. Harmless, and deleting it is a signature change that would touch every characterization case — but it is a number in the config file that no longer decides anything, and the README table now says so | `config.py`, `status.py` |
| **DNS rebinding is not closed**, and side-effecting GETs are reachable cross-origin | `same_origin` compares the `Origin`'s authority against the `Host` header, which needs no configuration and no knowledge of what we were bound to — and `evil.com` resolved to 127.0.0.1 satisfies it, because both say `evil.com`. The MUTATION half of that attack is closed anyway: a JSON body forces a CORS preflight this server refuses and never answers with `Access-Control-Allow-Origin`. What is left open is a HOST ALLOWLIST (API.md §2.3 step 2), which wants the bound address plumbed into the check, plus the two GETs that act — `GET /api/focus` raises a window, `GET /api/limits?refresh=1` shells out — and are reachable by an `<img>` or a no-cors `fetch`. Annoying rather than dangerous, and `/api/v1` already makes focus a POST | `auth.py`, ADR 0014 |
| **the LEGACY surface still routes by `startswith` on the raw path** | `/api/statefoo` reaches the state handler, `/api/healthXYZ` reaches health — API.md §16.3 lists it as a legacy behaviour v1 fixes. It is not a hole today and the direction is why: every legacy path shadows a route with the SAME authentication, and the two routes whose answer depends on an exact match — `auth.EXEMPT` and `auth.ADMIN` — are matched exactly and by segment, so a shadow is a 401, never a free pass. `/api/v1` is now exact (below), which makes the asymmetry deliberate rather than accidental. Closing it on the legacy half is a routing table, and it lands with `/api/v1` proper rather than as a patch to nine `elif`s | `server.py`, API.md §2.3 step 5 |
| the audit records that a request ARRIVED, not what it did | the seam is `parse_request`, before the route, which is exactly what makes it impossible for a route to forget — and the cost is that a refused dispatch and a successful one look identical in the file. Pairing is the one route that writes a second line, because there the outcome is a credential. A general second write is a second thing to forget | ADR 0014, `auth.audit` |
| a stolen token that only ever READS leaves no evidence | `audit.log.jsonl` records mutations and refusals. Auditing reads would write a line every few seconds forever — the board polls `/api/state` — and bury the eleven lines that matter in a year of noise. The honest fix is a per-device read counter in `meta`, not a log nobody can grep | ADR 0014 |
| scopes (`read`/`act`/`admin`) are designed and not built | one token grants everything. API.md §2.2 splits them and issues two per device; a phone that can read but not act is not the product, so the split buys nothing yet — and a half-built ladder is worse than an honest absence, because it invites the belief that a `read` token is safe to hand out. The one row that could not wait, `admin`, is built **without** the ladder: device management answers to this machine holding no token, which is strictly stronger than a scope and becomes `scope == admin` unchanged when the rest arrives | API.md §2.2, §2.5a, ADR 0014, ADR 0015 |
| **the FIRST device is still minted at a shell** | Pairing shipped (ADR 0015) and every device after the first is a QR — but the bootstrap is genuinely circular: the tailnet bind refuses with nobody registered, and pairing needs the phone to be able to reach the server. So device one is `--add-device` and its token is carried by hand. Closing it needs a mode that binds the tailnet with NO device registered and serves only `POST /api/v1/pair` until one exists — which is the silent wide exposure ADR 0013 forbids unless it is built carefully, and it wants the lockdown machinery (API.md §2.3 step 7) that is also deferred. Doing it badly is worse than the one-time paste | ADR 0015, `auth.bind_refusal` |
| the pairing window does not survive a restart | deliberate — a door that reopens by itself is a door nobody closed — but it means a board that restarts mid-pairing gives the phone `pairing_not_open`, which reads like a network fault. The page polls and would say so; a native client has to be told to re-scan | `pairing.py` |
| `orchestra/qr.py` stops at version 10 | 213 bytes at level M against a ~45-byte payload, and above version 6 the block table triples for capacity nothing here can use. `encode` RAISES rather than truncating, so the boundary is loud. A MagicDNS name would have to exceed 190 characters to reach it | `qr.py` |
| the audit log is never rotated, and tokens never expire | ~150 B a mutation at a human click rate, so size is not the argument for rotation; the argument is that nothing prunes it. Revoke-and-remint is the whole rotation story today | `auth.py` |
| transcript memo can be defeated by a size+mtime_ns+inode-identical rewrite | adversarial only — transcripts are append-only; the 60 s cold reconcile bounds it | ADR 0011 |
| `dirty` cannot be memoised | it is the working tree; no cheap stat sees an edit. Bounded by `GIT_S` and dated by `freshness["git"]` | ADR 0011 |
| a dispatch's new branch is not nudged | the branch is cut by the launched agent minutes later, with no signal back; bounded by `GIT_S` | `dispatch.py` |
| `ENGINE.md` is stale in seven places, `FRESHNESS.md` in two | nine rows in ADR 0011's table now, not the four this row used to claim — steps 5.3–5.5 added `delegated`'s definition, `block_grace_s`'s derivation and `orphan_grace_s = 10`. Measurement supersedes them; the docs are a design record, not rewritten | ADR 0011 |
| the transcript corpus is ~5 GB / 18,773 files, +1,000/day | orchestra's own inputs are a slow disk leak; wants a retention policy | — |

**The load-bearing interface is the delta/event format introduced at step 4.** The browser
consumes it over SSE, the APNs pipeline is derived from it, and the Swift client reconciles
against it. Design it once, correctly, for all three consumers — that is the whole point of the
sequencing in ADR 0004.

Its envelope is now **closed, and closed as a class rather than as a list**. `publish` bumps `v`
on exactly three terms — the stopwatch-stripped cards, `counts`, `other_procs` — and all three
ride every frame; a term that can move the version and cannot ride one tells a client "something
changed" and gives it no way to learn what. Two tests pin it from both ends, because either half
alone rots: every bump term must reach a delta consumer, **and** nothing outside those three may
bump at all, so adding a fourth fails the suite instead of failing a phone. `other_procs` was the
one that was missing, and it now rides whole rather than being tracked in the changed-keys ring:
measured on the live 9-worktree fleet that is 1,172 B on a median 7,853 B delta (15 %, 2.9 % of a
41,384 B snapshot) against 22 version bumps in 150 s — 172 B/s per subscriber — and ring-tracking
it would save 166 B/s in exchange for a second changed-set, i.e. a second source of truth about
what moved, which is the exact shape of the bug being closed. Per-entity change tracking gets
paid for once, generally, in `/api/v1` §7.1's op address space, where `other` is already a leaf
beside `counts`, `free` and `order`. Everything absent from a frame is absent deliberately and
says why in `delta_since`: `drift`/`sweep_ms` (diagnostics, no vote, `/api/stats`),
`hostname`/`user` (constant for the process, side fetch), `free_worktrees` (a pure function of
the cards — on the wire it would be a second copy that can disagree) and `resumes` (owned by
`resume.py`, which the observer does not watch, so arming one moves no version and it could not
ride this stream however the frame were shaped).

---

## Start with these two

The ADRs record *decisions* and `VERIFIED-FACTS.md` records *numbers*. The transferable knowledge
is in two other places, and a newcomer should read them before touching code:

- **[METHOD.md](METHOD.md)** — every defect in this rebuild was something that *looked fine and
  was quietly wrong*: a monkeypatch silently disarmed by a module split, a test that could not
  have failed, a stale `.pyc` making a mutation appear caught, a safety net pointed at demo mode
  and therefore at nothing. Ten rules, each with the incident that earned it, plus the technique
  that did most of the work: **counterfactual replay** — scoring new logic against a corpus whose
  answers are already written down.
- **[TRANSCRIPT-FORMAT.md](TRANSCRIPT-FORMAT.md)** — what Claude Code actually writes to disk,
  measured with counts. The turn-boundary marker that reads as 3 % or 82 % depending on which
  question you ask; why `run_in_background: true` is neither necessary nor sufficient; the three
  on-disk shapes a task notification takes; why file mtime is a lying clock.

## How to pick this work up

1. Read `VERIFIED-FACTS.md`. Do not re-derive its measurements; do not contradict them.
2. Read the ADRs in order. They are short and they carry the *why*.
3. Read `ENGINE.md` for the component boundaries, then `FRESHNESS.md` for the mechanism.
4. Check *Current status* above for where the work actually is.
5. Re-measure before optimising. The baseline command:

   ```bash
   python3 - <<'EOF'
   import time
   import orchestra as o          # run from the repo root; ADR 0010 made it a package
   o.load_config()
   t = time.time(); o.collect_state()
   print(f"collect_state: {(time.time()-t)*1000:.0f} ms")
   EOF
   ```

## Conventions

- **New decisions get an ADR.** Copy the shape of an existing one: Context, Decision,
  Consequences, Alternatives rejected. Number sequentially. Never edit an accepted ADR to
  reverse it — write a new one that supersedes it, and mark the old one `Superseded by ADR
  NNNN`. The history is the value.
- **Tests stay stdlib `unittest`**, matching `tests/`. Zero dependencies, same as the app.
- **Preserve the render invariants.** `index.html` encodes hard-won rules — the tick must not
  clobber open controls, and the board must not re-sort under the cursor. Any push-based update
  path must preserve them; they are stated testably in `FRESHNESS.md`.
- **Zero dependencies is a real constraint**, not a slogan. Trading it requires an ADR.
