# orchestra for iOS — engineering plan

Status: **plan**. Written against `/Users/achill/Downloads/orchestr/orchestra.py` at HEAD
(2302 lines) — since [ADR 0010](adr/0010-split-into-a-package.md) that file is the `orchestra/`
package, so every `orchestra.py:NNN` citation below is a historical pointer; grep for the symbol
name, and read `python3 -m orchestra` wherever the entry point is named. The client is built for the **versioned, op-addressed, SSE-streamed API** the
backend ships first — `Orchestra-Api: 1.0`, served under `/api/v1/`. There is no legacy mode;
see §5.6.

Toolchain verified on this machine: **Xcode 26.6 (17F113), Swift 6.3.3, macOS 26.2**.

Companion documents:

| doc | owns |
|---|---|
| **`API.md`** | **the wire contract — every path, field, enum, status code and error. Authoritative; it wins over this document on all of them.** |
| `ARCHITECTURE.md` | server internals: the collector, the bus, SSE mechanics, cadence, auth, pairing, TLS, the route table, the push sinks |
| `UX.md` | screens, flows, the visual system, accessibility |
| `ROADMAP.md` | sequencing, milestones, the open decisions D1–D10 |
| `ENGINE.md`, `FRESHNESS.md`, `VERIFIED-FACTS.md`, `adr/0001`–`0008` | supporting detail and the measurements |
| **this** | everything that runs on the phone |

> **Endpoint and field names in this document are aliases.** It uses `/api/events`,
> `/api/health`, `mode=digest`, `expect_sid`, `last_write_at` and a `transitions[]` array
> riding on the state frame. The contract spells these `GET /api/v1/stream`,
> `GET /api/v1/health` + `GET /api/v1/meta`, *(no such parameter)*, `expect.{agent_id,pid,card_rev}`,
> `activity_at`, and the durable `GET /api/v1/events` log. **`API.md` §0.1 is the translation
> table and §0.2 lists what is assumed but not yet defined.**

---

# 1. Targets, versions, and the file tree

## 1.1 Deployment target: iOS 18.0

> ⚠ **This is `ROADMAP.md` D4 and it is open. `UX.md` §1.4 assumes 26.0.** The disagreement
> has exactly one real consequence: **`tabViewBottomAccessory`** (iOS 26) is where `UX.md` §2.2
> puts the connection state, the `⌗ N updates` pill, the Board⇄Branches toggle, the `＋`
> composer entry and the Undo snackbar — i.e. every frequently-touched control in the app, in
> the thumb zone, deliberately. On an 18.0 floor that becomes the `.safeAreaInset(edge: .bottom)`
> substitute `UX.md` §2.2 already names (same geometry, same contents, no glass material below
> 26). Icon Composer (§9.9 of `UX.md`) is the only other 26-gated item and it degrades to a flat
> icon. **Nothing else in either document changes.** Close D4 in M0 by checking what the
> author's own iPhone is running; if it is on 26, take 26 and delete the substitute.

Stated once and justified honestly, because the obvious justification does not hold.

The tempting argument is `onScrollPhaseChange(_:)` (iOS 18) for the re-sort hold. **That
argument is wrong.** `onScrollPhaseChange` transitions to `.idle` the instant deceleration
ends — precisely when the user starts *reading*, which is exactly when a re-sort is most
hostile. The requirement is "never re-sort while scrolling **or having just stopped**", and
the "just stopped" half needs a debounce that works identically on iOS 17 via
`simultaneousGesture(DragGesture(minimumDistance: 0))`. Two more common mistakes: `@Entry`
is a macro whose *generated code* back-deploys well below 18, and `ContentUnavailableView`
is 17.0.

The floor is **iOS 18.0** on grounds that do hold:

| reason | weight |
|---|---|
| `onScrollGeometryChange` + `onScrollPhaseChange` together give the hold rule without a `GeometryReader`/preference-key ladder that re-invalidates the board every frame at 120 Hz | primary |
| `Tab`-value `TabView` and `@Entry` ergonomics remove ~200 lines of boilerplate | secondary |
| iOS 17 is a 2023 OS; this is a single-user tool for a developer on current hardware, shipped via TestFlight | decisive in practice, and stated rather than dressed up |

iOS 26 features (Liquid Glass, `@Animatable`, `WKWebView` enhancements) are adopted behind
`if #available(iOS 26, *)` without moving the floor.

```
IPHONEOS_DEPLOYMENT_TARGET = 18.0
SWIFT_VERSION              = 6.0          // language mode
SWIFT_STRICT_CONCURRENCY   = complete
SWIFT_TREAT_WARNINGS_AS_ERRORS = YES
ENABLE_USER_SCRIPT_SANDBOXING  = YES
DEAD_CODE_STRIPPING            = YES
```

Package manifest is `swift-tools-version: 6.2` — required for
`SwiftSetting.defaultIsolation` (SE-0466), which the isolation strategy in §4 depends on.

## 1.2 Targets

| target | kind | links | why it exists |
|---|---|---|---|
| `Orchestra` | iOS app | all `OrchestraKit` products | the app |
| `OrchestraWidgets` | Widget extension | `OrchestraCore`, `OrchestraPersistence` **only** | home/lock-screen attention count, Live Activity UI |
| `OrchestraNotificationService` | Notification Service extension | `OrchestraCore`, `OrchestraPersistence`, `OrchestraAPI` | enrich a push body from `/api/events/<id>` |
| `OrchestraKit` | local SwiftPM package | — | everything testable without a simulator |
| `SnapshotTests` | app-hosted XCTest | `OrchestraUI`, `OrchestraTestSupport` | real `UIWindow` rendering (§9.3) |
| `UITests` | XCUITest | — | the scroll-hold behaviour + accessibility audits |

**The extensions deliberately do not link `OrchestraStore`.** That is a build-time guarantee
that a widget timeline provider or an NSE — both of which run outside `MainActor` and
outside the app process — cannot touch an `@Observable` store. It is enforced by the
package graph, not by discipline.

## 1.3 File tree

```
orchestra/
├── orchestra.py · index.html · map.html · limits.html · guide.html · start.sh · tests/
├── docs/mobile/
│   ├── VERIFIED-FACTS.md · ENGINE.md · FRESHNESS.md · REALTIME.md · PUSH.md · AUTH.md
│   ├── IOS-APP.md                      ← this file
│   ├── state-contract.json             ← generated by tests/test_state_contract.py (§9.2)
│   └── adr/0003-apns-from-stdlib.md … 0008-identity-addressed-mutations.md
└── ios/
    ├── Orchestra.xcodeproj/
    ├── Configs/
    │   ├── Shared.xcconfig · Debug.xcconfig · Release.xcconfig
    │   └── Signing.xcconfig             ← gitignored; TEAM_ID lives here only
    ├── TestPlans/
    │   ├── Unit.xctestplan · Snapshot.xctestplan · UI.xctestplan · Integration.xctestplan
    ├── App/                              # target: Orchestra — composition + SwiftUI only
    │   ├── OrchestraApp.swift
    │   ├── AppModel.swift                # owns every store; the composition root
    │   ├── RootView.swift                # Tab(value:) container
    │   ├── Board/
    │   │   ├── BoardScreen.swift
    │   │   ├── BoardScrollHold.swift     # thin adapter over Rules/ReorderHold
    │   │   ├── TileStrip.swift
    │   │   ├── WorktreeCardView.swift
    │   │   ├── SessionRowView.swift
    │   │   ├── FinishButton.swift        # the two-step arm→confirm control
    │   │   └── OtherProcsView.swift
    │   ├── Session/
    │   │   ├── SessionDetailScreen.swift
    │   │   ├── ChatScreen.swift
    │   │   ├── ChatComposer.swift
    │   │   └── ResumeSheet.swift
    │   ├── Mission/
    │   │   ├── MissionComposerScreen.swift
    │   │   ├── ModelDecisionSheet.swift  # the needs_decision / ⚑ use X anyway dialog
    │   │   ├── IntentProgressView.swift  # ①②③④⑤ live progress
    │   │   └── DispatchLogList.swift
    │   ├── Limits/
    │   │   ├── LimitsScreen.swift · AccountCardView.swift · ReserveStepper.swift
    │   ├── Map/
    │   │   ├── BranchMapScreen.swift · BranchLaneCanvas.swift
    │   ├── Onboarding/
    │   │   ├── PairingScreen.swift · QRScannerView.swift · ManualServerEntryView.swift
    │   ├── Settings/
    │   │   ├── SettingsScreen.swift · ServerListView.swift · DiagnosticsView.swift
    │   ├── Adapters/                     # thin shims over UIKit/system singletons
    │   │   ├── PushAdapter.swift         # UNUserNotificationCenter, registerForRemote…
    │   │   ├── LiveActivityAdapter.swift # ActivityKit
    │   │   └── DeepLinkAdapter.swift     # calls OrchestraCore.DeepLink.parse
    │   ├── Resources/
    │   │   ├── Assets.xcassets · PrivacyInfo.xcprivacy
    │   └── Orchestra.entitlements
    ├── Widgets/
    │   ├── OrchestraWidgetBundle.swift
    │   ├── AttentionWidget.swift         # systemSmall / systemMedium / accessoryRectangular
    │   ├── AttentionProvider.swift       # reads ONLY the app-group cache
    │   ├── LimitResetActivity.swift      # Live Activity UI (Lock Screen + Dynamic Island)
    │   └── OrchestraWidgets.entitlements
    ├── NotificationService/
    │   ├── NotificationService.swift
    │   └── OrchestraNotificationService.entitlements
    ├── SnapshotTests/
    │   ├── BoardSnapshotTests.swift · SessionRowSnapshotTests.swift
    │   ├── LimitsSnapshotTests.swift · StateBannerSnapshotTests.swift
    │   └── __Snapshots__/                # PNG baselines, committed
    ├── UITests/
    │   ├── ScrollHoldUITests.swift · AccessibilityAuditTests.swift
    ├── Tools/
    │   ├── capture-fixtures.sh           # RecordingTransport → Fixtures/real/
    │   ├── scrub.py                      # strips paths, emails, prose from captures
    │   ├── check-fixture-hygiene.sh      # fails the build on a leaked email/path
    │   ├── lint-isolation.sh             # bans Task.detached, @unchecked, AnyView, …
    │   └── pick-sim.sh                   # resolves a simulator UDID for CI
    └── Packages/OrchestraKit/
        ├── Package.swift
        ├── Sources/
        │   ├── OrchestraCore/              # nonisolated · pure · zero I/O
        │   │   ├── Model/
        │   │   │   ├── FleetSnapshot.swift · Worktree.swift · Session.swift
        │   │   │   ├── SessionStatus.swift · Availability.swift · GitInfo.swift
        │   │   │   ├── LiveProc.swift · OtherProc.swift · Counts.swift
        │   │   │   ├── SessionLimit.swift · ResumeSchedule.swift
        │   │   │   ├── LimitsSnapshot.swift · Account.swift · AccountLimit.swift
        │   │   │   ├── Topology.swift · ChatMessage.swift · DispatchLogEntry.swift
        │   │   │   ├── Intent.swift · Freshness.swift · ServerMeta.swift
        │   │   ├── Persisted/             # Codable types the actors must decode
        │   │   │   ├── ServerProfile.swift · CachedSnapshot.swift · NSEInboxItem.swift
        │   │   │   └── PendingIntentRecord.swift
        │   │   ├── Identity/
        │   │   │   ├── WorktreeID.swift · SessionID.swift · IntentID.swift
        │   │   │   ├── ResumeKey.swift · TargetRef.swift
        │   │   ├── Format/
        │   │   │   ├── RelativeTime.swift · ClockLabel.swift · ETime.swift
        │   │   │   ├── AccountLabel.swift · ModelLabel.swift · TextTruncation.swift
        │   │   ├── Rules/
        │   │   │   ├── ReorderHold.swift · StalenessRule.swift · AttentionCount.swift
        │   │   │   ├── Severity.swift · ErrnoCause.swift · DeepLink.swift
        │   │   └── Time/
        │   │       ├── AppClock.swift · SystemAppClock.swift · ServerClock.swift
        │   ├── OrchestraPersistence/       # nonisolated · actors over the filesystem
        │   │   ├── FileStore.swift · KeychainStore.swift · Defaults.swift
        │   │   └── AppGroup.swift
        │   ├── OrchestraAPI/               # nonisolated · actor client + SSE
        │   │   ├── Transport/
        │   │   │   ├── Transport.swift · URLSessionTransport.swift
        │   │   │   ├── SSETransport.swift · SSEFrame.swift
        │   │   │   ├── ReachabilityProbing.swift · NWConnectionProbe.swift
        │   │   ├── APIClient.swift · Endpoint.swift · OrchestraError.swift
        │   │   ├── ServerCapabilities.swift · RequestContext.swift
        │   │   └── DTO/
        │   │       ├── StateFrameDTO.swift · WorktreeDTO.swift · SessionDTO.swift
        │   │       ├── LimitsDTO.swift · TopologyDTO.swift · ChatDTO.swift
        │   │       ├── IntentDTO.swift · MetaDTO.swift · PairDTO.swift
        │   │       └── Mapping/ (…+Domain.swift per DTO)
        │   ├── OrchestraStore/             # @MainActor by default · behaviour only
        │   │   ├── FleetStore.swift · FreshnessStore.swift · ConnectionStore.swift
        │   │   ├── LimitsStore.swift · ChatStore.swift · IntentStore.swift
        │   │   ├── ResumeStore.swift · TopologyStore.swift
        │   │   ├── LiveUpdateEngine.swift · ActionGateway.swift
        │   │   ├── PushCoordinator.swift · LiveActivityCoordinator.swift
        │   │   └── ServerRegistry.swift
        │   ├── OrchestraUI/                # @MainActor by default
        │   │   ├── Tokens/ (Palette.swift · Typography.swift · Motion.swift)
        │   │   ├── Components/
        │   │   │   ├── StatusPill.swift · AvailabilityBadge.swift
        │   │   │   ├── StalenessBanner.swift · ArmedButton.swift
        │   │   │   ├── CountdownText.swift · UsageBar.swift · ProcChip.swift
        │   │   └── Snapshot/ViewSnapshotter.swift
        │   └── OrchestraTestSupport/       # A PRODUCT — the app-hosted tests import it
        │       ├── MockTransport.swift · RecordingTransport.swift · StubProbe.swift
        │       ├── TestClock.swift · FixtureLoader.swift · Builders.swift
        │       ├── Mutations.swift        # generated malformed-payload cases
        │       ├── DemoServer.swift       # #if os(macOS)
        │       └── Fixtures/              # INSIDE the target dir — SwiftPM requires it
        │           ├── real/ · demo/ · malformed/ · contract/
        └── Tests/
            ├── OrchestraCoreTests/ · OrchestraPersistenceTests/ · OrchestraAPITests/
            ├── OrchestraStoreTests/ · OrchestraUIKitTests/ · OrchestraIntegrationTests/
```

## 1.4 `Package.swift`

```swift
// swift-tools-version: 6.2
import PackageDescription

// Stores and views are main-thread by construction.
let mainActorDefault: [SwiftSetting] = [
    .swiftLanguageMode(.v6),
    .defaultIsolation(MainActor.self),
    .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
]

// Models, persisted types, formatters, the network client and the clock seam must be
// callable from a widget timeline provider and a notification-service extension.
let nonisolatedDefault: [SwiftSetting] = [
    .swiftLanguageMode(.v6),
    .defaultIsolation(nil),
    .enableUpcomingFeature("NonisolatedNonsendingByDefault"),
]

let package = Package(
    name: "OrchestraKit",
    platforms: [.iOS(.v18), .macOS(.v15)],   // macOS so `swift test` runs headless in CI
    products: [
        .library(name: "OrchestraCore",        targets: ["OrchestraCore"]),
        .library(name: "OrchestraPersistence", targets: ["OrchestraPersistence"]),
        .library(name: "OrchestraAPI",         targets: ["OrchestraAPI"]),
        .library(name: "OrchestraStore",       targets: ["OrchestraStore"]),
        .library(name: "OrchestraUI",          targets: ["OrchestraUI"]),
        // A PRODUCT: SnapshotTests and UITests live outside the package and cannot
        // import a non-product target.
        .library(name: "OrchestraTestSupport",  targets: ["OrchestraTestSupport"]),
    ],
    dependencies: [],                        // zero, matching the project's identity
    targets: [
        .target(name: "OrchestraCore",        swiftSettings: nonisolatedDefault),
        .target(name: "OrchestraPersistence", dependencies: ["OrchestraCore"],
                swiftSettings: nonisolatedDefault),
        .target(name: "OrchestraAPI",         dependencies: ["OrchestraCore"],
                swiftSettings: nonisolatedDefault),
        .target(name: "OrchestraStore",       dependencies: ["OrchestraAPI", "OrchestraPersistence"],
                swiftSettings: mainActorDefault),
        .target(name: "OrchestraUI",          dependencies: ["OrchestraStore"],
                swiftSettings: mainActorDefault),
        .target(name: "OrchestraTestSupport",
                dependencies: ["OrchestraAPI", "OrchestraPersistence"],
                // Fixtures live INSIDE the target directory. SwiftPM rejects a resource
                // path that escapes the target dir, and rejects symlinks.
                resources: [.copy("Fixtures")],
                swiftSettings: nonisolatedDefault),

        .testTarget(name: "OrchestraCoreTests",
                    dependencies: ["OrchestraCore", "OrchestraTestSupport"],
                    swiftSettings: nonisolatedDefault),
        .testTarget(name: "OrchestraPersistenceTests",
                    dependencies: ["OrchestraPersistence", "OrchestraTestSupport"],
                    swiftSettings: nonisolatedDefault),
        .testTarget(name: "OrchestraAPITests",
                    dependencies: ["OrchestraAPI", "OrchestraTestSupport"],
                    swiftSettings: nonisolatedDefault),
        // MainActor default, or every @Test touching a store needs a hand-written
        // @MainActor — exactly the sprawl defaultIsolation exists to remove.
        .testTarget(name: "OrchestraStoreTests",
                    dependencies: ["OrchestraStore", "OrchestraTestSupport"],
                    swiftSettings: mainActorDefault),
        .testTarget(name: "OrchestraUIKitTests",
                    dependencies: ["OrchestraUI", "OrchestraTestSupport"],
                    swiftSettings: mainActorDefault),
        .testTarget(name: "OrchestraIntegrationTests",
                    dependencies: ["OrchestraAPI", "OrchestraTestSupport"],
                    swiftSettings: nonisolatedDefault),
    ]
)
```

**Zero third-party dependencies**, matching `ci.yml`'s stated rule ("No pip install — the
project is stdlib-only"). Two isolation traps this layout makes structurally impossible are
discussed in §4.3.

## 1.5 Info.plist and entitlements, in one place

`App/Resources/Info.plist` (the non-obvious keys only):

```xml
<key>NSAppTransportSecurity</key>
<dict>
  <key>NSExceptionDomains</key>
  <dict>
    <key>ts.net</key>
    <dict>
      <key>NSIncludesSubdomains</key><true/>
      <key>NSExceptionMinimumTLSVersion</key><string>TLSv1.2</string>
    </dict>
  </dict>
</dict>
<key>NSCameraUsageDescription</key>
<string>orchestra scans the pairing code shown on your Mac.</string>
<key>NSLocalNetworkUsageDescription</key>
<string>orchestra connects to your Mac over your Tailscale network.</string>
<key>NSSupportsLiveActivities</key><true/>
<key>NSSupportsLiveActivitiesFrequentUpdates</key><true/>
<key>BGTaskSchedulerPermittedIdentifiers</key>
<array><string>sh.orchestra.refresh</string></array>
<key>CFBundleURLTypes</key>
<array><dict>
  <key>CFBundleURLName</key><string>sh.orchestra.pair</string>
  <key>CFBundleURLSchemes</key><array><string>orc</string><string>orchestra</string></array>
</dict></array>
```

`Orchestra.entitlements`:

```xml
<key>aps-environment</key><string>development</string>   <!-- Release.xcconfig → production -->
<key>com.apple.developer.usernotifications.time-sensitive</key><true/>
<key>com.apple.security.application-groups</key>
<array><string>group.sh.orchestra</string></array>
<key>keychain-access-groups</key>
<array><string>$(AppIdentifierPrefix)sh.orchestra.shared</string></array>
```

Notes that matter and are easy to get wrong:

- The **`ts.net` ATS exception carries no `NSExceptionAllowsInsecureHTTPLoads`.** The server
  serves HTTPS on the tailnet (`AUTH.md` §9). The exception exists only to pin a minimum TLS
  version and to document the domain; a self-signed certificate accepted by a custom
  `URLSessionDelegate` trust evaluator is an ATS-clean load, so no blanket exception is
  needed. **`NSAllowsArbitraryLoads` is never set.**
- **MagicDNS is the canonical address form.** ATS domain exceptions do not apply to
  IP-address URLs, so a raw `https://100.113.110.31:4242` load is governed by the default
  policy — which the pinned self-signed cert satisfies via the delegate, but only because
  we never call `SecTrustEvaluateWithError`. Pairing prefers the MagicDNS name and falls
  back to the raw tailnet IP; both work, and both are pinned identically.
- `NSLocalNetworkUsageDescription` is included **defensively**. Whether iOS 18's
  local-network gate fires for a `utun`-routed 100.64/10 destination is **unverified** and
  is a day-one empirical task (§11). Including the key costs one plist entry; omitting it
  and being wrong costs a silent connection failure with no diagnosable cause.
- `aps-environment` is `development` in Debug and `production` in Release via
  `Signing.xcconfig`. The app reads the value out of its **embedded provisioning profile**
  at runtime and reports it to the server as `env` (§6.1) — it does **not** infer from
  `#if DEBUG`, because TestFlight builds are `DEBUG=0` yet a development-signed local build
  is not, and that confusion is historically the single most common APNs failure.

---

# 2. The layered architecture

