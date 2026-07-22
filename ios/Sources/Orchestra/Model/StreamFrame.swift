import Foundation

/// One frame off `GET /api/events`, decoded from a real capture.
///
/// The stream is phase 2's job; the type lands now because the frame was on the
/// wire while the models were being written, and modelling it from a live
/// capture is free today and archaeology later.
///
/// Verified against a snapshot frame taken 2026-07-22:
/// `id: 1` / `event: state` / `data: {"type":"snapshot","v":1,"at":…,"order":[…],
/// "cards":{…},"counts":{…},"other_procs":[…],"freshness":{…}}`.
///
/// **`base` is absent on a snapshot and present on a delta.** The task brief
/// lists it unconditionally; `delta_since` puts it on the delta branch only, and
/// its own docstring pins the two branches to "the same field set modulo
/// `base`".
public struct StreamFrame: Sendable, Equatable, Decodable {
    public enum Kind: String, Sendable, Codable {
        case snapshot, delta
    }

    public let type: Kind
    /// The version this frame leaves the client at. Also the SSE `id:`.
    public let v: Int
    /// The version this delta was computed against. Snapshot: nil.
    public let base: Int?
    public let at: Double
    /// **The board's triage order, and it rides EVERY frame.** A delta names only
    /// the changed cards; a client that patched its own dictionary and kept its
    /// own positions would leave a card that flipped to `needs_input` exactly
    /// where it was, forever.
    public let order: [String]
    /// On a delta this carries ONLY the changed cards. Everything else on the
    /// frame rides whole, so applying it reconstructs the snapshot exactly.
    public let cards: [String: Worktree]
    public let counts: Counts
    public let otherProcs: [OtherProc]
    /// How old each KIND of probe is. Moves with no version bump — that is the
    /// point of the no-bump path — so it can never be the cause of a stale card.
    public let freshness: Freshness

    public init(type: Kind, v: Int, base: Int?, at: Double, order: [String],
                cards: [String: Worktree], counts: Counts,
                otherProcs: [OtherProc], freshness: Freshness) {
        self.type = type
        self.v = v
        self.base = base
        self.at = at
        self.order = order
        self.cards = cards
        self.counts = counts
        self.otherProcs = otherProcs
        self.freshness = freshness
    }

    enum CodingKeys: String, CodingKey {
        case type, v, base, at, order, cards, counts, freshness
        case otherProcs = "other_procs"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = (try? c.decode(Kind.self, forKey: .type)) ?? .snapshot
        v = try c.decodeIfPresent(Int.self, forKey: .v) ?? 0
        base = try c.decodeIfPresent(Int.self, forKey: .base)
        at = try c.decodeIfPresent(Double.self, forKey: .at) ?? 0
        order = try c.decodeIfPresent([String].self, forKey: .order) ?? []
        cards = try c.decodeIfPresent([String: Worktree].self, forKey: .cards) ?? [:]
        counts = try c.decodeIfPresent(Counts.self, forKey: .counts) ?? Counts()
        otherProcs = try c.decodeIfPresent([OtherProc].self, forKey: .otherProcs) ?? []
        freshness = try c.decodeIfPresent(Freshness.self, forKey: .freshness) ?? Freshness()
    }
}

/// When each probe tier last ran. Absolute epochs — the client subtracts.
public struct Freshness: Sendable, Equatable, Codable {
    public var worktrees: Double?
    public var procs: Double?
    public var transcripts: Double?
    public var git: Double?

    public init(worktrees: Double? = nil, procs: Double? = nil,
                transcripts: Double? = nil, git: Double? = nil) {
        self.worktrees = worktrees
        self.procs = procs
        self.transcripts = transcripts
        self.git = git
    }

    /// The oldest tier, which is what the staleness banner must speak for: a
    /// board whose git is 40 s old and whose procs are 1 s old is 40 s old.
    public func oldest() -> Double? {
        [worktrees, procs, transcripts, git].compactMap { $0 }.min()
    }
}

/// `GET /api/health` — the one route that answers with no token, and therefore
/// the one route whose payload is a security decision. It says a server that
/// speaks this protocol is alive here and what its clock reads, and nothing that
/// varies with what the fleet is doing.
public struct ServerHealth: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let service: String
    public let api: String
    /// The server's clock. Every other route's timestamps are unreadable to a
    /// client whose own clock is wrong, so this is the skew source.
    public let time: Double

    public init(ok: Bool, service: String, api: String, time: Double) {
        self.ok = ok
        self.service = service
        self.api = api
        self.time = time
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        service = try c.decodeIfPresent(String.self, forKey: .service) ?? ""
        api = try c.decodeIfPresent(String.self, forKey: .api) ?? ""
        time = try c.decodeIfPresent(Double.self, forKey: .time) ?? 0
    }

    enum CodingKeys: String, CodingKey { case ok, service, api, time }
}
