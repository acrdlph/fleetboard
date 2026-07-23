import Foundation
import Observation

/// Everything the app knows about push: whether this device is registered, what
/// the server's pipeline can do, and the preferences that live on the server.
///
/// **It holds no UIKit.** `UNUserNotificationCenter`, the authorization prompt
/// and `registerForRemoteNotifications` are the App layer's `PushAdapter`; this
/// store is the part that can be tested without a simulator — it takes a token
/// as a hex string, not as an opaque `Data` from a delegate, and it takes the
/// authorization outcome as a `Bool`, not a `UNAuthorizationStatus`. The seam is
/// deliberate: the one path that decides whether push works at all (a real token
/// reaching the real server) is drivable from a test and from a `#if DEBUG`
/// launch seam, because a check only ever done by hand is a check that stops
/// being done.
@MainActor
@Observable
public final class PushStore {
    public enum Registration: Sendable, Equatable {
        /// No token yet — either authorization not asked, or APNs has not
        /// answered `didRegisterForRemoteNotifications`.
        case idle
        case registering
        /// The server took the token. `environment` is which of Apple's two
        /// hosts it will send through; `warnings` is anything it wants surfaced
        /// (the one that matters: Focus will suppress P1).
        case registered(environment: String, warnings: [String])
        /// The server refused the token, or the request never landed. Its own
        /// sentence, never "an error occurred".
        case failed(String)
    }

    public private(set) var registration: Registration = .idle
    /// The OS authorization outcome, set by the adapter. `nil` until asked.
    /// `false` means the user declined — in which case a registered token still
    /// reaches the server (for a later re-ask) but no banner will ever show, and
    /// the settings screen says so rather than pretending push works.
    public private(set) var authorizationGranted: Bool?
    /// Whether the app even asked yet — distinguishes "declined" from "not asked".
    public private(set) var authorizationAsked = false
    public private(set) var status: PushStatus?
    /// The preferences the screen edits. Loaded from the local mirror on init;
    /// there is **no server route that returns a device's stored preferences**
    /// (`GET /api/v1/push/status` returns the pipeline's health and a
    /// `registered` bool, nothing about rules or quiet hours — reported in
    /// `ios/README.md`), so this local copy is the display truth and every save
    /// POSTs the whole set and trusts the server's echo.
    public private(set) var settings: PushSettings
    /// A hard snooze's end, if one is set — the "muted until 14:30" the screen
    /// shows. The server holds the authoritative `muted_until`; this is the echo
    /// of the last mute this app set.
    public private(set) var mutedUntil: Date?

    private let client: OrchestraClient
    private let defaults: UserDefaults
    /// The token last sent to THIS server, so a foreground that finds the same
    /// token does not re-POST for nothing, and a rotated one does.
    private var lastSentToken: String?

    private static let settingsKey = "sh.orchestra.push-settings"
    private static let tokenKey = "sh.orchestra.push-token"

    public init(client: OrchestraClient, defaults: UserDefaults = .standard) {
        self.client = client
        self.defaults = defaults
        self.settings = Self.loadSettings(defaults) ?? PushSettings()
        self.lastSentToken = defaults.string(forKey: Self.tokenKey)
    }

    // MARK: - authorization

    /// The adapter reports what the OS answered. Recorded so the settings screen
    /// can distinguish "not asked", "declined" and "granted" — three states with
    /// three different next actions.
    public func noteAuthorization(granted: Bool, asked: Bool = true) {
        authorizationAsked = asked
        authorizationGranted = granted
    }

    // MARK: - registration

    /// The token from `didRegisterForRemoteNotificationsWithDeviceToken`, already
    /// hex-encoded, plus the environment the build was signed for. Idempotent by
    /// token: re-registering the same token is skipped unless it never confirmed.
    ///
    /// `force` re-sends even an unchanged token — used on a deliberate
    /// re-registration (a foreground after a long background) where the tz offset
    /// may have moved even though the token did not.
    public func register(tokenHex: String, environment: String,
                         tzOffsetMin: Int = PushStore.currentTZOffsetMin(),
                         force: Bool = false) async {
        let unchanged = tokenHex == lastSentToken
        if unchanged, !force, case .registered = registration { return }
        registration = .registering
        do {
            let reply = try await client.registerPush(
                token: tokenHex, environment: environment, tzOffsetMin: tzOffsetMin,
                appVersion: Self.appVersion, settings: settings.settingsBody)
            if reply.ok {
                lastSentToken = tokenHex
                defaults.set(tokenHex, forKey: Self.tokenKey)
                registration = .registered(environment: reply.environment ?? environment,
                                           warnings: reply.warnings)
            } else {
                registration = .failed("the server did not accept the token")
            }
        } catch let error as OrchestraError {
            registration = .failed(Self.message(for: error))
        } catch {
            registration = .failed(ErrnoCause.classify(error).headline)
        }
    }

