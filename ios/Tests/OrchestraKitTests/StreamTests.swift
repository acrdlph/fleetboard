import Foundation
import Testing
@testable import OrchestraKit

/// The SSE line parser, against the framing the server really writes.
///
/// The literals below were copied out of a live `GET /api/events` capture on
/// 2026-07-22 (`id: 62` / `event: state` / `data: {…}` / blank, then
/// `: keepalive`), which is the whole vocabulary `server._write_frame` and
/// `server._stream` can produce. The rest of the cases are the parts of the
/// format the server does not use today and a client must survive anyway — a
/// multi-line `data:`, a field with no colon, an unknown field.
struct SSEDecoderTests {

    /// Feed a whole transcript, terminators already stripped, and collect what
    /// came out.
    static func run(_ text: String, cursor: String? = nil) -> (tokens: [SSEToken], last: String?) {
        var decoder = SSEDecoder(lastEventID: cursor)
        var out: [SSEToken] = []
        for line in text.components(separatedBy: "\n") {
            if let token = decoder.feed(line) { out.append(token) }
        }
        return (out, decoder.lastEventID)
    }

    @Test func parsesTheFramingTheServerWrites() {
        let capture = """
        id: 62
        event: state
        data: {"type":"snapshot","v":62}

        : keepalive

        id: 63
        event: state
        data: {"type":"delta","v":63,"base":62}


        """
        let (tokens, cursor) = Self.run(capture)
        #expect(tokens.count == 3)
        guard case .event(let first) = tokens[0] else { Issue.record("not an event"); return }
        #expect(first.name == "state")
        #expect(first.id == "62")
        #expect(first.data == #"{"type":"snapshot","v":62}"#)
        #expect(tokens[1] == .comment(" keepalive"))
        guard case .event(let second) = tokens[2] else { Issue.record("not an event"); return }
        #expect(second.id == "63")
        // The cursor is what goes back in `Last-Event-ID`, and it is the whole
        // resync path.
        #expect(cursor == "63")
    }

    /// **A keepalive must not blank the reconnect cursor.** The SSE last-event-ID
    /// buffer belongs to the CONNECTION, not to the event, so anything arriving
    /// between two frames leaves it alone. If it did not, a phone that
    /// reconnected right after a 25 s quiet period would ask for a full 38 KB
    /// snapshot instead of a delta, every time the fleet went quiet.
    @Test func aKeepaliveDoesNotClearTheCursor() {
        let (tokens, cursor) = Self.run("""
        id: 90
        event: state
        data: {}

        : keepalive

        : keepalive


        """)
        #expect(tokens.count == 3)
        #expect(cursor == "90")
    }

    /// Exactly one leading space is removed from a value, and only one.
    @Test func oneLeadingSpaceIsStrippedAndOnlyOne() {
        let (tokens, _) = Self.run("data:  x\n\n")
        #expect(tokens == [.event(SSEEvent(name: "message", data: " x", id: nil))])
    }

    /// Multiple `data:` lines join with a newline; the trailing one is dropped.
    @Test func multipleDataLinesJoinWithNewlines() {
        let (tokens, _) = Self.run("data: a\ndata: b\ndata: \n\n")
        #expect(tokens == [.event(SSEEvent(name: "message", data: "a\nb\n", id: nil))])
    }

    /// An event with no `data:` at all is NOT dispatched — that is the spec, and
    /// it is what stops a stray `id:` line being delivered as an empty frame
    /// that the store would then try to decode.
    @Test func anEventWithNoDataIsNotDispatched() {
        let (tokens, cursor) = Self.run("id: 7\nevent: state\n\n")
        #expect(tokens.isEmpty)
        #expect(cursor == "7", "the id still lands, because the id is the connection's")
    }

