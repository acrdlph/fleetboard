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
