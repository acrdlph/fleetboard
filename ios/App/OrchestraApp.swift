import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

@main
struct OrchestraApp: App {
    @State private var model = AppModel()

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
                .task { await model.start() }
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

    init() {
        let client = OrchestraClient()
        self.client = client
        self.pairing = PairingStore(client: client)
        self.fleet = FleetStore(client: client)
    }

    func start() async {
        await pairing.restore()
        #if DEBUG
        await pairFromLaunchEnvironment()
        #endif
    }

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
