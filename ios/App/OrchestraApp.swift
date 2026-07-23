import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

@main
struct OrchestraApp: App {
    @State private var model = AppModel()
    // Catches the two remote-registration callbacks the SwiftUI `App` lifecycle
    // does not surface — the device token and the failure. See `PushController`.
    @UIApplicationDelegateAdaptor(OrchestraAppDelegate.self) private var appDelegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                // Night is the product; Daylight is a legibility mode. Phase 1
                // ships Night only — but every token already carries its light
                // variant, because notification banners, widgets and Live
                // Activities render in the OS appearance and `.preferredColorScheme`
                // does not reach them.
                .preferredColorScheme(.dark)
                .accessibilityIgnoresInvertColors(true)
                .task {
                    // Wire the delegate to the model's controller before start:
                    // a token buffered by the delegate replays the instant this
                    // runs, and start() may itself register for one.
                    appDelegate.controller = model.pushController
                    await model.start()
                }
                .onChange(of: scenePhase) { _, phase in
                    Task { await model.scenePhaseChanged(to: phase) }
                }
        }
    }
}

/// The composition root. Owns every store; nothing else constructs one.
@MainActor
@Observable
final class AppModel {
    let client: OrchestraClient
    let pairing: PairingStore
    let fleet: FleetStore
    let limits: LimitsStore
    /// The branch map. App-level rather than per-screen so an appear-fetch is not
    /// re-paid every time the map is pushed and popped — the git sweep behind it
    /// is the one server read a phone must not repeat casually.
    let topology: TopologyStore
    /// Everything the app can make the fleet DO. App-level and not per-screen,
    /// because a dispatch outlives the sheet that started it — see `ActionsStore`.
    let actions: ActionsStore
    /// Push: registration, the server-held preferences, the pipeline's health.
    let push: PushStore
    /// The one-way channel from a notification tap to the navigation stack.
    let router: PushRouter
    /// The adapter over `UNUserNotificationCenter` — authorization, the reply
    /// category, the delegate. Held here so the app delegate can hand it the
    /// device token and so `scenePhaseChanged` can re-assert registration.
    let pushController: PushController

    /// Requested once. Authorization prompts the user, and pairing can happen
    /// after launch, so the push flow is armed both from `start()` and from the
    /// paired view's appearance — and this makes the second call a no-op.
    private var pushStarted = false

    init() {
        let client = OrchestraClient()
        self.client = client
        self.pairing = PairingStore(client: client)
        let fleet = FleetStore(client: client)
        self.fleet = fleet
        self.limits = LimitsStore(client: client)
        self.topology = TopologyStore(client: client)
        self.actions = ActionsStore(client: client, fleet: fleet)
        let push = PushStore(client: client)
        self.push = push
        let router = PushRouter()
        self.router = router
        self.pushController = PushController(store: push, router: router)
    }

    func start() async {
        await pairing.restore()
        #if DEBUG
        await pairFromLaunchEnvironment()
        #endif
        if pairing.isPaired {
            fleet.start()
            ensurePushStarted()
        }
        #if DEBUG
        await runPushDebugSeams()
        #endif
    }

    /// Arm push exactly once: become the notification delegate, register the
    /// reply category, ask for authorization, register for a remote token, and
    /// read the pipeline's current health. Called from `start()` and, because
    /// pairing can happen after launch, from the paired view too.
    func ensurePushStarted() {
        guard pairing.isPaired, !pushStarted else { return }
        pushStarted = true
        pushController.start()
        Task {
            await pushController.requestAuthorization()
            await push.refreshStatus()
        }
    }

