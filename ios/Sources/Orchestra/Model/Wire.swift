import Foundation

// The board payload, modelled from a LIVE server rather than from API.md.
//
// Captured 2026-07-22 from `GET /api/state` on a real nine-worktree fleet
// (38,615 bytes, 9 worktrees, 36 sessions, 4 live procs, 5 other procs) and
// cross-checked against `GET /api/events`'s snapshot frame. Every optionality
// below is a fact about that capture or about the code that writes it, and the
// ones that differ from the document are listed in `ios/README.md`.
//
// Two rules hold everywhere in this file:
//
//   1. **A field the server omits is `Optional` in Swift, not defaulted at the
//      decoder.** `turn_ended` is written by `transcripts.py` only on the path
//      that computed it — 33 of 36 live sessions carried it, 3 did not — and a
//      non-optional `Bool` there throws `keyNotFound` and takes the whole board
//      with it.
//   2. **A field the server can write as `null` is `Optional` even when it is
//      always present.** `git.ahead`/`git.behind` are absent-as-null whenever a
//      branch has no upstream: 2 of 9 live worktrees, and `↑null` is not a
//      thing the row can render.

/// `GET /api/state`.
///
/// Note what is NOT here and rides only on `GET /api/events`: `v` (the version),
/// `order` (the board's triage order) and `freshness`. And what rides only here:
/// `hostname`, `user`, `free_worktrees`, `resumes`. The two payloads are
/// deliberately different shapes — `observer.delta_since`'s docstring is the
/// authority and it says why for each field.
public struct FleetState: Sendable, Equatable, Decodable {
    public let generatedAt: Double
    public let hostname: String
    public let user: String
    public let counts: Counts
    public let freeWorktrees: [String]
    public let worktrees: [Worktree]
    public let otherProcs: [OtherProc]
    /// Keyed `"{worktree}|{sid}"` with a literal pipe (`resume.py:68`).
    public let resumes: [String: ResumeSchedule]

    public init(generatedAt: Double, hostname: String, user: String, counts: Counts,
                freeWorktrees: [String], worktrees: [Worktree],
                otherProcs: [OtherProc], resumes: [String: ResumeSchedule]) {
        self.generatedAt = generatedAt
        self.hostname = hostname
        self.user = user
        self.counts = counts
        self.freeWorktrees = freeWorktrees
        self.worktrees = worktrees
        self.otherProcs = otherProcs
        self.resumes = resumes
    }

    enum CodingKeys: String, CodingKey {
        case generatedAt = "generated_at"
        case hostname, user, counts
        case freeWorktrees = "free_worktrees"
        case worktrees
        case otherProcs = "other_procs"
        case resumes
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        generatedAt = try c.decode(Double.self, forKey: .generatedAt)
        hostname = try c.decodeIfPresent(String.self, forKey: .hostname) ?? ""
        user = try c.decodeIfPresent(String.self, forKey: .user) ?? ""
        counts = try c.decodeIfPresent(Counts.self, forKey: .counts) ?? Counts()
        freeWorktrees = try c.decodeIfPresent([String].self, forKey: .freeWorktrees) ?? []
        worktrees = try c.decode([Worktree].self, forKey: .worktrees)
        otherProcs = try c.decodeIfPresent([OtherProc].self, forKey: .otherProcs) ?? []
        resumes = try c.decodeIfPresent([String: ResumeSchedule].self, forKey: .resumes) ?? [:]
    }

    public var generated: Date { Date(timeIntervalSince1970: generatedAt) }
}

/// Session-level tallies. `observer.py:245` — six keys, always all six.
///
/// `UX.md` §3.1.3 specifies a nested `{"sessions": {...}, "cards": {...}}` shape
/// so the headline can count WORKTREES without client arithmetic. The server
/// ships the flat session-level dict below and nothing else; the card tallies
/// are derived in `Triage.swift` and that derivation is the thing to delete when
/// the server starts shipping them.
public struct Counts: Sendable, Equatable, Codable, Hashable {
    public var working: Int = 0
    public var needsInput: Int = 0
    public var limit: Int = 0
    public var blocked: Int = 0
    public var waiting: Int = 0
    public var ended: Int = 0

    public init() {}

    enum CodingKeys: String, CodingKey {
        case working, limit, blocked, waiting, ended
        case needsInput = "needs_input"
    }

    /// `index.html` — what the board calls "needs you", at session level.
    /// `limit` is deliberately excluded: a limit-stuck agent is not actionable.
    public var attention: Int { needsInput + blocked + waiting }
    /// What a badge would count: only the two that interrupt.
    public var interrupting: Int { needsInput + blocked }
}

