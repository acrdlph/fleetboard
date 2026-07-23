import Foundation

/// What a scanned QR — or a typed code plus an address — reduces to.
///
/// The QR carries `orc://p?h=<host>[&p=<port>]&c=<code>` and it carries the CODE,
/// never the token (ADR 0015). That indirection is the whole security argument: a
/// QR on a screen is visible to the room, to a video call, and to every
/// screenshot of it forever. A pairing code in that picture is worthless 120
/// seconds later.
public struct PairingTicket: Sendable, Equatable, Hashable {
    public let host: String
    public let port: Int
    /// Already normalised. Never displayed back to the user in this form — the
    /// board groups it `7ZVT-Z9N5` for reading aloud.
    public let code: String

    public init(host: String, port: Int, code: String) {
        self.host = host
        self.port = port
        self.code = code
    }

    /// The server omits `p` when it is the default, to save five bytes of a
    /// budget that decides the QR's version and therefore how far a camera can
    /// read it from (`pairing.payload_url`).
    public static let defaultPort = 4242

    /// Parse the scanned string. Returns nil for anything that is not this
    /// scheme — a scanner pointed at a Wi-Fi QR must fail quietly, not pair.
    public init?(url: String) {
        guard let comps = URLComponents(string: url.trimmingCharacters(in: .whitespacesAndNewlines)),
              comps.scheme?.lowercased() == "orc" || comps.scheme?.lowercased() == "orchestra"
        else { return nil }
        // `orc://p?…` — "p" arrives as the HOST of the URL, not as the path,
        // because there are two slashes. Accept it in either position rather
        // than depending on which, since `orc:p?…` is a legal spelling too.
        let marker = (comps.host ?? "") + comps.path
        guard marker.replacingOccurrences(of: "/", with: "") == "p" else { return nil }
        let items = comps.queryItems ?? []
        func value(_ n: String) -> String? { items.first { $0.name == n }?.value }
        guard let h = value("h"), !h.isEmpty,
              let rawCode = value("c") else { return nil }
        let normalised = PairingTicket.normalise(rawCode)
        guard normalised.count == 8 else { return nil }
        host = h
        port = value("p").flatMap { Int($0) } ?? PairingTicket.defaultPort
        code = normalised
    }

    /// The client half of `pairing.normalise`, and it must agree with it
    /// exactly. Generous about FORM — case, spaces, dashes, underscores — and
    /// folding the four glyphs Crockford removed, because the manual fallback is
    /// a human reading a screen and `I`/`1`, `O`/`0`, `L`/`1` is where that goes
    /// wrong. Not generous about VALUE: the comparison happens on the server,
    /// under `hmac.compare_digest`.
    public static func normalise(_ raw: String) -> String {
        var out = ""
        for ch in raw.uppercased() {
            if ch == " " || ch == "-" || ch == "_" || ch == "\t" || ch == "\n" { continue }
            switch ch {
            case "I", "L": out.append("1")
            case "O": out.append("0")
            case "U": out.append("V")
            default: out.append(ch)
            }
        }
        return out
    }

    /// `7ZVTZ9N5` → `7ZVT-Z9N5`. Display only, matching `pairing.grouped`.
    public static func grouped(_ code: String) -> String {
        code.count == 8 ? "\(code.prefix(4))-\(code.suffix(4))" : code
    }
}

/// `POST /api/v1/pair` — 200.
public struct PairResponse: Sendable, Equatable, Decodable {
    public let deviceID: String
    public let label: String
    /// The only time this string exists anywhere but the Keychain. The registry
    /// keeps sha256 and nothing else, so a lost token is re-minted, never
    /// recovered.
    public let token: String
    public let server: ServerFacts

    public init(deviceID: String, label: String, token: String, server: ServerFacts) {
        self.deviceID = deviceID
        self.label = label
        self.token = token
        self.server = server
    }

    enum CodingKeys: String, CodingKey {
        case label, token, server
        case deviceID = "device_id"
    }

    /// No `spki`, no `cert_not_after`. API.md §3.3 lists both and both belong to
    /// the TLS design ADR 0013 replaced; sending them as nulls would invite a
    /// client to implement pinning against nothing.
    public struct ServerFacts: Sendable, Equatable, Decodable {
        public let host: String
        public let port: Int
        public let hostname: String
        public let api: String
        public let tls: Bool

        public init(host: String, port: Int, hostname: String, api: String, tls: Bool) {
            self.host = host
            self.port = port
            self.hostname = hostname
            self.api = api
            self.tls = tls
        }

        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            host = try c.decodeIfPresent(String.self, forKey: .host) ?? ""
            port = try c.decodeIfPresent(Int.self, forKey: .port) ?? PairingTicket.defaultPort
            hostname = try c.decodeIfPresent(String.self, forKey: .hostname) ?? ""
            api = try c.decodeIfPresent(String.self, forKey: .api) ?? "1"
            tls = try c.decodeIfPresent(Bool.self, forKey: .tls) ?? false
        }

        enum CodingKeys: String, CodingKey { case host, port, hostname, api, tls }
    }
}

/// The failure half of `/api/v1` — these routes answer with REAL status codes
/// rather than the legacy board's in-payload `{"ok": false}`, deliberately,
/// because a Swift client has to branch on 409 vs 403 vs 429 before it has
/// parsed anything.
public struct APIRefusal: Sendable, Equatable, Decodable {
    public let error: String
    public let message: String

    public init(error: String, message: String) {
        self.error = error
        self.message = message
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        error = try c.decodeIfPresent(String.self, forKey: .error) ?? "unknown"
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
    }

    enum CodingKeys: String, CodingKey { case error, message }

    /// The strings `pairing.py` actually writes. Named so the UI can say the
    /// right thing rather than echoing server prose into a phone-sized banner.
    public enum Code {
        public static let notOpen = "pairing_not_open"
        public static let codeWrong = "pairing_code_wrong"
        public static let attempts = "pairing_attempts"
        public static let locked = "pairing_locked"
        public static let peerRefused = "peer_not_permitted"
        public static let badRequest = "pairing_bad_request"
        public static let unauthorized = "unauthorized"
        public static let tokenUnknown = "token_unknown"
    }
}
