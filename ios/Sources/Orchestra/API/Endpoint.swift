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
