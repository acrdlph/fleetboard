import Foundation
import UserNotifications
#if canImport(UIKit)
import UIKit
#endif

/// The App layer's whole relationship with the OS notification system: it asks
/// for authorization, registers the reply category, becomes the delegate, and
/// funnels every tap and every inline reply through ONE handler that the
/// `#if DEBUG` seams call too — so the path that answers an agent from a banner
/// is the same path a test drives, because a simulator has no way to type into a
/// notification and a check only ever done by hand stops being done.
///
/// It holds no server logic. Registration and reply are `PushStore`'s; deep
/// links are `PushRouter`'s. This class is the adapter between those value-typed
/// stores and `UNUserNotificationCenter`, which is neither.
@MainActor
final class PushController: NSObject, UNUserNotificationCenterDelegate {
    private let store: PushStore
    private let router: PushRouter
    /// The environment this build registers under — read once from the embedded
    /// provisioning profile, `sandbox` on a simulator that has none. The server
    /// auto-heals a wrong guess on Apple's `400 BadDeviceToken`, but a right one
    /// saves the round trip.
    let environment: String

    /// The last inline-reply outcome, kept so a `#if DEBUG` seam and the
    /// diagnostics can see that a reply reached the server and what it said —
    /// the banner's own reply field cannot be screenshotted mid-type.
    private(set) var lastReplyState: Outgoing.State?

    init(store: PushStore, router: PushRouter) {
        self.store = store
        self.router = router
        self.environment = PushEnvironment.current()
        super.init()
    }

    /// Set the delegate and register the categories. Idempotent, and safe to call
    /// before authorization — the categories must exist the instant a banner
    /// arrives, and a banner can arrive before the app has ever run its
    /// authorization flow (a reinstall that restored the token).
    func start() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(Self.categories())
    }

    /// The reply category and the plain one. `ORC_REPLY` carries the text field;
    /// `ORC_INFO` carries nothing and just opens the app on tap.
    ///
    /// The reply action is **not** `.foreground`: opening the app would defeat
    /// the entire point, which is answering without leaving the lock screen. Nor
    /// does it require authentication — the 2 a.m. blocked-agent case this exists
    /// for is one where reaching for Face ID is the friction that loses the
    /// feature; the safety is server-side, where `identity.resolve` re-checks the
    /// address at the instant it types.
    static func categories() -> Set<UNNotificationCategory> {
        let reply = UNTextInputNotificationAction(
            identifier: PushCategory.replyAction,
            title: "Reply",
            options: [],
            textInputButtonTitle: "Send",
            textInputPlaceholder: "Answer the agent…")
        return [
            UNNotificationCategory(identifier: PushCategory.reply, actions: [reply],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: PushCategory.info, actions: [],
                                   intentIdentifiers: [], options: []),
        ]
    }

    /// Ask the OS, record the outcome, and register for remote notifications if
    /// granted. Provisional is not requested: this app's P1 is a time-sensitive
    /// alert meant to break through Focus, and a provisional (quiet) grant would
    /// bury exactly the notification the feature exists for.
    func requestAuthorization() async {
        let center = UNUserNotificationCenter.current()
        #if DEBUG
        // `ORC_PUSH_PROVISIONAL` asks for PROVISIONAL authorization, which on an
        // OS that honours it is granted SILENTLY — the intended way to arm push
        // on a simulator that cannot tap "Allow". (Measured caveat, iOS 26: the
        // sim prompts even for provisional, and with no accessible Simulator
        // window there is no way to satisfy it — so the banner-PRESENTATION half
        // is not scriptable here; the delegate handling is driven directly by the
        // `ORC_PUSH` seams instead, which is what proves it.)
        if ProcessInfo.processInfo.environment["ORC_PUSH_PROVISIONAL"] != nil {
            let granted = (try? await center.requestAuthorization(options: [.provisional])) ?? false
            store.noteAuthorization(granted: granted)
            #if canImport(UIKit)
            UIApplication.shared.registerForRemoteNotifications()
            #endif
            return
        }
        #endif
        let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        store.noteAuthorization(granted: granted)
        // Register for remote notifications even when the grant is only partial:
        // a token on file is what lets a later re-ask deliver anything, and the
        // server keeps it whether or not a banner will show today.
        #if canImport(UIKit)
        UIApplication.shared.registerForRemoteNotifications()
        #endif
    }

    /// Re-assert registration on a foreground — the tz offset may have moved even
    /// if the token did not, and iOS gives no background callback for a zone
    /// change. Cheap: `registerForRemoteNotifications` returns the same token
    /// synchronously and the store skips a POST when nothing changed.
    func refreshRegistration() {
        #if canImport(UIKit)
        UIApplication.shared.registerForRemoteNotifications()
        #endif
    }

    // MARK: - remote-registration results (forwarded from the app delegate)

    func registered(deviceToken: Data) {
        let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task { await store.register(tokenHex: hex, environment: environment) }
    }

    func registrationFailed(_ error: any Error) {
        store.noteRegistrationFailure(error.localizedDescription)
    }

    // MARK: - the one handler

    /// Every tap, action and inline reply lands here — from the delegate and from
    /// the debug seams alike.
    func handle(message: PushMessage, actionIdentifier: String, replyText: String?) async {
        switch actionIdentifier {
        case PushCategory.replyAction:
            guard let target = message.replyTarget else { return }
            let text = replyText ?? ""
            lastReplyState = await store.replyFromNotification(target, text: text)
        case UNNotificationDismissActionIdentifier:
            break
        default:
            // The default (a plain tap) and any custom open action: land on the
            // session the notification is about. An account-level event with no
            // worktree lands on the board, which is where it is answered.
            router.navigate(to: message.deepLink)
        }
    }

    // MARK: - UNUserNotificationCenterDelegate

    // The requirements are `nonisolated` and carry non-`Sendable` parameters, so
    // — exactly like `QRScannerView`'s metadata delegate — the witnesses are
    // `nonisolated`, extract the `Sendable` facts synchronously (a decoded
    // `PushMessage`, the action id, the typed text; `UNNotificationResponse`
    // itself must not cross the boundary), and hop to the main actor.

    /// A notification that arrives while the app is FOREGROUND still shows a
    /// banner — the board does not make the notification redundant, because the
    /// notification is the one thing that says WHICH agent, and the board makes
    /// you find it.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound, .list])
    }

    /// A tap or an inline reply. `completionHandler` is called only AFTER the
    /// handling finishes — an inline reply launches the app into the background
    /// and calling it early would let iOS suspend the app mid-send. It is a
    /// non-`Sendable` closure the OS invokes on the main thread; `nonisolated
    /// (unsafe)` carries it into the main-actor `Task` without minting a new
    /// `@unchecked Sendable` type.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let message = PushMessage(userInfo: response.notification.request.content.userInfo)
        let action = response.actionIdentifier
        let text = (response as? UNTextInputNotificationResponse)?.userText
        nonisolated(unsafe) let done = completionHandler
        Task { @MainActor [weak self] in
            if let message {
                await self?.handle(message: message, actionIdentifier: action, replyText: text)
            }
            done()
        }
    }
}

