# orchestra for iOS — phase 1

A native SwiftUI client for the orchestra board. This phase gets a token onto the
phone and draws the Fleet list from a real server; the SSE stream, actuation and
push are later phases.

## Build and run it — the only way this is verified

Everything below runs from a shell. No Xcode GUI, no Apple ID, no team.

```sh
# 1. the headless suites — models, transport classification, rules, formatters
cd ios && swift test                    # 35 tests, ~1 s, macOS, no simulator

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
```

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
    ├── Model/    Wire · Enums · StreamFrame · Pairing
    ├── API/      OrchestraClient (actor) · Endpoint · OrchestraError · Keychain
    ├── Rules/    Triage
    ├── Format/   RelativeTime · TextRules
    ├── Store/    FleetStore · PairingStore     (@MainActor @Observable)
    └── UI/       Palette · Typography · StatusStyle · FleetView · rows
```

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

## Two defects found by looking, not by reading

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

## Open, and deliberately not done in this phase

- **The SSE stream.** `StreamFrame` decodes a real snapshot and a synthesised
  delta already; nothing subscribes yet. Phase 2.
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
