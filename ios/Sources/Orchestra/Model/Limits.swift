import Foundation

/// `GET /api/limits`, modelled from a live capture on 2026-07-22 (5,831 B, six
/// accounts).
///
/// **Two fields called `generated_at` on this API are different types.**
/// `/api/state.generated_at` is a float epoch; this one is an **ISO-8601
/// string** on the real path (it comes straight out of `cclimits`) and `null` in
/// demo mode (`limits.demo_limits`). A client that reached for the same decoder
/// for both gets `typeMismatch` and loses the screen. It is decoded as a string
/// here, and `fetched_at` — which IS a float epoch, written by orchestra itself
/// — is what the "fetched 4m ago" line uses.
public struct LimitsReport: Sendable, Equatable, Decodable {
    /// False when `cclimits` is missing or failed. Then `accounts` is empty and
    /// `error` is the whole screen — `UX.md` §3.7's whole-page error state.
    public let available: Bool
    public let error: String?
    /// orchestra's own clock, when this was fetched. The one usable timestamp.
    public let fetchedAt: Double?
    /// `cclimits`'s own stamp, ISO-8601, or absent. Display only.
    public let generatedAt: String?
    public let accounts: [AccountLimits]

    public init(available: Bool, error: String?, fetchedAt: Double?,
                generatedAt: String?, accounts: [AccountLimits]) {
        self.available = available
        self.error = error
        self.fetchedAt = fetchedAt
        self.generatedAt = generatedAt
        self.accounts = accounts
    }

    enum CodingKeys: String, CodingKey {
        case available, error, accounts
        case fetchedAt = "fetched_at"
        case generatedAt = "generated_at"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = try c.decodeIfPresent(Bool.self, forKey: .available) ?? false
        error = try c.decodeIfPresent(String.self, forKey: .error)
        fetchedAt = try c.decodeIfPresent(Double.self, forKey: .fetchedAt)
        generatedAt = try c.decodeIfPresent(String.self, forKey: .generatedAt)
        accounts = try c.decodeIfPresent([AccountLimits].self, forKey: .accounts) ?? []
    }

    public var fetched: Date? { fetchedAt.map { Date(timeIntervalSince1970: $0) } }

    /// Most headroom first. The board's own auto-pick ranks the same way, so the
    /// top row is the account a dispatch would land on.
    public var ranked: [AccountLimits] {
        accounts.sorted { ($0.headroomPercent ?? -1) > ($1.headroomPercent ?? -1) }
    }
}

public struct AccountLimits: Sendable, Equatable, Decodable, Identifiable {
    /// `cclimits`'s slug — NOT orchestra's account label. The two disagree
    /// routinely (`default` here is `main` on the board), which is exactly why
    /// `fb_label` exists.
    public let slug: String
    /// **orchestra's own label**, and the only one that matches a session's
    /// `account` on the board. Absent when the account has no `config_dir`.
    public let fbLabel: String?
    public let email: String?
    public let plan: String?
    public let configDir: String?
    /// False when this one account could not be read; `error` says why.
    public let ok: Bool
    public let error: String?
    /// The minimum remaining across the account's non-model-scoped limits.
    public let headroomPercent: Double?
    public let limits: [LimitBar]
    /// The buffer auto-dispatch keeps free. Absent when there is no `config_dir`.
    public let reservePercent: Int?
    /// Headroom is below the reserve: auto-dispatch will not pick this account,
    /// **but a person still can**. Saying only the first half is the copy trap.
    public let reserveBlocked: Bool

    public var id: String { slug }

    public init(slug: String, fbLabel: String?, email: String?, plan: String?,
                configDir: String?, ok: Bool, error: String?, headroomPercent: Double?,
                limits: [LimitBar], reservePercent: Int?, reserveBlocked: Bool) {
        self.slug = slug
        self.fbLabel = fbLabel
        self.email = email
        self.plan = plan
        self.configDir = configDir
        self.ok = ok
        self.error = error
        self.headroomPercent = headroomPercent
        self.limits = limits
        self.reservePercent = reservePercent
        self.reserveBlocked = reserveBlocked
    }

