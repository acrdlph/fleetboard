import Foundation

/// The branch map's arithmetic, kept out of the view so it can be pinned by a
/// test. Everything here is `UX.md` §5.3–5.7, adapted to the fact that the real
/// server ships neither the axis scalars nor `role` (see `Topology.swift`).
public enum BranchMap {

    // MARK: - Roles (§5.6)

    /// Three roles, not two. `ahead == 0` is not "nothing to know": a branch can
    /// sit at the trunk's OLD tip, hundreds behind, and dispatching into it starts
    /// that far back. `ConfidAI-security-audit` on the live fleet: `ahead == 0`,
    /// 172 behind — `stale`, not `parked`.
    public enum Role: Sendable, Equatable {
        /// Real divergence — has its own commits. Full row, main section.
        case diverged
        /// At the trunk's old tip and behind. Full row, `⌁ stale checkout` note.
        case stale
        /// At the trunk tip with nothing behind. Collapsible when also idle.
        case parked
    }

    public static func role(ahead: Int, behind: Int) -> Role {
        if ahead > 0 { return .diverged }
        return behind <= 0 ? .parked : .stale
    }

    public static func role(_ b: TopoBranch) -> Role { role(ahead: b.ahead, behind: b.behind) }

    // MARK: - Debt tiers (§5.4)

    /// A FIXED log ladder, never fleet-relative quantiles — a worktree must not
    /// change colour because an unrelated one moved. Returns `nil` for `behind == 0`
    /// (the chip is not shown at all).
    public enum DebtTier: Int, Sendable, Equatable {
        case low = 1       // 1–9      textDisabled
        case moderate = 2  // 10–99    statusLimit
        case high = 3      // 100–999  statusTurn
        case severe = 4    // ≥ 1000   statusNeeds
    }

    public static func debtTier(_ behind: Int) -> DebtTier? {
        switch behind {
        case ..<1: nil
        case 1..<10: .low
        case 10..<100: .moderate
        case 100..<1000: .high
        default: .severe
        }
    }

    // MARK: - The axis (§5.3)

    /// The log knee. A constant on both platforms (`map.html` uses 900), and the
    /// only reason the server was ever going to ship it was the disagree-guarantee
    /// that a single-computer axis makes moot.
    public static let axisS: Double = 900.0
    /// The anchor is floored here so a young repo is not compressed into a sliver.
    public static let axisFloorS: Double = 6 * 3600

    /// A reversed, clamped, offset log scale mapping an epoch to `[0, 1]`, where
    /// `1.0` is `now` and `0.0` is the clamped anchor. Negative means older than
    /// the anchor — the `⟨` cap at the left margin.
    ///
    /// `now` is explicit, not `Date()`, for the reason every rule in this app
    /// takes one: a scale that reads a hidden clock can only be tested by waiting.
    public struct AxisScale: Sendable, Equatable {
        public let now: Double
        public let s: Double
        public let anchorAgeS: Double
        public let denom: Double

        public init(now: Double, anchorAgeS: Double, s: Double = BranchMap.axisS) {
            self.now = now
            self.s = s
            self.anchorAgeS = max(anchorAgeS, BranchMap.axisFloorS)
            self.denom = log1p(self.anchorAgeS / s)
        }

        /// `1.0` = now, `0.0` = the anchor, `< 0` = older than the anchor.
        public func u(_ ts: Double) -> Double {
            1.0 - log1p(max(0, now - ts) / s) / denom
        }

        /// Clamped to `[0, 1]` and mapped into `[padL, padL + width]`.
        public func x(_ ts: Double, padL: Double, width: Double) -> Double {
            padL + width * min(1.0, max(0.0, u(ts)))
        }

        public func isClipped(_ ts: Double) -> Bool { u(ts) < -0.001 }
    }

    /// The clamped per-group anchor, computed from the fork set because the server
    /// does not ship it. The oldest fork, but never more than `6 ×` the 75th
    /// percentile of fork ages — so one five-months-old outlier does not smear the
    /// other eight into the last few points of the axis. Floored at 6 h by
    /// `AxisScale`. On the live fleet this clamps a 146-day outlier down to a
    /// ~325 h anchor and clips exactly that one branch.
    public static func axis(for group: TopoGroup, now: Double) -> AxisScale {
        let ages = group.branches.map { max(0, now - $0.forkTs) }.sorted()
        guard let oldest = ages.last else {
            return AxisScale(now: now, anchorAgeS: axisFloorS)
        }
        let clamp = 6 * percentile(ages, 0.75)
        return AxisScale(now: now, anchorAgeS: min(oldest, clamp))
    }

    /// Type-7 (linear-interpolation) percentile, the numpy/`quantile` default. A
    /// named, testable function rather than an index expression buried in `axis`,
    /// because which branch clips depends on it.
    public static func percentile(_ sortedAsc: [Double], _ q: Double) -> Double {
        guard let first = sortedAsc.first else { return 0 }
        if sortedAsc.count == 1 { return first }
        let pos = q * Double(sortedAsc.count - 1)
        let lo = Int(pos.rounded(.down))
        let hi = min(lo + 1, sortedAsc.count - 1)
        let frac = pos - Double(lo)
        return sortedAsc[lo] + (sortedAsc[hi] - sortedAsc[lo]) * frac
    }

