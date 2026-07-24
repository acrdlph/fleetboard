import Foundation
import Testing
@testable import OrchestraKit

/// Fixes to the state layer — the staleness matrix, the server-restart/epoch
/// reset, the reconnect-backoff reset, and SSE event-name routing. Driven with
/// literals and an explicit clock, exactly like `LiveLinkTests`, so nothing here
/// waits on a socket.
@MainActor
struct FixesStoreTests {

    static func store() -> FleetStore { FleetStore(client: OrchestraClient()) }

    static func snapshot() throws -> StreamFrame { try FleetApplierTests.snapshot() }
    static func board() throws -> FleetState { try DecodeTests.board() }

    // MARK: - F1/F8 the staleness matrix

    /// **A seeded board with no stream is on screen, not absent.** Presence is
    /// board DATA, not a stream token — `/api/state` seeds carry no token, and
    /// the old guard keyed on `lastTokenAt` reported `.absent` for a board the
    /// user was looking at.
    @Test func aSeededBoardIsPresentEvenWithNoStreamToken() throws {
        let store = Self.store()
        store.apply(try Self.board())
        // link is still `.idle` (a seed never opens a socket), and there is a
        // board — so this is fresh, never absent.
        #expect(store.staleness(now: Date()) != .absent)
        #expect(store.staleness(now: Date()) == .fresh)
    }

    /// **F8 — a non-live link degrades toward stale.** A board loaded hours ago
    /// and shown behind a link that is merely `.idle`/`.connecting` is not fresh
    /// just because the link is young: the whole connect window (a sleeping Mac
    /// can take a minute to fail) otherwise renders an hours-old board undimmed.
    @Test func anIdleLinkOverAnOldBoardReadsStale() throws {
        let store = Self.store()
        let t0 = Date()
        store.apply(try Self.board())        // link stays .idle
        // Inside the budget: nothing has gone wrong yet.
        #expect(store.staleness(now: t0.addingTimeInterval(10)) == .fresh)
        // Past two keepalives with a still-idle link: the board is stale and says
        // its true age, not `.fresh`.
        let verdict = store.staleness(now: t0.addingTimeInterval(120))
        #expect(verdict.isStale)
        if case .stale(let age) = verdict {
            #expect(age > 100)
        } else {
            Issue.record("expected .stale, got \(verdict)")
        }
    }

    /// **F1(b) — a side fetch must not refresh the LIVENESS clock.** The wedged
    /// socket with working HTTP: the socket is dead but the 20 s `/api/state`
    /// side fetch keeps succeeding. It used to bump `lastTokenAt` through
    /// `publish`, so the `.silent` detector could never fire. A seed publish (the
    /// public stand-in for a side-fetch publish) must leave the liveness clock
    /// where the last stream token left it.
    @Test func aSideFetchDoesNotRefreshTheLivenessClock() throws {
        let store = Self.store()
        let landed = Date()
        store.ingest(try Self.snapshot())            // link .live, token at ~landed
        #expect(store.link == .live)
        // A later /api/state body lands (simulated by a seed publish). Its own
        // clock is now, but it is NOT a stream token.
        store.apply(try Self.board())
        // 90 s after the last real token, the socket is silent past two
        // keepalives — the board must say so, even though a side fetch "just"
        // published.
        let verdict = store.staleness(now: landed.addingTimeInterval(90))
        #expect(verdict.isStale)
        if case .silent = verdict {} else { Issue.record("expected .silent, got \(verdict)") }
    }

