import Foundation

/// Why a request did not produce a payload — as five states a phone on a flaky
/// tailnet can actually be in, not as one spinner.
///
/// This is the single largest support burden of this transport. iOS permits one
/// active VPN configuration at a time, Tailscale competes with corporate VPNs,
/// and its extension can be killed under memory pressure. A client that shows
/// the same "loading…" for all of them is unusable, and the user action is
/// different in every case:
///
/// | case | what the user does |
/// |---|---|
/// | `.offline` | turn on Wi-Fi or cellular |
/// | `.tailnetDown` | open Tailscale on this phone and connect |
/// | `.macUnreachable` | wake the Mac, or check it is still on the tailnet |
/// | `.serverStopped` | run `./start.sh` on the Mac |
/// | `.unauthorized` | re-pair — this device was revoked, or never paired |
public enum OrchestraError: Error, Sendable, Equatable {
    /// This phone has no network path at all.
    case offline
    /// No route to `100.64/10`. Tailscale is not up on this device.
    case tailnetDown
    /// The peer is not answering — asleep, or off the tailnet. Merged with
    /// `.tailnetDown` in the copy where the platform cannot tell them apart.
    case macUnreachable
    /// TCP connected and was refused or reset: the Mac is there, orchestra is
    /// not. **The only cause the user can fix from the phone**, and the only rung
    /// that is reliably distinguishable — `ECONNREFUSED` does propagate through
    /// Tailscale's netstack, which forwards the peer's real RST.
    case serverStopped
    /// **App Transport Security refused the load before a packet left the
    /// phone.** Not a network failure at all — a build failure — and it is put
    /// in its own case because the two need opposite reactions: a user can do
    /// nothing about it, and a developer must not spend an afternoon on the
    /// tailnet. Proven reachable: deleting the tailnet IP from the Info.plist's
    /// `NSExceptionDomains` produces exactly this, `NSURLErrorAppTransportSecurity
    /// RequiresSecureConnection` (-1022).
    case transportBlocked
    /// 401. The token is missing, unknown, or the device was revoked.
    case unauthorized(APIRefusal?)
    /// 403 — reached this server but not this route. Device management answers
    /// to the Mac holding no token; a phone can never have it.
    case forbidden(APIRefusal?)
    /// Any other HTTP status, with whatever the server said about it.
    case http(status: Int, refusal: APIRefusal?)
    /// The bytes arrived and were not the shape this client knows.
    case decoding(String)
    /// The request was cancelled — a task torn down, a screen dismissed. Never
    /// shown to the user.
    case cancelled
    /// Everything else, carrying the raw code so a bug report has a number in it.
    case unknown(code: Int, description: String)

    /// Whether the app should keep its last-good board on screen and just say
    /// "not fresh", rather than replacing it with an error. Every transport
    /// failure qualifies; only `.unauthorized` tears the session down.
    public var keepsLastGoodData: Bool {
        if case .unauthorized = self { return false }
        return true
    }
}

/// The errno → cause ladder, as a pure function with no I/O so it can be tested
/// without a network.
///
/// `URLError` buries the real cause in `userInfo` under
/// `NSUnderlyingErrorKey` → an `NSPOSIXErrorDomain` error whose `code` is the
/// errno. The `URLError.Code` alone is not enough: `.cannotConnectToHost` covers
/// both "refused" and "no route", which are the two ends of this ladder.
public enum ErrnoCause {
    /// ECONNREFUSED — the host said "nothing is listening".
    public static let connectionRefused: Int32 = 61
    /// ETIMEDOUT — nothing came back at all.
    public static let timedOut: Int32 = 60
    /// EHOSTUNREACH — no route to that host.
    public static let hostUnreachable: Int32 = 65
    /// ENETUNREACH — no route to that network. On a phone with Tailscale off,
    /// this is what a packet to 100.64/10 gets.
    public static let networkUnreachable: Int32 = 51

    public static func cause(forErrno e: Int32) -> OrchestraError {
        switch e {
        case connectionRefused: .serverStopped
        case timedOut, hostUnreachable: .macUnreachable
        case networkUnreachable: .tailnetDown
        default: .unknown(code: Int(e), description: "errno \(e)")
        }
    }