public struct Worktree: Sendable, Equatable, Decodable, Identifiable {
    public var id: String { name }

    /// The card key. `discover_worktrees` dedupes by absolute path, so two roots
    /// each holding a `ConfidAI` produce two cards with the SAME name — which a
    /// name-keyed dictionary silently drops. Every dictionary built from these
    /// uses `uniquingKeysWith:`, never `uniqueKeysWithValues`.
    public let name: String
    public let path: String
    public let git: GitInfo
    /// Server-sorted by severity then freshness, capped at `max_sessions`
    /// (6 on this fleet). The client never re-sorts.
    public let sessions: [Session]
    public let liveProcs: [LiveProc]
    public let availability: Availability

    public init(name: String, path: String, git: GitInfo, sessions: [Session],
                liveProcs: [LiveProc], availability: Availability) {
        self.name = name
        self.path = path
        self.git = git
        self.sessions = sessions
        self.liveProcs = liveProcs
        self.availability = availability
    }

    enum CodingKeys: String, CodingKey {
        case name, path, git, sessions, availability
        case liveProcs = "live_procs"
    }

    /// Terminals in this worktree that no session claimed a pid for.
    public var looseProcs: [LiveProc] {
        let claimed = Set(sessions.compactMap(\.pid))
        return liveProcs.filter { !claimed.contains($0.pid) }
    }

    public var endedCount: Int { sessions.filter { $0.status == .ended }.count }
}

public struct GitInfo: Sendable, Equatable, Decodable {
    /// Never null on the wire: `gitrepo.git_info` falls back to
    /// `"detached@<sha>"` and then to `"?"`. Decoded leniently anyway.
    public let branch: String
    /// Null when `git log -1` failed — an empty repository, or a broken one.
    public let commit: Commit?
    /// The count of `git status --porcelain=v2` entries. Observed up to 998.
    public let dirty: Int
    /// **Both null when the branch has no upstream** — `# branch.ab` is ABSENT
    /// from porcelain v2, not `+0 -0`. 2 of 9 live worktrees. The row omits the
    /// `↑↓` pair entirely rather than rendering a zero it did not measure.
    public let ahead: Int?
    public let behind: Int?

    public init(branch: String, commit: Commit?, dirty: Int, ahead: Int?, behind: Int?) {
        self.branch = branch
        self.commit = commit
        self.dirty = dirty
        self.ahead = ahead
        self.behind = behind
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        branch = try c.decodeIfPresent(String.self, forKey: .branch) ?? "?"
        commit = try c.decodeIfPresent(Commit.self, forKey: .commit)
        dirty = try c.decodeIfPresent(Int.self, forKey: .dirty) ?? 0
        ahead = try c.decodeIfPresent(Int.self, forKey: .ahead)
        behind = try c.decodeIfPresent(Int.self, forKey: .behind)
    }

    enum CodingKeys: String, CodingKey { case branch, commit, dirty, ahead, behind }

    /// True only when the server measured an upstream. `ahead == nil` and
    /// `ahead == 0` are different facts and the row must not conflate them.
    public var hasUpstream: Bool { ahead != nil && behind != nil }

    public struct Commit: Sendable, Equatable, Decodable {
        /// `git log --format=%h` honours `core.abbrev` PER REPOSITORY — 8 chars
        /// in one worktree of this fleet and 9 in the other eight. Never slice.
        public let hash: String
        public let ts: Int
        /// Arrives UNTRUNCATED (107 characters observed live). Truncation is the
        /// view's job and it happens once, at the row.
        public let subject: String

        public init(hash: String, ts: Int, subject: String) {
            self.hash = hash
            self.ts = ts
            self.subject = subject
        }

        public var date: Date { Date(timeIntervalSince1970: TimeInterval(ts)) }
    }
}

public struct Session: Sendable, Equatable, Decodable, Identifiable {
    public var id: String { sid }