    /// **Backgrounding drops the stream; foregrounding resyncs.** A phone is not
    /// a browser tab: a suspended app cannot read a socket, and what iOS does
    /// instead of keeping one alive for it is leave the Mac holding one of its
    /// 32 subscriber slots for a client that is not there.
    ///
    /// The trigger is `.background` and **not `.inactive`** — that phase is
    /// Control Center, the app switcher, a call banner, a permission alert. A
    /// stream torn down every time a notification banner slid past would
    /// reconnect a dozen times an hour for nothing.
    func scenePhaseChanged(to phase: ScenePhase) async {
        guard pairing.isPaired else { return }
        switch phase {
        case .background:
            fleet.stop()
        case .active:
            // Idempotent by construction: `start()` will not open a second
            // socket, and the resync costs one delta because the version we
            // still hold goes back as `Last-Event-ID`.
            await fleet.resume()
            // Re-assert the token: the tz offset may have moved while backgrounded
            // (a flight, a DST edge) and quiet hours are evaluated in the device's
            // zone. Cheap — the store skips the POST when the token is unchanged
            // and only the offset rides.
            ensurePushStarted()
            pushController.refreshRegistration()
        default:
            break
        }
    }

    #if DEBUG
    /// The push test seams, and they exist for the same reason `ORC_SEND` does:
    /// a simulator cannot be typed into from a script, so the one flow that
    /// proves this feature — a real token reaching the server, a tap deep-linking
    /// to the right session, an inline reply reaching an agent — would be
    /// verifiable only by hand, and a check only ever done by hand stops being
    /// done. Each seam presses exactly the button the OS presses, through the
    /// same `PushStore` and `PushController` the delegate uses.
    ///
    /// ```
    /// SIMCTL_CHILD_ORC_PUSH_TOKEN=<64+ hex>            # drive registration
    /// SIMCTL_CHILD_ORC_PUSH='{"ev":"session.needs_answer","wt":"X","sid":"…"}'
    /// SIMCTL_CHILD_ORC_PUSH_ACTION=reply               # default | reply
    /// SIMCTL_CHILD_ORC_PUSH_REPLY='yes, ship it'       # the typed answer
    /// ```
    private func runPushDebugSeams() async {
        let env = ProcessInfo.processInfo.environment
        if let hex = env["ORC_PUSH_TOKEN"], !hex.isEmpty {
            await push.register(tokenHex: hex, environment: pushController.environment, force: true)
        }
        guard let raw = env["ORC_PUSH"], !raw.isEmpty,
              let data = raw.data(using: .utf8),
              let userInfo = (try? JSONSerialization.jsonObject(with: data)) as? [AnyHashable: Any],
              let message = PushMessage(userInfo: userInfo) else { return }
        let action = env["ORC_PUSH_ACTION"] == "reply"
            ? PushCategory.replyAction : "default"
        await pushController.handle(message: message, actionIdentifier: action,
                                    replyText: env["ORC_PUSH_REPLY"])
    }
    #endif

    #if DEBUG
    /// A test seam, and it is here for a specific reason.
    ///
    /// **A simulator has no camera and no way to be typed into from a script.**
    /// `xcrun simctl openurl` reaches the app but iOS puts a system
    /// "Open in orchestra?" dialog in front of it, which needs a finger. So the
    /// one flow that decides whether this app works at all — get a real token
    /// from a real server — would be verifiable only by hand, and a check that
    /// is only ever done by hand is a check that stops being done.
    ///
    /// ```
    /// SIMCTL_CHILD_ORC_PAIR_URL='orc://p?h=…&p=…&c=…' \
    ///   xcrun simctl launch booted sh.orchestra.app
    /// ```
    ///
    /// It is `#if DEBUG`, it reads an environment variable a Release build
    /// cannot see, and it takes exactly the same `PairingTicket` through exactly
    /// the same `PairingStore.pair` as the camera and the typed form. It is a
    /// way to press the button, not a second way to pair.
    private func pairFromLaunchEnvironment() async {
        guard !pairing.isPaired,
              let raw = ProcessInfo.processInfo.environment["ORC_PAIR_URL"],
              let ticket = PairingTicket(url: raw) else { return }
        await pairing.pair(with: ticket, label: AppModel.deviceLabel)
    }
    #endif

    func unpair() async {
        await pairing.unpair()
    }

    /// What the device calls itself, which is what shows up in the Mac's
    /// `--list-devices` — so the row the user revokes is the phone in their hand.
    static var deviceLabel: String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }
}