    /// A field with no colon is a field with an empty value; an unknown field is
    /// ignored. Neither may derail the event that is being built.
    @Test func unknownAndValuelessFieldsAreIgnored() {
        let (tokens, _) = Self.run("weird\nfoo: bar\ndata: kept\n\n")
        #expect(tokens == [.event(SSEEvent(name: "message", data: "kept", id: nil))])
    }

    @Test func retryIsParsedRatherThanTreatedAsData() {
        let (tokens, _) = Self.run("retry: 3000\n\n")
        #expect(tokens == [.retry(3000)])
    }

    /// The real snapshot fixture, pushed through the parser and then the frame
    /// decoder — the two halves of the receive path, joined.
    @Test func aRealSnapshotSurvivesTheWholeReceivePath() throws {
        let body = try DecodeTests.fixture("snapshot-frame")
        let json = try #require(String(data: body, encoding: .utf8))
        // The server writes the payload on ONE `data:` line (`_write_frame` is a
        // single f-string), so this is the shape that really arrives.
        let wire = "id: 62\nevent: state\ndata: \(json.replacingOccurrences(of: "\n", with: ""))\n\n"
        let (tokens, _) = Self.run(wire)
        #expect(tokens.count == 1)
        guard case .event(let event) = tokens.first else { Issue.record("no event"); return }
        let frame = try StreamFrame.decode(Data(event.data.utf8))
        #expect(frame.type == .snapshot)
        #expect(frame.cards.count == 9)
    }
}

/// The byte splitter, which exists because of one measured platform behaviour.
struct SSELineSplitterTests {

    static func split(_ chunks: [String]) -> [String] {
        var splitter = SSELineSplitter()
        return chunks.flatMap { splitter.feed(Data($0.utf8)) }
    }

    /// **The empty line must survive.**
    ///
    /// This is the whole reason the transport does not use
    /// `URLSession.AsyncBytes.lines`. `AsyncLineSequence` only yields when its
    /// buffer is non-empty, so it drops blank lines — and in SSE the blank line
    /// is not whitespace, it is the dispatch instruction. Verified against the
    /// live server: `.lines` delivered `id:`, `event:` and `data:` and never the
    /// blank line between frames, so a client built on it holds a healthy socket
    /// and never fires an event. It looks exactly like a board that cannot
    /// connect.
    @Test func anEmptyLineSurvives() {
        #expect(Self.split(["data: x\n\n"]) == ["data: x", ""])
    }

    /// A frame split across two `didReceive data:` callbacks is the normal case
    /// for a 38 KB snapshot, not an edge case.
    @Test func aLineSplitAcrossChunksIsRejoined() {
        #expect(Self.split(["data: he", "llo\n\n"]) == ["data: hello", ""])
    }

    /// All three SSE terminators, and the one that a naive splitter turns into a
    /// SPURIOUS blank line: a `\r` ending one chunk with its `\n` opening the
    /// next. A spurious blank line is a spurious dispatch.
    @Test func crlfSplitAcrossChunksIsOneTerminatorNotTwo() {
        #expect(Self.split(["a\r", "\nb\n"]) == ["a", "b"])
        #expect(Self.split(["a\r\nb\rc\nd"]) == ["a", "b", "c"])
    }

    /// Trailing bytes with no terminator are held, not emitted — an SSE line is
    /// not a line until it is terminated.
    @Test func anUnterminatedTailIsHeld() {
        #expect(Self.split(["data: half"]) == [])
    }

    /// The two halves joined: real framing, chunked at an awkward boundary,
    /// through the splitter and then the decoder.
    @Test func chunkedFramingReachesTheDecoder() {
        var splitter = SSELineSplitter()
        var decoder = SSEDecoder()
        var events: [SSEEvent] = []
        for chunk in ["id: 62\nevent: sta", "te\ndata: {\"v\":62}", "\n\n: keepalive\n\n"] {
            for line in splitter.feed(Data(chunk.utf8)) {
                if case .event(let e)? = decoder.feed(line) { events.append(e) }
            }
        }
        #expect(events.count == 1)
        #expect(events.first?.name == "state")
        #expect(events.first?.id == "62")
        #expect(events.first?.data == #"{"v":62}"#)
    }
}