```
        ┌────────────────────────── nonisolated ───────────────────────────┐
 SSE ──▶│ SSETransport ─▶ actor APIClient ─▶ Sendable domain values        │
 HTTP ─▶│ URLSessionTransport                 (structs from OrchestraCore)  │
        └──────────────────────────────┬───────────────────────────────────┘
                                       │ values only — never a store, never a view
        ┌──────────────────────────────▼─────────────── @MainActor ────────┐
        │ AppModel                                                          │
        │   ServerRegistry · ConnectionStore · LiveUpdateEngine             │
        │   FleetStore · FreshnessStore · LimitsStore · ChatStore           │
        │   IntentStore · ResumeStore · TopologyStore                       │
        │   ActionGateway · PushCoordinator · LiveActivityCoordinator       │
        └──────────────────────────────┬────────────────────────────────────┘
                                       │ Equatable value structs
                            SwiftUI views (no store in a leaf view)
```

Rule that keeps `@Observable` legal under Swift 6: **no store is ever passed into a
`Task.detached`, into an actor, or across an isolation boundary.** `@Observable` classes are
not `Sendable` and must not be made so.

## 2.1 Transport — two protocols, both injectable

The Python suite mocks by assigning module attributes (`fb.run = FakeGit()`) and funnels
every subprocess through one `run()` seam. The iOS analogue is two protocols. Both are
injectable, because a static reachability probe that calls `NWConnection` from inside
`perform` makes every error test non-hermetic and adds seconds to every failure.

```swift
// OrchestraAPI/Transport/Transport.swift
public struct TransportResponse: Sendable {
    public let status: Int
    public let headers: [String: String]      // keys lowercased at construction
    public let body: Data
}

public protocol Transport: Sendable {
    func send(_ request: URLRequest) async throws -> TransportResponse
    func stream(_ request: URLRequest) -> AsyncThrowingStream<SSEFrame, Error>
}

public protocol ReachabilityProbing: Sendable {
    /// Runs CONCURRENTLY with a failing request, never after it. Result is cached for the
    /// duration of the current .offline(_) state and invalidated on NWPathMonitor change,
    /// on foreground, or on a backoff tick.
    func probe(host: String, port: Int, deadline: Duration) async -> TransportFailure
}
```

| implementation | used by |
|---|---|
| `URLSessionTransport` / `NWConnectionProbe` | the app |
| `MockTransport` / `StubProbe(returning:)` | every unit test |
| `RecordingTransport` | `Tools/capture-fixtures.sh`, writes into `Fixtures/real/` |

### Two `URLSession`s, not one

```swift
public struct URLSessionTransport: Transport {
    private let reads: URLSession     // SSE stream + snapshot resync + chat + limits
    private let actions: URLSession   // POST /api/intent and friends
    private let pinned: PinnedTrustDelegate

    public init(profile: ServerProfile, pin: String) {
        self.pinned = PinnedTrustDelegate(pin: pin)
        func base() -> URLSessionConfiguration {
            let c = URLSessionConfiguration.ephemeral    // no shared cookie/cache jar
            c.requestCachePolicy = .reloadIgnoringLocalCacheData
            c.waitsForConnectivity = false               // we own the reconnect machine
            c.multipathServiceType = .none               // never bounce a tailnet route
            c.allowsExpensiveNetworkAccess = true
            c.timeoutIntervalForResource = 3600          // an SSE stream is long-lived
            c.tlsMinimumSupportedProtocolVersion = .TLSv12
            return c
            // NOT set: httpAdditionalHeaders["Accept-Encoding"]. URLSession already sends
            // gzip/deflate/br and transparently inflates; overriding it narrows the set and
            // whether CFNetwork keeps auto-inflating is undocumented. It would turn the
            // server's gzip landing into a decode failure on an endpoint that worked
            // yesterday. Content-Length is likewise reserved and derived from httpBody.
        }
        let r = base(); r.httpMaximumConnectionsPerHost = 4
        let a = base(); a.httpMaximumConnectionsPerHost = 2
        self.reads   = URLSession(configuration: r, delegate: pinned, delegateQueue: nil)
        self.actions = URLSession(configuration: a, delegate: pinned, delegateQueue: nil)
    }
}
```

Two pools, because a single pool at `httpMaximumConnectionsPerHost = 2` lets one slow POST
occupy every slot, and the board would freeze during exactly the operation you most want to
watch. Intents return in <100 ms by contract (§2.4), so there should be no slow POSTs left —
but structural impossibility beats a contract promise.

**`URLSession.shared` is banned by lint.** It has no delegate and therefore cannot accept
the pinned self-signed certificate; any accidental use (a helper, `AsyncImage`, a pasted
snippet) hard-fails with an opaque `NSURLErrorServerCertificateUntrusted`.

### SSE parsing — delegate-based, deliberately

```swift
// OrchestraAPI/Transport/SSETransport.swift
//
// The ONLY URLSessionDataDelegate in the app, and the only @unchecked Sendable.
// Justified: URLSession.AsyncBytes + .lines iterates ONE BYTE at a time through the
// async-sequence machinery — for a 35 KB snapshot frame that is ~35,000 async
// iterations plus String building, on the always-on foreground path.
// Allowlisted by name in Tools/lint-isolation.sh. ~60 reviewed lines, one lock.
final class SSEDelegate: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private let lock = NSLock()                 // guards `buffer` + `cont` only
    private var buffer = Data()
    private var cont: AsyncThrowingStream<SSEFrame, Error>.Continuation?

    func urlSession(_ s: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock(); defer { lock.unlock() }
        buffer.append(data)
        // Frames are separated by a blank line. Split on \n\n, keep the tail.
        while let r = buffer.range(of: Data("\n\n".utf8)) {
            let raw = buffer.prefix(upTo: r.lowerBound)
            buffer.removeSubrange(buffer.startIndex ..< r.upperBound)
            if let f = SSEFrame(raw: raw) { cont?.yield(f) }   // ": keepalive" parses to nil
        }
    }

    func urlSession(_ s: URLSession, task: URLSessionTask, didCompleteWithError e: Error?) {
        lock.lock(); let c = cont; cont = nil; lock.unlock()
        if let e { c?.finish(throwing: e) } else { c?.finish() }
    }
}

public struct SSEFrame: Sendable, Equatable {
    public let id: Int?          // the SSE `id:` field — the version, for Last-Event-ID
    public let event: String     // hello | state | intent | resync | bye
    public let data: Data
}
```

`SSEFrameTests` benchmarks this against a 35 KB frame and the number is recorded in §11. The
delegate exists on a measured argument or not at all.

## 2.2 `APIClient` — one decode, correct cancellation

```swift
// OrchestraAPI/APIClient.swift
public actor APIClient {
    private let transport: any Transport
    private let decoder: JSONDecoder          // actor-isolated stored property,
    private let encoder: JSONEncoder          // NEVER a `static let` (global mutable state)
    private var context: RequestContext        // host, port, token, caps — a Sendable snapshot

    public init(transport: any Transport, context: RequestContext) { … }

    public func meta() async throws -> ServerMeta
    public func state(since: Int?, mode: StateMode) async throws -> StateFrame
    public func events(lastEventID: Int?) -> AsyncThrowingStream<SSEFrame, Error>
    public func limits(refresh: Bool) async throws -> LimitsSnapshot
    public func topology() async throws -> Topology
    public func chat(account: String, sid: SessionID,
                     since: Double?, limit: Int) async throws -> [ChatMessage]
    public func dispatchLog(limit: Int) async throws -> [DispatchLogEntry]
    public func intents(activeOnly: Bool) async throws -> [Intent]
    @discardableResult
    public func postIntent(_ req: IntentRequest) async throws -> Intent
    public func pair(code: String, deviceName: String) async throws -> PairResult
    public func registerPush(_ reg: PushRegistration) async throws
}
```

```swift
private func perform<T: Decodable & Sendable>(_ e: Endpoint, as _: T.Type) async throws -> T {
    var req = URLRequest(url: context.url(for: e))
    req.httpMethod = e.method
    req.timeoutInterval = e.timeout
    req.setValue("application/json", forHTTPHeaderField: "Accept")
    req.setValue("orchestra-ios/\(Bundle.shortVersion)", forHTTPHeaderField: "X-Orchestra-Client")
    req.setValue("Bearer \(context.token)", forHTTPHeaderField: "Authorization")
    if let opID = e.opID { req.setValue(opID, forHTTPHeaderField: "X-Orchestra-Op-Id") }
    if let body = try e.jsonBody(encoder: encoder) {
        // do_POST reads exactly Content-Length bytes (orchestra.py:2249). URLSession sends
        // CHUNKED for httpBodyStream, which the server reads as 0 bytes → payload = {} →
        // every field None. PROHIBITION: never use httpBodyStream against this API.
        req.httpBody = body
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    }

    let res: TransportResponse
    do { res = try await transport.send(req) }
    catch {
        // A cancelled Swift task makes URLSession throw URLError(.cancelled) (-999),
        // NOT CancellationError. Getting this wrong means every tab switch surfaces as
        // .offline(.unknown(-999)) and greys the board out.
        if Task.isCancelled { throw OrchestraError.cancelled }
        if let u = error as? URLError, u.code == .cancelled { throw OrchestraError.cancelled }
        throw OrchestraError.transport(TransportFailure.classify(error))
    }

    switch res.status {
    case 200, 202:  break
    case 401:       throw OrchestraError.unauthorized
    case 403:       throw OrchestraError.forbidden(reason: res.jsonError ?? "forbidden")
    case 409:       throw OrchestraError.conflict(res.jsonError ?? "already in flight")
    case 429:       throw OrchestraError.rateLimited(retryAfter: res.retryAfterSeconds)
    default:
        // send_error(404) writes an HTML body. Never assume JSON off the happy path.
        throw OrchestraError.httpStatus(
            code: res.status,
            isHTML: res.headers["content-type"]?.contains("text/html") ?? true)
    }

    // ONE decode. Each DTO decodes its own envelope fields; there is no separate
    // Envelope pass over a 35 KB payload.
    let decoded: T
    do { decoded = try decoder.decode(T.self, from: res.body) }
    catch let d as DecodingError { throw OrchestraError.decoding(.from(d, endpoint: e.path)) }
    if let f = decoded as? EnvelopeBearing, f.isFailure { throw OrchestraError.from(f, endpoint: e) }
    return decoded
}
```

```swift
/// Absent `ok` means SUCCESS. The Python suite already encodes this
/// (`out.get("ok", True)`, tests/test_orchestra.py:558) and `start_dispatch`'s success path
/// returns `{"job": …}` with no `ok` key at all.
public protocol EnvelopeBearing {
    var ok: Bool? { get }
    var message: String? { get }
    var error: String? { get }
    var available: Bool? { get }
}
public extension EnvelopeBearing {
    var isFailure: Bool { ok == false || available == false }
}
```

## 2.3 `Endpoint` — timeouts derived, not guessed

```swift
public enum StateMode: String, Sendable { case full, digest }

public enum Endpoint: Sendable, Hashable {
    case meta
    case events(lastEventID: Int?)                       // SSE
    case state(since: Int?, mode: StateMode)             // resync / poll fallback
    case limits(refresh: Bool)
    case topology
    case chat(account: String, sid: String, since: Double?, limit: Int)
    case dispatchLog(limit: Int)
    case intents(activeOnly: Bool)
    case intent(IntentRequest)                           // POST — the only actuation
    case pairEnroll(code: String, deviceName: String)    // POST — token-less by design
    case pushRegister(PushRegistration)                  // POST
    case pushUnregister(deviceID: String)
    case eventsOpen                                      // withdrawal reconcile (§6.4)
    case eventDetail(id: String)                         // NSE enrichment

    var timeout: TimeInterval {
        switch self {
        case .meta:                         return 5
        case .events:                       return 3600     // resource timeout governs
        case .state:                        return 20       // post-migration collect ≈0.3 s
        case .limits(let refresh):          return refresh ? 100 : 20  // server budget: 90 s
        case .topology:                     return 30
        case .chat, .dispatchLog, .intents,
             .eventsOpen, .eventDetail:     return 20
        case .intent:                       return 15       // returns <100 ms by contract
        case .pairEnroll, .pushRegister,
             .pushUnregister:               return 15
        }
    }

    /// Reads retry freely. Intents are replay-safe by construction — the same `id` returns
    /// the existing record. Pairing is single-use and must never be retried blind.
    var isRetryable: Bool { if case .pairEnroll = self { return false }; return true }

    var opID: String? {
        if case .intent(let r) = self { return r.id.rawValue }
        return nil
    }
}
```

### Query encoding — refuse locally rather than fail remotely

`read_chat` matches `account=([^&]+)` with no `urllib.parse.unquote` (orchestra.py:2219)
while `index.html` sends `encodeURIComponent`. The client encodes identically **and refuses
in advance** rather than shipping a request it knows cannot match.

```swift
// OrchestraCore/Format/AccountLabel.swift
public extension String {
    /// Byte-identical to JavaScript's encodeURIComponent.
    var jsURIComponent: String {
        let ok = CharacterSet(charactersIn:
          "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.!~*'()")
        return addingPercentEncoding(withAllowedCharacters: ok) ?? self
    }
    var isRegexSafeAccountLabel: Bool { self == jsURIComponent }
}
```

`APIClient.chat` throws
`.clientPrecondition("account label '\(a)' contains characters this server cannot decode")`
when the label is unsafe — a real error with real copy, instead of a mystery
`unknown account my%20account`. The server-side `parse_qs` fix is hand-off item 9 in §12.

## 2.4 The actuation contract — intents, not long POSTs

**Every mutation is a durable server-side record plus a stream of phase changes.** The
client posts and returns; progress arrives on the SSE `intent` channel.

```jsonc
// POST /api/intent
{"id": "ios-3f1c9e2a-…",       // client-generated idempotency key, MANDATORY
 "kind": "finish",              // finish | dispatch | send | resume | reserve | kill
 "target": "wt:orbital-web",    // ADR 0008 durable identity: "wt:<name>" | "sid:<uuid>"
 "confirm": false,              // false ⇒ arm; true ⇒ execute an armed intent
 "payload": {}}

// response — ALWAYS fast (<100 ms). Never blocks on git fetch, cclimits, or ps.
{"ok": true,
 "intent": {"id": "ios-3f1c…", "kind": "finish", "target": "wt:orbital-web",
            "phase": "armed", "created_at": 1784636700.0, "expires_at": 1784636760.0,
            "message": "✓ confirm within 60s — closeout brief goes to the agent"}}
```

What this single change buys, itemised against the hazards in the current server:

| hazard today | how the intent model kills it |
|---|---|
| `POST /api/finish` can exceed 60 s (`git fetch` 30 s + `claude_processes()` twice + osascript 10 s) — iOS's default `timeoutIntervalForRequest` — and the app may be suspended mid-flight | the POST returns in <100 ms; there is no long request to suspend |
| a retry after a timeout launches a **second** agent (tmux names embed `%H%M%S`, so any retry ≥1 s later doubles) | the same `id` returns the existing record; replay is idempotent by construction |
| the finish arm window is 100 % client-side (`window._armFinish`, index.html:722-748), desyncs across clients, and has nowhere to live in a notification action | `phase: armed` + `expires_at` is server-side and durable; a notification button can confirm it |
| `_closeouts` is process memory, so a server restart silently reverts `✕ close` to `✓ finish` and re-types a ~600-char brief at an agent mid-closeout | `phase: brief_sent` is part of the record |
| job ids are lost after 20 dispatches or a restart (`_jobs` capped at 20, `_job_seq` resets) | intents persist with a TTL; `GET /api/intents?active=1` is the resync |
| there is no way to stop a mission dispatched by accident | `kind: kill` is an intent like any other |

**The offline action queue is deleted.** An intent armed on the server survives the phone
going offline; there is nothing left to replay from the device.

## 2.5 `ActionGateway` — thin, but it holds three locally-checkable preconditions

```swift
// OrchestraStore/ActionGateway.swift   (@MainActor by module default)
@Observable
public final class ActionGateway {
    public func arm(_ kind: Intent.Kind, _ target: TargetRef,
                    payload: IntentPayload = .none) async -> Result<Intent, OrchestraError>
    public func confirm(_ id: IntentID) async -> Result<Intent, OrchestraError>
    public func cancel(_ id: IntentID)  async -> Result<Intent, OrchestraError>

    /// Sending text into a live agent. Identity, not pid (ADR 0008): the server re-resolves
    /// sid → process at execution time and returns `.targetGone` if it no longer matches.
    ///
    /// The previous design's "re-fetch a fresh snapshot first" guard is DELETED:
    /// `cached_state()` has no cache-busting parameter and `do_GET` discards the query
    /// string, so no such fetch exists — and a 4 s-stale snapshot was never a guard anyway.
    public func send(text: String, to sid: SessionID) async -> Result<Intent, OrchestraError> {
        // send_to_process collapses all newlines to single spaces (orchestra.py:1344).
        let normalized = text
            .replacingOccurrences(of: #"\s*\n\s*"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        guard !normalized.isEmpty else {
            return .failure(.clientPrecondition("empty message"))
        }
        // VERIFIED on tmux 3.6a: `send-keys -t T -l "-n foo"` exits 1, "unknown flag -n".
        // orchestra.py:1359 lacks the `--` sentinel. Refuse locally with a fixable reason
        // rather than surfacing an opaque "tmux send-keys failed".
        if normalized.hasPrefix("-") && !caps.has(.dashSentinel) {
            return .failure(.clientPrecondition(
                "this server can't deliver a message starting with '-' — "
                + "prefix it with a word, or update orchestra"))
        }
        return await post(.init(id: IntentID(), kind: .send,
                                target: .session(sid), confirm: true,
                                payload: .text(normalized)))
    }
}
```

The composer shows a live *"will be sent as one line"* hint the moment the text contains a
newline, instead of silently mangling it. And `ok: true` on a send currently means "keys
accepted", not "agent received" — until hand-off item 10 adds `_proven_in_transcript`
receipt proof, the UI appends `· delivery unverified`.

---

# 3. The Codable model layer

Two layers, deliberately: **DTOs** in `OrchestraAPI/DTO/` decode the wire exactly as the
server writes it; **domain types** in `OrchestraCore/Model/` are what the app renders. The
mapping boundary is where truncation, defaulting and enum-widening happen, so a wire change
breaks in one file rather than in fifty views.

Every domain type is `Sendable`, `Equatable`, and a `struct`. Nothing in `OrchestraCore`
touches Foundation networking, the filesystem, or `Date()`.

## 3.1 Identity

```swift
// OrchestraCore/Identity/
public struct WorktreeID: Hashable, Sendable, Codable, RawRepresentable {
    public let rawValue: String                  // the server's card key == worktree name
    public init(_ v: String) { rawValue = v }
    public init?(rawValue: String) { self.rawValue = rawValue }
    public var target: TargetRef { .worktree(self) }
}

public struct SessionID: Hashable, Sendable, Codable, RawRepresentable {
    public let rawValue: String                  // the FULL transcript UUID, not `id`
    public init(_ v: String) { rawValue = v }
    public init?(rawValue: String) { self.rawValue = rawValue }
    public var target: TargetRef { .session(self) }
}

public struct IntentID: Hashable, Sendable, Codable, RawRepresentable {
    public let rawValue: String
    public init() { rawValue = "ios-" + UUID().uuidString.lowercased() }
    public init?(rawValue: String) { self.rawValue = rawValue }
}

/// The resumes dict on /api/state is keyed "{worktree}|{sid}" with a literal pipe.
public struct ResumeKey: Hashable, Sendable, Codable {
    public let worktree: WorktreeID
    public let session: SessionID
    public init?(wire: String) {
        let parts = wire.split(separator: "|", maxSplits: 1, omittingEmptySubsequences: false)
        guard parts.count == 2 else { return nil }
        worktree = WorktreeID(String(parts[0]))
        session  = SessionID(String(parts[1]))
    }
    public var wire: String { "\(worktree.rawValue)|\(session.rawValue)" }
}

public enum TargetRef: Hashable, Sendable, Codable, CustomStringConvertible {
    case worktree(WorktreeID)
    case session(SessionID)
    case account(String)
    public var description: String {
        switch self {
        case .worktree(let w): return "wt:\(w.rawValue)"
        case .session(let s):  return "sid:\(s.rawValue)"
        case .account(let a):  return "acct:\(a)"
        }
    }
}
```

`Session` uses `id = SessionID(sid)`, never `id: \.self` on a struct that changes every
frame — that would destroy SwiftUI's structural identity on every update.

> **Known backend collision, named because the client cannot paper over it.**
> `discover_worktrees` (orchestra.py:120-138) dedupes by absolute path, so two roots each
> holding a `ConfidAI` directory produce **two cards with the same name** — and a
> name-keyed `cards` dict silently drops one, while a `wt:<name>` mutation is genuinely
> ambiguous. Hand-off item 12 (§12): derive the card key from the path, or refuse to serve
> a fleet with duplicate basenames. Until then the client counts collisions into
> `DiagnosticsView` and constructs every dictionary with
> `Dictionary(_:uniquingKeysWith: { a, _ in a })` — `uniqueKeysWithValues` would crash.

## 3.2 Enums that must not trap on an unknown value

```swift
// OrchestraCore/Model/SessionStatus.swift
public enum SessionStatus: String, Sendable, Codable, CaseIterable {
    case working, needsInput = "needs_input", blocked, waiting, ended, limit
    case unknown                                  // NOT on the wire — see below

    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = SessionStatus(rawValue: raw) ?? .unknown
    }
    /// `.unknown` also carries the server's `procs_known == false` case: when ps/lsof
    /// failed wholesale, a session is NOT provably ended and must not be rendered as such.
    public var isAttention: Bool { self == .needsInput || self == .blocked }
}

public enum Availability: String, Sendable, Codable {
    case free, attention, waiting, busy
    case unknown
    public init(from decoder: Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Availability(rawValue: raw) ?? .unknown
    }
}
```

A hard `Codable` enum would make a single new server-side status value crash decoding of
the **entire 35 KB payload** — one unknown string, whole board gone. `.unknown` renders as a
neutral pill and is counted into diagnostics.

## 3.3 The board

