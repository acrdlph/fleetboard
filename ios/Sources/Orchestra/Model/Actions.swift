import Foundation

// The four mutations, modelled from a LIVE server on 2026-07-22 rather than from
// API.md or UX.md §7 — both of which describe a contract this server does not
// have. Every shape below was produced by an actual request against
// `100.113.110.31:4269` and is quoted in `ios/README.md`'s findings table.
//
// **The single most important fact about this file: a refusal is HTTP 200.**
//
//     POST /api/send     {"pid": 93506, "text": "x"}
//     → 200 {"ok": false, "error": "unaddressed", "message": "refusing to act…"}
//
//     POST /api/dispatch {"mission": "probe"}
//     → 200 {"ok": false, "message": "pick a model and an effort first…"}
//
//     POST /api/finish   {"worktree": "nope"}
//     → 200 {"ok": false, "message": "unknown worktree 'nope'"}
//
// `server.do_POST` builds the body from the module's return value and then
// writes `send_response(200)` unconditionally. The ONE status that is not 200 is
// **415 `content_type_required`** for a POST without `Content-Type:
// application/json`, which is the CSRF guard. So a client that branches on the
// status line sees success for every refusal in the app, and the `ok` field is
// the status.

/// The common shape of a mutation's answer: `{"ok": …, "message": …}`, plus the
/// machine-readable `error` code the identity guard adds.
///
/// `message` is carried verbatim to the screen and is never replaced with a
/// generic string. These messages are the whole UX of a refusal — *"refusing to
/// act on pid 93506 alone — pids are recycled, so a bare pid can name a
/// different agent by the time the click lands (ADR 0008)"* is a sentence a
/// client cannot improve on and must not summarise.
public struct ActionReply: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let message: String?
    /// `identity.py` sets this on the two refusals it owns: `unaddressed` and
    /// `identity_gone`. Absent everywhere else — `terminal.send_to_process`'s own
    /// failures ("empty message", "tmux send-keys failed") carry prose only.
    public let error: String?

    public init(ok: Bool, message: String?, error: String? = nil) {
        self.ok = ok
        self.message = message
        self.error = error
    }

    enum CodingKeys: String, CodingKey { case ok, message, error }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message)
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }

    /// What to put on screen. Never empty, never invented when the server spoke.
    public var text: String {
        if let message, !message.isEmpty { return message }
        return ok ? "done" : "the server refused, without saying why"
    }

    /// The identity guard refused: the agent this was addressed to is not the
    /// agent that would have been typed at. Both codes mean **nothing was
    /// typed**, which is what makes them safe to offer a re-address for.
    public var isIdentityRefusal: Bool {
        error == "identity_gone" || error == "unaddressed"
    }
}

// MARK: - send

/// `POST /api/send {account, sid, text}` → `{"ok": …, "message": …}`.
///
/// **`ok: false` does not always mean "nothing was typed".** `send_to_process`
/// has three delivery paths and they differ:
///
/// | path | `ok: false` means |
/// |---|---|
/// | osascript (Terminal/iTerm2) | the AppleScript did not report `true` — nothing landed |
/// | **tmux** | `ok = rc1 == 0 and rc2 == 0`, and those are two calls: `send-keys -l <text>` then `send-keys Enter`. **A failure of the second leaves the text sitting in the composer, un-submitted.** |
/// | unscriptable host | refused before anything was attempted |
///
/// So a tmux failure is *indeterminate*, not a clean "no". `Actuation` encodes
/// that, and the UI says so rather than offering a retry that would type the
/// message a second time under it.
public typealias SendReply = ActionReply

// MARK: - dispatch

/// `POST /api/dispatch` answers with **one of two entirely different bodies**.
///
/// * `{"job": "job-214849-1"}` — accepted; the work runs on a background thread
///   and is polled at `GET /api/dispatch/status?job=…`. Note there is **no
///   `ok`** on this branch at all.
/// * `{"ok": false, …}` — refused synchronously, either for a missing
///   model/effort or for the headroom dialog (`needs_decision`).
///
/// Modelled as a two-case enum rather than one struct full of optionals, because
/// they are two outcomes and the call site has to branch on which.
public enum DispatchStart: Sendable, Equatable, Decodable {
    case accepted(job: String)
    case refused(DispatchRefusal)

