import Foundation

// Push, modelled from what the SERVER actually emits — `notify.compose` in the
// Python package, read field by field, not from API.md §9.23 (which describes a
// payload with `expect_sid`, an `intent_id` and a `phase`, none of which ride
// this wire). Every shape here is decode-tested against a literal copy of a
// `compose` payload in `PushTests`.
//
// The one thing `compose` does NOT put on the wire is a `category`, and inline
// reply cannot exist without one: iOS decides whether to hang a text field on a
// banner from `aps.category`, at delivery, before the user touches it. So the
// category is DERIVED here from `ev`/`level` — a pure function of the payload —
// and stamped onto the notification by the service extension (`mutable-content:
// 1` is set by `compose` for exactly this) or, failing that, carried in the
// payload directly. Where it comes from is a transport detail; what it must be
// is `PushMessage.categoryID`, and that is decided here so one rule serves the
// banner, the extension and the tests.

// MARK: - the incoming payload

/// One decoded APNs payload — the JSON body `notify.compose` builds, minus the
/// `aps` envelope iOS consumes itself.
///
/// A value with no I/O and no clock, so the whole routing decision — where a tap
/// lands, whether a banner can be replied to — is testable without a simulator
/// and without Apple.
public struct PushMessage: Sendable, Equatable {
    /// `ev` — the event type, e.g. `session.needs_answer`. The switch that
    /// decides category and copy keys on this, so an unknown value is kept
    /// verbatim rather than folded to a default that would silently mis-route.
    public let event: String
    /// `event_id` — the durable id in the server's `EventLog`. The extension
    /// fetches prose with it (`GET /api/v1/events/<id>`); the app reconciles
    /// against it. Nil on a hand-crafted probe.
    public let eventID: String?
    /// `dedupe_key` — collapses a self-superseding state on the lock screen.
    public let dedupeKey: String?
    /// `at` — the epoch the edge was derived. Absolute, never a duration.
    public let at: Double?
    /// `wt` — the worktree the event is about. Null for account-level events
    /// (`account.limit_hit`), which is why the deep link degrades to the board.
    public let worktree: String?
    /// `sid` — the session. **The only address inline reply has** — the payload
    /// carries no account, and it does not need to: `/api/send` resolves a bare
    /// sid to a live process (`identity.resolve`), and an account would only be
    /// a corroborator it does not have.
    public let sid: String?
    /// `level` — P1 / P2 / P3. Decides the reply affordance for events whose
    /// type this build does not recognise.
    public let level: String?
    /// `aps.alert.title` / `.subtitle` / `.body`. Title and subtitle always ride
    /// the wire; body only when the device opted into `privacy: "detail"`,
    /// otherwise the extension fills it over the tailnet.
    public let title: String?
    public let subtitle: String?
    public let body: String?

    public init(event: String, eventID: String? = nil, dedupeKey: String? = nil,
                at: Double? = nil, worktree: String? = nil, sid: String? = nil,
                level: String? = nil, title: String? = nil, subtitle: String? = nil,
                body: String? = nil) {
        self.event = event
        self.eventID = eventID
        self.dedupeKey = dedupeKey
        self.at = at
        self.worktree = worktree
        self.sid = sid
        self.level = level
        self.title = title
        self.subtitle = subtitle
        self.body = body
    }

    /// Decode from the `userInfo` dictionary iOS hands a notification. It is
    /// `[AnyHashable: Any]` — untyped — so this reads defensively and never
    /// throws: a payload it cannot fully parse still deep-links on whatever
    /// addresses it did find, because a tap that lands on the board beats a tap
    /// that lands nowhere.
    public init?(userInfo: [AnyHashable: Any]) {
        guard let event = userInfo["ev"] as? String else { return nil }
        self.event = event
        self.eventID = userInfo["event_id"] as? String
        self.dedupeKey = userInfo["dedupe_key"] as? String
        self.at = (userInfo["at"] as? NSNumber)?.doubleValue ?? (userInfo["at"] as? Double)
        self.worktree = userInfo["wt"] as? String
        self.sid = userInfo["sid"] as? String
        self.level = userInfo["level"] as? String
        if let aps = userInfo["aps"] as? [AnyHashable: Any],
           let alert = aps["alert"] as? [AnyHashable: Any] {
            self.title = alert["title"] as? String
            self.subtitle = alert["subtitle"] as? String
            self.body = alert["body"] as? String
        } else {
            self.title = nil
            self.subtitle = nil
            self.body = nil
        }
    }

    /// The notification category, decided from the event so one rule serves the
    /// banner, the service extension and the tests.
    ///
    /// **A category earns the reply field only when a typed answer would reach
    /// the agent and mean something.** `session.needs_answer` and
    /// `session.blocked` are exactly those: an agent stopped on a question or a
    /// permission prompt, where "yes", "1", or a sentence is the whole
    /// interaction. Everything else — a limit reset, a freed worktree, a resume
    /// that fired — is a fact to read, not a prompt to answer, and a reply field
    /// on it would type into whatever the agent is doing next.
    public var categoryID: String {
        Self.isAnswerable(event: event) ? PushCategory.reply : PushCategory.info
    }

    /// Whether a typed reply would reach a waiting agent. A `nil`/unknown event
    /// is treated as NOT answerable: inventing a reply target for a payload this
    /// build does not understand is how a message ends up typed at the wrong
    /// prompt.
    public static func isAnswerable(event: String?) -> Bool {
        switch event {
        case "session.needs_answer", "session.blocked": true
        default: false
        }
    }