```swift
// OrchestraCore/Model/FleetSnapshot.swift
public struct FleetSnapshot: Sendable, Equatable {
    public var version: Int                       // monotonic; bumps only on real change
    public var generatedAt: Date                  // absolute, from the server
    public var hostname: String
    public var user: String
    public var counts: Counts
    public var order: [WorktreeID]                // SERVER-supplied. The client never sorts.
    public var cards: [WorktreeID: Worktree]
    public var freeWorktrees: [WorktreeID]
    public var otherProcs: [OtherProc]
    public var resumes: [ResumeKey: ResumeSchedule]
    public var freshness: Freshness

    public var visible: [Worktree] { order.compactMap { cards[$0] } }
}

public struct Counts: Sendable, Equatable, Codable {
    public var working = 0, needsInput = 0, limit = 0
    public var blocked = 0, waiting = 0, ended = 0

    /// index.html:425 — attn = needs_input + blocked + waiting. Limit-stuck agents are
    /// deliberately excluded: they are not actionable. Sessions with `handed_to` are
    /// already excluded server-side (orchestra.py:798-801).
    public var attention: Int { needsInput + blocked + waiting }
    public var interrupting: Int { needsInput + blocked }    // what the badge counts

    enum CodingKeys: String, CodingKey {
        case working, limit, blocked, waiting, ended
        case needsInput = "needs_input"
    }
}
```

```swift
// OrchestraCore/Model/Worktree.swift
public struct Worktree: Sendable, Equatable, Identifiable {
    public let id: WorktreeID
    public var name: String
    public var path: String
    public var git: GitInfo
    public var sessions: [Session]                // server-sorted; capped at max_sessions (6)
    public var liveProcs: [LiveProc]
    public var availability: Availability
    /// Present ONLY when a closeout brief was typed at this card's agent AND live_procs is
    /// non-empty (orchestra.py:776-781). Its presence flips ✓ finish → ✕ close.
    public var closeoutSentAt: Date?
    public var cardRev: String?                   // staleness token for actuation (AUTH §11)

    public var hiddenEndedCount: Int { sessions.filter { $0.status == .ended }.count }
    /// Terminals in this worktree that no session has claimed.
    public func looseProcs() -> [LiveProc] {
        let claimed = Set(sessions.compactMap(\.pid))
        return liveProcs.filter { !claimed.contains($0.pid) }
    }
}

public struct GitInfo: Sendable, Equatable {
    public var branch: String                     // never null: falls back to "detached@…", "?"
    public var commit: Commit?                    // null on an empty repo
    public var dirty: Int                         // count of `git status --porcelain` lines
    public var ahead: Int?                        // null when the branch has no upstream
    public var behind: Int?                       // ditto; observed values up to 2030
    public var isStale: Bool                      // git tier lagged its cadence (ENGINE §3.3)

    public struct Commit: Sendable, Equatable {
        public var hash: String                   // 8 AND 9 chars observed — never slice to 7
        public var timestamp: Date
        public var subject: String                // TRUNCATED AT THE MAPPING BOUNDARY (§3.8)
    }
}
```

```swift
// OrchestraCore/Model/Session.swift
public struct Session: Sendable, Equatable, Identifiable {
    public var id: SessionID { sid }

    public let sid: SessionID                     // the real key
    public let shortID: String                    // fp.stem[:8] — display only, redundant
    public var account: String                    // orchestra LABEL ("main"), not cclimits slug
    public var lastWriteAt: Date                  // ABSOLUTE (ENGINE §3.4 replaces age_s)
    public var cwd: String
    public var subdir: String?                    // nil when cwd == worktree root
    public var branch: String?                    // transcript gitBranch; may say "HEAD"
    public var model: String                      // NOT an enum: "opus-4-8", "", "haiku-4-5-…"
    public var pendingTools: [String]
    public var pendingWorkflows: Int
    public var pendingBackgroundAgents: Int
    public var topic: String?                     // server-truncated to 140
    public var lastAssistant: String?             // 240
    public var lastUser: String?                  // 140
    public var subagentSaid: String?              // 240, only when sub_mtime > mtime
    public var subagentsActive: Bool
    public var pid: Int32?
    /// true ONLY when the process's CLAUDE_CONFIG_DIR account matched the session's
    /// account (orchestra.py:664). false = a freshness-order GUESS. The UI must render a
    /// false pairing as a guess (dashed chip), never as fact.
    public var pidCertain: Bool
    public var status: SessionStatus
    public var limit: SessionLimit?               // present iff status == .limit
    /// An account label. Present iff status == .limit and a fresher non-ended session
    /// exists on the same card. Means "work continued elsewhere — NOT actionable".
    /// A push pipeline that alerts on .limit without checking this fires on non-problems.
    public var handedTo: String?
    public var toolRunning: Bool                  // wire key present only when true
    public var backgroundShell: Bool              // ditto
    public var turnEnded: Bool                    // FRESHNESS §: evidence, not a clock guess

    /// index.html:466-472 — first match wins.
    public var busySignal: String? {
        if subagentsActive            { return "⚙ subagents running" }
        if pendingWorkflows > 0       { return "⚙ awaiting \(pendingWorkflows) workflow(s)" }
        if pendingBackgroundAgents > 0 { return "⚙ awaiting \(pendingBackgroundAgents) background agent(s)" }
        if toolRunning {
            return "⚙ running: " + (backgroundShell ? "background shell"
                                                    : pendingTools.first ?? "tool")
        }
        return nil
    }
    public var isActionable: Bool { status.isAttention || (status == .limit && handedTo == nil) }
}

public struct SessionLimit: Sendable, Equatable {
    public var worst: String?      // "Session" | "Weekly" | a model name like "Fable"
    public var group: LimitGroup?  // .session | .weekly
    public var resetsAt: Date?     // ABSOLUTE (ENGINE §3.4 replaces resets_in)

    /// ALL FOUR NULL IS A REAL, COMMON STATE — the transcript-regex fallback branch
    /// (orchestra.py:728-732) fires when the CLI wrote "you've hit your session limit" but
    /// the cclimits cache is cold. Render "limited, reset time unknown"; the resume flow
    /// must then demand an explicit due time (schedule_resume returns need_time:true).
    public var isTimeUnknown: Bool { resetsAt == nil }

    public enum LimitGroup: String, Sendable, Codable { case session, weekly }
}

public struct LiveProc: Sendable, Equatable, Identifiable {
    public var id: Int32 { pid }
    public var pid: Int32
    public var cpu: Double
    public var startedAt: Date?                   // derived server-side from `ps` etime
    public var etimeRaw: String                   // "15:02" | "12:43:46" | "2-03:14:22"
    public var tty: String?
    /// NOT an enum: can be "tmux -L fleet" — the string embeds the socket name.
    /// Match on the `tmux` field or on `reachable`, never on this.
    public var host: String?
    public var account: String?
    public var tmuxTarget: String?                // "session:win.pane"
    /// tmuxTarget != nil || (host ∈ {Terminal, iTerm2} && tty != nil).
    /// THE gate for whether chat/send can work at all.
    public var reachable: Bool
    public var subdir: String?
}

public struct OtherProc: Sendable, Equatable, Identifiable {
    public var id: Int32 { pid }
    public var pid: Int32
    public var cpu: Double
    public var etimeRaw: String
    public var tty: String?
    public var host: String?
    public var cwd: String?
}
```

## 3.4 Limits

```swift
// OrchestraCore/Model/LimitsSnapshot.swift
public struct LimitsSnapshot: Sendable, Equatable {
    public var available: Bool
    public var error: String?
    public var fetchedAt: Date                    // epoch FLOAT on the wire
    /// cclimits' own generated_at is an ISO-8601 STRING here, and null in demo mode —
    /// while /api/state.generated_at is a Double epoch. Same field name, three shapes.
    public var cclimitsGeneratedAt: Date?
    public var accounts: [Account]

    public func account(labelled label: String) -> Account? {
        // JOIN ON fb_label, NEVER slug: for the default home they differ
        // (slug "default" vs fb_label "main"), and session.account is the label.
        accounts.first { $0.label == label }
    }
}

public struct Account: Sendable, Equatable, Identifiable {
    public var id: String { label ?? slug }
    public var slug: String                       // cclimits' identity
    public var label: String?                     // fb_label — orchestra's, injected. THE key.
    public var email: String?                     // REAL email. Privacy-sensitive. §7.5
    public var plan: String                       // "max" | "pro"
    public var configDir: String
    public var ok: Bool
    public var error: String?
    public var headroomPercent: Double?
    public var limits: [AccountLimit]
    public var reservePercent: Int
    public var reserveBlocked: Bool
    /// FRESHNESS §6: false when cclimits failed for THIS account. Absent ≠ unlimited —
    /// limits_by_account() drops !ok accounts today, which mis-triages a limit-parked
    /// session as `waiting`, i.e. the loudest state in the app.
    public var known: Bool
    public var fresh: Bool

    /// Model-scoped caps do NOT block an account; they strand only sessions on that model.
    public var accountWideExhausted: Bool { limits.contains { $0.exhaustedNow && !$0.modelScoped } }
}

public struct AccountLimit: Sendable, Equatable, Identifiable {
    public var id: String { label + group }
    public var label: String                      // "Session" | "Weekly" | "Fable"
    public var group: String                      // "session" | "weekly"
    public var percentUsed: Double                // the BAR shows USED
    public var remainingPercent: Double?          // the COLOUR comes from remaining
    public var modelScoped: Bool
    public var exhaustedNow: Bool
    public var resetsAt: Date?                    // ISO-8601 STRING on the wire, often null

    public var remaining: Double { remainingPercent ?? (100 - percentUsed) }
}
```

## 3.5 Intents, resumes, dispatch log, chat

```swift
// OrchestraCore/Model/Intent.swift
public struct Intent: Sendable, Equatable, Identifiable, Codable {
    public enum Kind: String, Sendable, Codable {
        case finish, dispatch, send, resume, reserve, kill
    }
    public enum Phase: String, Sendable, Codable {
        case armed, running
        case briefSent = "brief_sent"
        case closing, done, failed, interrupted
        case unknown
        public init(from d: Decoder) throws {
            self = Phase(rawValue: try d.singleValueContainer().decode(String.self)) ?? .unknown
        }
        public var isTerminal: Bool { self == .done || self == .failed || self == .interrupted }
    }

    public let id: IntentID
    public let kind: Kind
    public let target: TargetRef
    public var phase: Phase
    public let createdAt: Date
    public let expiresAt: Date?
    public var message: String?
    public var progress: [String]                 // "① picked → wt · [acct]", "  effort confirmed ✓"
    public var result: IntentResult?
}

public struct IntentResult: Sendable, Equatable, Codable {
    public var ok: Bool
    public var message: String
    /// start_finish's tier. exit | pending | slim | brief | parked | noop | dispatch.
    public var mode: String?
    public var session: String?                   // "mission-<wt>-HHMMSS"
    public var worktree: String?
    public var account: String?
    public var model: String?
    public var effort: String?
    public var effortConfirmed: Bool?
    public var kickoffSent: Bool?
    public var attach: String?                    // "tmux -L fleet attach -t …"
}

public struct IntentRequest: Sendable, Hashable, Codable {
    public let id: IntentID
    public let kind: Intent.Kind
    public let target: TargetRef
    public let confirm: Bool
    public let payload: IntentPayload
}

public enum IntentPayload: Sendable, Hashable, Codable {
    case none
    case text(String)                                            // send
    case mission(MissionSpec)                                    // dispatch
    case resume(due: Date?, delay: TimeInterval?, model: String?) // resume
    case reserve(account: String, percent: Int)                  // percent MUST be an Int:
                                                                 // int("50.5") raises server-side
}

public struct MissionSpec: Sendable, Hashable, Codable {
    public var mission: String
    public var worktree: WorktreeID?              // nil ⇒ AUTO (cleanest free worktree)
    public var account: String?                   // nil ⇒ AUTO (most model headroom)
    public var model: String                      // fable | opus | sonnet | haiku — REQUIRED
    public var effort: String                     // high | xhigh | max | ultracode — REQUIRED
    public var forceModel: Bool                   // bypasses the reserve/headroom check
}

/// Returned as a typed error when the server refuses on headroom grounds.
public struct ModelDecision: Sendable, Equatable {
    public var model: String
    public var message: String                    // server prose, shown verbatim
    public var canOpus: Bool
    public var opusAccount: String?
    public var opusLeft: Int?
}
```

```swift
// OrchestraCore/Model/ResumeSchedule.swift
public struct ResumeSchedule: Sendable, Equatable, Identifiable {
    public var id: ResumeKey { key }
    public let key: ResumeKey
    public var account: String
    public var model: String?
    public var delay: TimeInterval
    public var resetsAt: Date?
    public var dueAt: Date
    public var createdAt: Date?                   // ABSENT in demo mode
    public var attempts: Int                      // gives up at RESUME_MAX_ATTEMPTS = 10
    public var status: Status
    public var message: String?                   // "sent 'continue' — sent via tmux"
    public var firedAt: Date?
    public var startedAt: Date?                   // hand-off item 16: the `firing` window

    public enum Status: String, Sendable, Codable {
        case pending, done, failed
        case firing                               // NEW: fire_resume can block ~14 minutes
        case unknown
        public init(from d: Decoder) throws {
            self = Status(rawValue: try d.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }
}

// OrchestraCore/Model/ChatMessage.swift
public struct ChatMessage: Sendable, Equatable, Identifiable {
    public var id: String { "\(role.rawValue)-\(index)-\(timestamp?.timeIntervalSince1970 ?? 0)" }
    public let index: Int
    public let role: Role
    /// Server-cleaned to 900 chars: ANSI stripped, <tags> under 80 chars stripped, and
    /// ALL NEWLINES COLLAPSED TO SINGLE SPACES. Code blocks and lists arrive as one
    /// run-on line and cannot be recovered client-side. Render as prose, not as code.
    public let text: String
    public let timestamp: Date?
    public enum Role: String, Sendable, Codable { case you, agent }   // NOT user/assistant
}

// OrchestraCore/Model/DispatchLogEntry.swift
public struct DispatchLogEntry: Sendable, Equatable, Identifiable {
    public var id: String { "\(session)-\(timestampRaw)" }
    /// "%Y-%m-%dT%H:%M:%S" — TIMEZONE-NAIVE LOCAL, second resolution. Unparseable as an
    /// absolute instant without knowing the Mac's zone, so we keep the raw string and
    /// render it verbatim rather than lying with a localised time.
    public let timestampRaw: String
    public let session: String
    public let worktree: String
    public let account: String
    public let model: String?
    public let effort: String?
    public let missionOriginal: String            // FULL untruncated author prose
    public let kickoff: String
    public let isCloseout: Bool
    public let alive: Bool                        // computed at read time from tmux
}
```

## 3.6 The wire frame

```swift
// OrchestraCore/Model/StateFrame.swift
public struct StateFrame: Sendable, Equatable {
    public enum Kind: String, Sendable, Codable { case snapshot, delta }
    public let kind: Kind
    public let version: Int
    public let base: Int?                         // delta only: the version it applies to
    public let at: Date
    /// MANDATORY on BOTH snapshot and delta frames. Dictionary.values has no defined
    /// order, so a delta branch that rebuilt the board from `cards` would reshuffle it
    /// into hash order on every event — and the held-order machinery would record nothing,
    /// silently disabling the one safety mechanism the deployment target was argued on.
    public let order: [WorktreeID]
    /// A null value means the card was REMOVED.
    public let cards: [WorktreeID: Worktree?]
    public let counts: Counts
    public let freshness: Freshness
    public let freeWorktrees: [WorktreeID]?
    public let otherProcs: [OtherProc]?
    public let resumes: [ResumeKey: ResumeSchedule]?
}

// OrchestraCore/Model/ServerMeta.swift
public struct ServerMeta: Sendable, Equatable, Codable {
    public let ok: Bool
    public let version: String                    // "2.0.0"
    public let contract: Int                      // the HARD gate — §5.6
    /// Written at RESPONSE-SERIALISATION time, not out of a cache. The ONLY skew source.
    public let serverTime: Date
    public let demo: Bool
    public let deviceID: String
    public let tokenFingerprint: String
    public let caps: Set<String>                  // sse, intents, digest, push, gzip, since, …
    public let limits: Limits
    public struct Limits: Sendable, Equatable, Codable {
        public let maxStreams: Int
        public let intentTTL: TimeInterval
    }
}

// OrchestraCore/Model/Freshness.swift
public struct Freshness: Sendable, Equatable, Codable {
    public var collectorOK: Bool
    public var lastTickAt: Date
    public var wakeGap: TimeInterval              // seconds the Mac was asleep, last transition
    public var perKind: [String: Date]            // "git" → last git tier completion, etc.
}
```

## 3.7 DTO ⇄ domain mapping — where the wire's sharp edges are blunted

```swift
// OrchestraAPI/DTO/SessionDTO.swift
struct SessionDTO: Decodable, Sendable {
    let sid: String
    let id: String
    let account: String
    let last_write_at: Double                     // contract 2 (ENGINE §3.4)
    let cwd: String
    let subdir: String?
    let branch: String?
    let model: String?
    let pending_tools: [String]?
    let pending_workflows: Int?
    let pending_bg_agents: Int?
    let topic: String?
    let last_assistant: String?
    let last_user: String?
    let subagent_said: String?
    let subagents_active: Bool?
    let pid: Int32?
    let pid_certain: Bool?
    let status: SessionStatus
    let limit: SessionLimitDTO?                   // absent unless status == limit
    let handed_to: String?                        // absent unless handed off
    let tool_running: Bool?                       // PRESENT ONLY WHEN TRUE — never false
    let bg_shell: Bool?                           // ditto
    let turn_ended: Bool?
}

extension Session {
    init(_ d: SessionDTO) {
        self.sid       = SessionID(d.sid)
        self.shortID   = d.id
        self.account   = d.account
        self.lastWriteAt = Date(timeIntervalSince1970: d.last_write_at)
        self.cwd       = d.cwd
        self.subdir    = d.subdir
        self.branch    = d.branch
        self.model     = d.model ?? ""            // "" is a REAL observed value
        self.pendingTools            = d.pending_tools ?? []
        self.pendingWorkflows        = d.pending_workflows ?? 0
        self.pendingBackgroundAgents = d.pending_bg_agents ?? 0
        self.topic         = d.topic
        self.lastAssistant = d.last_assistant
        self.lastUser      = d.last_user
        self.subagentSaid  = d.subagent_said
        self.subagentsActive = d.subagents_active ?? false
        self.pid        = d.pid
        self.pidCertain = d.pid_certain ?? false
        self.status     = d.status
        self.limit      = d.limit.map(SessionLimit.init)
        self.handedTo   = d.handed_to
        // Conditional booleans are ABSENT, not false. decodeIfPresent + `?? false`.
        self.toolRunning     = d.tool_running ?? false
        self.backgroundShell = d.bg_shell ?? false
        self.turnEnded       = d.turn_ended ?? false
    }
}
```

```swift
// OrchestraCore/Format/TextTruncation.swift
public enum Trunc {
    /// git.commit.subject and topology branch.subject are the ONLY completely untruncated
    /// strings in the payload — every session field is server-_clean()ed to 140/240/900.
    /// A 300-char subject arrives intact and blows up a phone card. Cut at the mapping
    /// boundary so no view has to remember.
    public static func subject(_ s: String, max: Int = 200) -> String {
        s.count <= max ? s : String(s.prefix(max - 1)) + "…"      // U+2026, matching _clean
    }
}
```

## 3.8 Formatters the client must reimplement exactly

`index.html` is the reference implementation and the phone must agree with it character for
character, or the same fleet reads differently on two screens.

```swift
// OrchestraCore/Format/RelativeTime.swift
public enum RelativeTime {
    /// index.html:390 `rel(s)`.  nil → "—" · <60 → "45s" · <3600 → "12m"
    /// · <86400 → "3h7m" (NO zero padding) · else → "2d"
    public static func short(_ seconds: TimeInterval?) -> String {
        guard let s = seconds, s.isFinite else { return "—" }
        let n = Int(max(0, s))
        if n < 60    { return "\(n)s" }
        if n < 3600  { return "\(n / 60)m" }
        if n < 86400 { return "\(n / 3600)h\((n % 3600) / 60)m" }
        return "\(n / 86400)d"
    }
}

// OrchestraCore/Format/ETime.swift
public enum ETime {
    /// live_proc.etime is a RAW `ps` string, not seconds. Three forms:
    /// "15:02" (mm:ss) · "12:43:46" (hh:mm:ss) · "2-03:14:22" (d-hh:mm:ss)
    public static func parse(_ raw: String) -> TimeInterval? {
        var days = 0.0, rest = raw
        if let dash = raw.firstIndex(of: "-") {
            days = Double(raw[raw.startIndex ..< dash]) ?? 0
            rest = String(raw[raw.index(after: dash)...])
        }
        let parts = rest.split(separator: ":").compactMap { Double($0) }
        switch parts.count {
        case 2: return days * 86400 + parts[0] * 60 + parts[1]
        case 3: return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2]
        default: return nil
        }
    }
}

// OrchestraCore/Format/AccountLabel.swift
public enum AccountLabel {
    /// index.html:922 — basename; ".claude" → "main"; otherwise strip a leading ".claude-".
    public static func fromConfigDir(_ dir: String) -> String {
        let base = (dir as NSString).lastPathComponent
        if base == ".claude" { return "main" }
        if base.hasPrefix(".claude-") { return String(base.dropFirst(8)) }
        if base.hasPrefix(".claude")  { return String(base.dropFirst(7)) }
        return base
    }
}

// OrchestraCore/Format/ClockLabel.swift
public enum ClockLabel {
    /// index.html:787 — "14:05" if today, else "Tue 14:05". Formatters are constructed
    /// once per call site and cached in the view model, never as a global `static let`.
    public static func short(_ date: Date, now: Date, calendar: Calendar) -> String
}
```

`OrchestraCoreTests` exercises every branch of every one of these against the JS source
values verbatim; see §9.1.

---

# 4. Swift 6 strict concurrency

`SWIFT_STRICT_CONCURRENCY = complete`, `SWIFT_TREAT_WARNINGS_AS_ERRORS = YES`. No
partial-checking escape hatch, no `@preconcurrency import`.

## 4.1 The isolation map

