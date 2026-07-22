# orchestra for iOS — phases 1–2

A native SwiftUI client for the orchestra board.

* **Phase 1** got a token onto the phone and drew the Fleet list from a real
  server.
* **Phase 2** made the board **live**: one `GET /api/events` socket, deltas
  applied by a port of `stream.js`'s applier, ages animated locally off absolute
  timestamps, a connection state that is honest about staleness, a lifecycle that
  drops the stream on background and resyncs on foreground, and the read-only
  screens the IA calls for — worktree detail, session chat, limits, account
  detail, server.

Actuation and push are later phases. Nothing here is load-bearing on push: it
lands as a registration call and a delegate.

## Build and run it — the only way this is verified

Everything below runs from a shell. No Xcode GUI, no Apple ID, no team.

```sh
# 1. the headless suites — models, transport classification, rules, formatters
cd ios && swift test                    # 64 tests, ~1 s, macOS, no simulator

# 2. the app
xcodebuild -project ios/Orchestra.xcodeproj -scheme Orchestra \
           -configuration Debug \
           -destination 'platform=iOS Simulator,name=iPhone 17 Pro Max' \
           -derivedDataPath /tmp/orc-dd build

# 3. put it on a simulator
xcrun simctl boot "iPhone 17 Pro Max"
xcrun simctl install booted /tmp/orc-dd/Build/Products/Debug-iphonesimulator/Orchestra.app

# 4. pair it against a real server, then LOOK at what it drew
python3 -m orchestra --port 4269 --tailnet          # in the engine checkout
curl -s -X POST -H 'Content-Type: application/json' -d '{}' \
     http://127.0.0.1:4269/api/v1/devices/pair/open | python3 -c 'import json,sys;print(json.load(sys.stdin)["url"])'
SIMCTL_CHILD_ORC_PAIR_URL='orc://p?h=…&p=4269&c=…' \
     xcrun simctl launch booted sh.orchestra.app
xcrun simctl io booted screenshot /tmp/board.png

# 5. every OTHER screen, without a finger
SIMCTL_CHILD_ORC_SCREEN=server                     xcrun simctl launch booted sh.orchestra.app
SIMCTL_CHILD_ORC_SCREEN=limits                     xcrun simctl launch booted sh.orchestra.app
SIMCTL_CHILD_ORC_SCREEN=limits:default             xcrun simctl launch booted sh.orchestra.app
SIMCTL_CHILD_ORC_SCREEN=wt:ConfidAI2               xcrun simctl launch booted sh.orchestra.app
SIMCTL_CHILD_ORC_SCREEN=chat:ConfidAI2/account2/<sid>  xcrun simctl launch booted sh.orchestra.app

# 6. drive it: cause a real change and watch the board move
touch ~/.claude-account2/projects/*/<sid>.jsonl     # → delta on the wire in ~1 s
```

`ORC_SCREEN` is the second `#if DEBUG` seam and it exists for the same reason as
the first: **a phase ends with the app run and LOOKED at**, and `xcrun simctl`
can install, launch and screenshot but cannot tap. An accessibility-driven click
is not a way out either — System Events answers `-25204` without a permission
grant a headless run does not have. So every screen gets one scriptable way in,
pushing exactly the `FleetRoute` values a tap pushes, through exactly the same
`navigationDestination`.

`ORC_PAIR_URL` is a **`#if DEBUG` test seam**, and it exists because a simulator
has no camera and cannot be typed into from a script — `xcrun simctl openurl`
does reach the app, but iOS puts a system *"Open in orchestra?"* dialog in front
of it that needs a finger. It takes the same `PairingTicket` through the same
`PairingStore.pair` as the camera and the typed form. It is a way to press the
button, not a second way to pair.

## Shape

```
ios/
├── Package.swift              swift test over Sources/Orchestra minus UI
├── Orchestra.xcodeproj/       one app target, file-system-synchronised groups
├── Orchestra-Info.plist       ATS, camera, URL scheme
├── Orchestra.entitlements     keychain access — see "the second-launch bug"
├── App/                       composition + the two views that need UIKit
└── Sources/Orchestra/
    ├── Model/    Wire · Enums · StreamFrame · Chat · Limits · Pairing
    ├── API/      OrchestraClient (actor) · EventStream · SSE · Endpoint
    │             OrchestraError · Keychain
    ├── Rules/    Triage
    ├── Format/   RelativeTime · TextRules
    ├── Store/    FleetStore · FleetApplier · ChatStore · LimitsStore
    │             PairingStore                    (@MainActor @Observable)
    └── UI/       Palette · Typography · StatusStyle · ConnectionBar
                  FleetView · WorktreeDetailView · ChatView · LimitsView
                  ServerView · rows
```