    enum CodingKeys: String, CodingKey { case job }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        if let job = try c.decodeIfPresent(String.self, forKey: .job), !job.isEmpty {
            self = .accepted(job: job)
        } else {
            self = .refused(try DispatchRefusal(from: decoder))
        }
    }
}

/// The synchronous refusal, including `needs_decision` — the headroom dialog.
///
/// Driven live: `{"mission": "probe", "model": "haiku", "effort": "low",
/// "account": "no-such-account"}` answers
///
/// ```json
/// {"ok": false, "needs_decision": true, "model": "haiku",
///  "message": "No haiku headroom on account [no-such-account] — no readable
///              account for this model.",
///  "can_opus": false, "opus_account": null, "opus_left": null}
/// ```
public struct DispatchRefusal: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let message: String?
    /// True when the refusal is the reserve dialog rather than a bad request.
    /// This is the ONLY refusal `force_model: true` can override.
    public let needsDecision: Bool
    public let model: String?
    /// Whether any account could run `opus` instead — the sheet's primary action.
    public let canOpus: Bool
    public let opusAccount: String?
    public let opusLeft: Double?

    public init(ok: Bool = false, message: String?, needsDecision: Bool = false,
                model: String? = nil, canOpus: Bool = false,
                opusAccount: String? = nil, opusLeft: Double? = nil) {
        self.ok = ok
        self.message = message
        self.needsDecision = needsDecision
        self.model = model
        self.canOpus = canOpus
        self.opusAccount = opusAccount
        self.opusLeft = opusLeft
    }

    enum CodingKeys: String, CodingKey {
        case ok, message, model
        case needsDecision = "needs_decision"
        case canOpus = "can_opus"
        case opusAccount = "opus_account"
        case opusLeft = "opus_left"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message)
        needsDecision = try c.decodeIfPresent(Bool.self, forKey: .needsDecision) ?? false
        model = try c.decodeIfPresent(String.self, forKey: .model)
        canOpus = try c.decodeIfPresent(Bool.self, forKey: .canOpus) ?? false
        opusAccount = try c.decodeIfPresent(String.self, forKey: .opusAccount)
        opusLeft = try c.decodeIfPresent(Double.self, forKey: .opusLeft)
    }

    public var text: String { message ?? "the server refused the dispatch" }
}

/// `GET /api/dispatch/status?job=…`.
///
/// `{"ok": false, "error": "unknown job"}` for a job id the server has forgotten
/// — and it forgets: `_jobs` keeps the last 20 and is **in-memory only**, so a
/// server restart erases every job. There is no durable intent store, whatever
/// `UX.md` §3.4 says.
public struct DispatchJob: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let error: String?
    /// The `①②③④⑤` lines, verbatim, in order. Sub-lines are two-space prefixed
    /// ("  effort confirmed ✓") and the view indents on that.
    public let progress: [String]
    public let done: Bool
    public let result: DispatchResult?

    public init(ok: Bool, error: String? = nil, progress: [String] = [],
                done: Bool = false, result: DispatchResult? = nil) {
        self.ok = ok
        self.error = error
        self.progress = progress
        self.done = done
        self.result = result
    }

    enum CodingKeys: String, CodingKey { case ok, error, progress, done, result }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        error = try c.decodeIfPresent(String.self, forKey: .error)
        progress = try c.decodeIfPresent([String].self, forKey: .progress) ?? []
        done = try c.decodeIfPresent(Bool.self, forKey: .done) ?? false
        result = try c.decodeIfPresent(DispatchResult.self, forKey: .result)
    }
}