    /// **F1(c) — a re-polled fallback board reads its true, small age.** The
    /// staleness age is the board's own clock, so a board reseeded every few
    /// seconds shows "5 s ago", never the unbounded age of the last frame that
    /// happened to arrive before the stream died.
    @Test func aRepolledBoardReadsItsRealAgeNotAnOldFrames() throws {
        let store = Self.store()
        store.apply(try Self.board())               // board data at ~now
        // Read far in the future with no reseed: stale, and old.
        let old = store.staleness(now: Date().addingTimeInterval(200))
        #expect(old.isStale)
        if case .stale(let age) = old { #expect(age > 150) }
        // A fresh poll reseats the board; read against the real clock and the age
        // has collapsed to the poll age, not climbed toward the dead frame's.
        store.apply(try Self.board())
        #expect(store.staleness(now: Date()) == .fresh)
    }

    /// A live but quiet fleet is still fresh inside the budget — the regression
    /// the whole clock split exists to avoid must not have crept back in.
    @Test func aQuietLiveFleetStaysFresh() throws {
        let store = Self.store()
        let landed = Date()
        store.ingest(try Self.snapshot())
        #expect(store.staleness(now: landed.addingTimeInterval(40)) == .fresh)
    }

    // MARK: - F4 event-name routing

    /// **Only `event: state` is a board frame.** An `event: hello` (or any other
    /// name) fed to `StreamFrame.decode` would parse as an empty v0 snapshot and
    /// blank the board; it is routed to `.ignore` instead.
    @Test func onlyStateEventsAreBoardFrames() {
        #expect(FleetStore.route(eventName: "state") == .frame)
        #expect(FleetStore.route(eventName: "hello") == .ignore)
        #expect(FleetStore.route(eventName: "message") == .ignore)
        #expect(FleetStore.route(eventName: "") == .ignore)
    }

    // MARK: - F2 restart / epoch reset

    /// The epoch is parsed from BOTH the current bare-integer id and the future
    /// `"<epoch>:<seq>"` cursor, and never the seq.
    @Test func epochIsParsedFromBothIdFormats() {
        #expect(FleetStore.epoch(fromEventID: "63") == nil)          // today's wire
        #expect(FleetStore.epoch(fromEventID: "9f2c1a04:4711") == "9f2c1a04")
        #expect(FleetStore.epoch(fromEventID: nil) == nil)
        #expect(FleetStore.epoch(fromEventID: ":5") == nil)          // empty epoch
    }

    /// A delta whose version sits below the held one can only be a restart — seq
    /// is monotonic within a boot and coalesced deltas jump it forward only.
    @Test func aBackwardsDeltaVersionIsARestart() {
        #expect(FleetStore.isBackwardsRestart(frameType: .delta, frameVersion: 3, held: 4200))
        #expect(!FleetStore.isBackwardsRestart(frameType: .delta, frameVersion: 4300, held: 4200))
        #expect(!FleetStore.isBackwardsRestart(frameType: .delta, frameVersion: 4200, held: 4200))
        // A snapshot always replaces, so a low-versioned snapshot is not a "gap".
        #expect(!FleetStore.isBackwardsRestart(frameType: .snapshot, frameVersion: 3, held: 4200))
        // Nothing held yet: a first delta is a gap for other reasons, not this one.
        #expect(!FleetStore.isBackwardsRestart(frameType: .delta, frameVersion: 3, held: nil))
    }

    /// Dark longer than the ring horizon drops the cursor so the reconnect takes
    /// a fresh snapshot instead of resuming a cursor that may alias into a new
    /// server boot's numbering.
    @Test func aLongDisconnectDropsTheCursor() {
        #expect(!FleetStore.shouldDropCursor(sinceLastFrame: nil))     // never streamed
        #expect(!FleetStore.shouldDropCursor(sinceLastFrame: 30))
        #expect(!FleetStore.shouldDropCursor(sinceLastFrame: 119))
        #expect(FleetStore.shouldDropCursor(sinceLastFrame: 121))
        #expect(FleetStore.shouldDropCursor(sinceLastFrame: 6 * 3600))
    }

    /// **F2 — a reconfigured server drops the held baseline.** After an unpair or
    /// a re-pair to another Mac, the old cursor and the old board must be gone —
    /// offering that cursor to a new server is what invites cross-boot aliasing,
    /// and the old cards must not stay composed on screen.
    @Test func serverDidChangeResetsTheApplierAndBoard() throws {
        let store = Self.store()
        store.ingest(try Self.snapshot())
        #expect(store.state != nil)
        #expect(store.version != nil)

        store.serverDidChange()
        #expect(store.state == nil)
        #expect(store.version == nil)
        #expect(store.staleness(now: Date()) == .absent)
    }

    // MARK: - F3/F7 backoff reset on a healthy stream

    /// **A stream that delivered a frame resets the reconnect ladder.** The
    /// backoff docstring says the counter resets when a FRAME arrives; the code
    /// never did it. A healthy hour of streaming must not leave a later routine
    /// drop waiting 60 s, nor flag `.offline` on the first transient error of a
    /// fresh incident.
    @Test func aHealthyStreamResetsTheAttemptLadder() {
        // Frames arrived this connection: whatever the accumulated count, the
        // next classification starts from a clean ladder.
        #expect(FleetStore.attemptAfterStream(sawFrame: true, attempt: 9) == 0)
        #expect(FleetStore.attemptAfterStream(sawFrame: true, attempt: 1) == 0)
        // A socket that opened and dropped with NO frame keeps backing off — the
        // hot-loop guard the docstring is careful about.
        #expect(FleetStore.attemptAfterStream(sawFrame: false, attempt: 3) == 3)
        #expect(FleetStore.attemptAfterStream(sawFrame: false, attempt: 0) == 0)
    }
}

/// PushStore's registration and settings fixes, driven through the store with a
/// throwaway `UserDefaults` suite so nothing touches the real domain.
@MainActor
struct FixesPushStoreTests {