/// Which of Apple's two APNs hosts this build's token is minted against.
///
/// **Never `#if DEBUG`.** A TestFlight build is `DEBUG=0` and yet `sandbox`;
/// trusting the compile flag is the single most common cause of "push just
/// doesn't work" (APNS-SETUP.md §4). The truth is in the embedded provisioning
/// profile's `aps-environment` entitlement, so it is read from there. A
/// simulator carries no profile, which reads as `sandbox` — correct, since a
/// simulator's token (when it has one) is a sandbox token.
enum PushEnvironment {
    static func current() -> String {
        guard let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
              let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .isoLatin1),
              let start = text.range(of: "<plist"),
              let end = text.range(of: "</plist>")
        else { return "sandbox" }
        let plist = String(text[start.lowerBound..<end.upperBound])
        guard let plistData = plist.data(using: .isoLatin1),
              let obj = try? PropertyListSerialization.propertyList(from: plistData, options: [], format: nil),
              let dict = obj as? [String: Any],
              let entitlements = dict["Entitlements"] as? [String: Any],
              let env = entitlements["aps-environment"] as? String
        else { return "sandbox" }
        return env == "production" ? "production" : "sandbox"
    }
}

/// The `UIApplicationDelegate` that exists ONLY to catch the two remote-
/// registration callbacks SwiftUI's `App` lifecycle does not surface — the
/// device token and the failure. It buffers whichever arrives before the
/// controller is wired (the token can come back before the first view's `.task`
/// runs) and replays it the moment the controller is set.
@MainActor
final class OrchestraAppDelegate: NSObject, UIApplicationDelegate {
    var controller: PushController? {
        didSet { flush() }
    }
    private var bufferedToken: Data?
    private var bufferedError: (any Error)?

    func application(_ application: UIApplication,
                     didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        if let controller {
            controller.registered(deviceToken: deviceToken)
        } else {
            bufferedToken = deviceToken
        }
    }

    func application(_ application: UIApplication,
                     didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        if let controller {
            controller.registrationFailed(error)
        } else {
            bufferedError = error
        }
    }

    private func flush() {
        guard let controller else { return }
        if let token = bufferedToken { controller.registered(deviceToken: token); bufferedToken = nil }
        if let error = bufferedError { controller.registrationFailed(error); bufferedError = nil }
    }
}