**The receive path, end to end.** `OrchestraClient.openEvents` opens the socket
and hands `Data` chunks to `SSELineSplitter` → `SSEDecoder` → `StreamFrame` →
`FleetApplier` → `FleetStore` → the views. Everything that can be wrong in a way
no amount of tapping would reveal — the line splitter, the SSE state machine, the
delta rules, the staleness rule — is a value type with no I/O and no clock, and
is covered by the 29 tests phase 2 added.

**`FleetApplier` is a PORT of `stream.js`'s `Fleet`, not a second
interpretation.** That file is the browser's applier, it is tested against the
Python reference (`tests/test_stream_js.py`), and it is what the desktop board
runs today. Two appliers that disagree about one rule produce two boards that
disagree about the fleet, and the disagreement is invisible until it matters. So
every rule in `FleetApplier` names the lines of `stream.js` it comes from.

**One module, not IOS-APP.md §1.2's six.** The layering there is enforced by the
SwiftPM graph — `OrchestraCore` structurally cannot see `OrchestraStore` — and
that is the right shape. It is deliberately not bought yet: two build systems
over one source tree cannot both be right about `import` statements, and a
hand-written `.pbxproj` that references a local SwiftPM package is the most
fragile thing that could live in this directory. Directories carry the layering
for now and no file crosses one; splitting them into real targets is additive.

Swift 6 language mode, `SWIFT_STRICT_CONCURRENCY = complete`, and
`SWIFT_TREAT_WARNINGS_AS_ERRORS = YES` on both configurations. There is exactly
one `@unchecked Sendable` in the app (`SessionBox` in `QRScannerView.swift`) and
it exposes two methods AVFoundation documents as callable off the main queue.

## What the running server actually serves — where the documents are wrong

Modelled from a live nine-worktree fleet on 2026-07-22 (`GET /api/state`,
38,615 B; `GET /api/events` snapshot frame), not from `API.md`. Reported here
rather than fixed, per the house rules.

### The wire

