import Foundation
import Testing
@testable import OrchestraKit

/// The branch map, against the payload the server REALLY produces.
///
/// The fixture is a `GET /api/topology` body captured live on 2026-07-23 from the
/// nine-worktree fleet, with commit subjects replaced by filler of the same
/// length (and same trailing ellipsis) — everything that decides a decode or a
/// placement is untouched. It is the LEGACY shape (`generated_at`, `fork_ts`,
/// `subject`, no `worktree_id`/`axis`/`role`/`dropped`), because that is the only
/// topology route the server serves.
struct BranchMapTests {

    static func topology() throws -> Topology {
        let url = try #require(Bundle.module.url(forResource: "Fixtures/topology",
                                                 withExtension: "json"))
        return try JSONDecoder().decode(Topology.self, from: try Data(contentsOf: url))
    }

    // MARK: - Decode

    @Test func decodesTheRealTopology() throws {
        let t = try Self.topology()
        #expect(t.groups.count == 1)
        let g = try #require(t.groups.first)
        #expect(g.repo == "confidai")
        #expect(g.base == "origin/main")
        #expect(g.branches.count == 9)
        #expect(g.trunkTs > 1_700_000_000)
        #expect(t.generatedAt > 1_700_000_000)
    }

    /// The join key is the worktree NAME, not a `worktree_id` — because the
    /// legacy payload has no id, and the board's cards key on the same name from
    /// the same `discover_worktrees`. A test that expects an id would pass against
    /// the doc and crash against the server.
    @Test func branchesCarryAWorktreeNameNotAnId() throws {
        let g = try #require(try Self.topology().groups.first)
        #expect(g.branches.map(\.worktree).contains("ConfidAI3"))
        #expect(Set(g.branches.map(\.worktree)).count == 9)
    }

    /// A detached HEAD is `"?"` on the wire, and it is a real state on this fleet
    /// (two of nine). A row must render it, not treat it as missing data.
    @Test func detachedHeadIsCarried() throws {
        let g = try #require(try Self.topology().groups.first)
        let detached = g.branches.filter(\.isDetached)
        #expect(detached.count == 2)
    }

    // MARK: - Roles (§5.6)

    @Test func rolesAreThreeNotTwo() {
        #expect(BranchMap.role(ahead: 83, behind: 500) == .diverged)
        // ahead == 0 is NOT nothing to know: behind > 0 is `stale`, the case the
        // whole three-role split exists for.
        #expect(BranchMap.role(ahead: 0, behind: 172) == .stale)
        #expect(BranchMap.role(ahead: 0, behind: 0) == .parked)
    }

    @Test func liveFleetRoles() throws {
        let g = try #require(try Self.topology().groups.first)
        func role(_ w: String) -> BranchMap.Role? {
            g.branches.first { $0.worktree == w }.map(BranchMap.role)
        }
        #expect(role("ConfidAI3") == .diverged)   // 83 ahead
        #expect(role("ConfidAI4") == .stale)      // 0 ahead, 135 behind
        #expect(role("ConfidAI2") == .parked)     // 0/0
    }

    // MARK: - Debt tiers (§5.4)

    @Test func debtTiersAreAFixedLadder() {
        #expect(BranchMap.debtTier(0) == nil)
        #expect(BranchMap.debtTier(9) == .low)
        #expect(BranchMap.debtTier(10) == .moderate)
        #expect(BranchMap.debtTier(99) == .moderate)
        #expect(BranchMap.debtTier(100) == .high)
        #expect(BranchMap.debtTier(999) == .high)
        #expect(BranchMap.debtTier(1000) == .severe)
        #expect(BranchMap.debtTier(1898) == .severe)
    }

    // MARK: - Percentile and the clamped axis (§5.3)

    @Test func percentileIsType7() {
        #expect(BranchMap.percentile([1, 2, 3, 4], 0.75) == 3.25)
        #expect(BranchMap.percentile([10], 0.75) == 10)
        #expect(BranchMap.percentile([], 0.75) == 0)
    }

    /// The whole reason for the clamp: one 146-day outlier must NOT smear the
    /// other eight into a sliver. With a global anchor every young fork sits in
    /// the last few points; with the clamp exactly the outlier clips and the rest
    /// spread across the axis.
    @Test func axisClampsTheOutlierAndKeepsTheRestOnScale() throws {
        let t = try Self.topology()
        let g = try #require(t.groups.first)
        let now = t.generatedAt
        let axis = BranchMap.axis(for: g, now: now)

        let clipped = g.branches.filter { axis.isClipped($0.forkTs) }
        #expect(clipped.count == 1, "exactly the 146-day outlier clips")
        #expect(clipped.first?.worktree == "ConfidAi5")

        // The anchor is the clamp (6 × p75), well under the raw oldest fork age.
        let ages = g.branches.map { now - $0.forkTs }.sorted()
        #expect(axis.anchorAgeS < ages.last!)
        #expect(axis.anchorAgeS >= BranchMap.axisFloorS)

        // Every non-clipped fork lands strictly inside [0, 1].
        for b in g.branches where b.worktree != "ConfidAi5" {
            let u = axis.u(b.forkTs)
            #expect(u >= 0 && u <= 1.0001)
        }
    }

    @Test func axisFloorsAYoungGroup() {
        // A group whose forks are all minutes old must not divide by a near-zero
        // span. The 6 h floor holds.
        let now = 1_784_000_000.0
        let young = TopoGroup(repo: "x", base: "origin/main", trunkTs: now,
                              trunkCommits: [],
                              branches: [tb("a", ahead: 3, forkAgeS: 300),
                                         tb("b", ahead: 1, forkAgeS: 120)])
        let axis = BranchMap.axis(for: young, now: now)
        #expect(axis.anchorAgeS == BranchMap.axisFloorS)
    }

    /// `now` = the anchor maps to 1.0; the anchor age maps to 0.0.
    @Test func axisEndpoints() {
        let axis = BranchMap.AxisScale(now: 1000, anchorAgeS: 100 * 3600)
        #expect(abs(axis.u(1000) - 1.0) < 1e-9)
        #expect(abs(axis.u(1000 - axis.anchorAgeS) - 0.0) < 1e-9)
    }

    // MARK: - Placement, sorting, stall detection (§5.10)

    @Test func parkedIdleBranchesCollapseOutOfTheMainList() throws {
        let g = try #require(try Self.topology().groups.first)
        let now = try Self.topology().generatedAt
        let placed = BranchMap.place(g, sections: [:], sort: .status, range: .all, now: now)
        // ConfidAI2 (0/0, clean) is the only collapsible-parked branch.
        #expect(placed.parked.map(\.branch.worktree) == ["ConfidAI2"])
        #expect(!placed.main.contains { $0.branch.worktree == "ConfidAI2" })
    }

    @Test func debtSortRanksTheMostBehindFirst() throws {
        let g = try #require(try Self.topology().groups.first)
        let now = try Self.topology().generatedAt
        let placed = BranchMap.place(g, sections: [:], sort: .debt, range: .all, now: now)
        // ConfidAi5 is 1898 behind — the worst, and diverged so it stays in main.
        #expect(placed.main.first?.branch.worktree == "ConfidAi5")
    }

    @Test func nameSortIsAlphabetical() throws {
        let g = try #require(try Self.topology().groups.first)
        let now = try Self.topology().generatedAt
        let placed = BranchMap.place(g, sections: [:], sort: .name, range: .all, now: now)
        let names = placed.main.map(\.branch.worktree)
        #expect(names == names.sorted { $0.localizedCaseInsensitiveCompare($1) == .orderedAscending })
    }

    @Test func stalledBranchesLeaveTheMainListByPosition() {
        let now = 1_784_000_000.0
        let g = TopoGroup(repo: "x", base: "origin/main", trunkTs: now, trunkCommits: [],
                          branches: [
                            // tip 10 days old, has commits → diverged AND stalled
                            tb("old", ahead: 5, forkAgeS: 20 * 86400, tipAgeS: 10 * 86400),
                            // tip 1 hour old → not stalled
                            tb("fresh", ahead: 5, forkAgeS: 2 * 86400, tipAgeS: 3600),
                          ])
        let placed = BranchMap.place(g, sections: [:], sort: .status, range: .all, now: now)
        #expect(placed.stalled.map(\.branch.worktree) == ["old"])
        #expect(placed.main.map(\.branch.worktree) == ["fresh"])
    }

    @Test func rangeFilterDropsOldForks() throws {
        let g = try #require(try Self.topology().groups.first)
        let now = try Self.topology().generatedAt
        // 7d window excludes the 146-day fork and any fork older than a week.
        let week = BranchMap.place(g, sections: [:], sort: .name, range: .week, now: now)
        let all = BranchMap.place(g, sections: [:], sort: .name, range: .all, now: now)
        let weekCount = week.main.count + week.stalled.count + week.parked.count
        let allCount = all.main.count + all.stalled.count + all.parked.count
        #expect(weekCount < allCount)
        #expect(!week.main.contains { $0.branch.worktree == "ConfidAi5" })
    }

    // MARK: - helpers

    static func tb(_ name: String, ahead: Int = 0, behind: Int = 0, dirty: Int = 0,
                   forkAgeS: Double, tipAgeS: Double = 0, now: Double = 1_784_000_000.0) -> TopoBranch {
        TopoBranch(worktree: name, branch: "feat/\(name)",
                   forkTs: now - forkAgeS, tipTs: now - tipAgeS,
                   ahead: ahead, behind: behind, dirty: dirty, hash: "abc1234",
                   subject: "s", commits: [])
    }

    func tb(_ name: String, ahead: Int = 0, behind: Int = 0, dirty: Int = 0,
            forkAgeS: Double, tipAgeS: Double = 0) -> TopoBranch {
        Self.tb(name, ahead: ahead, behind: behind, dirty: dirty,
                forkAgeS: forkAgeS, tipAgeS: tipAgeS)
    }
}
