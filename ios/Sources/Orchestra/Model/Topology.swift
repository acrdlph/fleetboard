import Foundation

// The branch map's data, modelled from the LIVE server rather than from the docs.
//
// `UX.md` §5.3 and `API.md` §9.7 describe a `GET /api/v1/topology` that ships
// `worktree_id`, `subject_short`, `base_ts`, `dropped[]`, an `axis` block with
// `s`/`anchor_age_s`, a `role`, and `_at` timestamp spellings. **None of that
// endpoint exists.** Decoded live on 2026-07-23 from `GET /api/topology` — the
// only topology route `server.py` serves (`server.py:335`) — against the real
// nine-worktree fleet, the payload is the LEGACY shape `gitrepo.branch_topology`
// writes:
//
//   * top level `generated_at` (not `at`/`fetched_at`/`epoch`/`seq`)
//   * groups carry `repo`, `base`, `trunk_ts`, `trunk_commits`, `branches`
//   * a branch carries `worktree` (the name, not a `worktree_id`), `branch`,
//     `fork_ts`/`tip_ts` (the `_ts` spelling, not `_at`), `ahead`, `behind`,
//     `dirty`, `hash`, `subject` (UNTRUNCATED — `subject_short` is never
//     written), and `commits` (epoch seconds, newest first, capped 40)
//   * there is NO `dropped[]`, NO per-branch `base_ts`, NO `axis`, NO `role`
//
// The consequences for this build, each honest rather than papered over:
//
//   1. The axis scalars (`s`, `anchor_age_s`) are not on the wire, so the client
//      derives the clamped per-group anchor from the fork set itself. §5.3's ⚠
//      note says exactly this is possible; its "client and server never disagree"
//      guarantee is then vacuous, which is fine — there is one computer deciding
//      the axis, so there is nobody to disagree with. See `BranchMap.axis`.
//   2. `role` is derived client-side from `ahead`/`behind` — §5.6 says it is
//      derivable and the server does not ship it, so this is the only path.
//   3. `subject` arrives untruncated; the row truncates once, at draw time.
//   4. The stale-`behind` marker of §5.8 has NO input — `base_ts` is computed
//      server-side (`gitrepo.py:205`) and dropped before serialisation — so it
//      cannot be drawn, and this build does not pretend to. Noted in `ios/README`.
//   5. Dropped worktrees are surfaced by DIFFERENCE against the board's worktree
//      list, not read from a `dropped[]` the server never sends (§5.10 unmapped).

/// `GET /api/topology`.
public struct Topology: Sendable, Equatable, Decodable {
    public let generatedAt: Double
    public let groups: [TopoGroup]

    public init(generatedAt: Double, groups: [TopoGroup]) {
        self.generatedAt = generatedAt
        self.groups = groups
    }

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case groups
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try c.decodeIfPresent(Double.self, forKey: .generatedAt)
            ?? Date().timeIntervalSince1970
        groups = try c.decodeIfPresent([TopoGroup].self, forKey: .groups) ?? []
    }

    public var generated: Date { Date(timeIntervalSince1970: generatedAt) }

    /// Every worktree the topology could place, across all groups. The set the
    /// board's worktree list is differenced against to find what was dropped.
    public var mappedWorktrees: Set<String> {
        Set(groups.flatMap { $0.branches.map(\.worktree) })
    }
}

/// One repo's trunk and the branches measured against it. Groups never share a
/// trunk (`gitrepo.py` keys them by `origin` url), so each carries its own axis.
public struct TopoGroup: Sendable, Equatable, Decodable, Identifiable {
    /// The repo slug — `key.rsplit("/")[-1]` with `.git` stripped. Two clones of
    /// the same origin collapse into one group, so this is a stable id.
    public var id: String { repo + "|" + base }
    public let repo: String
    public let base: String
    /// The freshest `origin/<main>` tip across the group's clones — the shared
    /// reference caret every row draws (`gitrepo.py:222`).
    public let trunkTs: Double
    /// Trunk commit epochs, newest first, capped 40.
    public let trunkCommits: [Double]
    public let branches: [TopoBranch]

