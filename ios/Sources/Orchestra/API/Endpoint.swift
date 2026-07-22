import Foundation

/// One request, with its deadline derived rather than guessed.
///
/// Timeouts are per-route because the routes are not alike: `/api/state` is a
/// 0.8 ms dict read off a background sweep and anything past a few seconds is a
/// transport problem, whereas a cold Network Extension tunnel needs time to wake
/// after foregrounding, which is why the floor is 5 s and not 2.
public struct Endpoint: Sendable {
    public enum Method: String, Sendable { case get = "GET", post = "POST" }

    public let method: Method
    public let path: String
    public let query: [URLQueryItem]
    public let body: Data?
    public let timeout: TimeInterval
    /// `/api/health` and `POST /api/v1/pair` are the server's only two exempt
    /// routes, and sending a token to them is pointless rather than harmful.
    /// Marking them keeps an unpaired app from looking like a broken paired one.
    public let requiresToken: Bool

    public init(method: Method, path: String, query: [URLQueryItem] = [],
                body: Data? = nil, timeout: TimeInterval, requiresToken: Bool) {
        self.method = method
        self.path = path
        self.query = query
        self.body = body
        self.timeout = timeout
        self.requiresToken = requiresToken
    }

    /// A cold tunnel wake-up is the thing this number exists to survive.
    public static let probeDeadline: TimeInterval = 5

    public static let health = Endpoint(method: .get, path: "/api/health",
                                        timeout: probeDeadline, requiresToken: false)

    public static let state = Endpoint(method: .get, path: "/api/state",
                                       timeout: 8, requiresToken: true)

    /// `GET /api/events` — the stream.
    ///
    /// **The timeout is 70 s and it is the most load-bearing number in this
    /// file.** `URLRequest.timeoutInterval` is not a deadline on the response;
    /// it is the maximum silence between packets. orchestra writes `: keepalive`
    /// only after `sse_keepalive_s` — **25 s** — of a composed view that has not
    /// changed, which on a quiet fleet is the only traffic on the socket. The
    /// board's normal request timeout is 10 s, so a stream opened on the normal
    /// session would be torn down by the phone every ten seconds of quiet,
    /// reconnected, torn down again — and the symptom is a board that looks
    /// perfect and burns a subscriber slot on a loop. 70 s is two keepalives
    /// plus slack, so a stream dies only when two consecutive keepalives are
    /// missed, which is a real death.
    public static let events = Endpoint(method: .get, path: "/api/events",
                                        timeout: 70, requiresToken: true)

    /// `GET /api/chat?account=&sid=` — the last 40 turns of one conversation.
    ///
    /// Identity-addressed like every other session-scoped route (ADR 0008): a
    /// pid does not appear, and could not be used if it did.
    public static func chat(account: String, sid: String) -> Endpoint {
        Endpoint(method: .get, path: "/api/chat",
                 query: [URLQueryItem(name: "account", value: account),
                         URLQueryItem(name: "sid", value: sid)],
                 timeout: 10, requiresToken: true)
    }

    /// `GET /api/topology` — the branch map.
    ///
    /// **The legacy route, because it is the only one that exists.** `API.md`
    /// §9.7's `GET /api/v1/topology` (with `worktree_id`, `subject_short`, an
    /// `axis` block and a `dropped[]`) is not served — `server.py:335` routes
    /// `/api/topology` alone, to `gitrepo.cached_topology`. Server-side it is ~90
    /// git subprocesses behind a 30 s TTL (`gitrepo.TOPO_TTL_S`), so this is
    /// fetched on appear and on pull-to-refresh, NEVER on a timer (§5.11). The
    /// deadline is generous because a cold cache pays the full sweep.
    public static let topology = Endpoint(method: .get, path: "/api/topology",
                                          timeout: 20, requiresToken: true)

    /// `GET /api/limits`. Without `refresh=1` this is a cache read and is fast;
    /// `refresh=1` shells out to `cclimits` for EVERY account under a 90 s
    /// server-side timeout, which is why this build never sends it. See
    /// `LimitsStore`.
    public static let limits = Endpoint(method: .get, path: "/api/limits",
                                        timeout: 15, requiresToken: true)

    /// The claim. `Content-Type: application/json` is not optional here: the
    /// server refuses any mutation without it with a **415
    /// `content_type_required`**, which is the CSRF guard — a JSON body forces a
    /// preflight this server never answers.
    public static func pair(code: String, label: String, platform: String) throws -> Endpoint {
        let payload: [String: String] = ["code": code, "label": label, "platform": platform]
        let body = try JSONSerialization.data(withJSONObject: payload)
        return Endpoint(method: .post, path: "/api/v1/pair", body: body,
                        timeout: probeDeadline, requiresToken: false)
    }

    // MARK: - The mutations
    //
    // Every one of them is a POST with a JSON body, and the body is not
    // decoration: a mutation without `Content-Type: application/json` is refused
    // **415 `content_type_required`** before it reaches a handler. That is the
    // CSRF guard, verified by sending one without.
    //
    // None of them carries an idempotency key, because the server has nowhere to
    // put one — see `Actuation`. A header this server ignores would look like a
    // guarantee and be none.