| module | default isolation | why |
|---|---|---|
| `OrchestraCore` | **nonisolated** | must be callable from a widget timeline provider, an NSE, and a `TestClock` actor |
| `OrchestraPersistence` | **nonisolated** | `FileStore` is an `actor`; it decodes `Persisted/` types from off-MainActor |
| `OrchestraAPI` | **nonisolated** | `APIClient` is an `actor`; the SSE delegate is `@unchecked Sendable` |
| `OrchestraStore` | **`MainActor`** | `@Observable` is not `Sendable`; stores are main-thread by construction |
| `OrchestraUI` | **`MainActor`** | SwiftUI |
| `OrchestraTestSupport` | **nonisolated** | must be importable from both kinds of test target |

## 4.2 The clock seam — introduced before any store is written

Every load-bearing behaviour here is time-dependent: intent arm windows, staleness
thresholds, reconnect backoff, the reorder grace, the Live Activity deadline, chat cadence,
fixture rebasing. None of it is testable against `Date.now`, and retrofitting a seam means
rewriting every store initialiser and every test. So it goes in **first**.

```swift
// OrchestraCore/Time/AppClock.swift — nonisolated, ~40 lines
public protocol AppClock: Sendable {
    var now: Date { get }
    func sleep(for duration: Duration) async throws
}

public struct SystemAppClock: AppClock {
    public init() {}
    public var now: Date { Date() }
    public func sleep(for d: Duration) async throws { try await Task.sleep(for: d) }
}

// The stdlib exposes `components`, not a Double `seconds`.
public extension Duration {
    var seconds: Double { Double(components.seconds) + Double(components.attoseconds) * 1e-18 }
}
```

```swift
// OrchestraTestSupport/TestClock.swift — ~80 lines. Never touches the system clock.
public actor TestClock: AppClock {
    public private(set) var current: Date
    private var sleepers: [(deadline: Date, cont: CheckedContinuation<Void, Error>)] = []

    public init(_ start: Date = .fixtureEpoch) { current = start }
    public nonisolated var now: Date { /* snapshot via a lock-free box */ }
    public func advance(by d: Duration) async { … }    // wakes sleepers in deadline order
    public func sleep(for d: Duration) async throws { … }
}
```

Every store takes `let clock: any AppClock`. `LiveUpdateEngine` sleeps on it.
`Intent.expiresAt` is compared against it. `ClockSeamTests` asserts `TestClock` never reads
the system clock, by advancing ten minutes in a test that completes in under 10 ms.

## 4.3 The two traps this package layout makes impossible

**Trap 1 — MainActor-isolated `Codable` conformances.** Under
`defaultIsolation(MainActor.self)`, a synthesised `Codable` conformance is MainActor-isolated.
`FileStore` is an `actor` and calls it from outside MainActor — which does not compile, and
the error points at generated code. **Fix:** every persisted type lives in
`OrchestraCore/Persisted/` and every store-over-the-filesystem lives in
`OrchestraPersistence`, both nonisolated. `OrchestraPersistenceTests` decodes each persisted
type from a nonisolated context, so the constraint is enforced by a red test rather than by
memory.

**Trap 2 — test targets inherit no settings.** Each `.testTarget` declares its isolation
explicitly (§1.4). The resulting pattern is documented once, here, rather than rediscovered
per test: a MainActor suite `await`s a nonisolated `APIClient` method, then observes the
store synchronously.

```swift
@Test func deltaAppliesInServerOrder() async throws {
    let frame = try await client.state(since: 41_203, mode: .full)   // nonisolated, crosses out
    store.apply(frame)                                               // MainActor, synchronous
    #expect(store.order.map(\.rawValue) == ["orbital-api", "orbital-web"])
}
```

## 4.4 The ten breakages, and the pattern for each

| # | naive break | the pattern |
|---|---|---|
| 1 | `@Observable final class Store: Sendable` does not compile; devs reach for `@unchecked Sendable` | stores are **never** `Sendable`; `@MainActor` via module default; cross-boundary traffic is `Sendable` value structs |
| 2 | `@State private var model = AppModel()` in a nonisolated `App` | `@main @MainActor struct OrchestraApp: App` |
| 3 | `NotificationCenter.default.notifications(named:)` — `Notification` is not `Sendable` | **no NotificationCenter for lifecycle.** `@Environment(\.scenePhase)` + `.onChange` |
| 4 | `Timer.scheduledTimer` / `DispatchSourceTimer` callbacks are nonisolated | one structured task per engine, started from a MainActor method, sleeping on the injected clock |
| 5 | `JSONDecoder`, `ISO8601DateFormatter`, `DateFormatter` as `static let` — global mutable state | actor-isolated **stored properties** of `APIClient`, or view-model-owned. Never static. |
| 6 | `URLSessionDelegate` callbacks are nonisolated | exactly **two** delegates exist: `SSEDelegate` (§2.1) and `PinnedTrustDelegate` (§7.3), both allowlisted by name in `lint-isolation.sh` |
| 7 | mutating a store from `Task.detached` | `Task.detached` **banned** in App/Store/UI; enforced by lint |
| 8 | widget `TimelineProvider` / NSE run outside MainActor and cannot touch stores | enforced by the **package graph** — neither extension target links `OrchestraStore` |
| 9 | `nonisolated(unsafe)` as a silencer | banned outside `OrchestraTestSupport`; same lint |
| 10 | **under `NonisolatedNonsendingByDefault` (SE-0461), a `nonisolated async func` in Core/API called with `await` from MainActor runs ON the main actor** | any `async` function in Core/API doing non-trivial work must be `@concurrent`. Lint flags `nonisolated async func` in those modules that is neither `@concurrent` nor actor-isolated. |

Trap 10 is the one that comes *with* the isolation strategy and fails **silently, in the
frame-time direction** — a 35 KB decode quietly moving onto the main thread does not throw,
it just drops frames. `Tools/lint-isolation.sh`:

```sh
#!/bin/sh
set -eu
SRC=ios/Packages/OrchestraKit/Sources
FAIL=0
check() { if grep -rn "$1" $2 --include=*.swift | grep -v "$3"; then
            echo "  ✗ $4"; FAIL=1; fi; }

check 'Task\.detached'        "$SRC/OrchestraStore $SRC/OrchestraUI ios/App" 'XXNOMATCHXX' \
      'Task.detached is banned outside tests'
check '@unchecked Sendable'   "$SRC" 'SSEDelegate\|PinnedTrustDelegate' \
      '@unchecked Sendable is allowlisted to two named delegates'
check 'nonisolated(unsafe)'   "$SRC" 'OrchestraTestSupport' \
      'nonisolated(unsafe) is banned outside OrchestraTestSupport'
check 'AnyView'               "$SRC/OrchestraUI ios/App/Board" 'XXNOMATCHXX' \
      'AnyView defeats the structural diff the board depends on'
check 'URLSession\.shared'    "$SRC ios/App" 'XXNOMATCHXX' \
      'URLSession.shared has no delegate and cannot accept the pinned cert'
check 'nonisolated func .*async' "$SRC/OrchestraCore $SRC/OrchestraAPI" '@concurrent' \
      'nonisolated async without @concurrent runs on the CALLER actor (SE-0461)'
exit $FAIL
```

## 4.5 Store shape — equality-guarded writes

```swift
// OrchestraStore/FleetStore.swift
@Observable
public final class FleetStore {
    public private(set) var cards: [WorktreeID: Worktree] = [:]
    public private(set) var order: [WorktreeID] = []
    public private(set) var counts: Counts = .init()
    public private(set) var freeWorktrees: [WorktreeID] = []
    public private(set) var otherProcs: [OtherProc] = []
    public private(set) var resumes: [ResumeKey: ResumeSchedule] = [:]
    public private(set) var version = 0
    public private(set) var pendingOrder: [WorktreeID]?
    /// Names appearing on two cards (the collision in §3.1). Computed ONCE inside apply()
    /// and stored — as a computed property reading `visible` it would register observation
    /// on the whole array and re-render every card every frame.
    public private(set) var ambiguousNames: Set<String> = []

    public var reorderHeld = false {
        didSet { if !reorderHeld && oldValue { flushOrder() } }
    }
    public var heldMoveCount: Int { pendingOrder.map { diffCount($0, order) } ?? 0 }
    public var visible: [Worktree] { order.compactMap { cards[$0] } }

    public func apply(_ frame: StateFrame) {
        // A delayed frame must never rewind the board.
        guard frame.version >= version else { return }

        switch frame.kind {
        case .snapshot:
            var next: [WorktreeID: Worktree] = [:]
            for (key, card) in frame.cards { if let card { next[key] = card } }
            set(\.cards, next)
        case .delta:
            var next = cards
            for (key, card) in frame.cards {
                if let card { next[key] = card } else { next.removeValue(forKey: key) }
            }
            set(\.cards, next)
        }

        // ORDER IS NEVER DERIVED FROM A DICTIONARY. Every frame carries it (§3.6).
        if reorderHeld && !order.isEmpty {
            pendingOrder = frame.order
            set(\.order, reconcileHeld(current: order, server: frame.order))
        } else {
            pendingOrder = nil
            withAnimation(order.isEmpty ? nil : .snappy(duration: 0.30)) {
                set(\.order, frame.order)
            }
        }
        set(\.counts, frame.counts)
        if let f = frame.freeWorktrees { set(\.freeWorktrees, f) }
        if let o = frame.otherProcs    { set(\.otherProcs, o) }
        if let r = frame.resumes       { set(\.resumes, r) }
        set(\.ambiguousNames, Self.duplicateNames(in: cards))
        version = frame.version
    }

    /// While held: removals apply (a vanished card must not linger), additions APPEND at
    /// the tail — matching index.html's "the end of the grid never shifts existing cards"
    /// — and nothing already on screen moves.
    private func reconcileHeld(current: [WorktreeID], server: [WorktreeID]) -> [WorktreeID] {
        let live = Set(server)
        var out = current.filter(live.contains)
        out.append(contentsOf: server.filter { !out.contains($0) })
        return out
    }

    private func flushOrder() {
        guard let p = pendingOrder else { return }
        withAnimation(.snappy(duration: 0.30)) { set(\.order, p) }   // the 300 ms FLIP
        pendingOrder = nil
    }

    /// The @Observable macro generates `set { withMutation(keyPath:) { _x = newValue } }`
    /// with NO equality check — observers fire on EVERY assignment regardless of value.
    /// Property splitting alone is necessary but NOT sufficient; this is the other half.
    private func set<V: Equatable>(_ kp: ReferenceWritableKeyPath<FleetStore, V>, _ new: V) {
        if self[keyPath: kp] != new { self[keyPath: kp] = new }
    }
}
```

Both halves are load-bearing. Splitting properties stops a view that never *reads* `cards`
from being invalidated when `cards` is assigned (Observation tracks access). The equality
guard stops an unchanged-value assignment from firing *that property's* observers.
`version` and `freshness` — the two things that legitimately change on many frames — live
in a separate `FreshnessStore`, so the staleness chip is the only thing that re-renders on
a no-op frame.

`FleetMergeTests` asserts, with `os_signpost` counters, that two identical frames produce
exactly **one** `body` evaluation for `TileStrip`, not two.

## 4.6 SwiftUI pitfalls, named

1. **Identity.** `ForEach(store.visible)` with `Worktree.id == WorktreeID`. Never
   `id: \.self` on a struct that changes every frame.
2. **Push values down, not the store.** `WorktreeCardView` takes `let worktree: Worktree`,
   never `@Environment(FleetStore.self)`. SwiftUI's structural diff then prunes unchanged
   cards for free — exactly what `index.html` achieves by hand with its `el._html` cache.
3. **Zero work in `body`.** The server sorts; the client renders.
4. **One clock, scoped tight.** Exactly one
   `TimelineView(.periodic(from: clock.now, by: 1))`, wrapped around the smallest countdown
   subtree (`CountdownText`), with `.animation(nil)` inside. The desktop's 1 s `setInterval`
   over every `.resume` button (index.html:784) would, ported naively, invalidate the whole
   tree every second. The ticker is **suspended while `scrollPhase != .idle`** and while
   Low Power Mode is on — nobody reads a timestamp mid-flick.
5. **No `AnyView`** anywhere in the board hierarchy; it defeats the diff item 2 depends on.
6. **Animate only when not held.** `.snappy(duration: 0.30)` matches the desktop's 300 ms
   `cubic-bezier(0.22, 1, 0.36, 1)` FLIP.
7. **`.task(id:)`, never `.onAppear` + `Task {}`** — the manual version leaks a loop per
   navigation push.
8. `os_signpost` around `apply`, decode and `body`; a `MetricKit` subscriber for hang rate,
   launch time and energy. Budgets are measured, not asserted.

## 4.7 The re-sort hold, as a pure testable rule

```swift
// OrchestraCore/Rules/ReorderHold.swift — nonisolated, no SwiftUI import
public enum ScrollActivity: Sendable, Equatable {
    case idle, tracking, interacting, decelerating, animating
}

/// The touch translation of index.html's `gridHover` (index.html:594-598). On iOS a tap
/// fires pointerenter and pointerleave may never fire, so hover is not the analogue —
/// motion plus a dwell grace is. A naive port either locks the board into permanent
/// "⌗ re-sort held" or never holds at all.
public func shouldHoldReorder(activity: ScrollActivity,
                              lastMotionAt: Date,
                              now: Date,
                              grace: TimeInterval = 2.5) -> Bool {
    if activity != .idle { return true }
    return now.timeIntervalSince(lastMotionAt) < grace
}
```

The view is a one-line adapter. `ReorderHoldTests` exercises the full 5×N matrix with a
`TestClock`. The grace window is released early by an explicit affordance: while held **and**
the server order differs, the board shows a tappable chip
`⌗ 3 cards want to move — tap to re-sort` (the mobile translation of the desktop's passive
`⌗ re-sort held`). Tapping applies the order with the FLIP. **Nothing ever moves under a
stationary finger without the user asking.**

Alongside it, the direct port of `_movedAt` (index.html:609) — a card whose grid position
changed within the last **700 ms swallows its first tap** and toasts
`ConfidAI moved — tap again`. Both guards ship, because they cover different hazards: the
staleness guard (`cardRev`, §7.6) protects against the card's state having changed, and the
motion guard protects against a *different* card sliding under the thumb, where `cardRev`
would be perfectly fresh for the wrong target.

---

# 5. The sync engine

## 5.1 SSE is the primary path; polling is the fallback

```swift
// OrchestraStore/LiveUpdateEngine.swift
@Observable
public final class LiveUpdateEngine {
    public enum Mode: Sendable, Equatable {
        case idle
        case streaming(since: Date)
        case polling(Duration, reason: PollReason)
    }
    public enum PollReason: Sendable, Equatable { case sseUnavailable, streamFlapping, relayed }
    public enum ViewFocus: Sendable, Equatable {
        case board, session(SessionID), limits, map, mission
    }

    public private(set) var mode: Mode = .idle

    public func start() async
    public func stop()
    public func scenePhaseChanged(to phase: ScenePhase) async
    public func focusChanged(to focus: ViewFocus)
    public func reconcileOnForeground() async
}
```

**Streaming is the normal state.** One `GET /api/events` socket. An idle board costs one
`: keepalive` comment per ≤25 s. Frames carry only what changed — measured at ~120–200 B
per delta (`ENGINE.md §3.5`), against 36,326 B for a full poll.

```
event: hello
data: {"server_time":1784636692.641,"v":41203,"tick":3.0,"hb":25.0,
       "collector_ok":true,"wake_gap":0.0,"caps":["sse","intents","digest","push"]}

id: 41207
event: state
data: {"type":"delta","v":41207,"base":41203,"at":1784636698.1,
       "order":["orbital-api","orbital-web","kepler-worker"],
       "cards":{"orbital-api":{…}},"counts":{…},"freshness":{…}}

id: 41208
event: intent
data: {"id":"ios-9f2c…","kind":"finish","target":"wt:orbital-web",
       "phase":"brief_sent","at":1784636701.0,
       "message":"closeout brief sent — ✕ close verifies the landing"}

event: resync
data: {"v":41900,"reason":"cursor_too_old"}

: keepalive
```

Reconnect sends `Last-Event-ID: 41208`. An unknown or too-old version yields a
`{"type":"snapshot", …}` frame (512-version ring server-side).

Three requirements the client depends on, all cheap, all in the frame contract:

1. **`order: [String]` on every snapshot and delta.** §3.6 explains why.
2. **A `: keepalive` comment at ≤25 s.** SSE over a NAT'd WireGuard tunnel dies without a
   FIN; the keepalive is the only liveness signal. Absence for 40 s ⇒ reconnect.
3. **`event: hello` carrying `server_time`, `tick`, `hb`, `collector_ok`, `wake_gap`** —
   and `hb` frames repeating `tick`/`hb`/`collector_ok`/`wake_gap`, so a client that
   connected during a 3 s busy period widens its threshold when the server drops to a 20 s
   tick instead of going permanently amber.

**HTTP/1.0 stays pinned server-side.** `REALTIME.md §5.2` is explicit: SSE over HTTP/1.1
without chunked framing or `Connection: close` hangs `EventSource` forever waiting on a
`Content-Length` that never arrives. The per-request TCP+WireGuard handshake cost is fixed
by making **one** request, not by adding keep-alive to many. (The `AUTH.md` track sets
`protocol_version = "HTTP/1.1"` for the non-stream endpoints together with a
`Handler.timeout` and an explicit `Connection: close` on the stream route; the two tracks
must land that pair together or the stream hangs.)

## 5.2 Polling fallback cadence

Entered when `/api/meta` lacks `sse`, when the stream errors twice inside 60 s, or on a
relayed path (§5.7). The cadence table exists **only** for that path.

| condition | interval |
|---|---|
| any `needs_input` / `blocked` visible | 4 s |
| anything `working` | 8 s |
| only `limit` / `waiting` / `ended` | 20 s |
| board behind a detail screen | 30 s |
| Low Power Mode | floor 20 s |
| `NWPath.isExpensive` | floor 15 s |
| `NWPath.isConstrained` (Low Data Mode) | floor 45 s **and `mode=digest`** |

The loop is **non-blocking**: `poll()` runs as a cancellable child task with a deadline of
one cadence interval and is cancel-and-skipped rather than serialised, so a slow read never
stalls the loop.

Note on **Low Data Mode**: the correct response is to keep the *cheapest* transport, not to
switch it off. A 60 s poll of the 9.2 KB gzipped snapshot is ~552 KB/hour — **13× more**
than the stream it would replace. So in Low Data Mode the client keeps the stream, sends
`low=1` (server drops to a 20 s tick and a 50 s heartbeat) and suppresses everything
discretionary: no auto-chat refresh, no topology, no limits except on explicit tap, no
prefetch, no snapshot refetch when the cursor is replayable.

**Never put topology on a timer from the phone.** `branch_topology()` is ~90 forks / 3.07 s
today with a 30 s TTL that the desktop map already always misses. Fetch on sheet open, then
only on explicit pull-to-refresh or when a delta touched a card's `git`.

## 5.3 Network classification — the platform signal is not trustworthy here

`isExpensive` / `isConstrained` are read off the **default** path. This design mandates an
always-on packet tunnel whose `utun` interface reports as `.other`, and expensiveness does
not reliably propagate from the underlying physical link. Since §5.2 promises Low Data Mode
is honoured, an unenforceable signal is unacceptable.

```swift
// OrchestraStore/ConnectionStore.swift (excerpt)
let defaultPath  = NWPathMonitor()                                  // reachability
let wifiPath     = NWPathMonitor(requiredInterfaceType: .wifi)
let cellularPath = NWPathMonitor(requiredInterfaceType: .cellular)
// transport := whichever auxiliary monitor is .satisfied, cross-checked against
// defaultPath.availableInterfaces; isConstrained taken from the auxiliary path.
```

Plus a user-visible **manual override** ("treat this connection as cellular") as the honest
fallback. This must be measured on a real device with Tailscale on and off before the
cadence table is treated as spec (§11).

## 5.4 Clock skew — one source, one owner

```swift
// OrchestraCore/Time/ServerClock.swift — nonisolated value, single owner
public struct ServerClock: Sendable, Equatable {
    public private(set) var offset: TimeInterval = 0     // server minus device
    private var samples = 0

    public mutating func observe(serverTime: Double, midpoint: Date) {
        let s = serverTime - midpoint.timeIntervalSince1970
        // A zero offset is a LEGITIMATE value, not "unseeded" — count samples instead of
        // using `offset == 0` as a sentinel, or a correctly NTP-synced pair never smooths.
        offset = samples == 0 ? s : offset * 0.8 + s * 0.2
        samples += 1
    }
    public func now(device: Date) -> Date { device.addingTimeInterval(offset) }
}
```

```swift
@Observable public final class ConnectionStore {
    public private(set) var clock = ServerClock()          // THE clock. var, not let.
    public private(set) var caps: ServerCapabilities = .none

    /// Skew is sampled ONLY here, from /api/meta.server_time (written at serialisation),
    /// never from `generated_at` or a frame's `at` — those come out of a cache up to 4 s
    /// stale on top of ~1.6 s of collection, and would bake a permanent negative bias into
    /// every countdown and into the fresh/stale/cold thresholds that gate actuation.
    public func noteMeta(_ m: ServerMeta, sentAt: Date, receivedAt: Date) {
        let rtt = receivedAt.timeIntervalSince(sentAt)
        guard rtt < 1.0 else { return }                    // discard a noisy sample
        clock.observe(serverTime: m.serverTime.timeIntervalSince1970,
                      midpoint: sentAt.addingTimeInterval(rtt / 2))
        caps = ServerCapabilities(m.caps)
        if rtt > 0.150 { state = .degraded(.relayed(rttMS: Int(rtt * 1000))) }
    }
}
```

`APIClient` holds **no** clock and **no** capability set; it receives a `Sendable`
`RequestContext` snapshot at call time. Two clocks in two isolation domains with no sync
direction is untestable and produces layer-dependent behaviour.

If `|offset| > 120` the app surfaces it once: *"your phone's clock is 3 minutes behind the
Mac — times may look wrong."* A wrong reset countdown makes ▶ resume lie, so it is not
papered over.