    /// The first 8 characters of `sid`. Display only — never an address.
    public let shortID: String
    /// The full transcript UUID. **This plus `account` is the address for every
    /// mutation** (ADR 0008): a bare pid is refused.
    public let sid: String
    /// orchestra's own account LABEL ("main", "account3"), not a cclimits slug.
    public let account: String
    /// Absolute epoch. `age_s` left the wire deliberately (step 5 phase 5) so
    /// that the payload is time-invariant and a clock tick can never bump the
    /// version. The client animates elapsed time from this.
    public let lastWriteAt: Double
    public let cwd: String
    /// Nil when `cwd` is the worktree root.
    public let subdir: String?
    public let branch: String
    /// NOT an enum: "fable-5", "opus-4-8", "haiku-4-5-20251001", and "" all
    /// observed live in one fleet.
    public let model: String
    public let pendingTools: [String]
    public let pendingWorkflows: Int
    public let pendingBackgroundAgents: Int
    /// Present on the wire as `pending_bg_tools` and NOT modelled in
    /// IOS-APP.md §3.3. It is what stops `delegated` under-counting — a
    /// `tool_use` that launched background work counts until its task
    /// notification lands.
    public let pendingBackgroundTools: Int
    /// Server-truncated to 140. Nil when the transcript has no user turn to
    /// take one from (5 of 36 live sessions).
    public let topic: String?
    /// Server-truncated to 240.
    public let lastAssistant: String?
    /// Server-truncated to 140. Nil on 3 of 36 live sessions.
    public let lastUser: String?
    /// Server-truncated to 240; only when the subagent tree is fresher.
    public let subagentSaid: String?
    public let subagentsActive: Bool
    public let pid: Int32?
    /// True only when the process's `CLAUDE_CONFIG_DIR` account matched this
    /// session's account. **False means the pairing is a freshness-order GUESS**
    /// and the UI must render it as one, never as fact.
    public let pidCertain: Bool
    public let status: SessionStatus
    /// **Absent from the payload entirely on some sessions** — 3 of 36 live.
    /// `transcripts.py` writes it only on the path that computed it and reads it
    /// back with `.get(..., False)`. A non-optional `Bool` here is a crash.
    public let turnEnded: Bool?
    /// Present iff `status == .limit` (`observer.py:146`).
    public let limit: SessionLimit?
    /// An account label. Present iff `status == .limit` AND a fresher live
    /// session exists on the same card — meaning "work continued elsewhere, this
    /// is NOT actionable". Anything that alerts on `.limit` without checking
    /// this fires on non-problems.
    public let handedTo: String?
    /// **Wire key present ONLY when true** (`transcripts.py:951`).
    public let toolRunning: Bool
    /// **The wire key is `bg_shell`.** IOS-APP.md §3.3 calls it
    /// `background_shell`; the server has never written that name.
    public let bgShell: Bool

    public init(shortID: String, sid: String, account: String, lastWriteAt: Double,
                cwd: String, subdir: String?, branch: String, model: String,
                pendingTools: [String], pendingWorkflows: Int,
                pendingBackgroundAgents: Int, pendingBackgroundTools: Int,
                topic: String?, lastAssistant: String?, lastUser: String?,
                subagentSaid: String?, subagentsActive: Bool, pid: Int32?,
                pidCertain: Bool, status: SessionStatus, turnEnded: Bool?,
                limit: SessionLimit?, handedTo: String?, toolRunning: Bool,
                bgShell: Bool) {
        self.shortID = shortID
        self.sid = sid
        self.account = account
        self.lastWriteAt = lastWriteAt
        self.cwd = cwd
        self.subdir = subdir
        self.branch = branch
        self.model = model
        self.pendingTools = pendingTools
        self.pendingWorkflows = pendingWorkflows
        self.pendingBackgroundAgents = pendingBackgroundAgents
        self.pendingBackgroundTools = pendingBackgroundTools
        self.topic = topic
        self.lastAssistant = lastAssistant
        self.lastUser = lastUser
        self.subagentSaid = subagentSaid
        self.subagentsActive = subagentsActive
        self.pid = pid
        self.pidCertain = pidCertain
        self.status = status
        self.turnEnded = turnEnded
        self.limit = limit
        self.handedTo = handedTo
        self.toolRunning = toolRunning
        self.bgShell = bgShell
    }