    static func defaults(_ name: String = UUID().uuidString) -> UserDefaults {
        UserDefaults(suiteName: name)!
    }

    /// **F6 — a mute is remembered across a relaunch.** The server holds the
    /// authoritative `muted_until` and returns it on no route, so a persisted
    /// mute is the only way a relaunch shows an active snooze rather than an
    /// unmuted screen over a server still suppressing pushes.
    @Test func anActiveMuteSurvivesARelaunch() {
        let d = Self.defaults()
        // Simulate a mute the server is still honouring: an hour out.
        d.set(Date().addingTimeInterval(3600).timeIntervalSince1970,
              forKey: "sh.orchestra.push-muted-until")
        let store = PushStore(client: OrchestraClient(), defaults: d)
        #expect(store.mutedUntil != nil)
    }

    /// A mute that has already passed is not resurrected on launch.
    @Test func anExpiredMuteIsNotRestored() {
        let d = Self.defaults()
        d.set(Date().addingTimeInterval(-60).timeIntervalSince1970,
              forKey: "sh.orchestra.push-muted-until")
        let store = PushStore(client: OrchestraClient(), defaults: d)
        #expect(store.mutedUntil == nil)
    }

    /// **F5 — clearing the server identity forces a re-register.** After the
    /// paired server changes, the registration memo is dropped so the next
    /// `register` cannot be skipped as "unchanged" against a server that has none
    /// of this device's state.
    @Test func serverDidChangeClearsTheRegistrationMemo() {
        let d = Self.defaults()
        d.set("cafef00d", forKey: "sh.orchestra.push-token")
        d.set(60, forKey: "sh.orchestra.push-tz")
        d.set("device-A", forKey: "sh.orchestra.push-server")
        let store = PushStore(client: OrchestraClient(), defaults: d)

        store.serverDidChange()
        #expect(store.registration == .idle)
        #expect(d.string(forKey: "sh.orchestra.push-token") == nil)
        #expect(d.string(forKey: "sh.orchestra.push-server") == nil)
        #expect(d.object(forKey: "sh.orchestra.push-tz") == nil)
    }

    /// **F6 — a failed save rolls the mirror back.** With no server configured,
    /// `savePushSettings` throws `.unauthorized`; the optimistic write must be
    /// undone in memory AND in the persisted mirror, so the screen never shows a
    /// preference the server never accepted.
    @Test func aFailedSaveRestoresThePreviousSettings() async {
        let d = Self.defaults()
        let store = PushStore(client: OrchestraClient(), defaults: d)   // no profile
        let before = store.settings

        var next = store.settings
        next.set(.yourTurn, on: true)               // a non-default override
        #expect(next != before)

        let message = await store.save(next)
        #expect(message != nil, "an unconfigured client refuses, and that is shown once")
        // The in-memory mirror rolled back…
        #expect(store.settings == before)
        #expect(store.settings.isOn(.yourTurn) == false)
        // …and so did the persisted copy: a relaunch reads the old value, not the
        // rejected one.
        let reloaded = PushStore(client: OrchestraClient(), defaults: d)
        #expect(reloaded.settings.isOn(.yourTurn) == false)
    }
}