**Time-invariance is why this matters more, not less.** `ENGINE.md §3.4` removes `age_s`
and `resets_in` from the wire in favour of absolute instants, so every relative label is
computed on-device at 1 Hz — meaning device-now minus a server instant, which is exactly
the subtraction skew corrupts. The old
`activityDate(ageS:generatedAt:)` helper is deleted; there is no `age_s`.

## 5.5 Freshness — liveness and recency are two different things

Keyed on "last applied delta", a healthy idle stream goes STALE and disables every control.
Keyed loosely on "any frame", a wedged collector reads LIVE forever. Both are real; the fix
is to stop conflating them.

```swift
// OrchestraStore/FreshnessStore.swift
public struct ConnectionFreshness: Sendable, Equatable {
    public var lastFrameAt: Date        // ANY frame: hello, state, intent, hb, keepalive
    public var serverAt: Date           // skew-corrected collector generated_at
    public var hbPeriod: TimeInterval   // from the latest hb — tracks server cadence changes
    public var tick: TimeInterval
    public var collectorOK: Bool
}

public enum BoardState: Sendable, Equatable {
    case live
    case lagging(TimeInterval)          // frames fine, data behind
    case collectorStuck(String?)
    case stale(TimeInterval)
    case offline(TransportFailure)
    case macAsleep(TimeInterval)        // wake_gap > 120 on the last hello
    case absent                         // never connected
}
```

| state | condition | board | actuation |
|---|---|---|---|
| **live** | frames within `hb*1.6+5`, `serverAt` within `3*tick+10`, `collectorOK` | green dot, no chrome | enabled |
| **lagging** | frames fine, `serverAt` behind, or one missed hb | amber dot + "data as of 34s ago" | **enabled** — no functional change |
| **collectorStuck** | `collector_ok: false`, or `serverAt` behind > 90 s | amber bar naming it | disabled |
| **stale** | no frame for `hb*3+10` | board dims to 55 % (reusing the `.ended` convention), amber bar | **disabled, with the reason inline on the control** — never hidden, because hiding reflows under the thumb |
| **offline** | 3 failed reconnects, or `NWPath` unsatisfied | red bar + "Retry now" | disabled |
| **macAsleep** | `NWPath.satisfied` but connect refused/timed out, or `wake_gap > 120` | "your Mac was asleep for 3h 12m — catching up" | disabled |
| **absent** | first launch | `ContentUnavailableView` — the empty state `index.html` never had (`#grid` with zero worktrees renders completely blank today) | n/a |

One rule falls out of absolute timestamps and is easy to get backwards:

> **Ages keep ticking while stale. Statuses dim.**

An age derives from an absolute transcript mtime, so "8m ago" stays literally true with a
dead stream, and it degrades in the *safe* direction — counting up toward "we don't know".
A `● WORKING` badge on a four-minute-old snapshot is a lie and gets dimmed. A frozen board
that still looks alive is the failure to design against; a live board that looks frozen
trains the user to ignore the indicator, which destroys it for the real case.

Per-field freshness (`ENGINE.md §3.3`) drives sub-badges: a card whose `git` tier is >3× its
cadence behind shows a small `git 47s` marker rather than presenting stale ahead/behind as
fact.

**Regression test, explicitly:** idle fleet, zero deltas for five simulated minutes → the
client asserts `.live` throughout.

## 5.6 Version gate — one hard check, no degradation matrix

```swift
guard meta.contract >= 2 else {
    throw OrchestraError.serverTooOld(found: meta.contract, required: 2)
}
```

One blocking screen: *"starbase is running orchestra 1.x. This app needs 2.0 or newer — pull
and run `./start.sh`."* Justified by ADR 0004 ("settle the contract once; backend first;
iOS last"): the server ships before the app and the user controls both. A capability matrix
against a server we ship ourselves is unpaid complexity, and the failure it would hide —
the user restarting an old `orchestra.py` — is better surfaced than smoothed over.

`caps` still exists, but only for *optional* features that gate **copy**, not function:
`dashSentinel`, `sendReceipt`, `digest`, `chatSince`, `push`.

## 5.7 Reconnection

```
attempt 0: immediate; then 1s, 2s, 4s, 8s, 15s, 30s, 60s (cap)
jitter ±25 % on every step; full jitter on the 30 s and 60 s steps
```

The counter resets to 0 **only after `hello` arrives** — not on TCP connect, or a server
that accepts and immediately rejects produces a hot loop.

| trigger | behaviour |
|---|---|
| `bye` from the server | reconnect **immediately, no backoff** — it is a clean signal |
| path change (wifi ↔ LTE) | **debounced 500 ms**, deduped on `(path.status, Set(availableInterfaces.map(\.type)))`, then reconnect |
| `scenePhase → .active` | **idempotent**: if a stream is open and delivered a frame within `hbPeriod`, do nothing |
| pull-to-refresh | reconnect immediately, reset the counter |
| `401` | **do not retry** — enter "re-pair this device". A token problem is not a network problem. |
| `403` | surface the distinct `error` string; never retry |
| `503` | backoff; should be unreachable for the user's own device thanks to `sub_id` eviction server-side |
| `404` on `/api/events` | drop to the polling ladder; do not re-probe SSE for 10 minutes |

**A single-flight flag guards reconnect** so at most one attempt is ever in flight.
Unguarded, "reconnect immediately" on both path change and `scenePhase` emits 3–5 attempts
per handoff — eroding the exact property (720× fewer connection setups) the design
optimises for, and self-inflicting 503s.

## 5.8 Foreground reconcile — concurrent, never serialised behind limits

```swift
public func reconcileOnForeground() async {
    let t0 = clock.now
    async let meta  = api.meta()
    async let frame = api.state(since: fleet.version, mode: .full)
    async let lim   = limits.prime()             // CONCURRENT, never a gate
    async let live  = intents.refreshActive()    // durable server-side records
    async let open  = push.reconcileDelivered()  // §6.4 withdrawal reconcile

    if let m = try? await meta {
        connection.noteMeta(m, sentAt: t0, receivedAt: clock.now)
    }
    if let f = try? await frame { fleet.apply(f) }     // the board paints HERE
    _ = try? await lim
    _ = try? await live
    _ = try? await open
    await engine.start()
}
```

A widely-repeated claim — that `index.html` primes limits *before* fetching state — is
**false**. Verified at index.html:1167-1168: `tick(); primeLimits();`. The board fetch fires
first, and `primeLimits`'s own comment says *"re-render from the state we already have —
refetching it here would run the expensive worktree/git/tmux scan a second time on every
boot."* Serialising limits before state would block the board for up to **30 s**
(`cached_limits` subprocess timeout, orchestra.py:1035) against a 2.5 s launch budget.

**The cold-start limits dependency is real but relocated.** Today `collect_state` reads
`limits_by_account()` from a lazily populated cache and *"never fetches on the state path"*
(orchestra.py:701-704) — so a phone polling only `/api/state` sees limit-stuck agents as
`waiting`, i.e. mis-triaged into the loudest state in the app. `FRESHNESS.md §6` fixes this
server-side (hand-off item 4). Until it lands, the ~1 s window before limits arrive renders
a `triage pending` chip on `waiting` sessions rather than paying 30 s to avoid it.

## 5.9 Backgrounding

`.background` (**not `.inactive`** — that is Control Center, the app switcher, a call
banner, a permission alert; treat it as "pause animations, keep the socket") cancels the
stream and all reads, debounced 2 s.

- Cancel by cancelling **both** the consuming `Task` **and** the retained
  `URLSessionDataTask`. `URLSession.AsyncBytes` does not necessarily unblock promptly when
  a task parked in a socket read is cancelled, and a logically-cancelled-but-lingering
  socket holds a server subscriber slot.
- **Do not** wrap the stream in `beginBackgroundTask`. That is a battery cost for a few
  seconds before the system kills it anyway.
- **Do** wrap the snapshot persist in `beginBackgroundTask` with an expiration handler,
  encode off the main actor, and write with `.atomic`. `.background` is not a guaranteed
  execution window and a truncated cache file silently breaks the "cold open shows truth"
  property.
- Every intent POST is wrapped too. The intent model makes the window ~100 ms rather than
  120 s, but the window is not zero:

```swift
let task = await UIApplication.shared.beginBackgroundTask(withName: "intent-\(id.rawValue)")
defer { Task { @MainActor in UIApplication.shared.endBackgroundTask(task) } }
```

On expiry the intent id is persisted to `pending-intents.json`; `reconcileOnForeground`
resolves it via `GET /api/intents`.

One `BGAppRefreshTask` (`sh.orchestra.refresh`, `earliestBeginDate` +15 min) refreshes the
**widget timeline** from a single `GET /api/state?mode=digest`. Build no correctness on it:
it is a floor, not a schedule; it is disabled in Low Power Mode; and it is not scheduled at
all while force-quit. Real background awareness is push (§6).

## 5.10 Cold open — an honest budget

"A truthful board in under 400 ms" is not achievable and should not be promised. You pay
TCP SYN/SYN-ACK **plus** request/response (2 RTT minimum), the packet tunnel is frequently
not resident after a gap so the first connection triggers on-demand bring-up, and on carrier
CGNAT the path is commonly **DERP-relayed** at 100–300 ms per RTT — before SwiftUI cold
launch.

| path | budget |
|---|---|
| cold launch → populated board **from cache** | **≤ 400 ms** (`MXAppLaunchMetric.histogrammedTimeToFirstDraw`) |
| cold launch → fresh, Wi-Fi, direct | ≤ 2.5 s |
| cold launch → fresh, LTE, direct | ≤ 4 s |
| cold launch → fresh, **DERP-relayed**, after a long gap | ≤ 7 s (p95) |

Three mitigations:

1. Render the disk snapshot instantly, explicitly marked "data as of 14m ago", controls
   disabled with a **determinate** "reconnecting…" affordance — never silently disabled.