    enum CodingKeys: String, CodingKey {
        case id, sid, account, cwd, subdir, branch, model, topic, pid, status, limit
        case lastWriteAt = "last_write_at"
        case pendingTools = "pending_tools"
        case pendingWorkflows = "pending_workflows"
        case pendingBackgroundAgents = "pending_bg_agents"
        case pendingBackgroundTools = "pending_bg_tools"
        case lastAssistant = "last_assistant"
        case lastUser = "last_user"
        case subagentSaid = "subagent_said"
        case subagentsActive = "subagents_active"
        case pidCertain = "pid_certain"
        case turnEnded = "turn_ended"
        case handedTo = "handed_to"
        case toolRunning = "tool_running"
        case bgShell = "bg_shell"
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sid = try c.decode(String.self, forKey: .sid)
        shortID = try c.decodeIfPresent(String.self, forKey: .id) ?? String(sid.prefix(8))
        account = try c.decodeIfPresent(String.self, forKey: .account) ?? ""
        lastWriteAt = try c.decodeIfPresent(Double.self, forKey: .lastWriteAt) ?? 0
        cwd = try c.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        subdir = try c.decodeIfPresent(String.self, forKey: .subdir)
        branch = try c.decodeIfPresent(String.self, forKey: .branch) ?? "?"
        model = try c.decodeIfPresent(String.self, forKey: .model) ?? ""
        pendingTools = try c.decodeIfPresent([String].self, forKey: .pendingTools) ?? []
        pendingWorkflows = try c.decodeIfPresent(Int.self, forKey: .pendingWorkflows) ?? 0
        pendingBackgroundAgents = try c.decodeIfPresent(Int.self, forKey: .pendingBackgroundAgents) ?? 0
        pendingBackgroundTools = try c.decodeIfPresent(Int.self, forKey: .pendingBackgroundTools) ?? 0
        topic = try c.decodeIfPresent(String.self, forKey: .topic)
        lastAssistant = try c.decodeIfPresent(String.self, forKey: .lastAssistant)
        lastUser = try c.decodeIfPresent(String.self, forKey: .lastUser)
        subagentSaid = try c.decodeIfPresent(String.self, forKey: .subagentSaid)
        subagentsActive = try c.decodeIfPresent(Bool.self, forKey: .subagentsActive) ?? false
        pid = try c.decodeIfPresent(Int32.self, forKey: .pid)
        pidCertain = try c.decodeIfPresent(Bool.self, forKey: .pidCertain) ?? false
        status = try c.decodeIfPresent(SessionStatus.self, forKey: .status) ?? .unknown
        turnEnded = try c.decodeIfPresent(Bool.self, forKey: .turnEnded)
        limit = try c.decodeIfPresent(SessionLimit.self, forKey: .limit)
        handedTo = try c.decodeIfPresent(String.self, forKey: .handedTo)
        toolRunning = try c.decodeIfPresent(Bool.self, forKey: .toolRunning) ?? false
        bgShell = try c.decodeIfPresent(Bool.self, forKey: .bgShell) ?? false
    }

    public var lastWrite: Date { Date(timeIntervalSince1970: lastWriteAt) }

    /// `index.html`'s busy tag — **first match wins**, in the desktop's order.
    public var busySignal: String? {
        if subagentsActive { return "subagents running" }
        if pendingWorkflows > 0 { return "awaiting \(pendingWorkflows) workflow(s)" }
        if pendingBackgroundAgents > 0 { return "awaiting \(pendingBackgroundAgents) background agent(s)" }
        if pendingBackgroundTools > 0 { return "awaiting \(pendingBackgroundTools) background tool(s)" }
        if toolRunning {
            return "running: " + (bgShell ? "background shell" : (pendingTools.first ?? "tool"))
        }
        return nil
    }

    /// A limit session that has been handed off is NOT actionable — that is the
    /// entire reason `handed_to` exists.
    public var isActionable: Bool {
        status.isAttention || (status == .limit && handedTo == nil)
    }
}

/// `observer.py:136-151`. **All three fields null together is a real and common
/// state**: the transcript-regex fallback fires when the CLI wrote its limit
/// notice but the cclimits cache is cold, and then the row can only honestly say
/// "limited, reset time unknown".
public struct SessionLimit: Sendable, Equatable, Decodable {
    public let worst: String?
    public let group: String?
    public let resetsAt: Double?

    public init(worst: String?, group: String?, resetsAt: Double?) {
        self.worst = worst
        self.group = group
        self.resetsAt = resetsAt
    }

    enum CodingKeys: String, CodingKey {
        case worst, group
        case resetsAt = "resets_at"
    }

    public var resets: Date? { resetsAt.map { Date(timeIntervalSince1970: $0) } }
    public var isTimeUnknown: Bool { resetsAt == nil }
}

