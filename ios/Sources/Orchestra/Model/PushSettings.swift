import Foundation

// The device's notification preferences, modelled from what the server STORES
// and HONOURS — `notify.Preferences` and `prefs_from_device`, read directly —
// not from UX.md §8. A toggle that the server does not read is a toggle that
// lies, so every field here maps to one `set_push` key the pipeline actually
// consults:
//
//   quiet_hours  -> {enabled, from, to, allow_p1}     notify.quiet_now
//   rules        -> {"session.needs_answer": Bool, …}  Preferences.wants
//   privacy      -> "structural" | "detail"            compose(privacy=…)
//   nudge_min    -> Int                                 Preferences.nudge_min
//
// Filtering delivered pushes ON the phone is impossible — the payload is already
// on the lock screen by the time any code runs — so all of this is SERVER state.
// The screen edits a copy and POSTs the whole thing; the server is the one place
// it is true.

/// One notifiable event type, as the server names it, with the default it
/// applies when the device has said nothing. The defaults are `EVENT_TYPES[…]
/// ["default"]` verbatim — `session.your_turn` and `worktree.free` are OFF, and
/// that asymmetry is the product: you are told when an agent needs you, not
/// every time one goes idle.
public enum PushEventType: String, Sendable, CaseIterable, Identifiable {
    case needsAnswer = "session.needs_answer"
    case blocked = "session.blocked"
    case yourTurn = "session.your_turn"
    case limitHit = "account.limit_hit"
    case limitReset = "account.limit_reset"
    case resumeArmed = "resume.armed"
    case resumeFired = "resume.fired"
    case resumeFailed = "resume.failed"
    case dispatchSucceeded = "dispatch.succeeded"
    case dispatchFailed = "dispatch.failed"
    case worktreeFree = "worktree.free"
    case sessionDied = "session.died"

    public var id: String { rawValue }

    /// `EVENT_TYPES[type]["default"]`, mirrored exactly. A test pins every one of
    /// these against the Python table (`PushSettingsTests`) so the two cannot
    /// drift into disagreeing about what "off by default" means.
    public var defaultOn: Bool {
        switch self {
        case .yourTurn, .resumeArmed, .worktreeFree: false
        default: true
        }
    }

    /// P1 / P2 / P3, for the badge the row shows so "off by default" reads next
    /// to "and it would have been a quiet P3 anyway".
    public var level: String {
        switch self {
        case .needsAnswer, .blocked, .resumeFailed, .dispatchFailed: "P1"
        case .yourTurn, .limitHit, .resumeFired, .sessionDied: "P2"
        case .limitReset, .resumeArmed, .dispatchSucceeded, .worktreeFree: "P3"
        }
    }

    /// A short human label for the row.
    public var label: String {
        switch self {
        case .needsAnswer: "Needs an answer"
        case .blocked: "Blocked / permission"
        case .yourTurn: "Your turn"
        case .limitHit: "Limit hit"
        case .limitReset: "Limit reset"
        case .resumeArmed: "Auto-resume armed"
        case .resumeFired: "Auto-resume fired"
        case .resumeFailed: "Auto-resume failed"
        case .dispatchSucceeded: "Dispatch succeeded"
        case .dispatchFailed: "Dispatch failed"
        case .worktreeFree: "Worktree freed"
        case .sessionDied: "Session died"
        }
    }
}

/// The nightly quiet window, evaluated by the server in the DEVICE's zone
/// (`notify.quiet_now`). Times are `"HH:mm"`, device-local; the offset that
/// makes "the phone's night" mean the phone's night rides registration, not this.
///
/// `allowP1` is the one that matters at 2 a.m.: with it on, a blocked agent still
/// reaches you through quiet hours; with it off, quiet means quiet.
public struct QuietHours: Sendable, Equatable, Codable {
    public var enabled: Bool
    public var from: String
    public var to: String
    public var allowP1: Bool

    public init(enabled: Bool = false, from: String = "23:00",
                to: String = "08:00", allowP1: Bool = true) {
        self.enabled = enabled
        self.from = from
        self.to = to
        self.allowP1 = allowP1
    }