/// The delta applier — the rules ported from `stream.js`, each tested against
/// the thing it exists to prevent.
struct FleetApplierTests {

    static func snapshot() throws -> StreamFrame {
        try StreamFrame.decode(try DecodeTests.fixture("snapshot-frame"))
    }

    /// Rebuild a frame with mutations, so every test starts from a payload the
    /// server really produced.
    static func frame(_ mutate: (inout [String: Any]) -> Void) throws -> StreamFrame {
        var raw = try #require(try JSONSerialization.jsonObject(
            with: try DecodeTests.fixture("snapshot-frame")) as? [String: Any])
        mutate(&raw)
        return try StreamFrame.decode(try JSONSerialization.data(withJSONObject: raw))
    }

    @Test func aSnapshotReplacesEverything() throws {
        var applier = FleetApplier()
        #expect(applier.version == nil)
        #expect(applier.apply(try Self.snapshot()) == .applied)
        #expect(applier.cards.count == 9)
        #expect(applier.order.count == 9)
        #expect(applier.version != nil)
    }

    /// A delta names only what moved. Everything else must survive untouched.
    @Test func aDeltaMergesOnlyTheCardsItNames() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        #expect(applier.apply(snap) == .applied)
        let target = snap.order[3]
        let before = applier.cards.count

        let delta = try Self.frame { raw in
            var cards = raw["cards"] as! [String: Any]
            var card = cards[target] as! [String: Any]
            card["availability"] = "attention"
            raw["cards"] = [target: card]
            raw["type"] = "delta"
            raw["base"] = snap.v
            raw["v"] = snap.v + 1
        }
        #expect(applier.apply(delta) == .applied)
        #expect(applier.cards.count == before, "a delta must not drop the cards it did not name")
        #expect(applier.cards[target]?.availability == .attention)
        #expect(applier.version == snap.v + 1)
    }

    /// **`null` means the worktree is gone.**
    ///
    /// `delta_since` builds `cards` as `{k: snap.cards.get(k) for k in keys}`
    /// over a ring of changed card NAMES (`observer.py`), so a name in the ring
    /// that is no longer in the snapshot yields `None` and serialises as JSON
    /// `null`. This is the one frame that says a worktree disappeared, and
    /// phase 1's `[String: Worktree]` could not even DECODE it — the frame threw
    /// `valueNotFound` and the dead card stayed on the board.
    @Test func aNullCardRemovesIt() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        #expect(applier.apply(snap) == .applied)
        let doomed = snap.order[0]

        let delta = try Self.frame { raw in
            raw["cards"] = [doomed: NSNull()]
            raw["order"] = snap.order.filter { $0 != doomed }
            raw["type"] = "delta"
            raw["base"] = snap.v
            raw["v"] = snap.v + 1
        }
        #expect(applier.apply(delta) == .applied)
        #expect(applier.cards[doomed] == nil)
        #expect(applier.cards.count == 8)
        #expect(applier.composed(side: FleetSide())?.worktrees.count == 8)
    }

    /// **The gap test is `base`, and only `base`.**
    ///
    /// The server waits on the version and THEN asks for a delta, so publishes
    /// landing in between are coalesced into one frame whose `v` jumps by more
    /// than one. A client testing `v == held + 1` would call this a lost frame
    /// and resync — on every busy moment, which on a fleet mid-turn is a
    /// reconnect loop.
    @Test func aCoalescedDeltaIsAppliedNotTreatedAsAGap() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        _ = applier.apply(snap)
        let delta = try Self.frame { raw in
            raw["type"] = "delta"
            raw["base"] = snap.v
            raw["v"] = snap.v + 7          // seven publishes coalesced into one frame
        }
        #expect(applier.apply(delta) == .applied)
        #expect(applier.version == snap.v + 7)
    }

    /// A mismatched `base` is a gap, and a gap must mutate NOTHING — the caller
    /// throws the connection away, and half-applying a frame we are about to
    /// discard only makes the discarded state harder to reason about.
    @Test func aMismatchedBaseGapsAndChangesNothing() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        _ = applier.apply(snap)
        let held = applier
        let delta = try Self.frame { raw in
            raw["cards"] = [String: Any]()
            raw["type"] = "delta"
            raw["base"] = snap.v + 99      // we never saw whatever produced this
            raw["v"] = snap.v + 100
        }
        #expect(applier.apply(delta) == .gap)
        #expect(applier == held, "a gap must leave the applier byte-identical")
    }

    /// A delta arriving before any snapshot is a gap: there is no base to apply
    /// it to. This is the state `seed` deliberately leaves the applier in.
    @Test func aDeltaWithNothingHeldIsAGap() throws {
        var applier = FleetApplier()
        let delta = try Self.frame { raw in
            raw["type"] = "delta"
            raw["base"] = 1
            raw["v"] = 2
        }
        #expect(applier.apply(delta) == .gap)
    }

    /// **`order` decides the board, never the dictionary's key order.** A delta
    /// names only what moved, so a client that kept its own positions would
    /// leave a card that just flipped to `needs_input` exactly where it was —
    /// forever.
    @Test func theBoardIsOrderedByTheFramesOrderAndNotByTheCardDictionary() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        _ = applier.apply(snap)
        let flipped = Array(snap.order.reversed())
        let delta = try Self.frame { raw in
            raw["cards"] = [String: Any]()
            raw["order"] = flipped
            raw["type"] = "delta"
            raw["base"] = snap.v
            raw["v"] = snap.v + 1
        }
        _ = applier.apply(delta)
        #expect(applier.composed(side: FleetSide())?.worktrees.map(\.name) == flipped)
    }

    /// Seeding from `/api/state` leaves the version nil ON PURPOSE: that payload
    /// carries no version, and a delta applied on top of it would be applied to
    /// an unknown base. The next delta therefore gaps, and the gap is what
    /// fetches an authoritative snapshot.
    @Test func seedingFromStateLeavesNoBaseADeltaCouldLandOn() throws {
        var applier = FleetApplier()
        applier.seed(try DecodeTests.board())
        #expect(applier.version == nil)
        #expect(applier.cards.count == 9)
        let delta = try Self.frame { raw in
            raw["type"] = "delta"
            raw["base"] = 1
            raw["v"] = 2
        }
        #expect(applier.apply(delta) == .gap)
    }

    /// `free_worktrees` is derived and never sent, because on the wire it would
    /// be a second copy of a fact that can then disagree with the first.
    @Test func freeWorktreesIsDerivedAndMatchesTheServersOwnList() throws {
        var applier = FleetApplier()
        _ = applier.apply(try Self.snapshot())
        let board = try DecodeTests.board()
        let composed = try #require(applier.composed(side: FleetSide(board)))
        #expect(Set(composed.freeWorktrees) == Set(board.freeWorktrees))
        #expect(composed.hostname == board.hostname)
        #expect(composed.resumes.count == board.resumes.count)
    }

    /// A name in `order` with no card is skipped rather than faked. The server
    /// never sends one; the applier must not produce a hole in the board if it
    /// ever does.
    @Test func anOrderNamingACardTheFrameDoesNotCarryIsSkipped() throws {
        var applier = FleetApplier()
        let snap = try Self.snapshot()
        let frame = try Self.frame { raw in
            raw["order"] = snap.order + ["a-worktree-that-is-not-here"]
        }
        _ = applier.apply(frame)
        #expect(applier.composed(side: FleetSide())?.worktrees.count == 9)
    }
}