/// The terminal value of a dispatch job.
///
/// **`effort_confirmed` is genuinely tri-state.** `_run_dispatch` leaves it
/// `None` when no effort was asked for, sets `False` when `/effort` did not echo
/// "set effort level" back into the pane, and `True` when it did. A `Bool` with a
/// `?? false` default would render "UNCONFIRMED ⚠" for the case where nothing
/// was ever attempted.
public struct DispatchResult: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let message: String?
    /// The tmux session name, e.g. `mission-confidai7-214849`.
    public let session: String?
    public let worktree: String?
    public let account: String?
    public let model: String?
    public let effort: String?
    public let effortConfirmed: Bool?
    public let kickoffSent: Bool?
    /// The full brief that was typed, with the header the server prepends.
    public let kickoff: String?
    /// `tmux -L fleet attach -t <session>` — the one string worth copying.
    public let attach: String?

    public init(ok: Bool, message: String?, session: String? = nil,
                worktree: String? = nil, account: String? = nil, model: String? = nil,
                effort: String? = nil, effortConfirmed: Bool? = nil,
                kickoffSent: Bool? = nil, kickoff: String? = nil, attach: String? = nil) {
        self.ok = ok
        self.message = message
        self.session = session
        self.worktree = worktree
        self.account = account
        self.model = model
        self.effort = effort
        self.effortConfirmed = effortConfirmed
        self.kickoffSent = kickoffSent
        self.kickoff = kickoff
        self.attach = attach
    }

    enum CodingKeys: String, CodingKey {
        case ok, message, session, worktree, account, model, effort, kickoff, attach
        case effortConfirmed = "effort_confirmed"
        case kickoffSent = "kickoff_sent"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message)
        session = try c.decodeIfPresent(String.self, forKey: .session)
        worktree = try c.decodeIfPresent(String.self, forKey: .worktree)
        account = try c.decodeIfPresent(String.self, forKey: .account)
        model = try c.decodeIfPresent(String.self, forKey: .model)
        effort = try c.decodeIfPresent(String.self, forKey: .effort)
        effortConfirmed = try c.decodeIfPresent(Bool.self, forKey: .effortConfirmed)
        kickoffSent = try c.decodeIfPresent(Bool.self, forKey: .kickoffSent)
        kickoff = try c.decodeIfPresent(String.self, forKey: .kickoff)
        attach = try c.decodeIfPresent(String.self, forKey: .attach)
    }

    public var text: String { message ?? (ok ? "launched" : "the dispatch failed") }
}

// MARK: - finish

/// `POST /api/finish {worktree}` — synchronous, and it can take a minute.
///
/// `start_finish` runs `git fetch origin` (30 s timeout), a merge-base, a
/// `git status`, a full `claude_processes()` scan and then an osascript send.
/// There is no job id and no phase stream: `UX.md` §4.4's *"Finish returns an
/// `intent_id` immediately and phases stream"* describes a server that does not
/// exist. So the client shows an honest indeterminate elapsed counter and gives
/// the request a 120 s deadline.
///
/// `mode` is the whole outcome vocabulary and it is **absent** on three of the
/// early refusals (demo, unknown worktree, no trunk ref, and the
/// "terminal can't be scripted" case) — which is why it is optional.
public struct FinishReply: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let message: String?
    public let mode: FinishMode?
    /// Short reason the close is blocked: `"3 leftover file(s)"` or
    /// `"branch not landed on origin/main"`. Present on `pending`, `chat`, `nudge`.
    public let left: String?
    /// Up to five raw `git status --porcelain` lines.
    public let files: [String]
    /// The epoch the brief went to the agent — an **absolute** stamp, so the card
    /// counts up from it against its own clock rather than rendering an elapsed
    /// string the server computed and the phone read minutes later.
    public let sentAt: Double?

    public init(ok: Bool, message: String?, mode: FinishMode? = nil,
                left: String? = nil, files: [String] = [], sentAt: Double? = nil) {
        self.ok = ok
        self.message = message
        self.mode = mode
        self.left = left
        self.files = files
        self.sentAt = sentAt
    }

    enum CodingKeys: String, CodingKey {
        case ok, message, mode, left, files
        case sentAt = "sent"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message)
        mode = try c.decodeIfPresent(FinishMode.self, forKey: .mode)
        left = try c.decodeIfPresent(String.self, forKey: .left)
        files = try c.decodeIfPresent([String].self, forKey: .files) ?? []
        sentAt = try c.decodeIfPresent(Double.self, forKey: .sentAt)
    }

    public var text: String { message ?? (ok ? "done" : "the closeout was refused") }

    public var sent: Date? { sentAt.map { Date(timeIntervalSince1970: $0) } }
}