    /// Classify a `URLError`, preferring the underlying errno when there is one.
    ///
    /// **The honest caveat, which the copy has to respect**: rungs 1 and 2 will
    /// often both collapse to a timeout, because a packet to an absent tailnet
    /// peer is black-holed rather than producing an ICMP-derived errno. So
    /// `.tailnetDown` and `.macUnreachable` are two names for one screen until
    /// there is a measurement that separates them. `.serverStopped` is the one
    /// that is reliable, and it is also the only one the user can act on.
    public static func classify(_ error: any Error) -> OrchestraError {
        if let orc = error as? OrchestraError { return orc }
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain, ns.code == NSURLErrorCancelled {
            return .cancelled
        }
        if let posix = underlyingPOSIX(ns) {
            return cause(forErrno: posix)
        }
        guard ns.domain == NSURLErrorDomain else {
            return .unknown(code: ns.code, description: ns.localizedDescription)
        }
        switch URLError.Code(rawValue: ns.code) {
        case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed,
             .internationalRoamingOff:
            return .offline
        case .cannotFindHost, .dnsLookupFailed:
            // MagicDNS resolves through the tunnel; if the name will not resolve
            // the tunnel is the thing that is down.
            return .tailnetDown
        case .timedOut:
            return .macUnreachable
        case .cannotConnectToHost:
            return .serverStopped
        case .secureConnectionFailed, .appTransportSecurityRequiresSecureConnection:
            // ADR 0013: there is no TLS on this server. Reaching here means the
            // ATS exception is not doing its job, which is a build problem, not
            // a network one — say so with the real code rather than blaming the
            // tailnet.
            return .transportBlocked
        default:
            return .unknown(code: ns.code, description: ns.localizedDescription)
        }
    }

    private static func underlyingPOSIX(_ ns: NSError) -> Int32? {
        var current: NSError? = ns
        var depth = 0
        while let e = current, depth < 4 {
            if e.domain == NSPOSIXErrorDomain { return Int32(e.code) }
            current = e.userInfo[NSUnderlyingErrorKey] as? NSError
            depth += 1
        }
        return nil
    }
}

extension OrchestraError {
    /// The headline. Short, lower-case, and it names the THING that is wrong —
    /// never "an error occurred".
    public var headline: String {
        switch self {
        case .offline: "no network"
        case .transportBlocked: "this build cannot reach that address"
        case .tailnetDown: "tailnet unreachable"
        case .macUnreachable: "the Mac isn't answering"
        case .serverStopped: "orchestra isn't running"
        case .unauthorized: "this device isn't paired"
        case .forbidden: "not allowed from a phone"
        case .http(let s, _): "the server said \(s)"
        case .decoding: "the board didn't parse"
        case .cancelled: "cancelled"
        case .unknown: "something went wrong"
        }
    }

    /// The second line: what to DO about it.
    public var guidance: String {
        switch self {
        case .offline:
            "This phone has no network path. Turn on Wi-Fi or cellular."
        case .transportBlocked:
            "App Transport Security refused a plain-HTTP load before anything left "
            + "the phone. This build's Info.plist needs an NSExceptionDomains entry "
            + "for that host — see ADR 0013. Nothing on the Mac is wrong."
        case .tailnetDown:
            "Open Tailscale on this phone and connect. orchestra cannot start it for you."
        case .macUnreachable:
            "The Mac may be asleep, or off the tailnet. Wake it and try again."
        case .serverStopped:
            "The Mac is reachable but nothing is listening. Run ./start.sh there."
        case .unauthorized(let r):
            r?.message ?? "Pair again from the board — this device may have been revoked."
        case .forbidden(let r):
            r?.message ?? "Device management answers to the Mac itself, never to a token."
        case .http(_, let r):
            r?.message ?? "An unexpected status came back."
        case .decoding(let what):
            "The payload did not match what this build expects: \(what)"
        case .cancelled:
            ""
        case .unknown(let code, let description):
            "\(description) (\(code))"
        }
    }
}