/// The store's connection rules, driven with literals and an explicit clock.
@MainActor
struct LiveLinkTests {

    static func store() -> FleetStore {
        FleetStore(client: OrchestraClient())
    }

    /// **An idle fleet must read live.** orchestra writes `: keepalive` only
    /// after 25 s of a composed view that has not changed, so a threshold keyed
    /// on "last frame" alone at anything under that dims a perfectly healthy
    /// board every 25 seconds — IOS-APP.md §5.5's named regression.
    @Test func aQuietStreamIsStillLive() throws {
        let store = Self.store()
        let landed = Date()
        store.ingest(try FleetApplierTests.snapshot())
        // Forty seconds of nothing but keepalives: past one keepalive period,
        // inside the budget.
        #expect(store.staleness(now: landed.addingTimeInterval(40)) == .fresh)
    }

    /// Past two keepalives with no token at all, the socket is wedged and the
    /// board must say so even though nothing has thrown.
    @Test func silencePastTwoKeepalivesIsNotFresh() throws {
        let store = Self.store()
        let landed = Date()
        store.ingest(try FleetApplierTests.snapshot())
        let verdict = store.staleness(now: landed.addingTimeInterval(90))
        #expect(verdict.isStale)
        if case .silent = verdict {} else { Issue.record("expected .silent, got \(verdict)") }
    }