    public init(repo: String, base: String, trunkTs: Double,
                trunkCommits: [Double], branches: [TopoBranch]) {
        self.repo = repo
        self.base = base
        self.trunkTs = trunkTs
        self.trunkCommits = trunkCommits
        self.branches = branches
    }

    enum CodingKeys: String, CodingKey {
        case repo, base, branches
        case trunkTs = "trunk_ts"
        case trunkCommits = "trunk_commits"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        repo = try c.decodeIfPresent(String.self, forKey: .repo) ?? "?"
        base = try c.decodeIfPresent(String.self, forKey: .base) ?? "?"
        trunkTs = try c.decodeIfPresent(Double.self, forKey: .trunkTs) ?? 0
        trunkCommits = try c.decodeIfPresent([Double].self, forKey: .trunkCommits) ?? []
        branches = try c.decodeIfPresent([TopoBranch].self, forKey: .branches) ?? []
    }

    public var trunk: Date { Date(timeIntervalSince1970: trunkTs) }
}

/// Where one worktree's branch really sits: its merge-base fork, its tip, and the
/// drift either side of the trunk.
public struct TopoBranch: Sendable, Equatable, Decodable, Identifiable {
    /// The worktree name — the join key into the board, exactly as `discover_worktrees`
    /// produced it for both payloads.
    public var id: String { worktree }
    public let worktree: String
    /// `"?"` for a detached HEAD (`git branch --show-current` empty).
    public let branch: String
    /// The merge-base timestamp. The server already clamps `min(fork_ts, tip_ts)`
    /// (`gitrepo.py:228`), so a fork never draws right of its own tip.
    public let forkTs: Double
    public let tipTs: Double
    public let ahead: Int
    public let behind: Int
    public let dirty: Int
    public let hash: String
    /// **Untruncated** on this wire. The row clips it once, at draw time.
    public let subject: String
    /// Commit epochs `mb..HEAD`, newest first, capped 40. Not drawn in v1 (§5.7)
    /// but carried so the count and the oldest stamp are honest.
    public let commits: [Double]

    public init(worktree: String, branch: String, forkTs: Double, tipTs: Double,
                ahead: Int, behind: Int, dirty: Int, hash: String,
                subject: String, commits: [Double]) {
        self.worktree = worktree
        self.branch = branch
        self.forkTs = forkTs
        self.tipTs = tipTs
        self.ahead = ahead
        self.behind = behind
        self.dirty = dirty
        self.hash = hash
        self.subject = subject
        self.commits = commits
    }

    enum CodingKeys: String, CodingKey {
        case worktree, branch, ahead, behind, dirty, hash, subject, commits
        case forkTs = "fork_ts"
        case tipTs = "tip_ts"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        worktree = try c.decodeIfPresent(String.self, forKey: .worktree) ?? "?"
        branch = try c.decodeIfPresent(String.self, forKey: .branch) ?? "?"
        forkTs = try c.decodeIfPresent(Double.self, forKey: .forkTs) ?? 0
        tipTs = try c.decodeIfPresent(Double.self, forKey: .tipTs) ?? 0
        ahead = try c.decodeIfPresent(Int.self, forKey: .ahead) ?? 0
        behind = try c.decodeIfPresent(Int.self, forKey: .behind) ?? 0
        dirty = try c.decodeIfPresent(Int.self, forKey: .dirty) ?? 0
        hash = try c.decodeIfPresent(String.self, forKey: .hash) ?? ""
        subject = try c.decodeIfPresent(String.self, forKey: .subject) ?? ""
        commits = try c.decodeIfPresent([Double].self, forKey: .commits) ?? []
    }

    public var tip: Date { Date(timeIntervalSince1970: tipTs) }
    public var fork: Date { Date(timeIntervalSince1970: forkTs) }
    /// Detached HEAD — work on no branch, the strongest stall signal in the
    /// payload (§5.10).
    public var isDetached: Bool { branch == "?" }
}