2. **Warm the tunnel in parallel with launch:** fire a tiny `HEAD /api/health` the instant
   `scenePhase` becomes `.active`, concurrently with the stream connect, so tunnel bring-up
   overlaps startup instead of serialising after it. (`do_HEAD` currently 501s; it is added
   by the `AUTH.md` track's step 1.)
3. Measure it and show it in `DiagnosticsView`.

## 5.11 The five-rung reachability ladder

The single largest support burden of this transport is "is Tailscale actually up on the
phone?" — iOS permits one active VPN configuration at a time, Tailscale competes with
corporate VPNs, and its extension can be killed under memory pressure. Five distinct states,
five different user actions, and a naive client shows the same spinner for all of them.

```swift
// OrchestraCore/Rules/ErrnoCause.swift — pure, unit-tested with zero I/O
public func cause(forErrno e: Int32) -> TransportFailure {
    switch e {
    case 61:      return .serverStopped     // ECONNREFUSED — the host said "nothing listening"
    case 60, 65:  return .macUnreachable    // ETIMEDOUT / EHOSTUNREACH
    case 51:      return .tailnetDown       // ENETUNREACH — no route at all
    default:      return .unknown(urlErrorCode: Int(e))
    }
}
```

| rung | detection | headline | secondary |
|---|---|---|---|
| 1 | `NWPathMonitor` sees no route to 100.64/10; connect fails instantly | **tailnet unreachable** | check Tailscale is connected on this phone → deep link to the Tailscale app. **The app cannot start Tailscale programmatically — only detect and instruct.** |
| 2 | connect **times out** | **starbase isn't answering** | the Mac may be asleep, or off the tailnet |
| 3 | TCP connects, then refused/reset | **orchestra isn't running** | the Mac is reachable — run `./start.sh` on starbase |
| 4 | `PinnedTrustDelegate.onMismatch` fires | **this Mac is presenting a different certificate** | expected `jnK0…`, received `9xQa…` — re-pair, or check you're on the right machine |
| 5 | `/api/health` `contract` < 2, or 404 on a known route | **starbase is running orchestra 1.x** | pull and run `./start.sh` |

**Honest caveat.** `ECONNREFUSED` **does** propagate through Tailscale's netstack (it
forwards the peer's real RST), so rung 3 — the only cause the user can fix from the phone —
is reliable. Rungs 1 and 2 will likely both collapse to a timeout, because a packet to an
absent peer is black-holed rather than producing an ICMP-derived errno. So they are merged
into one honest state with honest copy rather than guessed apart, the probe deadline is
**5 s** (a cold NE tunnel needs time to wake after foregrounding), and splitting them is
gated on the measurement in §11.

## 5.12 DERP relay — named, detected, surfaced

- Detected cheaply: RTT to `/api/meta` at connect; `> 150 ms` ⇒ `.degraded(.relayed(rttMS:))`.
- Copy: *"relayed connection — Tailscale couldn't reach starbase directly, so this is slower."*
- Behaviour: **keep SSE** (one socket is exactly what a relay handles best). If the stream
  fails twice, the polling fallback floors at 20 s and switches to `mode=digest`.
- `DiagnosticsView` shows path type, RTT, frame rate, bytes/hour, clock skew, negotiated
  caps, decode faults and name collisions. That is what makes it worth having.

---

# 6. Push

The push layer is **pluggable by contract**: the server exposes one `PushSink` protocol with
`APNsSink`, `NtfySink` and `NoopSink` behind it, fed by the **same `transitions[]` array**
the collector already computes for SSE (`FRESHNESS.md §6`: one edge detector, three
consumers). This section covers the phone half and states plainly where the ntfy fallback
cannot reach.

## 6.1 Registration

```swift
// OrchestraStore/PushCoordinator.swift  (@MainActor; testable — no UIKit in here)
@Observable
public final class PushCoordinator {
    public enum Authorization: Sendable, Equatable {
        case notDetermined, provisional, authorized, denied
    }
    public private(set) var authorization: Authorization = .notDetermined
    public private(set) var registration: PushRegistration?
    public private(set) var lastDeliveryReport: DeliveryReport?

    /// Requested as .provisional on FIRST LAUNCH — notifications arrive quietly in
    /// Notification Center with no prompt and no possibility of a permanent first-run
    /// denial gutting the premise. A settings row offers promotion to full alerts.
    public func requestProvisional() async
    public func promoteToAlerts() async

    /// Re-sent on EVERY foreground: cheap, idempotent, and tokens change on reinstall,
    /// on restore-from-backup, and occasionally on OS update. Silent permanent failure
    /// otherwise, with at least two independent causes and no diagnostic.
    public func registerDeviceToken(_ token: Data) async
    public func unregister() async
    public func handleRemote(_ payload: [AnyHashable: Any]) async
}

public struct PushRegistration: Sendable, Equatable, Codable {
    public var deviceID: String
    public var token: String                      // 64-hex APNs device token
    public var environment: String                // "production" | "sandbox" — from the
                                                  // embedded provisioning profile, NOT #if DEBUG
    public var bundleID: String
    public var topics: [String]                   // attention, limit, intent, dispatch
    public var timeZone: String                   // IANA identifier ("America/Los_Angeles"),
                                                  // NOT an offset — quiet hours must survive DST
    public var settings: Settings

    public struct Settings: Sendable, Equatable, Codable {
        public var authorizationStatus: String
        public var timeSensitiveAllowed: Bool     // the entitlement resolved at RUNTIME
        public var criticalAllowed: Bool          // always false; we never request it
        public var alert: Bool, sound: Bool, badge: Bool
        public var lowPowerMode: Bool
    }
}
```

`App/Adapters/PushAdapter.swift` is a ~40-line shim over `UNUserNotificationCenter` and
`UIApplication.registerForRemoteNotifications`; all state and policy live in the store so
they are testable without a device.

Reporting `timeSensitiveAllowed` matters: without the Time Sensitive entitlement iOS
**silently clamps** `interruption-level: time-sensitive` to `active`, which any Focus mode —
including Sleep — suppresses. That is precisely the 2am blocked-agent case the product
exists for, failing with no server-side error. `GET /api/push/status` surfaces it so the
server can warn instead of pushing into a void.

## 6.2 Payload shape

```jsonc
{"aps": {"alert": {"title": "▲ needs you · confidai-api",
                   "body": "opus asked a question · 2 agents need you"},
         "interruption-level": "time-sensitive",
         "thread-id": "wt:confidai-api",
         "category": "ORCH_NEEDS_ANSWER",
         "badge": 3,
         "sound": "default",
         "mutable-content": 1,
         "content-available": 1},
 "ev": "needs_you", "v": 41207, "event_id": "evt-000431",
 "wid": "confidai-api", "sid": "0bc2125a-…",
 "counts": {"needs_input": 2, "blocked": 1, "limit": 1, "working": 3},
 "intent_hint": {"kind": "send", "target": "sid:0bc2125a-…"}}
```

Two things that are commonly got wrong and are load-bearing here:

- **`content-available: 1` rides on the *alert* push.** Alert and silent push are not
  disjoint. An `apns-push-type: alert`, `apns-priority: 10` payload carrying both
  `aps.alert` and `content-available: 1` displays the alert **and** invokes
  `application(_:didReceiveRemoteNotification:fetchCompletionHandler:)` when the app is
  backgrounded and not force-quit — and because the alert justifies delivery, it is not
  subject to the pure-background throttle. The app refreshes its cache while showing the
  alert, for free.
- **The counts travel in the payload, not behind the NSE.** That is what makes the
  on-demand-tunnel mode and a locked keychain degrade to *useful* rather than generic.

## 6.3 The Notification Service Extension

```swift
// NotificationService/NotificationService.swift
final class NotificationService: UNNotificationServiceExtension {
    private var handler: ((UNNotificationContent) -> Void)?
    private var best: UNMutableNotificationContent?

    override func didReceive(_ req: UNNotificationRequest,
                             withContentHandler done: @escaping (UNNotificationContent) -> Void) {
        handler = done
        best = req.content.mutableCopy() as? UNMutableNotificationContent
        guard let best else { return done(req.content) }

        Task {
            defer { done(best) }                       // EVERY path calls the handler
            guard let id = req.content.userInfo["event_id"] as? String,
                  let profile = try? await AppGroup.currentProfile(),
                  let token = try? KeychainStore.readToken(scope: .read, device: profile.deviceID)
            else { return }
            // 3 s, not 30. The SLOW case is far more common than the unreachable case, and
            // an NSE that hangs past budget silently delivers the ORIGINAL payload —
            // presenting as "sometimes the notification is generic", which nobody debugs.
            guard let detail = try? await NSEFetcher(profile: profile, token: token)
                                        .event(id: id, timeout: 3.0)
            else { return }
            best.body = detail.enrichedBody
            best.subtitle = detail.subtitle
            // The NSE APPENDS to an inbox and NEVER writes snapshot-<id>.json — §7.4.
            try? await AppGroup.appendInbox(NSEInboxItem(detail))
        }
    }

    override func serviceExtensionTimeWillExpire() {
        if let best { handler?(best) }                  // implemented UNCONDITIONALLY
    }
}
```

Four constraints, each of which is a real, silent failure if missed:

| constraint | consequence if ignored |
|---|---|
| **Keychain accessibility.** Items default to `kSecAttrAccessibleWhenUnlocked`, so an NSE on a locked device — the whole scenario — cannot read the token. Stored `AfterFirstUnlockThisDeviceOnly` in the shared access group. | falls through to the generic body **every single time** |
| **Timeout 3 s, not the 30 s budget.** | "sometimes the notification is generic", undiagnosable |
| **~24 MB memory cap**; jetsam delivers the original payload. The NSE fetches `GET /api/events/<id>` (~200 B), never `/api/state`. | random genericness under memory pressure |
| **Tunnel residency.** In on-demand-VPN mode the packet tunnel is not resident and bring-up can consume most of the budget. The NSE does not attempt to force it; the payload already carries the counts. | most notifications generic on cellular |

`NSETests` exercises `didReceive` against fixture payloads and asserts `contentHandler` is
invoked on **every** path including decode failure and the expiry path.

## 6.4 Notification actions — including inline reply

This is the highest-value mobile feature in the plan. `UNTextInputNotificationAction` runs
in the app launched into the background with roughly 30 s — answering an `AskUserQuestion`
from the lock screen collapses the entire round trip the product exists to shorten.

```swift
// App/Adapters/PushAdapter.swift
enum Categories {
    static let all: Set<UNNotificationCategory> = [
        UNNotificationCategory(
            identifier: "ORCH_NEEDS_ANSWER",
            actions: [
                UNTextInputNotificationAction(
                    identifier: "REPLY", title: "Reply",
                    options: [.authenticationRequired],      // ← pocket-tap protection
                    textInputButtonTitle: "Send",
                    textInputPlaceholder: "type an instruction for this agent…"),
                UNNotificationAction(identifier: "SNOOZE_15", title: "Snooze 15m",
                                     options: []),
                UNNotificationAction(identifier: "OPEN", title: "Open",
                                     options: [.foreground]),
            ],
            intentIdentifiers: [],
            options: [.customDismissAction]),

        UNNotificationCategory(identifier: "ORCH_BLOCKED", actions: [
            replyAction,
            UNNotificationAction(identifier: "CONTINUE", title: "Continue",
                                 options: [.authenticationRequired]),
            snooze15, openAction], intentIdentifiers: [], options: []),

        UNNotificationCategory(identifier: "ORCH_YOUR_TURN", actions: [
            replyAction, snooze60,
            // finish deliberately OPENS THE APP — it can type a ~600-char closeout brief
            // into a live agent, and it is a two-step arm→confirm. That deserves a screen.
            UNNotificationAction(identifier: "FINISH", title: "Finish…",
                                 options: [.foreground]),
            openAction], intentIdentifiers: [], options: []),

        UNNotificationCategory(identifier: "ORCH_LIMIT", actions: [
            // resume/schedule is naturally idempotent: re-POSTing overwrites the same key.
            UNNotificationAction(identifier: "ARM_RESUME", title: "Arm auto-resume",
                                 options: [.authenticationRequired]),
            snooze60, openAction], intentIdentifiers: [], options: []),

        UNNotificationCategory(identifier: "ORCH_DIED",  actions: [openAction, snooze60],
                               intentIdentifiers: [], options: []),
        UNNotificationCategory(identifier: "ORCH_INFO",  actions: [openAction],
                               intentIdentifiers: [], options: [],
                               categorySummaryFormat: "%u more from this worktree"),
    ]
}
```

**Only idempotent operations get banner buttons.** `dispatch` and `finish → dispatch` have
zero double-fire protection server-side today, and a retry ≥1 s later launches a **second**
agent that merges and pushes the same branch. Neither is ever one tap from a lock screen.
Every actuating action carries `.authenticationRequired`.

### The reply path must be durable, and durability comes first

```swift
// App/Adapters/PushAdapter.swift
func userNotificationCenter(_ c: UNUserNotificationCenter,
                            didReceive response: UNNotificationResponse) async {
    guard let text = (response as? UNTextInputNotificationResponse)?.userText,
          let sid = SessionID(rawValue: response.notification.request.content
                                        .userInfo["sid"] as? String ?? "")
    else { return }

    // 1. PERSIST FIRST. The outbox is written BEFORE any network call, into the App
    //    Group, so a killed process cannot destroy the user's typed reply.
    let op = PendingIntentRecord(id: IntentID(), kind: .send, target: .session(sid),
                                 text: text, createdAt: Date())
    try? await AppGroup.appendOutbox(op)

    // 2. Post. Same op id on every retry — replay is a no-op server-side.
    switch await gateway.send(text: text, to: sid, opID: op.id) {
    case .success:
        try? await AppGroup.resolveOutbox(op.id)
    case .failure:
        // 3. A local notification carrying the draft. "Re-present it in the app" is not a
        //    mechanism when no app is running.
        await LocalNotifier.post(title: "✗ reply not delivered · \(sid.short)",
                                 body: text, category: "ORCH_RETRY", userInfo: op.asUserInfo)
    }
}
```

The outbox is drained and reconciled on every foreground against
`GET /api/intents?active=1`, and the client-generated `id` makes retry free.

### Withdrawal — nothing may outlive its truth

A question asked at 14:00 that the user answers at their Mac at 14:01 must not leave
"▲ needs you" on the lock screen indefinitely. `apns-collapse-id` supersedes only
*undelivered* pushes. On a 9-worktree fleet, Notification Center becomes a graveyard within
a day, and a surface where nothing you see is necessarily still true is worse than no
surface. Three mechanisms, because none alone is reliable:

1. **Withdrawal push** — a background push
   (`apns-push-type: background`, priority 5, `collapse-id: wd:<install>`) carrying
   `{"withdraw": ["session.needs_answer|<sid>|3"], "badge_hint": 2}`; the app calls
   `removeDeliveredNotifications(withIdentifiers:)`.
2. **`request.identifier = dedupe_key`** on every notification, so both the withdrawal and
   the reconcile path can target it precisely.
3. **Foreground reconcile** — `GET /api/events/open` returns the currently-FIRED dedupe
   keys; the app withdraws every delivered notification not in the set. Background pushes
   are throttled and not guaranteed, so **this is the reliable path**; the push is the fast
   one.

**Badge.** It counts entities currently in the FIRED phase for *interrupting* rules only
(`needsInput + blocked + died`) — not raw `counts`, which includes states the dwell logic
deliberately does not alert on. Set on every alert push and every withdrawal push, and
recomputed from `/api/events/open` on every foreground so opening the app is self-healing.
Beyond that it lags while backgrounded, and the design says so rather than spending scarce
background-push budget chasing it.

## 6.5 Deep linking — a pure function in Core, not in the App target

```swift
// OrchestraCore/Rules/DeepLink.swift
public enum Route: Sendable, Equatable {
    case board
    case worktree(WorktreeID)
    case session(SessionID)
    case limits
    case map
    case mission(prefill: String?)
    case pair(PairPayload)
}

/// orchestra://worktree?name=<pct-encoded>   ·  orchestra://session?sid=<uuid>
/// orc://p?h=<host>&c=<code>&f=<pin>        ·  orchestra://mission?text=<pct-encoded>
///
/// A QUERY PARAMETER, not a path segment: worktree names and paths contain "/".
public func parse(_ url: URL) -> Route?
```

Routing lives in `OrchestraCore` and not in the App target, because the App target has no
unit test home and this is pure string handling with a security-relevant failure mode — a
malformed `orchestra://` URL landing on the wrong worktree, in an app whose actions type
into terminals running `--dangerously-skip-permissions`. Parameterized tests cover
percent-encoded names, absent worktrees, unknown hosts, extra parameters, and the
ambiguous-basename case from §3.1.

`App/Adapters/DeepLinkAdapter.swift` is the ~30-line `onOpenURL` shim.

## 6.6 Live Activities — one, deliberately

The temptation is two: a dispatch progress activity and a persistent attention indicator.
Both are wrong.

- **Dispatch progress: dropped.** A dispatch is 10–20 s wall clock, often with start-plus-
  first-update inside 1–3 s. That is a whole Widget Extension surface set (Lock Screen +
  Dynamic Island compact/minimal/expanded) plus a push-to-start token lifecycle, for a flash
  that is essentially invisible on any iPhone without a Dynamic Island — and driving
  ①→✓ would need six pushes in twenty seconds against a scarce, user-disableable budget.
  The `intent` SSE channel already renders it in-app, more responsively, at zero push cost.
  If the user backgrounds mid-dispatch: end the activity and send **one** terminal alert.
- **Persistent attention indicator: not a Live Activity.** Backed by `aps.badge` plus a
  Notification Center entry. ActivityKit terminates an activity after roughly 8 hours
  active / 12 on the Lock Screen; an ambient count that silently vanishes overnight is
  worse than none, because the user learns to trust it.

**The one kept: a limit-reset countdown.**

```swift
// Widgets/LimitResetActivity.swift
public struct LimitResetAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable, Sendable {
        public var resetsAt: Date
        public var account: String
        public var worst: String?          // "Fable" | "Weekly" | "Session"
        public var armedResume: Date?      // ⏱ auto-resume armed for …
        public var phase: Phase
        public enum Phase: String, Codable, Sendable { case waiting, firing, done, failed }
    }
    public var worktree: String
}
```

- Rendered with `Text(timerInterval:)` so it **self-updates on-device and needs zero pushes
  for its whole life** — the exact native expression of §5.5's "ages tick, statuses dim".
- Started **only when the reset is under 6 hours out.** A *weekly* cap reset can be seven
  days away; the activity would die long before it, on a fresh Lock Screen, with no signal.
  Beyond 6 h, schedule a notification near the reset instead.
- Explicit `staleDate`; ended at `min(reset, start + 7h)` with a body that says why.
- `NSSupportsLiveActivitiesFrequentUpdates` is declared, **and** the app checks
  `ActivityAuthorizationInfo().frequentPushesEnabled` (user-disableable per app) and
  `ProcessInfo.processInfo.isLowPowerModeEnabled` before promising any cadence.
- **Push-to-start** tokens are re-POSTed to `/api/push/lastart` on **every** element of
  `Activity<LimitResetAttributes>.pushToStartTokenUpdates` — they arrive asynchronously,
  are scoped per `ActivityAttributes` type, and rotate. Registering once at pairing cannot
  express that lifecycle, and a stale one silently no-ops.
- Intermediate updates use `apns-priority: 5`; only the terminal one uses 10.

**Related backend fix this depends on (hand-off item 16):** `fire_resume` can block for ~14
minutes (90 s cclimits + 420 s `_wait_composer_idle` + three retries) while the schedule
still reads `pending` with a past `due_at`, so the client cannot distinguish "armed" from
"firing right now for the last nine minutes". A `firing` status plus `started_at` lets the
activity show `▶ resuming…` instead of a countdown stuck at zero — and the desktop board
gets the same fix for free.

## 6.7 The ntfy cliff, stated so nobody reads "pluggable" as "optional"

| feature | APNs | ntfy on iOS |
|---|---|---|
| inline text reply | ✅ | ❌ — no text-input action |
| thread grouping / summaries | ✅ | ❌ |
| **withdraw a delivered notification (§6.4)** | ✅ | ❌ — the whole mechanism is unavailable |
| Live Activities | ✅ | ❌ |
| badge | ✅ | ❌ |
| deep link into the SwiftUI app | ✅ | ❌ — opens the ntfy app or a web view |
| quiet hours, budgets, digests | server-side | server-side |

Also: the ntfy iOS app receives *instant* push only for `ntfy.sh` topics, so even a
self-hosted server proxies a contentless wake-up through `ntfy.sh` — the topic identifier
transits a third party regardless. Therefore **`include_transcript_text` is forced off for
the ntfy sink unconditionally, in code**, and bodies degrade to structure only (worktree,
status transition, count). ntfy is positioned as a **pre-Apple-account bring-up channel**,
not a permanent peer.

---

# 7. Keychain, pairing, and the biometric gate

## 7.1 Pairing — an enrollment code, never a long-lived secret in a QR

```
orc://p?h=achills-macbook-pro.tail1205d9.ts.net&c=7K3M9QP2&f=jnK0svnXpNeIqfgF5CQuaQ
```

| field | meaning |
|---|---|
| `h` | MagicDNS name, or the raw 100.x IPv4 when the name would overflow the QR |
| `p` | port — **omitted when 4242**; the client defaults it |
| `c` | pairing code: `secrets.token_bytes(5)` → 40 bits → 8 **Crockford base32** chars (no I, L, O, U), displayed `XXXX-XXXX`, TTL 120 s, **single use**, 5 attempts per source IP |
| `f` | `base64url(sha256(SPKI-DER))[:16]` — the certificate pin, delivered out-of-band by camera |

The QR carries **no long-lived secret**, so photographing it after 120 s is worthless, and
during the window it buys one race that the legitimate device visibly wins or loses.

```swift
// OrchestraCore/Persisted/PairPayload.swift
public struct PairPayload: Sendable, Equatable, Codable {
    public let host: String
    public let port: Int          // defaults to 4242 when `p` is absent
    public let code: String       // normalised: strip [-\s], uppercase, I/L→1, O→0, U→V
    public let pin: String        // 22-char base64url

    public init?(url: URL)        // accepts orc://p and orchestra://pair
}
```

Crockford normalisation happens on **both** sides before comparison — `.replace("-","").upper()`
alone throws away the entire reason for choosing the alphabet.

### Flow

```
Mac (board /pair, or `python3 orchestra.py --pair`)      iPhone
──────────────────────────────────────────────────      ──────
1. "＋ pair a device" → code + QR + manual fields
                                                        2. Pair → camera (AVFoundation)
                                                        3. TLS to https://h:p, pin from the QR
                                                        4. POST /api/pair {code,label,platform}
5. validates: open? unexpired? peer in 100.64/10?
   per-IP attempts < 5? compare_digest on the
   NORMALISED code?
6. mints read + act tokens, writes the registry,
   audits, pins tailnet_allow_logins if unset
                                                        7. stores BOTH in the Keychain (§7.2)
8. pairing closes; the board lists the device           9. shows the pin for visual comparison
```

### The manual fallback compares the pin; it does not type it

Asking a user to type a 22-char case-sensitive base64url string with `-` and `_` on an iOS
keyboard inside 120 seconds — in exactly the situations where the camera already failed —
will fail, burn the window, and force a re-issue. It is also TOFU with extra steps.

Inverted: **manual entry takes host + port + the 8-char Crockford code only** (all typeable,
all case-insensitive). The app connects, computes the SPKI pin from the presented
certificate, and **displays it beside** the pin shown on the Mac for visual comparison;
`/api/pair`'s response also returns `server.spki` so the app can confirm agreement.
Comparing 22 characters is trivial; typing them is not. The guide says plainly that the
manual path is compare-on-first-use and the QR path is the out-of-band one.

`ManualServerEntryView` is a **first-class peer**, not a hidden fallback — the user is often
holding the phone away from the Mac showing the QR.

### Scanner

`AVCaptureMetadataOutput`, not VisionKit. `DataScannerViewController` requires *both*
`isSupported` and `isAvailable` and degrades silently on some hardware; `AVFoundation` is
unconditional and about 90 lines.

## 7.2 Keychain — two items, two accessibility classes

```swift
// OrchestraPersistence/KeychainStore.swift  (nonisolated; actor-safe)
public enum TokenScope: String, Sendable { case read, act }

public enum KeychainStore {
    public static let service = "sh.orchestra.token"
    /// Decided NOW: a keychain item cannot be moved into an access group later without
    /// rewriting it, and rewriting means re-pairing.
    public static let accessGroup = "$(AppIdentifierPrefix)sh.orchestra.shared"

    public static func storeRead(_ token: String, device: String) throws {
        let q: [String: Any] = [
            kSecClass as String:            kSecClassGenericPassword,
            kSecAttrService as String:      service,
            kSecAttrAccount as String:      "\(device).read",
            kSecAttrAccessGroup as String:  accessGroup,       // the NSE needs this
            kSecAttrSynchronizable as String: false,           // never iCloud Keychain:
                                                               // a tailnet credential must
                                                               // not fan out to other devices
            kSecAttrAccessible as String:   kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String:        Data(token.utf8)]
        SecItemDelete(q as CFDictionary)
        guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else { throw VaultError.store }
    }

    public static func storeAct(_ token: String, device: String) throws {
        var err: Unmanaged<CFError>?
        // .biometryCurrentSet invalidates on Face ID re-enrolment (the coercion case).
        // `.or .devicePasscode` is required, not optional: an attacker who can add a face
        // already has the passcode, so it costs little — and WITHOUT it, SecItemAdd fails
        // outright on a device with no biometry enrolled, blocking pairing entirely.
        guard let acl = SecAccessControlCreateWithFlags(
                nil, kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
                [.biometryCurrentSet, .or, .devicePasscode], &err)
        else { throw VaultError.acl }
        let q: [String: Any] = [
            kSecClass as String:              kSecClassGenericPassword,
            kSecAttrService as String:        service,
            kSecAttrAccount as String:        "\(device).act",
            kSecAttrSynchronizable as String: false,
            kSecAttrAccessControl as String:  acl,
            kSecValueData as String:          Data(token.utf8)]
        // NOTE: deliberately NOT in the access group — the notification extension has no
        // business acting.
        SecItemDelete(q as CFDictionary)
        guard SecItemAdd(q as CFDictionary, nil) == errSecSuccess else { throw VaultError.store }
    }
}
```

**Why two scopes.** The `read` token must work with **no user present** — background
refresh, decorating a push notification in the NSE — so it cannot sit behind biometry. If
that same token could act, the biometric gate would be decorative. `AfterFirstUnlock` (not
`WhenUnlocked`) because the app must reconnect after a device reboot without the user
unlocking *to the app* first.

**Stated honestly:** a stolen **unlocked** phone therefore still exfiltrates transcripts,
because `read` grants `/api/chat` and `/api/dispatchlog`. Mitigations are server-side —
`lockdown` degrades read as well as act, dormant devices auto-revoke at 30 days, and revoke
is one tap from the Mac or from another paired device. Face ID protects a stolen phone, not
a compromised one, and the guide must not oversell it.

## 7.3 Certificate pinning

```swift
// OrchestraAPI/Transport/PinnedTrustDelegate.swift
final class PinnedTrustDelegate: NSObject, URLSessionDelegate, @unchecked Sendable {
    let pin: String                                    // base64url(sha256(SPKI))[:16] from the QR
    var onMismatch: (@Sendable (String, String) -> Void)?

    /// SecKeyCopyExternalRepresentation returns the RAW X9.63 point (0x04||X||Y), NOT the
    /// SPKI — so we prepend the canonical named-curve prefix to match what the server
    /// hashed. This is why the server MUST generate its key with `-param_enc named_curve`:
    /// an explicit-parameters certificate has a DIFFERENT SPKI (335 bytes, no such prefix)
    /// and would fail this check 100 % of the time, with an undiagnosable TLS error.
    private static let p256SPKIPrefix = Data([
        0x30,0x59,0x30,0x13,0x06,0x07,0x2a,0x86,0x48,0xce,0x3d,0x02,0x01,
        0x06,0x08,0x2a,0x86,0x48,0xce,0x3d,0x03,0x01,0x07,0x03,0x42,0x00])

    func urlSession(_ s: URLSession, didReceive ch: URLAuthenticationChallenge,
                    completionHandler done: @escaping (URLSession.AuthChallengeDisposition,
                                                       URLCredential?) -> Void) {
        guard ch.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = ch.protectionSpace.serverTrust,
              let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf  = chain.first,
              let key   = SecCertificateCopyKey(leaf),
              let raw   = SecKeyCopyExternalRepresentation(key, nil) as Data?
        else { return done(.cancelAuthenticationChallenge, nil) }

        let digest = Data(SHA256.hash(data: Self.p256SPKIPrefix + raw).prefix(16))
        let got = digest.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        guard got.utf8.count == pin.utf8.count,
              zip(got.utf8, pin.utf8).reduce(0, { $0 | ($1.0 ^ $1.1) }) == 0
        else { onMismatch?(pin, got); return done(.cancelAuthenticationChallenge, nil) }
        done(.useCredential, URLCredential(trust: trust))
    }
}
```

`SecTrustEvaluateWithError` is **never** called: a self-signed certificate always fails it,
and the pin is a strictly stronger statement than "some CA vouched for this". Two permanent
consequences, stated so nobody over-invests:

- The certificate's SANs are irrelevant to the iOS client; they matter only for `curl` and
  browsers.
- The 825-day validity exceeds Apple's 398-day cap, which is harmless **only** because
  evaluation is bypassed. Validity is therefore a self-imposed policy, and expiry is read
  from `/api/health.cert_not_after` (a unix timestamp) — **not** from the certificate, since
  `SecCertificateCopyValues` is macOS-only and does not exist on iOS.

Pin mismatch is the *most likely* real break (the Mac's state directory moved, a new Mac,
someone ran `--regen-key`), so it gets its own error path with both values shown, letting a
genuine attack be distinguished from a regenerated key (§5.11 rung 4).

## 7.4 Persistence layout

**No SwiftData, no Core Data.** The payload is a ≤36 KB blob wholly replaced by its source
of truth, with zero relational queries, whose shape is dictated by a Python script with no
schema versioning. SwiftData buys migrations, a `ModelActor` story and `@Query` — all cost,
no benefit, plus a migration liability against an upstream that has none.

| what | where | why |
|---|---|---|
| `read` / `act` tokens | Keychain (§7.2) | never in a file, never in a backup |
| server profiles | App Group `profiles.json`, atomic | small, inspectable, backup-safe; **contains no token** |
| last snapshot / limits / topology | App Group `snapshot-<id>.json`, **written only by the app** | read by the widget |
| **NSE deliveries** | App Group `inbox-<id>.jsonl`, **append-only, written only by the NSE, consumed and truncated only by the app** | see the race below |
| pending intent ids | App Group `pending-intents.json` | survives a kill; resolved on foreground |
| reply outbox | App Group `outbox.jsonl` | §6.4 durability |
| chat transcripts | App Group `chat/<id>/<sid>.json`, LRU 20 files / 2 MB | detail opens instantly; last conversation readable offline |
| toggles, selected profile | `UserDefaults(suiteName:)` | scalars only |
| intent progress | memory + server | the server is the durable record; mirroring invites showing a phase that can never resolve |

```swift
// OrchestraPersistence/FileStore.swift
public actor FileStore {
    public func load<T: Decodable & Sendable>(_: T.Type, from key: Key) async throws -> T?
    public func save(_ value: some Encodable & Sendable, to key: Key) async throws
    public func appendLine(_ value: some Encodable & Sendable, to key: Key) async throws
    public func drainLines<T: Decodable & Sendable>(_: T.Type, from key: Key) async throws -> [T]
    public func evictLRU(prefix: String, keeping: Int, maxBytes: Int) async
}
```

Writes use `[.atomic, .completeFileProtectionUntilFirstUserAuthentication]`; the cache
directory sets `isExcludedFromBackup = true`.

**The cross-process race, fixed structurally.** An `actor` serialises within a process only.
The NSE reading `snapshot-<id>.json`, applying a delta and writing it back while the
foregrounded app writes a full frame is a lost update, and `.atomic` guarantees only that
the file is never *torn*. **Fix: no two processes ever write the same file.** The NSE
appends to `inbox-<id>.jsonl` and never touches the snapshot; the app is the only writer of
the snapshot and the only consumer of the inbox; the widget is read-only on both.

**Privacy is load-bearing, not an afterthought.** `/api/state` returns transcript prose,
`/api/limits` returns real account **email addresses**, `/api/dispatchlog` returns full
mission text (on this machine: production session UUIDs, prod-DB references, pasted emails).
The cached copies inherit that sensitivity exactly. `PrivacyInfo.xcprivacy` declares
`NSPrivacyAccessedAPICategoryFileTimestamp`, `DiskSpace` and `UserDefaults` with reason
codes, and no tracking domains.

## 7.5 The biometric gate — one `LAContext`, one prompt per intent

```swift
// OrchestraStore/ActGate.swift
@MainActor
public final class ActGate {
    private var ctx = LAContext()
    private var cached: (token: String, until: Date)?

    public init(clock: any AppClock) {
        // Reuse REQUIRES the same LAContext instance. Constructing one per call — the
        // obvious implementation — makes this structurally impossible.
        ctx.touchIDAuthenticationAllowableReuseDuration =
            LATouchIDAuthenticationMaximumAllowableReuseDuration          // 300 s
    }

    public func invalidate() { ctx.invalidate(); ctx = LAContext(); cached = nil }

    /// `fresh` forces a prompt regardless of the reuse window.
    public func token(reason: String, fresh: Bool) async throws -> String {
        if !fresh, let c = cached, c.until > clock.now { return c.token }
        ctx.localizedReason = reason
        // SecItemCopyMatching on a biometric item BLOCKS — never on the MainActor.
        let t = try await Task.detached { [ctx] in
            try KeychainStore.readAct(ctx: ctx, device: device)
        }.value
        cached = (t, clock.now.addingTimeInterval(fresh ? 0 : 300))
        return t
    }
}
```

```swift
public func act(_ intent: ActIntent) async throws {
    // The op id is minted ONCE at user-commit and reused across every retry, so a 429
    // backoff or a timeout retry never re-enters the gate and never re-prompts for a
    // request the user already approved.
    let token = try await gate.token(reason: intent.reason, fresh: intent.destructive)
    try await api.postIntent(intent.request, token: token)
}
```

| action | `destructive` | rationale |
|---|---|---|
| `finish`, `dispatch`, `kill` | **true** — always prompt | spends account quota, or types a 600-char brief at a live agent |
| `send`, `resume`, `reserve` | false — rides the 300 s reuse window | the highest-frequency mobile action is reading "1. yes 2. no" and typing "1"; a fresh Face ID prompt per send, three agents in thirty seconds, is what makes people stop using the app |

`invalidate()` is called on `scenePhase == .background` and on device lock.

Biometry is the **last** gate, not the only one. It layers on top of the confirmation UX the
desktop already has: the two-step arm→confirm, the mission composer refusing without an
explicit model and effort, and the `⚑ use X anyway` headroom dialog.

### The re-issue path, because `.biometryCurrentSet` will invalidate

Adding an Alternate Appearance — the standard fix for glasses — invalidates the `act` item.
With no remote recovery, a user who does that at an airport has a read-only app until they
physically return to their Mac and scan a QR inside 120 seconds. That is not an occasional
legitimate re-pair; it is an unrecoverable-in-the-field capability loss from a routine iOS
settings change.

```
POST /api/devices/self/reissue-act    (authenticated by the SURVIVING read token)
  → {"ok": true, "pending": true,
     "message": "approve this on your Mac's board to restore acting"}
```

Returns **no token**; it queues an approval that the desktop board confirms, which then
mints a fresh `act` token shown once. The approval is the security property — a read token
alone can never escalate. Rate-limited 3/day/device, loudly audited, and the pending state
rides in `/api/state.security` so the phone says "waiting for approval on your Mac" instead
of showing a dead button.

## 7.6 Actuation preconditions the client enforces

| action | precondition sent | why |
|---|---|---|
| `dispatch` (explicit worktree) | `cardRev` | it must still be free |
| `dispatch` (auto) | none | the user never chose a worktree; the **response echoes the resolved target** — "launched on ConfidAi7 · [account2]" — which is what they actually want |
| `finish` | `cardRev` | the two-step arm depends on `closeout_sent` and which processes are live |
| `send` | `expect_sid` | pid is the only addressing today and `send_to_process` verifies only that *some* claude process holds it |
| `resume/schedule` | `expect_sid` | `schedule_resume` does no existence check at all |

A `409 Conflict` returns a human-readable diff — *"ConfidAI changed while you were looking
at it: session 0bc2125a working → needs_input; live proc 41234 disappeared"* — presented in
a sheet **naming the target card**, with one "do it anyway" that resubmits with the fresh
rev. **Never auto-retry a 409.** A 409 means a human's mental model diverged from reality;
only a human closes that gap.

---

# 8. Widgets

Target `OrchestraWidgets` links `OrchestraCore` and `OrchestraPersistence` **only** — a
build-time guarantee that the timeline provider cannot touch an `@Observable` store or open
a socket.

```swift
// Widgets/AttentionProvider.swift
struct AttentionEntry: TimelineEntry, Sendable {
    let date: Date
    let counts: Counts
    let topCards: [WidgetCard]      // ≤3, pre-flattened for rendering
    let staleness: BoardStateKind   // live | lagging | stale | cold
    let generatedAt: Date
}

struct AttentionProvider: TimelineProvider {
    func placeholder(in _: Context) -> AttentionEntry { .placeholder }

    func getSnapshot(in ctx: Context, completion: @escaping (AttentionEntry) -> Void) {
        Task { completion(await Self.entryFromCache(now: Date())) }
    }

    func getTimeline(in ctx: Context, completion: @escaping (Timeline<AttentionEntry>) -> Void) {
        Task {
            // READS ONLY THE APP-GROUP CACHE. Never the network: a widget process has a
            // hard memory ceiling, no biometric context, and no business waking a VPN.
            let cached = await Self.entryFromCache(now: Date())
            // Entries are generated for the SCHEDULED STALENESS TRANSITIONS, so the widget
            // degrades honestly without a refresh instead of showing a confident lie.
            let entries = [
                cached,
                cached.aging(to: .lagging, at: cached.generatedAt.addingTimeInterval(40)),
                cached.aging(to: .stale,   at: cached.generatedAt.addingTimeInterval(300)),
            ].filter { $0.date > Date().addingTimeInterval(-1) }
            completion(Timeline(entries: entries, policy: .after(Date().addingTimeInterval(900))))
        }
    }
}
```

| family | content |
|---|---|
| `systemSmall` | the attention count (`needsInput + blocked`) as one big number, the tile label `▲ need you`, and a staleness dot |
| `systemMedium` | the same plus up to three card names with their status glyph and worktree, tap-targeted to `orchestra://worktree?name=…` |
| `accessoryRectangular` (Lock Screen) | `▲ 2 need you · ● 3 working`, single line |
| `accessoryCircular` | the count in a gauge, tinted by whether anything is attention |

Refresh is driven by `WidgetCenter.shared.reloadTimelines(ofKind:)` from three places: after
every successful foreground frame, after the `BGAppRefreshTask`, and from the NSE after it
appends to the inbox. Nothing else.

Design rules carried from the board and not re-litigated per surface:

- **Status is never colour-only.** Every status carries its glyph and word
  (`● WORKING`, `▲ NEEDS ANSWER`, `⛔ LIMIT HIT`, `■ BLOCKED`, `◆ YOUR TURN`, `○ ENDED`),
  matching `STATUS` at index.html:380. The widget is small and often glanced at in sunlight;
  `needs #d97757`, `turn #e8a87c` and `limit #d4b06a` are three warm tones in a narrow band.
- **Never render a limit-with-`handed_to` as attention.** It is excluded from `counts`
  server-side; the widget derives from `Counts`, so this is free — and it is why the widget
  must not recompute counts from `cards`.
- **A stale widget says so.** The staleness dot and, past five minutes, a `as of 14m` label.
  An ambient count that silently freezes is the failure mode that gets a widget removed.

---

# 9. Testing

`import Testing` (bundled with Xcode 26 — not a dependency). Four test plans, three of which
run without a simulator.

## 9.1 Unit — `swift test`, headless

```swift
// OrchestraTestSupport/MockTransport.swift
public actor MockTransport: Transport {
    public private(set) var recorded: [URLRequest] = []
    public func stub(path: String, fixture: String, status: Int = 200, latency: Duration = .zero)
    public func fail(path: String, with code: URLError.Code)
    public func script(_ frames: [SSEFrame])       // drives the SSE path deterministically
}
public struct StubProbe: ReachabilityProbing {
    let result: TransportFailure
    public func probe(host: String, port: Int, deadline: Duration) async -> TransportFailure { result }
}
```

### Fixtures are rebased. There is no raw-load API.

```swift
public enum FixtureLoader {
    public static let fixtureEpoch = Date(timeIntervalSince1970: 1_767_225_600)  // 2026-01-01Z

    /// Every epoch-typed field is shifted by (reference - fixture.at). MANDATORY:
    ///  (1) ServerClock fed a stale fixture computes a days-long offset;
    ///  (2) CountdownText inside a TimelineView renders against the wall clock, so an
    ///      unrebased snapshot PNG changes on every run — flaky by construction;
    ///  (3) RelativeTime's <60s and <3600s branches are otherwise unreachable through a
    ///      fixture and only ever tested in isolation.
    public static func load<T: Decodable>(_: T.Type, _ name: String,
                                          reference: Date = fixtureEpoch) throws -> T
    public static func contract() throws -> StateContract
    public static func all() throws -> [String]
}

@Test func everyFixtureIsLoadable() throws {
    let names = try FixtureLoader.all()
    #expect(names.count >= 12)   // a fixture that fails to bundle fails HERE, loudly, not
}                                // as a confusing "file not found" in an unrelated test
```

### Decoding

```swift
@Test(arguments: ["real/state-9wt-32sess", "real/state-limit-null-resets",
                  "real/state-handed-off", "real/state-v2-delta",
                  "real/state-unknown-status", "demo/state"])
func decodesAndMaps(_ name: String) throws {
    let dto = try FixtureLoader.load(StateFrameDTO.self, name)
    _ = StateFrame(dto)                                  // mapping must not trap
}

@Test func nullLimitRenders() throws {
    // orchestra.py:728-732 — the transcript-regex fallback sets all four to null.
    let s = try FixtureLoader.session("real/state-limit-null-resets", at: 0)
    #expect(s.status == .limit && s.limit?.resetsAt == nil)
    #expect(s.limitDisplay == "LIMIT HIT · usage")       // never "resets in nil"
}

@Test func handedOffIsNotAttention() throws { … }        // orchestra.py:798-801
@Test func conditionalBoolsDefaultFalse() throws { … }   // tool_running/bg_shell absent≠false
@Test func absentOkMeansSuccess() throws { … }
@Test func unknownStatusDoesNotFailWholePayload() throws { … }
@Test func htmlErrorBodyIsNotDecodedAsJSON() async throws { … }
@Test func orderIsAlwaysServerSupplied() throws { … }    // both snapshot and delta branches
```

### Malformed coverage is generated, not hand-authored

Two static "bad JSON" files were never adequate against a payload whose shape can change on
any upstream commit.

```swift
@Test(arguments: try Mutations.of("real/state-9wt-32sess"))    // ~300 cases, ~30 lines
func mutationsDegradeCleanly(_ m: Mutation) throws {
    // Drop one key at a time; swap one type at a time. The decoder must either succeed or
    // throw a typed OrchestraError.decoding — never trap, never crash.
    #expect(throws: Never.self) { _ = try? decode(m.data) }
}
```

### Formatters, verified against the JS source values verbatim

`rel()` → `nil→"—"`, `<60→"45s"`, `<3600→"12m"`, `<86400→"3h7m"` (no zero-padding), else
`"2d"`. `etime` → `"15:02"`, `"12:43:46"`, `"2-03:14:22"`. `acctLabel` →
`.claude→main`, `.claude-work→work`. `clock()` → today vs not-today.

### `ActionGatewayTests` — the component that is the only path to a POST

Each precondition rejects with the right error, and critically: **a rejection at step N
means no request reached the transport.**

```swift
@Test func dashPrefixedTextNeverReachesTheWire() async {
    let r = await gateway.send(text: "-- revert that", to: sid)
    #expect(r.isFailure)
    #expect(await transport.recorded.isEmpty)   // the invariant that protects the user
}
@Test func sameOpIDIsReusedAcrossRetries() async { … }
@Test func percentIsEncodedAsIntNotDouble() async { … }   // int("50.5") raises server-side
```

### Other required suites

`ReorderHoldTests` (the full 5×N phase matrix on a `TestClock`) · `ServerClockTests`
(zero-skew → offset ≈ 0; a 6 s-stale `generated_at` must not move the offset; one outlier
moves it by ≤20 % of the delta; `rtt > 1 s` is discarded) · `FreshnessTests` (the idle-fleet
regression, §5.5) · `ErrnoCauseTests` · `DeepLinkTests` · `FleetMergeTests` (signpost-counted
`body` evaluations) · `ClockSeamTests`.

## 9.2 The real contract, guarded from the Python side

The obvious drift guard — running the Swift decoder against `orchestra.py --demo` — tests
`demo_state()`, which is a **completely different code path** from `collect_state()` →
`scan_sessions()`. A maintainer editing `scan_sessions` would break the iOS client with
nothing turning red on either side.

**Fix: generate the contract on the Python side, commit it, consume it from both suites.**

```python
# tests/test_state_contract.py — stdlib unittest, ~40 lines, fits the existing suite
CONTRACT = ROOT / "docs/mobile/state-contract.json"

class TestStateContract(ConfigGuard):
    """The wire contract is an artifact, not folklore. If this fails, an iOS client in the
    field is about to break — update the artifact deliberately."""
    def test_contract_is_current(self):
        state = build_synthetic_fleet_and_collect()      # real collect_state, stubbed probes
        observed = {
            "session": {"required":    sorted(keys_in_every(state, "sessions")),
                        "conditional": sorted(keys_in_any(state, "sessions")
                                              - keys_in_every(state, "sessions"))},
            "card": {...}, "limit": {...}, "live_proc": {...}, "resume": {...},
        }
        self.assertEqual(observed, json.loads(CONTRACT.read_text()))
```

```swift
@Test func decoderCoversContract() throws {
    let c = try FixtureLoader.contract()             // the SAME committed artifact
    for key in c.session.required {
        #expect(SessionDTO.CodingKeys.allCases.map(\.stringValue).contains(key),
                "server requires `\(key)` and the decoder ignores it")
    }
    for key in c.session.conditional {
        #expect(SessionDTO.optionalKeys.contains(key))
    }
}
```

One artifact, two suites, drift caught on whichever side changes first — and the maintainer
running `python3 -m unittest discover -s tests` sees the break at the source.

### The demo/real delta test, done correctly

A blunt `realKeys.subtracting(demoKeys) == [5 keys]` assertion is wrong: `collect_state`
conditionally adds `limit`, `handed_to`, `tool_running` and `bg_shell`, and `demo_state`'s
`card()` (orchestra.py:974-980) assigns `pid`/`pid_certain` **only to non-`ended` sessions**
while real state sets both unconditionally — so the expected value depends on the mood of
the fleet at capture time. Also `closeout_sent` is a **card**-level key (orchestra.py:779),
not a session key. Split in two:

```swift
@Test func requiredKeysMatchTheDecodersNonOptionals() throws { … }      // stable

@Test func conditionalKeysAreWithinTheDeclaredAllowlist() throws {
    // A NEW server-side key fails loudly; an absent-because-false key does not.
    let allow: Set = ["limit", "handed_to", "tool_running", "bg_shell",
                      "pid", "pid_certain", "subdir", "branch", "turn_ended"]
    #expect(observedConditional.isSubset(of: allow))
}
```

**Demo/real parity is a prerequisite, not a note.** `demo_state()` is missing five session
fields real state always has (`last_user`, `pending_workflows`, `pending_bg_agents`,
`subagent_said`, `subagents_active`), adds a bogus `git_root: ""` to cards, omits
`tty`/`host` from `other_procs`; demo resumes omit `created_at`/`resets_at`; demo limits
accounts omit `fb_label`/`reserve_percent`/`reserve_blocked`. A Codable layer validated only
against `--demo` crashes on real data and vice versa. `test_demo_real_parity` asserts
identical key sets at every level. A note does not keep two payloads together; a red test
does.

## 9.3 Snapshot tests — a real `UIWindow`, not `ImageRenderer`

`ImageRenderer` does not render `UIViewRepresentable`, does not apply safe-area insets, size
classes or device traits, and renders UIKit-backed containers — notably `List`, which the
board is — incorrectly or blank. It would produce stable, green, meaningless PNGs of a view
that does not ship, which is **worse than no snapshot tests** because it manufactures
confidence. (It is also exactly why `swift-snapshot-testing` uses
`drawHierarchy(in:afterScreenUpdates:)`; the dependency is rejected on identity grounds, but
the capability gap is real and must be closed, not ignored.)

```swift
// OrchestraUI/Snapshot/ViewSnapshotter.swift — app-hosted, ~120 lines, zero dependencies
@MainActor
public enum ViewSnapshotter {
    public struct Device: Sendable {
        public let name: String, size: CGSize, scale: CGFloat, safeArea: UIEdgeInsets
        public static let iPhone17Pro = Device(name: "iPhone17Pro",
            size: .init(width: 402, height: 874), scale: 3,
            safeArea: .init(top: 59, left: 0, bottom: 34, right: 0))
        public static let iPhoneSE3 = Device(name: "iPhoneSE3",
            size: .init(width: 375, height: 667), scale: 2,
            safeArea: .init(top: 20, left: 0, bottom: 0, right: 0))
    }

    /// Hosts in a real UIWindow so List, safe areas, traits and size classes are real.
    public static func png(_ view: some View, device: Device,
                           style: UIUserInterfaceStyle = .dark,
                           dynamicType: UIContentSizeCategory = .large) -> Data {
        let host = UIHostingController(rootView: view)
        let window = UIWindow(frame: CGRect(origin: .zero, size: device.size))
        window.overrideUserInterfaceStyle = style
        window.rootViewController = host
        window.makeKeyAndVisible()
        host.view.layoutIfNeeded()
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))   // let List lay out
        let fmt = UIGraphicsImageRendererFormat.default(); fmt.scale = device.scale
        return UIGraphicsImageRenderer(size: device.size, format: fmt).pngData { _ in
            host.view.drawHierarchy(in: host.view.bounds, afterScreenUpdates: true)
        }
    }

    /// TWO criteria, because a naive differing-pixel percentage is the wrong metric on a
    /// mostly-dark board: 1 % of a 1206×2622 render is ~31,000 pixels — an entire
    /// mis-rendered card hides under it — while whole-screen anti-aliasing drift can
    /// exceed it harmlessly.
    public static func compare(_ a: Data, _ b: Data,
                               maxChannelDelta: UInt8 = 8,
                               maxDifferingFraction: Double = 0.002) -> SnapshotDiff
}
```

`ViewSnapshotterTests` proves the comparator with synthetic PNGs: identical → pass · a 4 px
shifted rectangle → **fail** · uniform +3/255 across every pixel → pass. Test infrastructure
is the last place to leave untested.

**Matrix** — `iPhone17Pro` × `iPhoneSE3` × { empty board · all-free · one `needs_input` ·
one `blocked` · one `limit` with a reset · one `limit` with a null reset · one `handed_to` ·
`brief_sent` (✕ close) · an `armed` intent · lagging banner · stale banner · offline banner ·
mac-asleep banner · `.accessibilityExtraExtraExtraLarge` on the busiest card · a 300-char
commit subject }. All rendered at `FixtureLoader.fixtureEpoch` on a `TestClock`, so
countdowns are byte-stable.

## 9.4 UI tests — the two things worth the cost

1. **The scroll hold.** Scroll the board while a scripted frame delivers a reordered
   payload; assert the top card's label is unchanged; then assert it **does** change after
   the grace elapses, or after the `⌗ tap to re-sort` chip is tapped. This is the behaviour
   the deployment floor was argued on, and it must have a test.
2. **Accessibility audits.** `try app.performAccessibilityAudit()` on each of the six
   screens — free, near-zero maintenance, and it catches contrast, hit-target size, missing
   labels and clipped text. Backed by a unit-level `PaletteContrastTests` that computes WCAG
   ratios for every foreground/background token pair and asserts **≥4.5:1** for body text
   and **≥3:1** for status pills. Where a ported token fails, the pill gains a glyph and the
   palette is adjusted **for the phone**, with the change documented — the desktop
   `index.html` palette is not silently forked.

Also worth stating, because it is an accessibility hazard specific to this app: **the arm
window must not be a timer under assistive technology.** When
`UIAccessibility.isVoiceOverRunning || isSwitchControlRunning || isGuidedAccessEnabled`, an
armed intent renders as an explicit two-button sheet (Confirm / Cancel) with **no expiry
countdown**, and the client suppresses local expiry (the server's `expires_at` still
applies; the client transparently re-arms if it lapses). Under Reduce Motion the FLIP
reorder becomes a cross-fade. Arm state expires on foreground and never silently persists
across a backgrounding — a confirm the user cannot see is a confirm they did not give.

## 9.5 Integration against `python3 orchestra.py --demo`

`Foundation.Process` does not exist in the iOS SDK, which is why `Package.swift` declares
`.macOS(.v15)` and this suite is a package test target.

```swift
#if os(macOS)
public actor DemoServer {
    /// HERMETIC. Without a sandbox the suite behaves differently on CI than on the
    /// developer's machine: load_config reads HERE/orchestra.config.json, so a locally
    /// spawned server inherits the developer's real roots and homes — while on CI
    /// read_dispatch_log returns {"entries": []} and read_chat returns {"ok": false}
    /// because claude_homes() is empty. Either the test asserts the CI shape and is
    /// vacuous, or the local shape and fails on CI.
    public static func start(scriptPath: URL) async throws -> Handle {
        let sandbox = try TempDir()
        try sandbox.write("orchestra.config.json", Fixtures.demoConfig)   // roots/homes → sandbox
        try sandbox.write("dispatch.log.jsonl",   Fixtures.demoDispatchLog)
        try sandbox.writeTranscript(home: "claude-demo", sid: "…", Fixtures.demoTranscript)

        // `--port 0` is NOT usable: load_config does `if args.port:` (orchestra.py:97) and 0
        // is falsy in Python, so --port 0 is silently IGNORED and the server binds 4242 —
        // colliding with the user's real board and looking like the test passed against
        // the wrong server. Random high port + an identity assertion instead.
        for _ in 0 ..< 5 {
            let port = Int.random(in: 43_000 ..< 44_000)
            let p = Process()
            p.executableURL = URL(filePath: "/usr/bin/env")
            p.arguments = ["python3", scriptPath.path, "--demo",
                           "--host", "127.0.0.1", "--port", String(port),
                           "--config", sandbox.configPath]
            p.environment = ["CLAUDE_CONFIG_DIRS": sandbox.homesPath]
            p.currentDirectoryURL = sandbox.url
            try p.run()
            // demo_state() reports hostname "starbase" — the harness must never test
            // against a real server that happens to hold a colliding port.
            if await servingDemo(port: port, deadline: .seconds(10)) {
                return Handle(p, port, sandbox)
            }
            p.terminate(); p.waitUntilExit()
        }
        throw DemoServerError.couldNotBind
    }
    deinit { proc.terminate(); proc.waitUntilExit() }   // never orphan a python3 on Ctrl-C
}
#endif
```

**The suite is not conditionally skippable.** A `.enabled(if:)` trait is how contract
coverage silently stops running.

```swift
#if os(macOS)
@Suite(.serialized) struct DemoServerTests {
    @Test func pythonToolchainIsPresent() throws {
        // FAILS, does not skip. Every contract guarantee lives in this suite.
        #expect(FileManager.default.isExecutableFile(atPath: "/usr/bin/env"))
        #expect(try Shell.run("python3", "--version").hasPrefix("Python 3."))
    }
    @Test func demoStateDecodesAndMaps()             async throws { … }
    @Test func demoRefusalsRenderVerbatim()          async throws { … }  // the four "demo mode — …"
    @Test func resumesRideAlongKeyedWorktreePipeSid() async throws { … }
    @Test func intentArmConfirmExpire()              async throws { … }
    @Test func sseDeliversHelloThenStateThenKeepalive() async throws { … }
    @Test func sseHasNoContentEncoding()             async throws { … }  // gzip breaks streaming
    @Test func lastEventIDReplaysOrResyncs()         async throws { … }
    @Test func unknownPathReturnsHTMLNotJSON()       async throws { … }
}
#endif
```

CI additionally fails the job if the executed test count for `OrchestraIntegrationTests`
falls below a committed floor.

**Note `--demo` is not currently a sandbox.** It does **not** cover `/api/dispatchlog` or
`/api/chat` — both read the real `dispatch.log.jsonl` and real transcript files (verified
byte-identical to production). That makes demo mode unusable for App Store screenshots and
for fixture capture until hand-off item 16 lands, and the integration suite works around it
by pointing `HERE` at the sandbox.

## 9.6 Fixture hygiene — enforced on the artifacts, not on the tool

`Tools/scrub.py` strips paths, emails and prose from live captures. A single missed pattern
commits a real account email or a production reference into git history **forever**.

- `tests/test_scrub.py` joins the existing Python suite (so it runs in the maintainer's
  normal workflow) and asserts the scrubber removes a known pattern set from a synthetic
  payload.
- `Tools/check-fixture-hygiene.sh` scans every committed fixture for `@`-containing strings,
  `/Users/`, `/home/`, and 40-hex blobs, and **fails the build** if found. It runs in CI and
  as a pre-commit hook.

## 9.7 CI — `.github/workflows/ios.yml`

```yaml
runs-on: macos-26      # NOT macos-15: swift-tools-version 6.2 and
                       # SwiftSetting.defaultIsolation (SE-0466) need Swift 6.2+, and
                       # macos-15 ships Xcode 16.x / Swift 6.0–6.1 — on that image the
                       # MANIFEST IS REJECTED and none of the steps below run.
steps:
  - uses: actions/checkout@v4
  - run: sudo xcode-select -s /Applications/Xcode_26.app
  - run: |                                       # silent toolchain drift is the normal
      swift --version                            # failure mode on hosted runners
      swift --version | grep -qE 'Swift version 6\.(2|3|[4-9])' \
        || { echo "need Swift 6.2+"; exit 1; }
  - run: swift build -c release --package-path ios/Packages/OrchestraKit
  - run: swift test            --package-path ios/Packages/OrchestraKit
  - run: python3 -m unittest discover -s tests   # the contract artifact guard (§9.2)
  - run: ios/Tools/check-fixture-hygiene.sh
  - run: ios/Tools/lint-isolation.sh
  - run: xcodebuild build -scheme Orchestra -destination 'generic/platform=iOS'
  - run: |
      SIM=$(xcrun simctl list devices available -j | ios/Tools/pick-sim.sh)   # resolved,
      xcodebuild test -scheme Orchestra -testPlan Snapshot -destination "$SIM" # not hardcoded
      xcodebuild test -scheme Orchestra -testPlan UI       -destination "$SIM"
```

The Snapshot plan declares **both** device configurations from §9.3.

The existing Python workflow is untouched: still no `pip install`, still Python 3.11 floor,
still `py_compile` plus `unittest discover`.

---

# 10. Build and distribution

## 10.1 What the paid Apple Developer Program actually buys — precisely

This is the question worth answering carefully, because most of the answer is not "you can
build an app".

**Free Apple ID (no paid membership).** You can already: install Xcode, build the app, run
it on the Simulator without limit, and install it on **your own** physical device via a
*personal team* provisioning profile. So the entire board, the sync engine, the whole UI,
and every test in §9 can be built and exercised for €0.

What a free Apple ID **cannot** do, and each of these is load-bearing for this specific app:

| capability | why this app needs it |
|---|---|
| **Push Notifications capability + APNs keys** | The free tier cannot enable the Push Notifications capability at all, and cannot create an APNs Auth Key (`.p8`). **Everything in §6 is impossible without membership** — no alerts, no NSE, no notification actions, no inline reply, no Live Activity pushes. This is the single biggest thing you are paying for. |
| **App Groups** | Free personal teams cannot use App Group entitlements. Without them the NSE cannot share a token or an inbox with the app, and the widget cannot read the cached snapshot — §7.4 and §8 both collapse. |
| **Keychain access groups** | Same: the NSE cannot read the `read` token, so every enriched notification degrades to generic. |
| **Time Sensitive Notifications entitlement** | Without it iOS silently clamps `time-sensitive` to `active`, which Sleep Focus suppresses — i.e. the 2am blocked-agent alert never arrives, with no error anywhere. |
| **Provisioning profiles that last** | A personal-team profile expires after **7 days**; the app stops launching and must be re-installed from Xcode. For a tool you rely on daily while away from the Mac, that is disqualifying on its own. |
| **TestFlight** | The only way to install on a second device, on a family member's phone, or to install without a Mac attached. |
| **More than 3 apps / 10 device IDs per week** | Personal teams are capped. |
| **App Store distribution** | Not needed here — this is a personal tool — but membership is the only route if that ever changes. |

**Cost:** US$99 / year (Apple Developer Program, individual). An LLC/organization membership
costs the same but requires a D-U-N-S number; for a single-user tool, individual is correct.

**Honest summary:** without the paid account you get a working board on your own phone that
has to be re-signed every 7 days and **cannot notify you about anything** — which removes
the entire reason to have it on a phone. The membership is not optional for this product;
it is a prerequisite for §6, §7.4 and §8.

**The no-account interim path is real and is planned for:** with a free Apple ID plus the
**ntfy** sink (§6.7), you get the board, the sync engine, actuation, and text-only alerts
delivered through the third-party ntfy app — no inline reply, no deep links, no badge, no
withdrawal, and a 7-day re-sign cycle. That is genuinely useful for bring-up and for
evaluating whether the €99 is worth it, and it is why the push layer is pluggable at the
server rather than at the phone.

## 10.2 Identifiers

| thing | value |
|---|---|
| App bundle ID | `sh.orchestra.app` |
| Widget extension | `sh.orchestra.app.widgets` |
| Notification service extension | `sh.orchestra.app.notifications` |
| App Group | `group.sh.orchestra` |
| Keychain access group | `$(AppIdentifierPrefix)sh.orchestra.shared` |
| APNs topic (alerts) | `sh.orchestra.app` |
| APNs topic (Live Activity) | `sh.orchestra.app.push-type.liveactivity` |
| Live Activity attributes type | `LimitResetAttributes` |
| BGTask identifier | `sh.orchestra.refresh` |
| URL schemes | `orc`, `orchestra` |

## 10.3 Signing

```
Configs/Shared.xcconfig       # everything non-secret, committed
  PRODUCT_BUNDLE_IDENTIFIER = sh.orchestra.app
  MARKETING_VERSION         = 1.0
  SWIFT_VERSION             = 6.0
  IPHONEOS_DEPLOYMENT_TARGET = 18.0
  SWIFT_STRICT_CONCURRENCY  = complete
  OTHER_SWIFT_FLAGS         = -warnings-as-errors

Configs/Debug.xcconfig
  #include "Shared.xcconfig"
  APS_ENVIRONMENT = development
  CODE_SIGN_STYLE = Automatic

Configs/Release.xcconfig
  #include "Shared.xcconfig"
  APS_ENVIRONMENT = production
  CODE_SIGN_STYLE = Manual
  CODE_SIGN_IDENTITY = Apple Distribution
  PROVISIONING_PROFILE_SPECIFIER = orchestra-appstore

Configs/Signing.xcconfig      # GITIGNORED — the only file holding the team
  DEVELOPMENT_TEAM = XXXXXXXXXX
```

`Orchestra.entitlements` references `$(APS_ENVIRONMENT)` so one plist serves both
configurations, and the app reads the resolved value from its **embedded provisioning
profile** at runtime (§1.5) rather than inferring it — TestFlight builds are `DEBUG=0` and
yet a local development-signed build is not, and that confusion is the classic APNs
"why is nothing arriving" bug.

Automatic signing is fine for Debug and for a single developer. Manual for Release keeps the
distribution profile explicit, which matters because three targets must all be signed with
matching App Group and Keychain entitlements — a mismatch there presents as a widget that
shows placeholder forever, with no error.

## 10.4 TestFlight

Distribution is **TestFlight internal testing only**. It is the right shape for this
product: up to 100 internal testers (Apple ID holders on the team), builds valid for 90
days, **no App Review for internal testers**, and installation over the air without a Mac.

```sh
# Tools/release.sh
set -euo pipefail
xcodebuild -scheme Orchestra -configuration Release \
           -destination 'generic/platform=iOS' \
           -archivePath build/Orchestra.xcarchive archive
xcodebuild -exportArchive -archivePath build/Orchestra.xcarchive \
           -exportOptionsPlist Configs/ExportOptions.plist \
           -exportPath build/export
xcrun altool --upload-app -f build/export/Orchestra.ipa -t ios \
             --apiKey "$ASC_KEY_ID" --apiIssuer "$ASC_ISSUER_ID"
```

`CFBundleVersion` is the CI run number; `MARKETING_VERSION` is hand-bumped.

**What would change if this ever went to the App Store** (it should not, but state it so the
decision is informed): App Review would want a demo account or a video, because the app is
useless without a paired Mac on the reviewer's tailnet — `--demo` mode exists partly for
this, which is another reason hand-off item 16 (demo-sandbox `/api/chat` and
`/api/dispatchlog`) matters. `PrivacyInfo.xcprivacy` is already required for TestFlight
uploads and is in the tree.

## 10.5 Build-time invariants

| invariant | enforced by |
|---|---|
| the extensions never link `OrchestraStore` | the package graph (§1.2) |
| no `Task.detached` / `@unchecked Sendable` / `nonisolated(unsafe)` / `AnyView` / `URLSession.shared` outside the allowlist | `Tools/lint-isolation.sh` in CI |
| no `nonisolated async func` without `@concurrent` in Core/API | same lint (SE-0461, §4.4) |
| no email, `/Users/`, `/home/` or 40-hex blob in a committed fixture | `Tools/check-fixture-hygiene.sh` |
| the Swift decoder covers every required contract key | `decoderCoversContract` + `tests/test_state_contract.py` |
| warnings are errors | `SWIFT_TREAT_WARNINGS_AS_ERRORS` |

---

# 11. Budgets and open verification tasks

## 11.1 Client budgets

| metric | budget | mechanism |
|---|---|---|
| cold launch → populated board **from cache** | ≤ 400 ms (`MXAppLaunchMetric.histogrammedTimeToFirstDraw`) | no dylibs beyond system frameworks; a ≤36 KB JSON decodes in ~3 ms; render cached with a staleness chip, never a spinner |
| cold launch → fresh (Wi-Fi / LTE / DERP) | 2.5 s / 4 s / 7 s p95 | §5.10 |
| board scroll, ProMotion | no frame > **8.3 ms** | ≤6 sessions/card, `List`, stable ids, `Equatable` values |
| board scroll, **iPhone SE 3 (60 Hz, A15)** | no frame > **16.6 ms** | the scroll benchmark runs on the SE — it is the device that fails first |
| steady memory | < 60 MB | two snapshots max; chat LRU 2 MB; `.ephemeral` sessions so no response cache accumulates |
| foreground CPU, streaming, idle board | < 0.5 % | one keepalive per 25 s |
| foreground CPU, streaming, busy board | < 2 % | ~120–200 B deltas |

## 11.2 Data, per hour of foreground — with the denominator stated

| mode | payload/hour | notes |
|---|---|---|
| SSE, idle board | **~5 KB** | 144 keepalives |
| SSE, busy board (~1 change / 3 s) | **~0.6 MB** | 1200 deltas × ~180 B, gzipped |
| SSE + chat open, server-side `since` | +~0.2 MB | incremental turns only |
| SSE + chat open, **no `since` (today)** | **+~32 MB** | 40 × 900 chars every 4 s — this is why hand-off item 8 is not optional |
| polling fallback, 4 s, full | ~8 MB gzipped (~32 MB raw) | |
| polling fallback, 4 s, `mode=digest` | ~0.9 MB | ~1 KB frames |
| `/api/limits` every 5 min | ~85 KB | 7,035 B measured |

A realistic heavy day (3 h foreground on SSE with two chat sessions) is **~3 MB**. On-wire
is roughly 3.5× payload once TCP/IP plus WireGuard's 32-byte header and 16-byte tag are
counted, so ~10 MB — still trivial, and the **connection-count** reduction (720/hr → ~12/hr)
is the larger win.

## 11.3 Energy — the budget that decides whether the app survives

| target | value | measured by |
|---|---|---|
| foreground, streaming, idle board, LTE | **≤ 3 %/hour** | Xcode Energy gauge + `MXAppRunTimeMetric` |
| foreground, streaming, busy board, LTE | **≤ 6 %/hour** | + `MXCellularConditionMetric` |
| foreground, polling fallback 4 s, LTE | ≤ 12 %/hour | the number that justifies SSE |
| background, no Live Activity | ≈ 0 | stream cancelled; push only |

A 4 s poll keeps the cellular radio RRC-connected effectively continuously, and the
NetworkExtension tunnel adds a second userspace process doing crypto per packet. **SSE's
primary value is energy; bandwidth is second.** Low Power Mode raises the polling floor to
20 s and disables the 1 Hz countdown ticker (countdowns then re-render on frame arrival
only).

Note also that **Tailscale itself is not free** and the user will see *Tailscale*, not
orchestra, in the iOS battery list. Two supported modes: always-on tunnel (the NSE can
enrich; higher steady-state cost) and **on-demand tunnel scoped to the app (the default)** —
which is precisely why §6.2 puts the counts in the payload rather than behind an NSE fetch.

## 11.4 Open verification tasks — day one, before code depends on them

1. **NE-tunnel errno behaviour.** Against a real tailnet: Mac asleep · Mac off the tailnet ·
   orchestra stopped. Confirm `ECONNREFUSED` propagates (expected: yes) and whether
   `.macUnreachable` / `.tailnetDown` are distinguishable (expected: no). §5.11's copy is
   written for the merged case; split only if measurement supports it.
2. **Local-network privacy gate** for a `utun`-routed 100.64/10 destination on iOS 18. The
   `NSLocalNetworkUsageDescription` key is included defensively either way (§1.5); the
   question is whether the prompt actually fires, because spending a scary permission
   dialog on the same first-run screen as the camera prompt, for nothing, is a real cost.
3. **`SSEDelegate` vs `AsyncBytes.lines`** on a 35 KB frame, on-device. If `AsyncBytes`
   measures acceptably, **delete the delegate** and cite the number — it exists on a
   measured argument or not at all.
4. **`isExpensive` / `isConstrained` through the tunnel**, with Tailscale on and off. §5.2's
   Low Data Mode promise is unenforceable if the signal does not propagate; §5.3's
   three-monitor approach and the manual override are the fallback, and the whole cadence
   table rests on this two-hour experiment.
5. **ActivityKit update budget** with `NSSupportsLiveActivitiesFrequentUpdates` — how many
   pushes actually land in 20 s, and what `frequentPushesEnabled` reports in practice.
6. **APNs end-to-end, twice, before calling push done:** a **two-token batch** against
   `api.sandbox.push.apple.com` with a real key asserting **both** return 200 (a single-URL
   403 proves nothing about batching), and **lock-screen inline reply twice** — locked since
   boot, and locked since last unlock. Those are different Keychain accessibility paths and
   the first is the one that fails silently with the wrong attribute.

---

# 12. Backend hand-off

Ordered. **Items 1–7 are prerequisites**: the client is built against them and there is no
legacy mode (§5.6). Everything is stdlib-only except where flagged.

| # | change | why the client needs it | ref |
|---|---|---|---|
| **1** | **Collapse the git storm** — one `git status --porcelain=v2 --branch` per worktree (measured 19 ms) instead of five calls; parallelise; memoise on `.git/HEAD` + index `stat()` | 1641 → ~570 ms; sets where the fleet-size curve breaks | `FRESHNESS.md` L241 |
| **2** | **Continuous collector** with a monotonic `v` that bumps only on composed-view change | push is impossible without it; kills the thundering herd structurally; makes `?since=` meaningful | ADR 0006 · `ENGINE.md §3.2` |
| **3** | **Time-invariant schema** — `last_write_at` replaces `age_s`, `resets_at` replaces `resets_in` | cards stop differing on every publish; the phone animates locally at 1 Hz with zero round-trips | `ENGINE.md §3.4` |
| **4** | **`limit` status without an inbound `/api/limits`** | otherwise a phone that never opens the limits view mis-triages limit-stuck agents into the loudest state in the app | `FRESHNESS.md §6` · orchestra.py:701-704 |
| **5** | **Per-device tokens** — enrollment code → `POST /api/pair`, `devices.json`, `DELETE /api/pair/<id>`; plus a `Host`/`Origin` allowlist | blocking. Today every tailnet node can `POST /api/dispatch` and run `--dangerously-skip-permissions`, and browser CSRF via a `text/plain` simple request already works on pure loopback | `AUTH.md` §7 |
| **6** | **`POST /api/intent`** — durable records, server-side arm, `expires_at`, idempotent by `id` | kills the long-POST/suspension hazard, the double-fire hazard and the client-side arm gate in one change | `ENGINE.md §3.1` · ADR 0008 |
| **7** | **`GET /api/events` (SSE) + `GET /api/meta`** | `hello`+`server_time` (skew), `order` on every frame, `: keepalive` ≤25 s, `Last-Event-ID` resume | ADR 0005 · `REALTIME.md §5.2-5.3` |
| **8** | **`since` / `limit` on `/api/chat`** | verified: `read_chat(account, sid, limit=40)` takes no `since` and `do_GET` never forwards `limit`. Without it chat costs **~32 MB/h** — 100× the whole board | §11.2 |
| **9** | **`parse_qs` the query string** (fixes the never-decoded `account` param) | any label needing percent-encoding is unreachable today | orchestra.py:2219 |
| **10** | `--` sentinel before every `send-keys -l` / `set-buffer` argument; `_proven_in_transcript` receipt on send | **verified on tmux 3.6a**: `send-keys -t T -l "-n foo"` exits 1, "unknown flag -n". And `ok:true` currently means "keys accepted", not "agent received" | orchestra.py:1359, :1717 |
| **11** | Reject a past `due_at` on resume rather than clamping to `now+5` | the clamp is a desktop convenience that is wrong for a client that can be offline for hours | orchestra.py:1966 |
| **12** | **Resolve the card-key collision** — path-derived key, or refuse a fleet with duplicate basenames | `discover_worktrees` dedupes by path while `cards` is keyed by name → one card silently lost, and `wt:<name>` mutations are ambiguous | orchestra.py:120-138 |
| **13** | `try/except` around `_run_dispatch` and `do_POST`; cap and validate `Content-Length` | a bad payload strands a job at `done:false` forever; a **negative** `Content-Length` becomes `read(-1)` = read-to-EOF = hang | orchestra.py:2249 |
| **14** | Atomic `save_resumes` (tmp + `os.replace`); locks on `_cache` / `_closeouts` | `write_text` truncate-then-write plus `except OSError: pass` silently loses every armed schedule | orchestra.py:1885 |
| **15** | Cache `read_dispatch_log` behind a 5 s TTL; tail-read instead of `read_text()` on the whole file | it reads and `json.loads`-parses the **entire** unbounded log plus a `tmux list-sessions` subprocess on every call, for 25 entries — 38 KB, larger than the board | orchestra.py:1613-1633 |
| **16** | **Demo-sandbox `/api/dispatchlog` and `/api/chat`; demo/real payload parity** | `--demo` currently serves real mission prose and real transcripts (verified byte-identical). Unusable for fixtures, for App Store screenshots, or as a Codable oracle | §9.2, §9.5 |
| **17** | `--port-file PATH` (3 lines) | `--port 0` is silently ignored (`if args.port:` — 0 is falsy) and binds 4242, colliding with the user's real board | orchestra.py:97 |
| **18** | `firing` status + `started_at` on resume schedules | `fire_resume` can block ~14 minutes while the schedule still reads `pending` with a past `due_at`; the Live Activity would show a countdown stuck at zero | §6.6 |
| **19** | `do_HEAD` | §5.10's tunnel warm-up; currently 501 | orchestra.py |
| **P1–P7** | **The push pipeline** — `POST`/`DELETE /api/push/register`, `POST /api/push/lastart`, `push.tokens.json`, a `PushSink` protocol with `APNsSink`/`NtfySink`/`NoopSink`, `notify(transitions)` fed by the existing edge detector, `.p8` handling with a 40-minute JWT cache, one warm `curl --http2`, the Live Activity route and push-to-start | §6. **Adds a hard dependency on macOS `curl` (nghttp2) and `openssl`** — stated, not smuggled; `pip install pyjwt httpx` remains the documented escape hatch | ADR 0003 · `PUSH.md` |

---

# 13. What this plan deliberately does not do

- **No end-to-end request signing.** Inside TLS inside WireGuard it defends against nothing
  in the threat model and costs clock-skew failures on a phone.
- **No offline action queue.** Intents are durable server-side; there is nothing left to
  replay from the device.
- **No graceful degradation against pre-contract-2 servers.** One hard gate, one honest
  screen (§5.6).
- **No ETag / `If-None-Match` on `/api/state`.** It can never match: `generated_at` and every
  timestamp change on each collection. `?since=<v>` with a version that bumps only on real
  change is the correct mechanism and it already exists in the backend plan.
- **No `?fields=` payload stripping.** Verified at index.html:480-483, `topic`, `last_user`,
  `last_assistant` and `subagent_said` *are* the four lines a card renders. With deltas at
  ~120–200 B the bandwidth argument disappears entirely.
- **No Critical Alerts.** The entitlement requires separate Apple approval with lead time
  and is not plausibly granted for a developer tool. `time-sensitive` is the ceiling, and
  the guide must say so rather than implying the app can pierce Do Not Disturb.
- **No `/api/focus` in the mobile surface.** It is a GET with a real side effect that, for
  tmux hosts, opens a **brand-new Terminal window per call** — and raising a window on a Mac
  the user is not looking at is meaningless from a phone. Any URLSession retry or prefetch
  would spam windows.
- **No SwiftData, no third-party packages.** §7.4 and §1.4.