    /// The `quiet_hours` object `set_push` stores, keyed exactly as
    /// `prefs_from_device` reads it (`from`/`to`/`allow_p1`, gated on `enabled`).
    public var wireBody: [String: Any] {
        ["enabled": enabled, "from": from, "to": to, "allow_p1": allowP1]
    }
}

/// The privacy of what rides Apple's servers. `structural` — the default — puts
/// the glyph, worktree and status on the lock screen and nothing else; the prose
/// is fetched over the tailnet and never transits Apple. `detail` sends the
/// transcript line in the payload — fewer round trips, more on Apple's disks.
public enum PushPrivacy: String, Sendable, Equatable, CaseIterable {
    case structural
    case detail

    public var label: String {
        switch self {
        case .structural: "Identifiers only"
        case .detail: "Include the message"
        }
    }

    public var explanation: String {
        switch self {
        case .structural:
            "The lock screen shows the worktree, the status and a glyph. The "
            + "actual message is fetched over the tailnet and never touches Apple."
        case .detail:
            "The message text is put in the notification itself. Fewer steps, but "
            + "it passes through Apple's servers to reach your phone."
        }
    }
}

/// The whole editable preference set. A value; the store holds one, the screen
/// binds to a copy, and `settingsBody` is what a Save POSTs.
public struct PushSettings: Sendable, Equatable {
    /// Explicit per-type overrides. A type ABSENT here means "use the server
    /// default", which is not the same as false — so this is a sparse map, not a
    /// full one, exactly like `Preferences.rules`.
    public var rules: [String: Bool]
    public var quietHours: QuietHours
    public var privacy: PushPrivacy
    /// How long a stalled closeout waits before it nudges, in minutes.
    public var nudgeMin: Int

    public init(rules: [String: Bool] = [:], quietHours: QuietHours = QuietHours(),
                privacy: PushPrivacy = .structural, nudgeMin: Int = 15) {
        self.rules = rules
        self.quietHours = quietHours
        self.privacy = privacy
        self.nudgeMin = nudgeMin
    }

    /// Whether a type is on, honouring an explicit override and otherwise the
    /// type's own default — the client mirror of `Preferences.wants`, so the
    /// toggle shows the same answer the server would give.
    public func isOn(_ type: PushEventType) -> Bool {
        rules[type.rawValue] ?? type.defaultOn
    }

    /// Set a type on or off. Kept SPARSE: setting a type back to its default
    /// removes the override rather than pinning it, so a later change to a
    /// default is inherited rather than frozen — the server does the same by
    /// falling through an absent key.
    public mutating func set(_ type: PushEventType, on: Bool) {
        if on == type.defaultOn {
            rules.removeValue(forKey: type.rawValue)
        } else {
            rules[type.rawValue] = on
        }
    }

    /// The body for `POST /api/v1/devices/self/settings`. Only the four keys that
    /// route accepts (`quiet_hours`, `rules`, `privacy`, `nudge_min`); anything
    /// else is dropped by `set_push`'s allow-list anyway, and sending it would
    /// imply it does something.
    public var settingsBody: [String: Any] {
        ["quiet_hours": quietHours.wireBody,
         "rules": rules,
         "privacy": privacy.rawValue,
         "nudge_min": nudgeMin]
    }

    /// Rebuild from a device's stored `push` object as `GET /api/v1/push/status`
    /// does not return it — so this decodes the shape `set_push` wrote, used when
    /// the settings screen loads what the server already holds.
    public init(fromStored push: [String: Any]) {
        var rules: [String: Bool] = [:]
        if let r = push["rules"] as? [String: Any] {
            for (k, v) in r where !(k.hasPrefix("_")) {
                if let b = v as? Bool { rules[k] = b }
            }
        }
        self.rules = rules
        let q = (push["quiet_hours"] as? [String: Any]) ?? [:]
        self.quietHours = QuietHours(
            enabled: (q["enabled"] as? Bool) ?? false,
            from: (q["from"] as? String) ?? "23:00",
            to: (q["to"] as? String) ?? "08:00",
            allowP1: (q["allow_p1"] as? Bool) ?? true)
        self.privacy = PushPrivacy(rawValue: (push["privacy"] as? String) ?? "structural") ?? .structural
        self.nudgeMin = (push["nudge_min"] as? Int) ?? 15
    }
}