| # | claim | what the server does |
|---|---|---|
| 1 | IOS-APP.md §3.3 — `background_shell` | the key is **`bg_shell`** (`transcripts.py:953`), and it is present ONLY when true, as is `tool_running` |
| 2 | IOS-APP.md §3.3 — `turnEnded: Bool` | **`turn_ended` is absent from the payload entirely on some sessions** (3 of 36 live). A non-optional `Bool` throws `keyNotFound` and takes the whole 38 KB board with it. This is the single sharpest edge on the wire |
| 3 | IOS-APP.md §3.3 lists `closeoutSentAt` and `cardRev` on `Worktree` | neither string appears anywhere in the server. There is no `card_rev` staleness token |
| 4 | IOS-APP.md §3.3 does not model `pending_bg_tools` | it is on every session and it feeds `busySignal` |
| 5 | task brief: SSE frame is `{type, v, base, at, order, cards, counts, other_procs, freshness}` | `base` is on the **delta** branch only; a snapshot has no `base` |
| 6 | task brief / API.md: `/api/state` is the board snapshot | `/api/state` and the SSE frame are **different shapes**. `/api/state` has `worktrees` (a list), `hostname`, `user`, `free_worktrees`, `resumes`, `generated_at` — and **no `v`, no `order`, no `freshness`**. The frame has `cards` (a dict), `order`, `v`, `freshness` — and none of the four `/api/state`-only terms. `delta_since`'s docstring is the authority and justifies each omission |
| 7 | UX.md §3.1.2 — five-valued `availability` | the server ships the legacy **four** (`free`/`attention`/`waiting`/`busy`). UX.md says so itself: *"a required change to API.md §10.2, not a description of it."* Not landed. `Rules/Triage.swift` derives the split client-side, against principle 3, and says so |
| 8 | UX.md §3.1.3 — `counts: {sessions: {...}, cards: {...}}` | `observer.py:245` writes six flat session-level keys and nothing else. The card tallies the headline needs are derived in `Triage.cardCounts` |
| 9 | UX.md §3.1.4 — the wire carries `activity_at` | it carries **`last_write_at`** (IOS-APP.md §0's alias table has this; UX.md does not) |
| 10 | UX.md §3.1.4 — `topic`/`last_user`/`last_assistant` may live only on a detail route | the live board carries all four prose fields, which is UX.md's own resolution (a). The row is built against that |
| 11 | IOS-APP.md §1.5 — "the server serves HTTPS on the tailnet… no blanket exception is needed" | superseded by ADR 0013: **plain HTTP, no TLS**, `"tls": false` in the pairing response. §1.5's whole ATS paragraph, its `NSExceptionMinimumTLSVersion` and its SPKI pinning are stale |
| 12 | `git.ahead`/`git.behind` | **null, not zero**, when a branch has no upstream — `# branch.ab` is absent from porcelain v2 rather than `+0 -0`. 2 of 9 live worktrees. `↑0` would be a measurement this client never made |
| 13 | `git.commit` | nullable |
| 14 | mutations need `Content-Type: application/json` | undocumented in the alias table; a POST without it is **415 `content_type_required`**, which is the CSRF guard. A Swift client that forgets it gets a status nothing in API.md explains |

### The stream, added in phase 2 — where the documents are further from the wire

Captured from a live `GET /api/events` on 2026-07-22 and decoded frame by frame.

| # | claim | what the server does |
|---|---|---|
| 15 | IOS-APP.md §5.1 — the stream opens with `event: hello` carrying `server_time`, `tick`, `hb`, `collector_ok`, `wake_gap`, `caps`, and repeats them in `event: hb` frames | **none of it exists.** `server._stream` writes exactly two things: `id: <v>` / `event: state` / `data: <envelope>`, and `: keepalive`. There is no hello, no heartbeat frame, no capability list, and no `event: intent`, `event: resync` or `bye`. Everything §5.1 builds on those fields — the derived freshness thresholds, `collector_stuck`, `mac_asleep`, the version gate, the capability matrix — has no trigger on this wire |
| 16 | IOS-APP.md §5.4 / §5.6 — `GET /api/meta` carries `server_time` and `contract` | **404.** `/api/meta`, `/api/hello`, `/api/stats` and `/api/v1/stream` are all 404 today. `observer.delta_since`'s own docstring points at `/api/stats` for `drift`/`sweep_ms`; that route is not in `server.do_GET`'s chain |
| 17 | UX.md §3.13 — the client receives `GET /api/v1/stream?since=<epoch>:<seq>` with field-addressed `ops`, an opaque cursor and a `dg` digest | the wire is `/api/events` with a bare integer version, `Last-Event-ID` as the cursor, and **card-level** deltas. §3.13's own text calls a card-level delta "not a delta"; it is what ships, and on this fleet it measures 7,675 B against a 38,193 B snapshot |
| 18 | the keepalive period is advertised so thresholds can be derived from it | `sse_keepalive_s` is a server config knob and **never reaches the wire**. The client carries the default (25 s) and says so in one place (`FleetStore.keepaliveS`) |
| 19 | a delta's `cards` values are cards | **a value can be `null`, and `null` means the worktree is GONE.** `delta_since` builds `{k: snap.cards.get(k) for k in keys}` over a ring of changed NAMES. Phase 1 modelled it as `[String: Worktree]`, which cannot decode `{"gone": null}` — so the one frame that says a worktree disappeared was the one frame the client threw away |
| 20 | `/api/limits.generated_at` is a timestamp like every other | it is an **ISO-8601 string** (straight out of `cclimits`) and `null` in demo mode, while `/api/state.generated_at` is a **float epoch**. Same key name, two types, on one API. `fetched_at` is the float and is what the "fetched 4m ago" line uses |
| 21 | `/api/chat` reports failure with a status code | it answers **200** with `{"ok": false, "error": "unknown account x"}`. A client trusting the HTTP line renders an empty conversation for a nameable failure |
| 22 | `/api/chat` decodes its query string | `server.do_GET` pulls the account out of the RAW path with `re.search(r"account=([^&]+)")` and never percent-decodes it, so a label containing a space or a `+` could never match `config.account_label`. Latent on this fleet — no label needs escaping — and it is why the client shows the server's refusal verbatim |
| 23 | UX.md §3.2 — `showing 4 of 6` comes from a server `session_count` | there is no such field, so a card truncated at `max_sessions` is indistinguishable from a complete one. The screen says nothing rather than a number it would have to guess |
| 24 | UX.md §3.4 — Activity is a tab | `GET /api/dispatchlog` returns `{"entries": []}` and the intent frames its rows are built from do not exist. The tab is not shipped; the two things it could say today live on the worktree and server screens |

### ATS, measured rather than assumed

IOS-APP.md §1.5 states *"ATS domain exceptions do not apply to IP-address URLs."*
**On iOS 26 they do**, as an exact `NSExceptionDomains` key. This was not read, it
was falsified: deleting the `100.113.110.31` entry from `Orchestra-Info.plist` and
rebuilding turns the working board into
`NSURLErrorAppTransportSecurityRequiresSecureConnection` (-1022), and putting it
back restores it. The entry is load-bearing and the test that says so can fail.

Both forms are covered, because both are real addresses for the same Mac: `ts.net`
with subdomains for the MagicDNS name, and the raw tailnet IP because
`pairing._server_facts` advertises the address the server is **bound** to — so the
QR hands the phone an IP literal. The IP entry is the one line in this directory
that changes when Tailscale reassigns the address.

`NSAllowsArbitraryLoads` is never set. `NSAllowsLocalNetworking` does not help:
it covers `.local` and link-local, not the `100.64/10` CGNAT range.

## Phase 2: three defects found by RUNNING it, not by reading

All three had the same shape — everything compiled, everything on screen was
correct, and the thing was quietly broken.

**`AsyncLineSequence` drops empty lines, and an empty line is how SSE ends an
event.** The obvious transport is `URLSession.AsyncBytes.lines`. Built that way,
the app held a healthy ESTABLISHED socket, received every byte of every frame,
and **never dispatched one** — the connection strip read `connecting…` forever
while `lsof` on the Mac showed the stream open and the server showed the snapshot
written. `AsyncLineSequence`'s iterator only yields when its buffer is non-empty,
so the blank line between frames is silently swallowed, and in SSE that blank
line is not whitespace, it is the dispatch instruction. Falsified directly
against the live server:

```
A. .lines over the first 3 lines — blank line delivered? false
B. byte-wise: 38229 bytes, 4 lines, 1 blank, in 349 ms
```

Byte-at-a-time over `AsyncBytes` restores the semantics and costs 349 ms per
38 KB frame — an async `next()` per byte. So the transport is a
`URLSessionDataDelegate` handing whole `Data` chunks to `SSELineSplitter`, which
is what `IOS-APP.md` §2.1 says ("delegate-based, deliberately") without saying
why. This is the why.

**`finishTasksAndInvalidate` leaks the socket; the stream needs
`invalidateAndCancel`.** Backgrounding cancels the consuming `Task`, which runs
the `defer` that tears the session down — and `finishTasksAndInvalidate` *waits
for outstanding tasks to finish*, which for a stream is never. Measured with
`lsof -nP -iTCP:4269` across the app's own lifecycle:

```
                    before          after
foreground             1              1
backgrounded           1   ← leak     0
re-foregrounded        2   ← leak     1
after 3 cycles         —              1
```

Every leak burns one of the server's 32 subscriber slots for a client that is not
there, which is the exact failure `stop()` exists to prevent.

**A foreground resume threw its own cursor away.** `resume()` restarts the stream
*and* forces a `/api/state` fetch, so there is always a window where the link is
`.connecting` and a good version is still held. `stream.js` seeds whenever the
stream is not live, and on a browser that is close enough; on a phone it nils the
version (a `/api/state` body carries none), the server answers our
`Last-Event-ID` with a delta, the delta has no base to land on — gap, resync, and
a full 38 KB snapshot for a resume that should have cost one delta. The
diagnostics screen is what showed it: `resyncs: 1` after three
background/foreground cycles, `0` after the fix. The rule is now a pure function,
`FleetStore.maySeed`, and the mutation that restores `stream.js`'s simpler
version is caught by a test.

And two more that only a screenshot could have found, both on views that
compiled, rendered, and were wrong: a dark vertical seam down the right edge of
every session row (a `.background` on the disclosure chevron covers the glyph's
own height, and the canvas shows through above and below it), and the chat
screen's read-only notice sitting UNDER the connection bar (a bottom-pinned row
inside a **pushed** navigation destination does not receive the `safeAreaInset`
the tab applied outside the `NavigationStack` — two attempts to fix it in place
failed the same way, so the notice moved to the top, where it is read anyway).