    /// Where a tap should land. The payload addresses a session (`sid`) inside a
    /// worktree (`wt`); the account it needs to open the conversation is not on
    /// the wire and is resolved from the live board by sid at the point of
    /// navigation. So the deep link is expressed in exactly what the payload
    /// knows, and the UI completes it.
    public var deepLink: PushDeepLink {
        PushDeepLink(worktree: worktree, sid: sid)
    }

    /// The reply this notification can carry, if any — the address `/api/send`
    /// needs, with the account deliberately absent (see `sid`).
    public var replyTarget: PushReplyTarget? {
        guard Self.isAnswerable(event: event), let sid, !sid.isEmpty else { return nil }
        return PushReplyTarget(sid: sid, worktree: worktree)
    }
}

/// The registered category identifiers. Two, because a notification is either a
/// prompt you can answer or a fact you read.
public enum PushCategory {
    /// Carries a `UNTextInputNotificationAction` — the inline reply. This is the
    /// single most valuable thing a phone does here: answer an agent from the
    /// banner without opening the app.
    public static let reply = "ORC_REPLY"
    /// No actions. A tap opens the app to the relevant screen; there is nothing
    /// to type.
    public static let info = "ORC_INFO"

    /// The action id iOS returns for a submitted inline reply.
    public static let replyAction = "ORC_REPLY_SEND"
}

/// Where a notification tap navigates. Expressed in the payload's own addresses
/// — a worktree and maybe a session — because the account the chat screen needs
/// is not on the wire and is resolved from the board later.
///
/// A pure value in the non-UI module so it can be tested; the UI maps it to a
/// `FleetRoute` once it can look the account up.
public struct PushDeepLink: Sendable, Equatable, Hashable {
    public let worktree: String?
    public let sid: String?

    public init(worktree: String?, sid: String?) {
        self.worktree = worktree
        self.sid = sid
    }

    /// Nothing to navigate to — an account-level event with no worktree. The tap
    /// opens the board, which is where an account question is answered anyway.
    public var isBoardOnly: Bool { (worktree ?? "").isEmpty }
}

/// The address an inline reply is sent to. `account` is intentionally not here:
/// the notification never carried one and `/api/send` does not need it.
public struct PushReplyTarget: Sendable, Equatable {
    public let sid: String
    public let worktree: String?

    public init(sid: String, worktree: String?) {
        self.sid = sid
        self.worktree = worktree
    }
}

// MARK: - registration

/// `POST /api/v1/devices/self/push` — 200. The server echoes back which host it
/// will send through and any warning it wants the device to see.
///
/// The one warning that matters: `time_sensitive_allowed: false` means a P1 —
/// the 2 a.m. blocked-agent case this whole feature exists for — will be
/// suppressed by any Focus, including Sleep. Surfaced, never swallowed.
public struct RegisterPushReply: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let backend: String?
    public let environment: String?
    public let warnings: [String]

    public init(ok: Bool, backend: String?, environment: String?, warnings: [String] = []) {
        self.ok = ok
        self.backend = backend
        self.environment = environment
        self.warnings = warnings
    }

    enum CodingKeys: String, CodingKey { case ok, backend, environment, warnings }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        backend = try c.decodeIfPresent(String.self, forKey: .backend)
        environment = try c.decodeIfPresent(String.self, forKey: .environment)
        warnings = try c.decodeIfPresent([String].self, forKey: .warnings) ?? []
    }
}

/// `GET /api/v1/push/status` — what the settings screen shows. `push` is the
/// sink's own health: a `NoopSink` names the missing config key, an `APNsSink`
/// names a bad Key ID or a missing binary. `registered` is whether THIS device
/// has a token on file.
public struct PushStatus: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let registered: Bool
    public let backend: String
    public let ready: Bool
    public let problems: [String]
    public let environment: String?
    /// The last send's outcome, e.g. `"200"` or `"403 InvalidProviderToken"` —
    /// a push that silently stopped after a restore is otherwise a quiet fleet,
    /// found a week late.
    public let last: String?

    public init(ok: Bool, registered: Bool, backend: String, ready: Bool,
                problems: [String] = [], environment: String? = nil, last: String? = nil) {
        self.ok = ok
        self.registered = registered
        self.backend = backend
        self.ready = ready
        self.problems = problems
        self.environment = environment
        self.last = last
    }

    enum RootKeys: String, CodingKey { case ok, registered, push }
    enum PushKeys: String, CodingKey {
        case backend, ready, problems, environment, last
    }

    public init(from decoder: any Decoder) throws {
        let root = try decoder.container(keyedBy: RootKeys.self)
        ok = try root.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        registered = try root.decodeIfPresent(Bool.self, forKey: .registered) ?? false
        let push = try root.nestedContainer(keyedBy: PushKeys.self, forKey: .push)
        backend = try push.decodeIfPresent(String.self, forKey: .backend) ?? "none"
        ready = try push.decodeIfPresent(Bool.self, forKey: .ready) ?? false
        problems = try push.decodeIfPresent([String].self, forKey: .problems) ?? []
        environment = try push.decodeIfPresent(String.self, forKey: .environment)
        // `last` is a nested object on the APNs sink and absent on the noop
        // sink; the settings screen only wants the one-line summary, and the
        // server's own `/push/status` collapses it, so read a string and shrug
        // at anything richer.
        last = try? push.decodeIfPresent(String.self, forKey: .last)
    }
}