    enum CodingKeys: String, CodingKey {
        case slug, email, plan, ok, error, limits
        case fbLabel = "fb_label"
        case configDir = "config_dir"
        case headroomPercent = "headroom_percent"
        case reservePercent = "reserve_percent"
        case reserveBlocked = "reserve_blocked"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        slug = try c.decodeIfPresent(String.self, forKey: .slug) ?? "?"
        fbLabel = try c.decodeIfPresent(String.self, forKey: .fbLabel)
        email = try c.decodeIfPresent(String.self, forKey: .email)
        plan = try c.decodeIfPresent(String.self, forKey: .plan)
        configDir = try c.decodeIfPresent(String.self, forKey: .configDir)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        error = try c.decodeIfPresent(String.self, forKey: .error)
        headroomPercent = try c.decodeIfPresent(Double.self, forKey: .headroomPercent)
        limits = try c.decodeIfPresent([LimitBar].self, forKey: .limits) ?? []
        reservePercent = try c.decodeIfPresent(Int.self, forKey: .reservePercent)
        reserveBlocked = try c.decodeIfPresent(Bool.self, forKey: .reserveBlocked) ?? false
    }

    /// What the board calls this account. Falls back to the `cclimits` slug so a
    /// row is never nameless.
    public var label: String { fbLabel ?? slug }

    /// Any limit that is out right now — including a model-scoped one, which
    /// does NOT block the account and must not be rendered as if it did.
    public var exhausted: [LimitBar] { limits.filter(\.exhaustedNow) }

    /// The account itself is out only when a limit that is not model-scoped is.
    public var accountExhausted: Bool {
        limits.contains { $0.exhaustedNow && !$0.modelScoped }
    }
}

/// One bar. `percent` is used, `remaining_percent` is left — the server ships
/// both and they are not always exact complements, so neither is derived here.
public struct LimitBar: Sendable, Equatable, Decodable, Identifiable {
    public let label: String
    /// `session` or `weekly`.
    public let group: String
    public let percent: Double
    public let remainingPercent: Double?
    /// A model cap. **Exhausting one blocks only sessions running that model;
    /// the account is still usable.** Collapsing that distinction is an explicit
    /// anti-goal in the server and in `UX.md` §3.7.
    public let modelScoped: Bool
    public let exhaustedNow: Bool
    /// Absolute epoch — countdowns never leave the server
    /// (`limits._absolutise_resets`). Null for a limit with no reset.
    public let resetsAt: Double?

    public var id: String { "\(group)/\(label)" }

    public init(label: String, group: String, percent: Double, remainingPercent: Double?,
                modelScoped: Bool, exhaustedNow: Bool, resetsAt: Double?) {
        self.label = label
        self.group = group
        self.percent = percent
        self.remainingPercent = remainingPercent
        self.modelScoped = modelScoped
        self.exhaustedNow = exhaustedNow
        self.resetsAt = resetsAt
    }

    enum CodingKeys: String, CodingKey {
        case label, group, percent
        case remainingPercent = "remaining_percent"
        case modelScoped = "model_scoped"
        case exhaustedNow = "exhausted_now"
        case resetsAt = "resets_at"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? "?"
        group = try c.decodeIfPresent(String.self, forKey: .group) ?? ""
        percent = try c.decodeIfPresent(Double.self, forKey: .percent) ?? 0
        remainingPercent = try c.decodeIfPresent(Double.self, forKey: .remainingPercent)
        modelScoped = try c.decodeIfPresent(Bool.self, forKey: .modelScoped) ?? false
        exhaustedNow = try c.decodeIfPresent(Bool.self, forKey: .exhaustedNow) ?? false
        resetsAt = try c.decodeIfPresent(Double.self, forKey: .resetsAt)
    }

    public var resets: Date? { resetsAt.map { Date(timeIntervalSince1970: $0) } }
    /// Clamped for drawing only. The number printed is the server's.
    public var fraction: Double { min(1, max(0, percent / 100)) }
}
