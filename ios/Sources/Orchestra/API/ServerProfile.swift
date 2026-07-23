import Foundation

/// Where the server is. One value, `Codable`, so it can go straight into
/// `UserDefaults` beside the token in the Keychain.
///
/// **Plain HTTP, deliberately** (ADR 0013). WireGuard already gives mutual
/// authentication and confidentiality between devices, so TLS on top would
/// secure a channel that is already secure — at the cost of certificate
/// generation, rotation, a trust store on the phone, and pinning logic that
/// fails closed and strands the user. The three layers that DO carry the
/// security are Tailscale, the per-device bearer token, and a server that binds
/// loopback until you opt in.
///
/// If this server is ever exposed through a tunnel, a LAN bind, or anything
/// reachable without WireGuard, ADR 0013 must be superseded and `scheme` stops
/// being a constant.
public struct ServerProfile: Sendable, Equatable, Codable, Hashable {
    /// A tailnet IP (`100.113.110.31`) or a MagicDNS name
    /// (`achills-macbook-pro.tail1205d9.ts.net`). Both work; both need their own
    /// ATS treatment, which is why the Info.plist carries two entries.
    public var host: String
    public var port: Int
    /// What the Mac calls itself. Display only — it is what makes a server list
    /// readable when there are two.
    public var hostname: String
    /// The device id the pairing minted, so the user can match the row on the
    /// Mac's `--list-devices` against the phone in their hand.
    public var deviceID: String

    public init(host: String, port: Int, hostname: String = "", deviceID: String = "") {
        self.host = host
        self.port = port
        self.hostname = hostname
        self.deviceID = deviceID
    }

    public init(_ facts: PairResponse.ServerFacts, deviceID: String) {
        self.host = facts.host
        self.port = facts.port
        self.hostname = facts.hostname
        self.deviceID = deviceID
    }

    public var baseURL: URL? {
        var c = URLComponents()
        c.scheme = "http"
        c.host = host
        c.port = port
        return c.url
    }

    /// A label the diagnostics screen can print without leaking anything.
    public var display: String {
        hostname.isEmpty ? "\(host):\(port)" : "\(hostname) — \(host):\(port)"
    }
}