    // MARK: - Sorting and sectioning (§5.10)

    public enum Sort: String, Sendable, CaseIterable, Identifiable {
        case status, debt, recent, name
        public var id: String { rawValue }
        public var label: String {
            switch self {
            case .status: "status"
            case .debt: "debt"
            case .recent: "recent"
            case .name: "name"
            }
        }
    }

    public enum Range: String, Sendable, CaseIterable, Identifiable {
        case week, month, all
        public var id: String { rawValue }
        public var label: String {
            switch self {
            case .week: "7d"
            case .month: "30d"
            case .all: "all"
            }
        }
        /// The oldest fork age this range admits, in seconds. `nil` = no bound.
        public var maxForkAgeS: Double? {
            switch self {
            case .week: 7 * 86400
            case .month: 30 * 86400
            case .all: nil
            }
        }
    }

    /// A worktree whose tip has not moved in this long is structurally stalled and
    /// drops into a trailing section, regardless of sort — the map's flagship
    /// insight expressed by POSITION, not colour (§5.10).
    public static let stalledAfterS: Double = 7 * 86400

    public static func isStalled(_ b: TopoBranch, now: Double) -> Bool {
        (now - b.tipTs) > stalledAfterS
    }

    /// A parked worktree collapses only when it is also idle. `dirty == 0` we have
    /// from the topology; the live board status is the other half, joined by name.
    public static func isCollapsibleParked(_ b: TopoBranch, section: BoardSection?) -> Bool {
        role(b) == .parked && b.dirty == 0 && (section == nil || section == .free)
    }

    /// One branch, everything the view needs pre-decided so the row is dumb.
    public struct Row: Sendable, Equatable, Identifiable {
        public var id: String { branch.worktree }
        public let branch: TopoBranch
        public let role: Role
        public let tier: DebtTier?
        /// The live board section for this worktree, joined by name. `nil` when the
        /// board has no card for it (rare — same source — but honest).
        public let section: BoardSection?
        public let stalled: Bool

        public init(branch: TopoBranch, section: BoardSection?, now: Double) {
            self.branch = branch
            self.role = BranchMap.role(branch)
            self.tier = BranchMap.debtTier(branch.behind)
            self.section = section
            self.stalled = BranchMap.isStalled(branch, now: now)
        }
    }

    /// A group's rows, filtered by range, split into the three visible buckets, and
    /// sorted. The split is structural (§5.10): stalled and collapsible-parked
    /// leave the main list no matter the sort.
    public struct Placed: Sendable, Equatable {
        public let main: [Row]
        public let stalled: [Row]
        public let parked: [Row]
        public init(main: [Row], stalled: [Row], parked: [Row]) {
            self.main = main
            self.stalled = stalled
            self.parked = parked
        }
    }

    public static func place(_ group: TopoGroup,
                             sections: [String: BoardSection],
                             sort: Sort, range: Range, now: Double) -> Placed {
        let maxAge = range.maxForkAgeS
        let rows = group.branches
            .filter { maxAge == nil || (now - $0.forkTs) <= maxAge! }
            .map { Row(branch: $0, section: sections[$0.worktree], now: now) }

        var main: [Row] = [], stalled: [Row] = [], parked: [Row] = []
        for r in rows {
            if isCollapsibleParked(r.branch, section: r.section) { parked.append(r) }
            else if r.stalled { stalled.append(r) }
            else { main.append(r) }
        }
        return Placed(main: sorted(main, by: sort),
                      stalled: sorted(stalled, by: sort),
                      parked: parked.sorted { $0.branch.worktree.localizedCaseInsensitiveCompare($1.branch.worktree) == .orderedAscending })
    }

    static func sorted(_ rows: [Row], by sort: Sort) -> [Row] {
        switch sort {
        case .name:
            return rows.sorted {
                $0.branch.worktree.localizedCaseInsensitiveCompare($1.branch.worktree) == .orderedAscending
            }
        case .debt:
            // Behind, descending — the whole point of the sort. Name breaks ties
            // so the order is stable across refreshes.
            return rows.sorted {
                $0.branch.behind != $1.branch.behind
                    ? $0.branch.behind > $1.branch.behind
                    : $0.branch.worktree.localizedCaseInsensitiveCompare($1.branch.worktree) == .orderedAscending
            }
        case .recent:
            return rows.sorted { $0.branch.tipTs > $1.branch.tipTs }
        case .status:
            // Board severity first (needs-you above busy above free), then the
            // freshest tip. A branch the board does not place sorts last.
            return rows.sorted {
                let a = $0.section?.mapRank ?? Int.max
                let b = $1.section?.mapRank ?? Int.max
                if a != b { return a < b }
                return $0.branch.tipTs > $1.branch.tipTs
            }
        }
    }
}

extension BoardSection {
    /// Severity order for the map's `status` sort — the board's own section order.
    var mapRank: Int {
        switch self {
        case .needsYou: 0
        case .yourTurn: 1
        case .working: 2
        case .limited: 3
        case .free: 4
        }
    }
}
