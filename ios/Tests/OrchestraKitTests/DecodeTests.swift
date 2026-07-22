import Foundation
import Testing
@testable import OrchestraKit

/// Decoding against a payload the server really produced.
///
/// The fixtures are a `GET /api/state` body and a `GET /api/events` snapshot
/// frame, both captured on 2026-07-22 from a nine-worktree fleet and then
/// scrubbed — transcript prose replaced with filler of the same LENGTH and the
/// same trailing ellipsis, `/Users/achill` rewritten, session UUIDs replaced with
/// stable hashes that keep the `id`-is-a-prefix-of-`sid` invariant. Everything
/// that decides a decode — which keys are present, which are null, which are
/// absent entirely — is untouched.
///
/// `METHOD.md` §5's rule applies here: this suite patches nothing and calls only
/// the public decode path, so it keeps working across a refactor and cannot pass
/// by testing a stub.
struct DecodeTests {

    static func fixture(_ name: String) throws -> Data {
        let url = try #require(Bundle.module.url(forResource: "Fixtures/\(name)",
                                                 withExtension: "json"))
        return try Data(contentsOf: url)
    }

    static func board() throws -> FleetState {
        try JSONDecoder().decode(FleetState.self, from: fixture("state"))
    }

    @Test func decodesTheRealBoard() throws {
        let state = try Self.board()
        #expect(state.worktrees.count == 9)
        #expect(state.worktrees.reduce(0) { $0 + $1.sessions.count } == 36)
        #expect(state.freeWorktrees.count == 6)
        #expect(state.otherProcs.count == 5)
        #expect(state.counts.ended == 32)
        #expect(state.generatedAt > 1_700_000_000)
    }

    /// The trap this whole model layer is shaped around.
    ///
    /// `transcripts.py` writes `turn_ended` only on the path that computed it and
    /// reads it back with `.get(..., False)`, so the key is **absent** on some
    /// sessions — 3 of 36 in the capture. A non-optional `Bool` there does not
    /// produce a wrong value, it produces `keyNotFound` and throws out the entire
    /// 38 KB board.
    @Test func turnEndedIsAbsentOnSomeSessionsAndTheBoardStillDecodes() throws {
        let raw = try #require(try JSONSerialization.jsonObject(
            with: Self.fixture("state")) as? [String: Any])
        let cards = try #require(raw["worktrees"] as? [[String: Any]])
        let sessions = cards.flatMap { ($0["sessions"] as? [[String: Any]]) ?? [] }
        let missing = sessions.filter { $0["turn_ended"] == nil }
        #expect(missing.count == 3,
                "the fixture must keep a session with no turn_ended, or this cannot fail")

        let state = try Self.board()
        let decoded = state.worktrees.flatMap(\.sessions)
        #expect(decoded.filter { $0.turnEnded == nil }.count == 3)
    }

    /// `# branch.ab` is ABSENT from porcelain v2 when there is no upstream — not
    /// `+0 -0`. So `ahead`/`behind` arrive as null, and the row must be able to
    /// tell "zero commits ahead" from "no upstream to be ahead of".
    @Test func aheadAndBehindAreNullWithoutAnUpstream() throws {
        let state = try Self.board()
        let noUpstream = state.worktrees.filter { !$0.git.hasUpstream }
        #expect(noUpstream.count == 2)
        for card in noUpstream {
            #expect(card.git.ahead == nil)
            #expect(card.git.behind == nil)
        }
        let withUpstream = state.worktrees.filter(\.git.hasUpstream)
        #expect(withUpstream.contains { ($0.git.ahead ?? 0) > 0 })
    }

    /// A status string this build has never seen must widen to `.unknown`, not
    /// throw. One unknown word must not be able to take the board with it.
    @Test func anUnknownStatusWidensRatherThanThrowing() throws {
        var raw = try #require(try JSONSerialization.jsonObject(
            with: Self.fixture("state")) as? [String: Any])
        var cards = try #require(raw["worktrees"] as? [[String: Any]])
        var first = cards[0]
        var sessions = try #require(first["sessions"] as? [[String: Any]])
        sessions[0]["status"] = "hypnotised"
        first["sessions"] = sessions
        cards[0] = first
        raw["worktrees"] = cards

        let mutated = try JSONSerialization.data(withJSONObject: raw)
        let state = try JSONDecoder().decode(FleetState.self, from: mutated)
        #expect(state.worktrees[0].sessions[0].status == .unknown)
        #expect(state.worktrees.count == 9, "the rest of the board must survive")
    }

    /// Same for `availability`, which is the field `UX.md` §3.1.2 wants the
    /// server to widen from four values to five. When that lands, this client
    /// must not crash on the new words on the way to being taught them.
    @Test func anUnknownAvailabilityWidensRatherThanThrowing() throws {
        var raw = try #require(try JSONSerialization.jsonObject(
            with: Self.fixture("state")) as? [String: Any])
        var cards = try #require(raw["worktrees"] as? [[String: Any]])
        cards[0]["availability"] = "needs_you"
        raw["worktrees"] = cards
        let state = try JSONDecoder().decode(
            FleetState.self, from: try JSONSerialization.data(withJSONObject: raw))
        #expect(state.worktrees[0].availability == .unknown)
    }

    /// `git.commit` is null on an empty or unreadable repository.
    @Test func aNullCommitDecodes() throws {
        var raw = try #require(try JSONSerialization.jsonObject(
            with: Self.fixture("state")) as? [String: Any])
        var cards = try #require(raw["worktrees"] as? [[String: Any]])
        var git = try #require(cards[0]["git"] as? [String: Any])
        git["commit"] = NSNull()
        cards[0]["git"] = git
        raw["worktrees"] = cards
        let state = try JSONDecoder().decode(
            FleetState.self, from: try JSONSerialization.data(withJSONObject: raw))
        #expect(state.worktrees[0].git.commit == nil)
    }

    /// `tool_running` and `bg_shell` are present ONLY when true — and the wire
    /// key is `bg_shell`, not `background_shell` as IOS-APP.md §3.3 has it.
    @Test func theBusyFlagsDefaultToFalseWhenAbsentAndUseTheServersSpelling() throws {
        let state = try Self.board()
        // Nothing in the capture carried them, so absence must have decoded.
        #expect(state.worktrees.flatMap(\.sessions).allSatisfy { !$0.toolRunning })

        let json = """
        {"sid":"a","account":"main","last_write_at":1,"cwd":"/x","subdir":null,
         "branch":"main","model":"opus-4-8","pending_tools":["Bash"],
         "pending_workflows":0,"pending_bg_agents":0,"pending_bg_tools":0,
         "topic":null,"last_assistant":null,"last_user":null,"subagent_said":null,
         "subagents_active":false,"pid":null,"pid_certain":false,"status":"blocked",
         "tool_running":true,"bg_shell":true}
        """
        let session = try JSONDecoder().decode(Session.self, from: Data(json.utf8))
        #expect(session.toolRunning)
        #expect(session.bgShell)
        #expect(session.busySignal == "running: background shell")
    }

    /// The busy tag is FIRST MATCH WINS, in the desktop's order.
    @Test func theBusySignalTakesTheFirstMatch() {
        func make(subagents: Bool = false, workflows: Int = 0, bgAgents: Int = 0,
                  bgTools: Int = 0, toolRunning: Bool = false,
                  tools: [String] = []) -> Session {
            Session(shortID: "a", sid: "a", account: "main", lastWriteAt: 0, cwd: "/x",
                    subdir: nil, branch: "main", model: "", pendingTools: tools,
                    pendingWorkflows: workflows, pendingBackgroundAgents: bgAgents,
                    pendingBackgroundTools: bgTools, topic: nil, lastAssistant: nil,
                    lastUser: nil, subagentSaid: nil, subagentsActive: subagents,
                    pid: nil, pidCertain: false, status: .working, turnEnded: nil,
                    limit: nil, handedTo: nil, toolRunning: toolRunning, bgShell: false)
        }
        #expect(make(subagents: true, workflows: 3).busySignal == "subagents running")
        #expect(make(workflows: 2, bgAgents: 1).busySignal == "awaiting 2 workflow(s)")
        #expect(make(bgAgents: 1, bgTools: 4).busySignal == "awaiting 1 background agent(s)")
        #expect(make(bgTools: 4).busySignal == "awaiting 4 background tool(s)")
        #expect(make(toolRunning: true, tools: ["Edit"]).busySignal == "running: Edit")
        #expect(make().busySignal == nil)
    }

    /// A handed-off limit session is NOT actionable — that is the whole reason
    /// `handed_to` exists, and anything that alerts without checking it fires on
    /// non-problems.
    @Test func aHandedOffLimitIsNotActionable() {
        func limited(handedTo: String?) -> Session {
            Session(shortID: "a", sid: "a", account: "main", lastWriteAt: 0, cwd: "/x",
                    subdir: nil, branch: "main", model: "", pendingTools: [],
                    pendingWorkflows: 0, pendingBackgroundAgents: 0,
                    pendingBackgroundTools: 0, topic: nil, lastAssistant: nil,
                    lastUser: nil, subagentSaid: nil, subagentsActive: false, pid: nil,
                    pidCertain: false, status: .limit, turnEnded: nil,
                    limit: SessionLimit(worst: nil, group: nil, resetsAt: nil),
                    handedTo: handedTo, toolRunning: false, bgShell: false)
        }
        #expect(limited(handedTo: nil).isActionable)
        #expect(!limited(handedTo: "account3").isActionable)
    }

    // MARK: - The event frame

    /// The frame's envelope, decoded from a real snapshot. `base` is absent on a
    /// snapshot and present on a delta — the task brief lists it unconditionally
    /// and `delta_since` puts it on one branch only.
    @Test func decodesTheSnapshotFrame() throws {
        let frame = try JSONDecoder().decode(
            StreamFrame.self, from: Self.fixture("snapshot-frame"))
        #expect(frame.type == .snapshot)
        #expect(frame.base == nil)
        #expect(frame.v >= 1)
        #expect(frame.cards.count == 9)
        #expect(frame.order.count == 9)
        #expect(Set(frame.order) == Set(frame.cards.keys),
                "order and cards must name the same fleet on a snapshot")
        #expect(frame.freshness.oldest() != nil)
        #expect(frame.otherProcs.count == 5)
    }

    /// A delta names only the cards that moved, and everything else rides whole.
    /// The client must accept a frame whose `cards` is a strict subset of
    /// `order` — that is the normal case, not a malformed one.
    @Test func aDeltaCarriesFewerCardsThanTheOrderNames() throws {
        var raw = try #require(try JSONSerialization.jsonObject(
            with: Self.fixture("snapshot-frame")) as? [String: Any])
        var cards = try #require(raw["cards"] as? [String: Any])
        let order = try #require(raw["order"] as? [String])
        for key in order.dropFirst(2) { cards.removeValue(forKey: key) }
        raw["cards"] = cards
        raw["type"] = "delta"
        raw["base"] = 41
        raw["v"] = 42

        let frame = try JSONDecoder().decode(
            StreamFrame.self, from: try JSONSerialization.data(withJSONObject: raw))
        #expect(frame.type == .delta)
        #expect(frame.base == 41)
        #expect(frame.v == 42)
        #expect(frame.cards.count == 2)
        #expect(frame.order.count == 9, "order rides WHOLE on every frame")
    }

    // MARK: - Pairing

    @Test func decodesAPairResponse() throws {
        let json = """
        {"ok": true, "device_id": "ccfd4521", "label": "iPhone",
         "token": "orc1_ccfd4521_wE3DtrRwk5QGh4U6Zx4qg8QY",
         "server": {"host": "100.113.110.31", "port": 4269,
                    "hostname": "MacBookPro", "api": "1", "tls": false}}
        """
        let response = try JSONDecoder().decode(PairResponse.self, from: Data(json.utf8))
        #expect(response.deviceID == "ccfd4521")
        #expect(response.server.port == 4269)
        #expect(response.server.tls == false)
        #expect(ServerProfile(response.server, deviceID: response.deviceID).baseURL?
                    .absoluteString == "http://100.113.110.31:4269")
    }

    @Test func decodesARefusal() throws {
        let json = """
        {"ok": false, "error": "pairing_not_open",
         "message": "no pairing window is open"}
        """
        let refusal = try JSONDecoder().decode(APIRefusal.self, from: Data(json.utf8))
        #expect(refusal.error == APIRefusal.Code.notOpen)
    }

    @Test func decodesHealth() throws {
        let json = """
        {"ok": true, "service": "orchestra", "api": "1.0", "time": 1784744126.03}
        """
        let health = try JSONDecoder().decode(ServerHealth.self, from: Data(json.utf8))
        #expect(health.ok)
        #expect(health.api == "1.0")
        #expect(health.time > 1_700_000_000)
    }
}