    /// `POST /api/send` — type into an agent's terminal.
    ///
    /// **Addressed by `sid` + `account`, and the pid is not sent at all.** Not
    /// merely "not required": `identity.resolve` treats a pid as a hint and
    /// cross-checks it against the session it names, so sending one can only ever
    /// turn a working request into `identity_gone`. `worktree` rides along as a
    /// second assertion the server checks for free.
    ///
    /// The deadline is 25 s because the osascript path has a 10 s subprocess
    /// timeout inside a `claude_processes()` scan that can itself take seconds.
    public static func send(account: String, sid: String, worktree: String,
                            text: String) throws -> Endpoint {
        let payload: [String: String] = ["account": account, "sid": sid,
                                         "worktree": worktree, "text": text]
        return Endpoint(method: .post, path: "/api/send",
                        body: try JSONSerialization.data(withJSONObject: payload),
                        timeout: 25, requiresToken: true)
    }

    /// `POST /api/dispatch` — launch a mission. **Spends real money.**
    ///
    /// `model` and `effort` are both required and the server says so rather than
    /// guessing (*"pick a model and an effort first — routing is deterministic,
    /// nothing is chosen for you"*). `worktree` and `account` are optional and
    /// `nil` means "you pick" — the server's `_pick_defaults` is the only picker,
    /// and the client does not mirror it.
    ///
    /// Returns fast — the work runs on a background thread — so the deadline is
    /// short. It is the POLL that waits.
    public static func dispatch(mission: String, worktree: String?, account: String?,
                                model: String, effort: String,
                                forceModel: Bool) throws -> Endpoint {
        var payload: [String: Any] = ["mission": mission, "model": model,
                                      "effort": effort, "force_model": forceModel]
        if let worktree { payload["worktree"] = worktree }
        if let account { payload["account"] = account }
        return Endpoint(method: .post, path: "/api/dispatch",
                        body: try JSONSerialization.data(withJSONObject: payload),
                        timeout: 20, requiresToken: true)
    }

    /// `GET /api/dispatch/status?job=…`. A read, and safe to repeat.
    ///
    /// The server matches the job id with `re.search(r"job=([\w-]+)")`, so an
    /// empty id silently becomes `{"ok": false, "error": "no job"}` rather than a
    /// 400 — driven live.
    public static func dispatchStatus(job: String) -> Endpoint {
        Endpoint(method: .get, path: "/api/dispatch/status",
                 query: [URLQueryItem(name: "job", value: job)],
                 timeout: 10, requiresToken: true)
    }

    /// `POST /api/finish` — the closeout.
    ///
    /// **120 s**, and that is measured off the server's own worst case rather
    /// than chosen: `start_finish` runs `git fetch origin` (30 s timeout), a
    /// merge-base, a `git status`, a full `claude_processes()` scan and an
    /// osascript send (10 s), all synchronously inside the request.
    public static func finish(worktree: String) throws -> Endpoint {
        let payload: [String: String] = ["worktree": worktree]
        return Endpoint(method: .post, path: "/api/finish",
                        body: try JSONSerialization.data(withJSONObject: payload),
                        timeout: 120, requiresToken: true)
    }

    /// `POST /api/resume/schedule` — arm or re-arm an auto-resume.
    ///
    /// Keyed `"{worktree}|{sid}"` server-side, so this is the one mutation in the
    /// app that is genuinely idempotent: arming twice replaces.
    ///
    /// Exactly one of `dueAt` (an absolute epoch the user picked) and
    /// `resetsAt` + `delayS` should be meaningful. Sending `resetsAt: nil` with
    /// no `dueAt` gets `{"ok": false, "need_time": true}`, which is a request for
    /// a time, not a failure.
    public static func resumeSchedule(worktree: String, sid: String, account: String,
                                      delayS: Double?, resetsAt: Double?,
                                      dueAt: Double?) throws -> Endpoint {
        var payload: [String: Any] = ["worktree": worktree, "sid": sid,
                                      "account": account]
        if let delayS { payload["delay_s"] = delayS }
        if let resetsAt { payload["resets_at"] = resetsAt }
        if let dueAt { payload["due_at"] = dueAt }
        return Endpoint(method: .post, path: "/api/resume/schedule",
                        body: try JSONSerialization.data(withJSONObject: payload),
                        timeout: 15, requiresToken: true)
    }

    /// `POST /api/resume/cancel`. Idempotent: a second cancel answers
    /// `{"ok": false, "message": "nothing armed for this session"}`, which is a
    /// statement about the world and not an error.
    ///
    /// **Cancel is not an abort.** If `fire_resume` is already executing, the pop
    /// removes the key and the side effect still happens. The sheet says so.
    public static func resumeCancel(worktree: String, sid: String) throws -> Endpoint {
        let payload: [String: String] = ["worktree": worktree, "sid": sid]
        return Endpoint(method: .post, path: "/api/resume/cancel",
                        body: try JSONSerialization.data(withJSONObject: payload),
                        timeout: 15, requiresToken: true)
    }

    func urlRequest(base: URL, token: String?) throws -> URLRequest {
        guard var comps = URLComponents(url: base.appendingPathComponent(path),
                                        resolvingAgainstBaseURL: false) else {
            throw OrchestraError.decoding("could not build a URL for \(path)")
        }
        if !query.isEmpty { comps.queryItems = query }
        guard let url = comps.url else {
            throw OrchestraError.decoding("could not build a URL for \(path)")
        }
        var req = URLRequest(url: url)
        req.httpMethod = method.rawValue
        req.timeoutInterval = timeout
        req.httpBody = body
        if body != nil {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if requiresToken, let token, !token.isEmpty {
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        req.setValue("orchestra-ios", forHTTPHeaderField: "User-Agent")
        // Never let a URL cache answer for a board. The whole product is
        // "is this true right now".
        req.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        return req
    }
}