## Phase 1: two defects found by looking, not by reading

Both are the shape METHOD.md is about — everything on screen was correct.

**The second-launch bug.** The app paired against the real server, drew the real
board, and came back to the pairing screen on the next launch. A target built
with `CODE_SIGNING_ALLOWED = NO` carries no entitlements, so it has no keychain
access group and `SecItemAdd` answers `errSecMissingEntitlement`. The token was
never written; only the *second* launch could see it. Fixed with ad-hoc signing
(`CODE_SIGN_IDENTITY = "-"`) plus `Orchestra.entitlements`, and verified by
pairing, terminating, relaunching with no seam, and watching the board come back.

**Every pid had a decimal point in it.** `Text("\(n)")` resolves to the
`LocalizedStringKey` overload, which formats the interpolated integer through the
locale — pid `34115` rendered as `34.115`. A pid is an identifier, not a
quantity, and neither is a commit count in a mono column. Every numeric literal
in the UI is now `Text(verbatim:)`.

## Measured, driving the real fleet

* **A `touch` on a watched transcript reached the phone in 1.27 s** — touch at
  `1784748313.813`, `delta v=72 base=71 cards=['ConfidAI2']` on the wire at
  `1784748315.081`. The board's two session rows swapped (the server re-sorts by
  freshness) and the top row went `19m` → `3s` with no interaction, no
  pull-to-refresh and no poll.