/// `finish.start_finish`'s eight outcomes, read off its own `return` statements.
///
/// Widened on decode like every other server enum in this client: a mode this
/// build has never heard of must not throw the reply away, because the reply
/// also carries the human message that says what happened.
public enum FinishMode: String, Sendable, Equatable, Decodable {
    /// Already landed and clean with an agent idling — `/exit` was typed.
    case exit
    /// The full closeout brief went to the live agent.
    case brief
    /// The branch had already landed; the short brief went instead.
    case slim
    /// Step two, and the landing still does not verify — a stalled closeout got
    /// the specifics typed at it.
    case nudge
    /// Step two refused: the landing does not verify yet.
    case pending
    /// Step two refused AND the agent is stuck on a question. A typed nudge
    /// would collide with its open dialog, so this routes to chat instead.
    case chat
    /// No agent needed — the board switched to the trunk and pulled.
    case parked
    /// Nothing to finish.
    case noop
    case unknown

    public init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = FinishMode(rawValue: raw) ?? .unknown
    }

    /// Whether this outcome means a brief is now with an agent, so the card is
    /// in step two and the button becomes `✕ close`.
    public var startsCloseout: Bool { self == .brief || self == .slim }
}

// MARK: - resume

/// `POST /api/resume/schedule` → `{"ok": true, "due_at": 1784753345.02,
/// "message": "auto-resume armed for 22:49"}`.
///
/// **This is the one mutation in the app that is genuinely idempotent**, and it
/// is idempotent by construction rather than by a key: `_resumes` is a dict keyed
/// `"{worktree}|{sid}"`, so arming twice replaces rather than adds. Driven live,
/// twice, and the second call produced one schedule.
///
/// `need_time` is **not an error**. It means no reset timestamp is known for this
/// limit, and the answer is to pick an exact time — so the sheet expands its
/// picker rather than showing a failure.
public struct ResumeReply: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let message: String?
    public let dueAt: Double?
    public let needTime: Bool

    public init(ok: Bool, message: String?, dueAt: Double? = nil, needTime: Bool = false) {
        self.ok = ok
        self.message = message
        self.dueAt = dueAt
        self.needTime = needTime
    }

    enum CodingKeys: String, CodingKey {
        case ok, message
        case dueAt = "due_at"
        case needTime = "need_time"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        message = try c.decodeIfPresent(String.self, forKey: .message)
        dueAt = try c.decodeIfPresent(Double.self, forKey: .dueAt)
        needTime = try c.decodeIfPresent(Bool.self, forKey: .needTime) ?? false
    }

    public var due: Date? { dueAt.map { Date(timeIntervalSince1970: $0) } }
    public var text: String { message ?? (ok ? "armed" : "the server refused") }
}

/// The delay ladder of `UX.md` §4.5, which mirrors the desktop select exactly.
public enum ResumeDelay: Sendable, Equatable, Hashable, CaseIterable {
    case oneMinute, fiveMinutes, fifteenMinutes, oneHour

    public var seconds: Double {
        switch self {
        case .oneMinute: 60
        case .fiveMinutes: 300
        case .fifteenMinutes: 900
        case .oneHour: 3600
        }
    }

    public var label: String {
        switch self {
        case .oneMinute: "1 min"
        case .fiveMinutes: "5 min"
        case .fifteenMinutes: "15 min"
        case .oneHour: "1 hour"
        }
    }
}
