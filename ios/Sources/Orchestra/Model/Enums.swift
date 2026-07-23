import Foundation

/// A session's status, as `orchestra/status.py:classify_session` publishes it.
///
/// The seven names come from the server's own rank table
/// (`observer.py:122`) plus `unknown`, which `classify_session` can return but
/// which — as of this writing — no call site can publish: the README's open
/// items record that `rank[s["status"]]` would raise `KeyError` on it. It is
/// modelled anyway, because the whole point of `.unknown` here is that a value
/// the client has never seen must not be able to destroy the decode.
///
/// **A hard `RawRepresentable` conformance would make one new server-side status
/// string throw out the entire 38 KB payload.** One unknown word, whole board
/// gone. So the decode widens instead: anything unrecognised lands on
/// `.unknown`, renders as a neutral pill, and is counted as a decode surprise.
public enum SessionStatus: String, Sendable, Codable, CaseIterable, Hashable {
    case working
    case needsInput = "needs_input"
    case blocked
    case waiting
    case limit
    case ended
    case unknown

    public init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = SessionStatus(rawValue: raw) ?? .unknown
    }

    /// The two statuses that mean a human is being waited on right now.
    public var isAttention: Bool { self == .needsInput || self == .blocked }

    /// `observer.py:122`. Lower sorts first — the board is severity-ordered and
    /// the client never re-sorts, but this is what makes a card's worst session
    /// findable without re-deriving the server's opinion.
    public var rank: Int {
        switch self {
        case .needsInput: 0
        case .limit:      1
        case .blocked:    2
        case .working:    3
        case .waiting:    4
        case .ended:      5
        case .unknown:    6
        }
    }
}

/// A worktree's availability — "is it safe to point a new agent here".
///
/// **These are the FOUR the server actually ships**, verified against a live
/// nine-worktree fleet: `free`, `attention`, `waiting`, `busy`
/// (`orchestra/status.py:card_availability`). `UX.md` §3.1.2 specifies FIVE
/// (`needs_you`, `your_turn`, `busy`, `limited`, `free`) and says so explicitly:
/// *"This is a required change to API.md §10.2, not a description of it."* That
/// server change has not landed. Until it does the client models what is on the
/// wire and derives its sections from session statuses in `Triage.swift`, with
/// the derivation isolated in one testable place so it can be deleted in one
/// commit when the server starts shipping the split.
public enum Availability: String, Sendable, Codable, Hashable {
    case free
    case attention
    case waiting
    case busy
    case unknown

    public init(from decoder: any Decoder) throws {
        let raw = try decoder.singleValueContainer().decode(String.self)
        self = Availability(rawValue: raw) ?? .unknown
    }
}