    /// APNs itself refused to hand out a token (`didFailToRegister…`). Recorded
    /// verbatim — on a simulator this is often "remote notifications are not
    /// supported", which is a statement about the simulator, not a bug.
    public func noteRegistrationFailure(_ description: String) {
        registration = .failed(description)
    }

    // MARK: - status

    @discardableResult
    public func refreshStatus() async -> Bool {
        do {
            status = try await client.pushStatus()
            return true
        } catch {
            // A status read that fails leaves the last-known status on screen —
            // it is diagnostic, never load-bearing, so a transport hiccup must
            // not blank it.
            return false
        }
    }

    // MARK: - preferences

    /// Persist locally and POST. The server echoes the merged set back, which is
    /// adopted so the screen shows what the server now holds, not what was asked.
    /// Returns a message on refusal, nil on success.
    @discardableResult
    public func save(_ new: PushSettings) async -> String? {
        settings = new
        Self.persist(new, defaults)
        do {
            let reply = try await client.savePushSettings(body: new.settingsBody)
            if !reply.ok { return reply.text }
            return nil
        } catch let error as OrchestraError {
            return Self.message(for: error)
        } catch {
            return ErrnoCause.classify(error).headline
        }
    }

    /// A hard snooze. `minutes <= 0` clears it.
    @discardableResult
    public func mute(minutes: Double) async -> String? {
        do {
            _ = try await client.mutePush(minutes: minutes)
            mutedUntil = minutes > 0 ? Date().addingTimeInterval(minutes * 60) : nil
            return nil
        } catch let error as OrchestraError {
            return Self.message(for: error)
        } catch {
            return ErrnoCause.classify(error).headline
        }
    }

    /// Fire the server's end-to-end self-test. Returns the sink's own summary —
    /// including the `403 InvalidProviderToken` that a real transport with no
    /// registered key correctly returns, which is a working pipeline reporting an
    /// unregistered credential, not a failure.
    public func sendTest() async -> String {
        do {
            let reply = try await client.testPush()
            await refreshStatus()
            return reply.text
        } catch let error as OrchestraError {
            return Self.message(for: error)
        } catch {
            return ErrnoCause.classify(error).headline
        }
    }

    // MARK: - inline reply

    /// Answer an agent from a notification. Addressed by sid alone; the outcome
    /// is classified exactly like a chat send, so a tmux half-failure is
    /// `ambiguous` and never offered as a clean success.
    public func replyFromNotification(_ target: PushReplyTarget,
                                      text: String) async -> Outgoing.State {
        let clean = WireText.collapsed(text)
        guard !clean.isEmpty else { return .refused("empty message") }
        do {
            let reply = try await client.reply(sid: target.sid, worktree: target.worktree,
                                               text: clean)
            switch Actuation.outcome(ofSend: reply) {
            case .succeeded: return .typed(reply.text)
            case .refused: return .refused(reply.text)
            case .ambiguous(let why): return .ambiguous(reply.text + " — " + why)
            case .indeterminate: return .lost(Actuation.indeterminateCopy(for: .send))
            }
        } catch let error as OrchestraError {
            if case .cancelled = error { return .lost(Actuation.indeterminateCopy(for: .send)) }
            return .lost(error.headline + " — " + Actuation.indeterminateCopy(for: .send))
        } catch {
            return .lost(Actuation.indeterminateCopy(for: .send))
        }
    }

    // MARK: - helpers

    /// A refusal message worth showing. `422 push_token_invalid` is the one the
    /// registration path can hit; its server sentence ("token must be 64–200 hex
    /// characters") is more useful than "the server said 422".
    private static func message(for error: OrchestraError) -> String {
        switch error {
        case .http(_, let refusal): refusal?.message ?? error.headline
        case .unauthorized(let r), .forbidden(let r): r?.message ?? error.headline
        default: error.guidance.isEmpty ? error.headline : error.headline
        }
    }

    public static func currentTZOffsetMin() -> Int {
        TimeZone.current.secondsFromGMT() / 60
    }

    static var appVersion: String? {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
    }

    // MARK: - local mirror

    private static func loadSettings(_ defaults: UserDefaults) -> PushSettings? {
        guard let data = defaults.data(forKey: settingsKey),
              let stored = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return PushSettings(fromStored: stored)
    }

    private static func persist(_ s: PushSettings, _ defaults: UserDefaults) {
        // Store in the same shape `set_push` writes, so `PushSettings(fromStored:)`
        // reads it back symmetrically.
        var obj: [String: Any] = ["rules": s.rules,
                                  "quiet_hours": s.quietHours.wireBody,
                                  "privacy": s.privacy.rawValue,
                                  "nudge_min": s.nudgeMin]
        obj["_v"] = 1
        if let data = try? JSONSerialization.data(withJSONObject: obj) {
            defaults.set(data, forKey: settingsKey)
        }
    }
}