* **A live agent's own write did the same**, unprompted: `v73 → v74`, one card,
  `17m` → `28s` on screen.
* **A delta is 7,675 B against a 38,193 B snapshot** on this nine-worktree fleet
  — 20 %, and it carries one card out of nine.
* **Not every write is fast.** A touch on an older transcript on the same card
  produced nothing for 16 s, then arrived on the ordinary sweep. The ~1 s figure
  is the kqueue-watched path; a file the watcher is not holding an fd for falls
  back to the cadence. Worth knowing before promising "~1 s" as a flat number.

## Open, and deliberately not done in this phase

- **Actuation.** Read-only throughout: no `/api/send`, no dispatch, no finish, no
  resume arming. The chat screen has no composer at all rather than a disabled
  one — a greyed-out field says "you can nearly reply", and reply is the one path
  where a wrong target means an unattended instruction typed at the wrong agent
  under `--dangerously-skip-permissions`.
- **The Activity tab.** See wire finding 24: no data source exists.
- **The branch map** (`UX.md` §5) and `/api/topology`.
- **Clock skew.** Every relative label is `device now` minus a server instant, and
  nothing corrects for skew. `IOS-APP.md` §5.4 samples it from
  `/api/meta.server_time`, which is a 404; `/api/health.time` is the one honest
  source on this server and wiring it is additive.
- **Push.** No APNs key exists and only the account holder can make one. Nothing
  here is load-bearing on it: it lands as a registration call and a delegate.
- **Real modules.** See "Shape".
- **The Asset Catalog.** `Palette` resolves all four variants of every token in
  one `UIColor(dynamicProvider:)`, which keeps UX.md §9.1's actual property (no
  ternary at any call site, so Contrast+ cannot be applied 60 %). A catalog is
  still the better home because it reaches widgets and notification content,
  which render out of process.
- **IBM Plex Mono.** The system monospaced face is used instead, which honours
  Bold Text and cannot fall back per-glyph — the silent failure UX.md §9.4 spends
  a page on. Bundling Plex changes no call site.
- **A device build.** Simulator only. A real device needs a team in a gitignored
  `Signing.xcconfig`; that is the one thing here that needs the paid account.