    /// **A foreground resume must not throw its cursor away.**
    ///
    /// `resume()` restarts the stream and forces a `/api/state` fetch, so there
    /// is always a window where the link is `.connecting` and a good version is
    /// still held. Seeding in that window nils the version — `/api/state`
    /// carries none — and the delta the server sends in answer to our
    /// `Last-Event-ID` then has no base to land on: a gap, a resync, and a full
    /// 38 KB snapshot for a resume that should have cost one delta. Caught by
    /// driving it: `resyncs: 1` after three background/foreground cycles.
    @Test func aStateFetchDoesNotDiscardACursorTheStreamIsAboutToUse() {
        #expect(!FleetStore.maySeed(link: .connecting, holdingVersion: true))
        #expect(!FleetStore.maySeed(link: .reconnecting(attempt: 2), holdingVersion: true))
        // Nothing to lose: this is the cold open and the polling fallback.
        #expect(FleetStore.maySeed(link: .connecting, holdingVersion: false))
        #expect(FleetStore.maySeed(link: .offline(.serverStopped), holdingVersion: true))
        #expect(FleetStore.maySeed(link: .refused("no observer"), holdingVersion: true))
        // A frame is always newer than a sweep.
        #expect(!FleetStore.maySeed(link: .live, holdingVersion: true))
        #expect(!FleetStore.maySeed(link: .live, holdingVersion: false))
    }