public struct LiveProc: Sendable, Equatable, Decodable, Identifiable {
    public var id: Int32 { pid }
    public let pid: Int32
    /// A rate with no absolute twin — one of the two fields the README's open
    /// items name as still derived from the clock. Drawn verbatim, never a
    /// version bump.
    public let cpu: Double
    /// Pre-formatted by `ps`: "15:02", "04:10:30", "2-03:14:22".
    public let etime: String
    public let tty: String?
    /// NOT an enum: can read "tmux -L fleet" — the string embeds the socket
    /// name. Match on `tmux` or on `reachable`, never on this.
    public let host: String?
    public let account: String?
    public let tmux: String?
    /// THE gate for whether chat/send can reach this agent at all.
    public let reachable: Bool
    public let subdir: String?

    public init(pid: Int32, cpu: Double, etime: String, tty: String?, host: String?,
                account: String?, tmux: String?, reachable: Bool, subdir: String?) {
        self.pid = pid
        self.cpu = cpu
        self.etime = etime
        self.tty = tty
        self.host = host
        self.account = account
        self.tmux = tmux
        self.reachable = reachable
        self.subdir = subdir
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pid = try c.decode(Int32.self, forKey: .pid)
        cpu = try c.decodeIfPresent(Double.self, forKey: .cpu) ?? 0
        etime = try c.decodeIfPresent(String.self, forKey: .etime) ?? ""
        tty = try c.decodeIfPresent(String.self, forKey: .tty)
        host = try c.decodeIfPresent(String.self, forKey: .host)
        account = try c.decodeIfPresent(String.self, forKey: .account)
        tmux = try c.decodeIfPresent(String.self, forKey: .tmux)
        reachable = try c.decodeIfPresent(Bool.self, forKey: .reachable) ?? false
        subdir = try c.decodeIfPresent(String.self, forKey: .subdir)
    }

    enum CodingKeys: String, CodingKey {
        case pid, cpu, etime, tty, host, account, tmux, reachable, subdir
    }
}

/// A `claude` process living outside every watched worktree.
public struct OtherProc: Sendable, Equatable, Decodable, Identifiable {
    public var id: Int32 { pid }
    public let pid: Int32
    public let cpu: Double
    public let etime: String
    public let tty: String?
    public let host: String?
    public let cwd: String?

    public init(pid: Int32, cpu: Double, etime: String, tty: String?, host: String?, cwd: String?) {
        self.pid = pid
        self.cpu = cpu
        self.etime = etime
        self.tty = tty
        self.host = host
        self.cwd = cwd
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        pid = try c.decode(Int32.self, forKey: .pid)
        cpu = try c.decodeIfPresent(Double.self, forKey: .cpu) ?? 0
        etime = try c.decodeIfPresent(String.self, forKey: .etime) ?? ""
        tty = try c.decodeIfPresent(String.self, forKey: .tty)
        host = try c.decodeIfPresent(String.self, forKey: .host)
        cwd = try c.decodeIfPresent(String.self, forKey: .cwd)
    }

    enum CodingKeys: String, CodingKey { case pid, cpu, etime, tty, host, cwd }
}

/// One armed auto-resume. Rides along on `/api/state` only — `resume.py` is not
/// watched by the observer, so arming one moves no version and it could never
/// ride the event stream however that frame were shaped.
public struct ResumeSchedule: Sendable, Equatable, Decodable {
    public let worktree: String
    public let sid: String
    public let account: String
    public let model: String?
    public let delayS: Double?
    public let status: String
    public let dueAt: Double?
    public let attempts: Int
    public let message: String?

    public init(worktree: String, sid: String, account: String, model: String?,
                delayS: Double?, status: String, dueAt: Double?, attempts: Int,
                message: String?) {
        self.worktree = worktree
        self.sid = sid
        self.account = account
        self.model = model
        self.delayS = delayS
        self.status = status
        self.dueAt = dueAt
        self.attempts = attempts
        self.message = message
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        worktree = try c.decodeIfPresent(String.self, forKey: .worktree) ?? ""
        sid = try c.decodeIfPresent(String.self, forKey: .sid) ?? ""
        account = try c.decodeIfPresent(String.self, forKey: .account) ?? ""
        model = try c.decodeIfPresent(String.self, forKey: .model)
        delayS = try c.decodeIfPresent(Double.self, forKey: .delayS)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "pending"
        dueAt = try c.decodeIfPresent(Double.self, forKey: .dueAt)
        attempts = try c.decodeIfPresent(Int.self, forKey: .attempts) ?? 0
        message = try c.decodeIfPresent(String.self, forKey: .message)
    }

    enum CodingKeys: String, CodingKey {
        case worktree, sid, account, model, status, attempts, message
        case delayS = "delay_s"
        case dueAt = "due_at"
    }

    public var due: Date? { dueAt.map { Date(timeIntervalSince1970: $0) } }
}
