import Foundation

/// The only thing in this app that talks to the server.
///
/// An `actor` rather than a `@MainActor` service, for one concrete reason: every
/// call here waits on a network, and a wait on the main actor is a frame the
/// board did not draw. Nothing it returns is a reference type, so what crosses
/// back to the store is a value and Swift 6 has nothing to complain about.
///
/// It holds the profile and the token source as mutable actor state because both
/// change at runtime — pairing installs them, an unpair clears them — and the
/// alternative (passing them into every call) puts the invariant "these two
/// always agree" in the caller, which is where it would eventually stop being
/// true.
public actor OrchestraClient {
    private let session: URLSession
    private let decoder: JSONDecoder
    private(set) var profile: ServerProfile?
    private(set) var tokens: any TokenSource

    /// The session-wide cap on a whole transfer (`timeoutIntervalForResource`).
    ///
    /// **It must exceed the largest per-route deadline, with slack.** Unlike
    /// `URLRequest.timeoutInterval` — which overrides the session's
    /// `timeoutIntervalForRequest` (the inter-packet idle timer) per request —
    /// the resource timeout is session-level and has *no* per-request override,
    /// so it hard-caps the entire task no matter what a route asked for. At the
    /// old value of 20 s it silently truncated `finish` (120 s) and `send`
    /// (25 s): a legitimate long closeout that the server completed in 25–45 s
    /// was aborted at 20 s with `NSURLErrorTimedOut` and misreported as
    /// `.macUnreachable`, inviting the exact double-actuation `finish` exists to
    /// prevent. 180 s is `finish`'s 120 s plus generous slack. `EndpointTests`
    /// pins the fleet-wide invariant `endpoint.timeout <= sessionResourceTimeout`
    /// so a new long route cannot silently reintroduce the cap.
    public static let sessionResourceTimeout: TimeInterval = 180

    public init(profile: ServerProfile? = nil,
                tokens: any TokenSource = StaticTokenSource(nil),
                session: URLSession? = nil) {
        self.profile = profile
        self.tokens = tokens
        self.decoder = JSONDecoder()
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            // The board is never cacheable — see `Endpoint.urlRequest`. Turning
            // the store off as well means there is no second place for a stale
            // answer to come from.
            config.urlCache = nil
            config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
            config.waitsForConnectivity = false   // we want the error, not a wait
            config.timeoutIntervalForRequest = 10
            // Per-request `timeoutInterval` (set in `Endpoint.urlRequest`) is the
            // tight idle deadline; the resource cap only exists to bound a task
            // whose packets keep trickling. It MUST clear the longest route.
            config.timeoutIntervalForResource = Self.sessionResourceTimeout
            config.httpAdditionalHeaders = ["Accept": "application/json"]
            self.session = URLSession(configuration: config)
        }
    }

    public func configure(profile: ServerProfile?, tokens: any TokenSource) {
        self.profile = profile
        self.tokens = tokens
    }

    public func currentProfile() -> ServerProfile? { profile }

    // MARK: - Routes

    public func health() async throws -> ServerHealth {
        try await send(.health, to: profile, as: ServerHealth.self)
    }

    public func health(at profile: ServerProfile) async throws -> ServerHealth {
        try await send(.health, to: profile, as: ServerHealth.self)
    }

    public func fleetState() async throws -> FleetState {
        try await send(.state, to: profile, as: FleetState.self)
    }

    /// One session's conversation. Addressed by `(account, sid)` — never a pid.
    public func chat(account: String, sid: String) async throws -> ChatTranscript {
        try await send(.chat(account: account, sid: sid), to: profile, as: ChatTranscript.self)
    }

    /// The branch map. A cache read on the server (30 s TTL); fetched on appear
    /// and pull-to-refresh only — never on a phone timer (§5.11).
    public func topology() async throws -> Topology {
        try await send(.topology, to: profile, as: Topology.self)
    }

    /// Per-account usage. The cache read only: `refresh=1` is a 90-second
    /// whole-fleet subprocess and is not something a phone should be able to
    /// start by accident.
    public func limits() async throws -> LimitsReport {
        try await send(.limits, to: profile, as: LimitsReport.self)
    }

    /// Exchange a pairing code for a device token.
    ///
    /// Takes its address from the TICKET, not from `profile`, because this is
    /// the call that creates the profile. A ticket also carries the address the
    /// server advertised for itself, which is the address the server is actually
    /// bound to — better than anything the user could type.
    public func pair(_ ticket: PairingTicket, label: String,
                     platform: String = "ios") async throws -> PairResponse {
        let target = ServerProfile(host: ticket.host, port: ticket.port)
        let endpoint = try Endpoint.pair(code: ticket.code, label: label, platform: platform)
        return try await send(endpoint, to: target, as: PairResponse.self)
    }

    // MARK: - The mutations
    //
    // Four routes that change the world, and one property they all share: **the
    // server answers 200 for a refusal**, putting the outcome in the body. So
    // none of these throws on a refusal — a refusal is a value, and its `message`
    // is the thing the screen shows. They throw only when no answer came back at
    // all, which is the case the UI must never render as "failed".

    /// Type `text` into an agent's terminal, addressed by `(account, sid)`.
    ///
    /// The caller is responsible for holding the in-flight lock; this actor does
    /// not, because a lock that lives here would be invisible to the view that
    /// has to grey the button out.
    public func send(account: String, sid: String, worktree: String,
                     text: String) async throws -> SendReply {
        try await send(Endpoint.send(account: account, sid: sid, worktree: worktree,
                                     text: text),
                       to: profile, as: SendReply.self)
    }

    /// Launch a mission. Returns as soon as the server has taken the job.
    public func dispatch(mission: String, worktree: String?, account: String?,
                         model: String, effort: String,
                         forceModel: Bool) async throws -> DispatchStart {
        try await send(Endpoint.dispatch(mission: mission, worktree: worktree,
                                         account: account, model: model,
                                         effort: effort, forceModel: forceModel),
                       to: profile, as: DispatchStart.self)
    }

    public func dispatchStatus(job: String) async throws -> DispatchJob {
        try await send(.dispatchStatus(job: job), to: profile, as: DispatchJob.self)
    }

    public func finish(worktree: String) async throws -> FinishReply {
        try await send(Endpoint.finish(worktree: worktree), to: profile,
                       as: FinishReply.self)
    }

    public func armResume(worktree: String, sid: String, account: String,
                          delayS: Double?, resetsAt: Double?,
                          dueAt: Double?) async throws -> ResumeReply {
        try await send(Endpoint.resumeSchedule(worktree: worktree, sid: sid,
                                               account: account, delayS: delayS,
                                               resetsAt: resetsAt, dueAt: dueAt),
                       to: profile, as: ResumeReply.self)
    }

    public func cancelResume(worktree: String, sid: String) async throws -> ResumeReply {
        try await send(Endpoint.resumeCancel(worktree: worktree, sid: sid),
                       to: profile, as: ResumeReply.self)
    }

    // MARK: - Push

    /// Register (or re-register) this device's APNs token. A refusal here is a
    /// value like every other mutation's — a 422 `push_token_invalid` throws
    /// `.http`, which the store reports rather than retrying.
    public func registerPush(token: String, environment: String, tzOffsetMin: Int,
                             appVersion: String?,
                             settings: [String: Any]?) async throws -> RegisterPushReply {
        try await send(Endpoint.registerPush(token: token, environment: environment,
                                             tzOffsetMin: tzOffsetMin,
                                             appVersion: appVersion, settings: settings),
                       to: profile, as: RegisterPushReply.self)
    }

    public func pushStatus() async throws -> PushStatus {
        try await send(.pushStatus, to: profile, as: PushStatus.self)
    }

    public func savePushSettings(body: [String: Any]) async throws -> ActionReply {
        try await send(Endpoint.pushSettings(body: body), to: profile, as: ActionReply.self)
    }

    public func mutePush(minutes: Double) async throws -> ActionReply {
        try await send(Endpoint.pushMute(minutes: minutes), to: profile, as: ActionReply.self)
    }

    public func testPush() async throws -> ActionReply {
        try await send(Endpoint.pushTest(), to: profile, as: ActionReply.self)
    }

    /// Answer an agent from a notification. Addressed by `sid` alone — the reply
    /// path has no account and does not need one.
    public func reply(sid: String, worktree: String?, text: String) async throws -> SendReply {
        try await send(Endpoint.reply(sid: sid, worktree: worktree, text: text),
                       to: profile, as: SendReply.self)
    }

    // MARK: - The one place a request is made

    private func send<T: Decodable & Sendable>(_ endpoint: Endpoint,
                                               to profile: ServerProfile?,
                                               as type: T.Type) async throws -> T {
        guard let profile, let base = profile.baseURL else {
            throw OrchestraError.unauthorized(nil)
        }
        let token = endpoint.requiresToken ? await tokens.bearerToken() : nil
        if endpoint.requiresToken && (token?.isEmpty ?? true) {
            // Do not spend a round trip to be told what we already know. An
            // unpaired app is a pairing screen, not a network failure.
            throw OrchestraError.unauthorized(nil)
        }
        let request = try endpoint.urlRequest(base: base, token: token)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw ErrnoCause.classify(error)
        }

        guard let http = response as? HTTPURLResponse else {
            throw OrchestraError.decoding("no HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            let refusal = try? decoder.decode(APIRefusal.self, from: data)
            switch http.statusCode {
            case 401: throw OrchestraError.unauthorized(refusal)
            case 403: throw OrchestraError.forbidden(refusal)
            default: throw OrchestraError.http(status: http.statusCode, refusal: refusal)
            }
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch let error as DecodingError {
            throw OrchestraError.decoding(Self.describe(error))
        } catch {
            throw OrchestraError.decoding(error.localizedDescription)
        }
    }

    /// `DecodingError`'s own description names the type and swallows the key
    /// path, which is the half that tells you which server field moved. This
    /// keeps the path, because "keyNotFound turn_ended at worktrees[3].sessions[1]"
    /// is a bug report and "The data couldn't be read" is not.
    static func describe(_ error: DecodingError) -> String {
        func path(_ context: DecodingError.Context) -> String {
            context.codingPath.map(\.stringValue).joined(separator: ".")
        }
        switch error {
        case .keyNotFound(let key, let ctx):
            return "missing `\(key.stringValue)` at \(path(ctx))"
        case .typeMismatch(let type, let ctx):
            return "expected \(type) at \(path(ctx))"
        case .valueNotFound(let type, let ctx):
            return "null where \(type) was required at \(path(ctx))"
        case .dataCorrupted(let ctx):
            return "corrupt at \(path(ctx)): \(ctx.debugDescription)"
        @unknown default:
            return error.localizedDescription
        }
    }
}