    /// **A refused fetch must back off, and a refused TOKEN must stop.**
    ///
    /// Found by driving a revoked device against the real server on 2026-07-22.
    /// `pump()` ticks every second and `refreshSide` only recorded the clock on
    /// SUCCESS, so a fetch that kept failing never advanced the window and the
    /// app issued `GET /api/state` at **1 Hz forever** — measured in
    /// `audit.log.jsonl`: 30 refusals in 26 seconds from one phone. That storm
    /// spent the server's 10/min per-IP auth budget in about a second, so the
    /// honest `this device is no longer paired` was overwritten by
    /// `the server said 429` before it could be read, on the bar AND on the
    /// screen — the one sentence that tells the user what to do, lost to the
    /// client's own retries.
    ///
    /// Two rules, and the third clause is why the retry arrow still works.
    @Test func aFailingSideFetchBacksOffAndATokenProblemStopsIt() {
        // The cadence applies to the last ATTEMPT, not the last success.
        #expect(!FleetStore.mayFetchSide(link: .offline(.serverStopped),
                                         force: false, sinceLastAttempt: 1))
        #expect(FleetStore.mayFetchSide(link: .offline(.serverStopped),
                                        force: false, sinceLastAttempt: 6))
        // A live stream keeps its slower side period.
        #expect(!FleetStore.mayFetchSide(link: .live, force: false, sinceLastAttempt: 6))
        #expect(FleetStore.mayFetchSide(link: .live, force: false, sinceLastAttempt: 21))
        // 401 is never polled at all — the stream loop already refuses to retry
        // it, and the side fetch was the half still hammering.
        #expect(!FleetStore.mayFetchSide(link: .unauthorized,
                                         force: false, sinceLastAttempt: 3600))
        #expect(!FleetStore.mayFetchSide(link: .unauthorized,
                                         force: false, sinceLastAttempt: nil))
        // …except when the user asks, which is the retry arrow and pull-to-refresh.
        #expect(FleetStore.mayFetchSide(link: .unauthorized,
                                        force: true, sinceLastAttempt: 0))
        // Nothing has been fetched yet: go.
        #expect(FleetStore.mayFetchSide(link: .connecting,
                                        force: false, sinceLastAttempt: nil))
    }

    /// **A refusal spends the window a success would have spent.**
    ///
    /// The rule above is a pure function, and a pure function cannot see WHERE
    /// the clock is written — which is precisely how the defect shipped: the
    /// cadence was correct and `sideAt` was assigned inside the `do` block,
    /// after the `await`, so only a success ever advanced it. Moving the write
    /// back out is the other half of the fix and this is the half that pins it:
    /// nothing here succeeds, and the window must still close.
    @Test func aRefusedSideFetchStillSpendsItsWindow() {
        let store = Self.store()                      // never live, never fetched
        let t0 = Date()
        #expect(store.beginSideFetch(now: t0, force: false))
        #expect(!store.beginSideFetch(now: t0.addingTimeInterval(1), force: false),
                "1 Hz against a server that is refusing us is the whole bug")
        #expect(!store.beginSideFetch(now: t0.addingTimeInterval(4.9), force: false))
        #expect(store.beginSideFetch(now: t0.addingTimeInterval(5), force: false))
        // And the user asking is always honoured, and spends the window too.
        #expect(store.beginSideFetch(now: t0.addingTimeInterval(5.1), force: true))
        #expect(!store.beginSideFetch(now: t0.addingTimeInterval(6), force: false))
    }

    /// Nothing has ever loaded is its own state — the only one that may show a
    /// skeleton.
    @Test func anEmptyStoreIsAbsentRatherThanStale() {
        let store = Self.store()
        #expect(store.staleness(now: Date()) == .absent)
    }

    /// A frame applied through the store lands on the board, and the version it
    /// leaves behind is the reconnect cursor.
    @Test func aFrameReachesTheBoardAndSetsTheCursor() throws {
        let store = Self.store()
        let snap = try FleetApplierTests.snapshot()
        #expect(store.ingest(snap) == .applied)
        #expect(store.state?.worktrees.count == 9)
        #expect(store.version == snap.v)
        #expect(store.link == .live)
        #expect(store.framesApplied == 1)
    }

    /// A gap does not reach the board at all: the store reports it so the loop
    /// can reconnect, and what is on screen stays exactly as it was.
    @Test func aGapLeavesTheBoardAlone() throws {
        let store = Self.store()
        let snap = try FleetApplierTests.snapshot()
        store.ingest(snap)
        let names = store.state?.worktrees.map(\.name)
        let orphan = try FleetApplierTests.frame { raw in
            raw["cards"] = [String: Any]()
            raw["type"] = "delta"
            raw["base"] = snap.v + 50
            raw["v"] = snap.v + 51
        }
        #expect(store.ingest(orphan) == .gap)
        #expect(store.state?.worktrees.map(\.name) == names)
        #expect(store.version == snap.v, "the cursor must not move on a gap")
        #expect(store.framesApplied == 1)
    }
}
