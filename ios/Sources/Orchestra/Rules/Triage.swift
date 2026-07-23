import Foundation

/// The board's sections — and the one place the server's four availabilities are
/// widened into `UX.md` §3.1.2's five.
///
/// **This file exists because of a documented gap, and it is meant to be
/// deleted.** `UX.md` §3.1.2 specifies a five-valued `availability`
/// (`needs_you`, `your_turn`, `busy`, `limited`, `free`) and states plainly that
/// it "is a required change to `API.md` §10.2, not a description of it". The
/// server ships the legacy four — verified against a live nine-worktree fleet on
/// 2026-07-22, which returned only `free`, `attention` and `busy`.
///
/// The defect the five exist to fix is real and visible in that capture:
/// `card_availability` returns `attention` both for "an agent is blocked and
/// needs you" and for "everyone is parked at the prompt", so a card that is
/// merely idle renders under the same alarm as one that is stuck.
///
/// Splitting it here violates `UX.md`'s principle 3 (no client-side severity) and
/// that is stated rather than hidden. The mitigation is containment: the
/// derivation is one pure function, tested against the server's own
/// `card_availability` ladder case for case, and when the server starts shipping
/// the five it collapses to reading one field.
public enum BoardSection: String, Sendable, CaseIterable, Hashable {
    case needsYou
    case yourTurn
    case working
    case limited
    case free

    public var title: String {
        switch self {
        case .needsYou: "NEEDS YOU"
        case .yourTurn: "YOUR TURN"
        case .working:  "WORKING"
        case .limited:  "WAITING ON LIMITS"
        case .free:     "FREE"
        }
    }

    /// The badge word on the card itself, which is not always the section title.
    public var badge: String {
        switch self {
        case .needsYou: "NEEDS YOU"
        case .yourTurn: "YOUR TURN"
        case .working:  "BUSY"
        case .limited:  "WAITING"
        case .free:     "FREE"
        }
    }

    /// `WORKING`, `FREE` and `OTHER AGENTS` are collapsed by default — a phone
    /// screen is not big enough to spend on the things that are fine.
    public var collapsedByDefault: Bool { self == .working || self == .free }
}

public enum Triage {
    /// Which section a card belongs in.
    ///
    /// The ladder is `status.card_availability`'s, in its order, with the one
    /// branch that conflates two meanings split in two. Every other rung returns
    /// exactly what the server would have returned.
    ///
    /// - Note: `handed_to` sessions are filtered FIRST, matching
    ///   `observer._attention_statuses` — a limit session whose work continued
    ///   on another account is not what the card is about.
    public static func section(for card: Worktree) -> BoardSection {
        let statuses = card.sessions
            .filter { !($0.status == .limit && $0.handedTo != nil) }
            .map(\.status)
        let hasLive = !card.liveProcs.isEmpty
        let working = statuses.contains(.working)

        // Rung 1, verbatim: nothing alive and nothing working — safe to point a
        // new agent here.
        if !(hasLive || working) { return .free }
        // Rung 2, verbatim.
        if statuses.contains(where: { $0.isAttention }) { return .needsYou }
        // Rung 3 — the SPLIT. The server returns `attention` here too; this is
        // "everyone is parked at the prompt", which needs direction, not rescue.
        if statuses.contains(.waiting) && !working { return .yourTurn }
        // Rung 4, verbatim: out of juice, not out of instructions.
        if statuses.contains(.limit) && !working { return .limited }
        return .working
    }

    /// What the server WOULD have said, from the same inputs. Used by the test
    /// that pins the split to the ladder it was split out of: fold `.needsYou`
    /// and `.yourTurn` back together and every card must land where
    /// `card_availability` put it.
    public static func serverAvailability(for section: BoardSection) -> Availability {
        switch section {
        case .needsYou, .yourTurn: .attention
        case .working: .busy
        case .limited: .waiting
        case .free: .free
        }
    }

    /// The board, sectioned, with the server's order preserved INSIDE each
    /// section. The client never sorts — `order` is the server's triage opinion
    /// and re-deriving it on a phone is how two clients start disagreeing.
    public static func groups(_ cards: [Worktree]) -> [Group] {
        var buckets: [BoardSection: [Worktree]] = [:]
        for card in cards {
            buckets[section(for: card), default: []].append(card)
        }
        return BoardSection.allCases.compactMap { s in
            guard let cards = buckets[s], !cards.isEmpty else { return nil }
            return Group(section: s, cards: cards)
        }
    }

    public struct Group: Sendable, Equatable, Identifiable {
        public var id: BoardSection { section }
        public let section: BoardSection
        public let cards: [Worktree]

        public init(section: BoardSection, cards: [Worktree]) {
            self.section = section
            self.cards = cards
        }
    }

    /// The headline counts WORKTREES, not sessions, because that is what a
    /// person reasons about on a phone.
    ///
    /// `UX.md` §3.1.3 has the server shipping this precomputed as
    /// `counts.cards`. It does not — `observer.py:245` writes six session-level
    /// keys and nothing else — so the arithmetic the spec says the client must
    /// not do happens here, in one function, and disappears the day the server
    /// ships the block.
    public static func cardCounts(_ cards: [Worktree]) -> [BoardSection: Int] {
        var out: [BoardSection: Int] = [:]
        for card in cards { out[section(for: card), default: 0] += 1 }
        return out
    }

    /// The two lines above the board.
    ///
    /// Line 1 is the answer to "who needs me", and zero is not "0 need you" — it
    /// is good news, said as good news. Line 2 is everything line 1 did not
    /// already say.
    public struct Headline: Sendable, Equatable {
        public let text: String
        /// Which section's hue the line takes.
        public let tone: BoardSection
        public let subhead: String

        public init(text: String, tone: BoardSection, subhead: String) {
            self.text = text
            self.tone = tone
            self.subhead = subhead
        }
    }

    public static func headline(_ cards: [Worktree]) -> Headline {
        let counts = cardCounts(cards)
        let needs = counts[.needsYou] ?? 0
        if needs > 0 {
            return Headline(text: "\(needs) need\(needs == 1 ? "s" : "") you",
                            tone: .needsYou, subhead: subhead(cards))
        }
        let turn = counts[.yourTurn] ?? 0
        if turn > 0 {
            // The one case where line 1 spends a section that line 2 also lists.
            return Headline(text: "\(turn) waiting on you", tone: .yourTurn,
                            subhead: subhead(cards, excluding: .yourTurn))
        }
        if (counts[.working] ?? 0) > 0 {
            return Headline(text: "all clear", tone: .working, subhead: subhead(cards))
        }
        return Headline(text: "nothing running", tone: .free, subhead: subhead(cards))
    }

    /// Line 2: everything the headline did not already say, in a fixed order so
    /// the eye learns where to look. Sections with nothing in them are omitted
    /// rather than printed as zero, and the section the headline spoke for is
    /// dropped — a subhead that repeats the 34 pt line above it is noise.
    public static func subhead(_ cards: [Worktree], excluding spoken: BoardSection? = nil) -> String {
        let counts = cardCounts(cards)
        let order: [(BoardSection, String)] = [
            (.working, "busy"), (.yourTurn, "waiting on you"),
            (.limited, "limited"), (.free, "free"),
        ]
        let parts = order.compactMap { section, word -> String? in
            guard section != spoken, let n = counts[section], n > 0 else { return nil }
            return "\(n) \(word)"
        }
        return parts.joined(separator: " · ")
    }
}
