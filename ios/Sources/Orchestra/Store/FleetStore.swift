import Foundation
import Observation

/// Where the board's data is coming from right now, and whether to believe it.
///
/// These are the states a phone on a tailnet is really in. `UX.md` §3.12
/// specifies more of them (`slow`, `collector_stuck`, `mac_asleep`) and each of
/// those is keyed on a field this server does not send — `collector_ok` and
/// `wake_gap` arrive in an `event: hello` that `IOS-APP.md` §5.1 describes and
/// that `server._stream` has never written. Modelling a state whose trigger
/// cannot fire would be a dead branch pretending to be a feature, so the states
/// below are exactly the ones this wire can produce.
public enum LinkState: Sendable, Equatable {
    /// Nothing has been started yet.
    case idle
    /// A stream is opening and no frame has landed on it.
    case connecting
    /// A frame has arrived on this socket. **Not "a socket opened"** — that is
    /// `stream.js`'s rule and it is the right one: a connection that is accepted
    /// and then produces nothing is indistinguishable from a healthy one until
    /// you insist on a frame.
    case live
    /// The socket died and another attempt is scheduled. The board on screen is
    /// the last good one and is stale.
    case reconnecting(attempt: Int)
    /// The server answered the stream request with a 503 and a reason: it is
    /// running no sweep (`--demo`), or all 32 subscriber slots are taken. Both
    /// are refusals, not failures — reconnecting fast would be rude and useless,
    /// so the board falls back to polling `/api/state` and retries slowly.
    case refused(String)
    /// Reconnection has been abandoned for now, with the last cause.
    case offline(OrchestraError)
    /// 401. **Never retried** — a token problem is not a network problem.
    case unauthorized

    /// Whether the board should be trusted as current.
    public var isLive: Bool { self == .live }
}

/// How old the data on screen is, decided against an explicit `now`.
///
/// **Liveness and recency are two different signals** (IOS-APP.md §5.5). Any
/// token — including the `: keepalive` comment orchestra writes after 25 s of a
/// composed view that has not changed — proves the socket is alive. Only a frame
/// proves the data is current. A threshold keyed on "last frame" alone at
/// anything under the keepalive period marks a perfectly healthy idle fleet as
/// stale every 25 seconds, and then dims the board of a fleet that is merely
/// quiet. That trains the user to ignore the indicator, which destroys it for
/// the real case.
public enum Staleness: Sendable, Equatable {
    case fresh
    /// The link says live and has been silent past two keepalives — a wedged
    /// socket that has not produced an error yet.
    case silent(TimeInterval)
    /// The link is down and this is the last board that arrived.
    case stale(TimeInterval)
    /// Nothing has ever loaded.
    case absent

    public var isStale: Bool { self != .fresh }

    /// How far behind, for the copy. `nil` when there is nothing to be behind.
    public var age: TimeInterval? {
        switch self {
        case .fresh, .absent: nil
        case .silent(let s), .stale(let s): s
        }
    }
}

/// The board's state, on the main actor.
///
/// `@MainActor` is not decoration here. Every property is read by SwiftUI during
/// a layout pass, and `@Observable`'s change tracking has no isolation of its
/// own — a write from a background task is a data race that shows up as a
/// corrupted diff rather than as a crash. The rule for the whole app is: values
/// cross from the transport actor, stores mutate on the main actor, views read.
@MainActor
@Observable
public final class FleetStore {
    public enum Phase: Sendable, Equatable {
        /// Nothing has ever loaded. This is the ONLY state that may show a
        /// skeleton — every later failure keeps the last good board.
        case cold
        case loading
        case loaded
        case failed(OrchestraError)
    }

    public private(set) var phase: Phase = .cold
    public private(set) var state: FleetState?
    /// Retained across failures on purpose: `UX.md` §3.1.5 — "skeletons never
    /// replace live data". A board that is 40 s old and says so beats a spinner.
    public private(set) var lastGoodAt: Date?
    /// The last transport failure, even when a stale board is still on screen.
    public private(set) var lastError: OrchestraError?
    /// Decode surprises, counted rather than swallowed. An enum widened to
    /// `.unknown` is a server change nobody told the client about, and it should
    /// be visible somewhere other than a pixel.
    public private(set) var unknownStatuses: Int = 0

    // MARK: - The live link

    public private(set) var link: LinkState = .idle
    /// The version the client holds. Also the SSE `Last-Event-ID` it reconnects
    /// with, which is the entire resync path.
    public var version: Int? { applier.version }
    /// When the last token of ANY kind arrived, on the device clock. Liveness.
    ///
    /// **Only STREAM tokens move this** — never the side fetch. A wedged socket
    /// with a working `/api/state` (the collector-stuck pathology FRESHNESS.md
    /// documents at load 26) otherwise had its 20 s side fetch bump this every
    /// cycle, so the `.silent` detector could never fire and an hours-old board
    /// stayed green until the 70 s URLSession timeout. `publish` no longer
    /// stamps it; `runStream` does, on every comment, retry and frame.
    public private(set) var lastTokenAt: Date?
    /// When the last frame was APPLIED, on the device clock. Recency.
    public private(set) var lastFrameAt: Date?
    /// When the board CONTENT last changed, on the device clock — a frame applied
    /// or a `/api/state` body seeded. Liveness and recency are two clocks
    /// (IOS-APP.md §5.5); this is the third, the one the "showing data from N ago"
    /// copy must speak for. A side fetch that refreshes only the side facts
    /// (hostname/user/resumes) does NOT move it, so a board re-polled every 5 s
    /// while the stream is down reads its true 5 s age rather than the unbounded
    /// age of the last frame that happened to arrive before the stream died.
    public private(set) var lastBoardDataAt: Date?
    /// How old each probe tier is, straight off the frame. Rides every frame and
    /// moves with no version bump, so it can never be the cause of a stale card.
    public private(set) var freshness = Freshness()
    /// Frames that arrived and did not decode. Never silent: a payload this
    /// build cannot read is a server change, and it belongs on a diagnostics
    /// line rather than in a `catch {}`.
    public private(set) var decodeFaults: Int = 0
    public private(set) var lastDecodeFault: String?
    /// Times the client found a gap and resynced. A board that resyncs
    /// constantly is worse than one that polls.
    public private(set) var resyncs: Int = 0
    /// Frames applied on this launch — the honest measure of "is it streaming".
    public private(set) var framesApplied: Int = 0

    /// orchestra writes `: keepalive` after `sse_keepalive_s` of silence. It is
    /// a server config knob and it does NOT ride the wire — `IOS-APP.md` §5.1's
    /// `hb` field arrives in an `event: hello` this server has never sent — so
    /// the client carries the default and says so here rather than in four
    /// places. Two keepalives plus slack: one missed comment is a hiccup, two is
    /// a dead socket.
    public static let keepaliveS: TimeInterval = 25
    public static let silenceBudget: TimeInterval = keepaliveS * 1.6 + 5
    /// The maximum packet silence the stream transport tolerates — two
    /// keepalives plus slack — derived from `keepaliveS` so the one server knob
    /// is mirrored in ONE place on the client. `Endpoint.events.timeout` and
    /// `EventStream.streamConfiguration`'s `timeoutIntervalForRequest` hard-code
    /// the same 70; they should read this. See `cross_file_needed`.
    public static let streamRequestTimeout: TimeInterval = keepaliveS * 2 + 20

    /// How long the stream may be dark before the held version can no longer be
    /// trusted to name a slot in the server's current ring. `observer.delta_since`
    /// answers any cursor inside its 512-entry ring with a delta, so a cursor
    /// minted by a PREVIOUS server boot can alias into a new boot's numbering and
    /// a delta then applies cleanly onto a foreign baseline — every card that
    /// differs but is outside the new boot's changed-set stays silently wrong.
    /// Past this horizon the client drops the cursor and takes one snapshot; the
    /// real fix is the `epoch:seq` cursor API §6.1 specifies, adopted below the
    /// instant the wire carries it.
    static let cursorHorizon: TimeInterval = 120

    private var applier = FleetApplier()
    /// The server boot the held baseline belongs to, parsed from the SSE `id:`
    /// when it carries one (`"<epoch>:<seq>"`, API §6.1). `nil` while the wire
    /// still sends a bare integer id — the backwards-version and disconnect-horizon
    /// guards cover that case until the epoch ships.
    private var streamEpoch: String?
    private var side = FleetSide()
    private let client: OrchestraClient
    private var streamTask: Task<Void, Never>?
    private var pumpTask: Task<Void, Never>?
    private var sideAt: Date?
    private var resyncTimes: [Date] = []

    /// Resyncs per minute before the stream is treated as a liability. Past it
    /// the board goes back to polling, which always works.
    private static let resyncBudget = 5
    /// The side fetch — `hostname`, `user` and `resumes`, the three things no
    /// frame carries. 20 s while streaming, 5 s when it is also the board.
    private static let sidePeriodLive: TimeInterval = 20
    private static let sidePeriodPolling: TimeInterval = 5

    /// The device's own network path. Read by the stream loop to avoid burning
    /// reconnect attempts against a radio that has nothing to reach, and by the
    /// UI to tell "this phone has no network" apart from "the server is not
    /// answering". See `PathMonitor`.
    public let path = PathMonitor()

    public init(client: OrchestraClient) {
        self.client = client
    }

    public var groups: [Triage.Group] {
        Triage.groups(state?.worktrees ?? [])
    }

    public var headline: Triage.Headline {
        Triage.headline(state?.worktrees ?? [])
    }

    /// How much to trust what is on screen, against an explicit clock.
    ///
    /// The clock is a parameter and not `Date()` because this is the rule that
    /// decides whether the board is dimmed, and a rule that reads a hidden clock
    /// can only be tested by waiting.
    public func staleness(now: Date) -> Staleness {
        // Presence is decided by BOARD DATA, not by a stream token: a board
        // seeded from `/api/state` with no stream yet is on screen and is not
        // absent.
        guard state != nil, let lastBoardDataAt else { return .absent }
        let boardAge = now.timeIntervalSince(lastBoardDataAt)
        switch link {
        case .live:
            // A quiet fleet keeps the socket warm with keepalives, so it is
            // SILENCE — not board age — that says the socket wedged. Board age
            // must not dim a healthy idle fleet (IOS-APP.md §5.5); only a token
            // drought does. Liveness clock, with the board clock as the floor
            // for a stream that landed one frame and then went quiet.
            let silence = now.timeIntervalSince(lastTokenAt ?? lastBoardDataAt)
            return silence > Self.silenceBudget ? .silent(silence) : .fresh
        case .idle, .connecting:
            // Nothing has failed yet — but a board loaded hours ago and shown
            // behind a link that is merely still opening is not fresh just
            // because the link is young. A foreground after hours must dim for
            // the whole connect window, not only after the first attempt fails.
            return boardAge > Self.silenceBudget ? .stale(boardAge) : .fresh
        case .reconnecting, .offline, .refused, .unauthorized:
            // The age is the board's, so a re-polled fallback board reads its
            // real (small) age instead of the unbounded age of the last frame.
            return .stale(boardAge)
        }
    }

    // MARK: - Lifecycle

    /// Start streaming and start the side fetch. Idempotent.
    ///
    /// **A phone is not a browser tab.** `stop()` must be called on background
    /// and this on foreground: a suspended app cannot read a socket, and what
    /// iOS will do instead of keeping one alive is leave the server holding one
    /// of its 32 subscriber slots for a client that is not there.
    public func start() {
        path.start()
        if streamTask == nil {
            streamTask = Task { [weak self] in await self?.streamLoop() }
        }
        if pumpTask == nil {
            pumpTask = Task { [weak self] in await self?.pump() }
        }
    }

    public func stop() {
        streamTask?.cancel()
        streamTask = nil
        pumpTask?.cancel()
        pumpTask = nil
        if case .unauthorized = link { return }
        link = .idle
    }

    /// Foregrounding. The stream is re-opened with the version still held, so a
    /// short absence costs one delta and a long one costs one snapshot — the
    /// server decides which, from the cursor, and the client never has to know
    /// which case it was in.
    public func resume() async {
        start()
        await refreshSide(force: true)
    }

    // MARK: - Manual refresh

    /// Pull-to-refresh. Fetches the side facts, and — when the stream is not
    /// live — the whole board with them.
    public func refresh() async {
        await refreshSide(force: true)
        // A refresh gesture on a dead stream is also a request to try again now.
        if !link.isLive {
            streamTask?.cancel()
            streamTask = nil
            resyncTimes = []
            start()
        }
    }

    // MARK: - The side fetch (`stream.js` `refresh()`)

    /// `GET /api/state`, for the three fields no frame carries — and, when there
    /// is no live stream, for the board itself.
    ///
    /// **It never seeds over a live stream.** `/api/state` carries no version,
    /// and the sweep that answered it may be older than the last frame applied;
    /// overwriting a streamed board with it would move the board backwards.
    /// May the side fetch run now?
    ///
    /// **The clock is the last ATTEMPT, not the last success**, and that is the
    /// whole rule. `pump()` ticks every second; when only a success recorded the
    /// clock, a fetch that kept failing never advanced the window and the app
    /// issued `GET /api/state` at 1 Hz for as long as the failure lasted.
    /// Measured against the real server with a revoked device: 30 refusals in
    /// 26 s from one phone, which spent orchestra's 10/min per-IP auth budget in
    /// about a second — so `this device is no longer paired` was replaced by
    /// `the server said 429` before anybody could read it. A client whose own
    /// retries hide the one sentence that says what to do.
    ///
    /// And 401 is not polled at all: `streamLoop` already refuses to retry a
    /// token problem, and the side fetch was the half still hammering. The
    /// exception is `force` — the retry arrow and pull-to-refresh, where the
    /// user is the one asking.
    static func mayFetchSide(link: LinkState, force: Bool,
                             sinceLastAttempt: TimeInterval?) -> Bool {
        if force { return true }
        if case .unauthorized = link { return false }
        guard let sinceLastAttempt else { return true }
        return sinceLastAttempt >= (link.isLive ? sidePeriodLive : sidePeriodPolling)
    }

    /// Ask, and — if the answer is yes — spend the window, in one step.
    ///
    /// Deciding and recording are the same call on purpose. Split, the decision
    /// is a pure function a test can pin while the recording sits at a call site
    /// no test reaches, which is exactly how this defect shipped: the rule was
    /// right and the clock was written in the `do` block, after the `await`, so
    /// only a SUCCESS ever advanced it. `now` is a parameter for the same reason
    /// `staleness(now:)` takes one — a rule that reads a hidden clock can only
    /// be tested by waiting.
    func beginSideFetch(now: Date, force: Bool) -> Bool {
        let since = sideAt.map { now.timeIntervalSince($0) }
        guard Self.mayFetchSide(link: link, force: force, sinceLastAttempt: since) else {
            return false
        }
        sideAt = now
        return true
    }

    private func refreshSide(force: Bool) async {
        guard beginSideFetch(now: Date(), force: force) else { return }
        if state == nil, phase == .cold { phase = .loading }
        do {
            let fresh = try await client.fleetState()
            side = FleetSide(fresh)
            let didSeed = Self.maySeed(link: link, holdingVersion: applier.version != nil)
            if didSeed {
                applier.seed(fresh)
            }
            // A seed replaced the board content; a side-facts-only refresh did
            // not, and must not reset the recency clock (that is what let a
            // wedged collector read fresh forever).
            publish(at: Date(), countingAsFrame: false, boardChanged: didSeed)
            lastError = nil
            if state != nil { phase = .loaded }
        } catch let error as OrchestraError {
            if case .cancelled = error { return }
            note(error)
        } catch {
            note(ErrnoCause.classify(error))
        }
    }

    /// May a `/api/state` body REPLACE the board?
    ///
    /// `stream.js` asks only "is the stream live", and in a browser that is close
    /// enough. On a phone it is not, because of one sequence that happens every
    /// single time the app is foregrounded: `resume()` restarts the stream AND
    /// forces a side fetch, so for a few hundred milliseconds the link is
    /// `.connecting` while a perfectly good cursor is still held. Seeding there
    /// sets the version to nil (a `/api/state` body carries no version), the
    /// stream then answers the `Last-Event-ID` we sent with a DELTA, and the
    /// delta has no base to land on — a gap, a resync, and a full 38 KB snapshot
    /// for a resume that should have cost one delta. Observed: `resyncs: 1` on
    /// the diagnostics screen after three background/foreground cycles.
    ///
    /// So: never discard a cursor the stream may still be about to use. Seed
    /// when there is nothing to lose, or when the stream has actually given up
    /// and polling IS the board.
    static func maySeed(link: LinkState, holdingVersion: Bool) -> Bool {
        if link.isLive { return false }    // a frame is always newer than a sweep
        if !holdingVersion { return true } // nothing to lose
        switch link {
        case .offline, .refused: return true       // polling is the board now
        case .idle, .connecting, .reconnecting, .live, .unauthorized: return false
        }
    }

    // MARK: - Server-restart / server-identity detection
    //
    // Three pure rules, each testable with literals, that keep a delta from
    // landing on a baseline it was not computed against.

    /// How one SSE event is routed. Only `state` is a board frame; every other
    /// name — an `event: hello`, a future channel — is a diagnostics-line
    /// surprise and is never fed to `StreamFrame.decode`, where a foreign payload
    /// parses (via the frame's lenient defaults) as an empty v0 snapshot that
    /// blanks the board and stomps the cursor to 0.
    enum EventRoute: Sendable, Equatable { case frame, ignore }
    static func route(eventName: String) -> EventRoute {
        eventName == "state" ? .frame : .ignore
    }

    /// The epoch half of an SSE `id:` (equivalently the reconnect cursor),
    /// tolerating BOTH the current bare integer — `"63"` → no epoch — and the
    /// `"<epoch>:<seq>"` form API §6.1 specifies for the v1 stream —
    /// `"9f2c1a04:4711"` → `"9f2c1a04"`. The seq is never parsed for time;
    /// equality of the epoch is the entire signal.
    static func epoch(fromEventID id: String?) -> String? {
        guard let id, let colon = id.lastIndex(of: ":") else { return nil }
        let epoch = String(id[id.startIndex..<colon])
        return epoch.isEmpty ? nil : epoch
    }

    /// Whether a delta whose version sits BELOW the held one is a server
    /// restart. `seq` is monotonic within a boot and coalesced deltas only ever
    /// jump it FORWARD, so a lower delta version can only mean the numbering
    /// reset under us — a foreign baseline a delta must never be applied to.
    static func isBackwardsRestart(frameType: StreamFrame.Kind,
                                   frameVersion: Int, held: Int?) -> Bool {
        guard frameType == .delta, let held else { return false }
        return frameVersion < held
    }

    /// Whether a reconnect after `sinceLastFrame` of silence should DROP the
    /// held cursor and take a fresh snapshot rather than resume from it. Past the
    /// ring horizon the cursor can alias into a new boot's numbering.
    static func shouldDropCursor(sinceLastFrame: TimeInterval?,
                                 horizon: TimeInterval = cursorHorizon) -> Bool {
        guard let sinceLastFrame else { return false }
        return sinceLastFrame > horizon
    }

    /// A stream that delivered at least one frame ran healthily; its exit must
    /// reset the reconnect ladder to 0 so a later routine drop retries at 1 s and
    /// the first transient error of a fresh incident does not flag `.offline`.
    static func attemptAfterStream(sawFrame: Bool, attempt: Int) -> Int {
        sawFrame ? 0 : attempt
    }

    /// The app was reconfigured to a DIFFERENT server (an unpair, or a re-pair to
    /// another Mac in the same session). The held cursor and composed board
    /// belong to the old server; offering that cursor to a new one invites
    /// exactly the cross-boot aliasing the guards above defend against, and the
    /// old Mac's cards would otherwise stay composed on screen. Wire this to the
    /// pairing `configure`/`unpair` path — see `cross_file_needed`.
    public func serverDidChange() {
        applier.reset()
        streamEpoch = nil
        side = FleetSide()
        state = nil
        lastBoardDataAt = nil
        lastFrameAt = nil
        lastTokenAt = nil
        freshness = Freshness()
        phase = .cold
    }

    private func note(_ error: OrchestraError) {
        lastError = error
        if case .unauthorized = error {
            link = .unauthorized
            state = nil
            phase = .failed(error)
            return
        }
        // A board already on screen stays on screen. Only "we have nothing and
        // cannot get anything" is a failure state the user must look at.
        phase = state == nil ? .failed(error) : .loaded
    }

    // MARK: - The stream

    private enum Exit: Sendable {
        /// `base` did not match: frames were missed. Reconnect with no cursor.
        case gap
        /// The socket closed with no error.
        case ended
        case failed(OrchestraError)
    }

    private func streamLoop() async {
        var attempt = 0
        while !Task.isCancelled {
            // Off-radio in a dead zone: say so at once and wait quietly for a
            // path rather than opening sockets into nothing. This is battery in
            // the field — a phone that keeps a failed connect on a retry timer
            // keeps the radio awake for it. `.offline(.offline)` is distinct from
            // the `.offline(error)` set below after four real attempts: this one
            // means "no network on this device", that one "network is here but
            // the server is not answering".
            if !path.isSatisfied {
                link = .offline(.offline)
                await path.waitUntilSatisfied()
                if Task.isCancelled { return }
                attempt = 0                 // a returned path earns a clean first try
            }
            link = attempt == 0 ? .connecting : .reconnecting(attempt: attempt)
            // Dark longer than the ring can plausibly reach: the held version may
            // alias into a new server boot's numbering, so drop it and take one
            // snapshot rather than risk a delta landing on a foreign baseline.
            if let lastFrameAt,
               Self.shouldDropCursor(sinceLastFrame: Date().timeIntervalSince(lastFrameAt)) {
                applier.reset()
                streamEpoch = nil
            }
            let cursor = applier.version.map(String.init)
            let framesBefore = framesApplied
            let exit = await runStream(cursor: cursor)
            if Task.isCancelled { return }
            // A stream that delivered a frame ran healthily; its exit must not
            // inherit the failed-attempt ladder of the drops before it — the
            // backoff docstring's own contract, finally implemented.
            attempt = Self.attemptAfterStream(sawFrame: framesApplied > framesBefore,
                                              attempt: attempt)

            switch exit {
            case .gap:
                resyncs += 1
                let now = Date()
                resyncTimes.append(now)
                resyncTimes = resyncTimes.filter { now.timeIntervalSince($0) < 60 }
                // A gap is repaired by RECONNECTING with no cursor: `delta_since`
                // answers an unknown cursor with a full snapshot, so the
                // reconnect IS the resync and there is no resync request to
                // invent. Budgeted, because the one thing worse than a gap is a
                // client that answers every gap by opening another socket.
                applier.reset()
                streamEpoch = nil
                attempt = 0
                if resyncTimes.count > Self.resyncBudget {
                    link = .refused("the stream gapped \(resyncTimes.count) times in a minute")
                    resyncTimes = []
                    await sleep(60)
                }
            case .ended:
                attempt += 1
                await backoff(attempt)
            case .failed(let error):
                lastError = error
                if case .cancelled = error { return }
                if case .unauthorized = error {
                    link = .unauthorized
                    return                      // a token problem is not retried
                }
                if case .http(let status, _) = error, status == 503 {
                    // Refused: no sweep running, or all 32 slots taken. Both are
                    // answers, not failures. Poll, and ask again on a slow clock.
                    link = .refused("the server is not streaming right now (503)")
                    await sleep(60)
                    attempt = 0
                    continue
                }
                attempt += 1
                if attempt >= 4 { link = .offline(error) }
                await backoff(attempt)
            }
        }
    }

    /// One connection, from open to death.
    private func runStream(cursor: String?) async -> Exit {
        let tokens = await client.openEvents(lastEventID: cursor)
        do {
            for try await token in tokens {
                if Task.isCancelled { return .failed(.cancelled) }
                switch token {
                case .comment, .retry:
                    // Liveness only. A keepalive proves the socket is alive and
                    // proves nothing at all about the data — see `staleness`.
                    lastTokenAt = Date()
                case .event(let event):
                    lastTokenAt = Date()
                    // **Only `event: state` frames are board frames.** The server
                    // writes that name on every frame (`server._write_frame`); an
                    // `event: hello` (IOS-APP.md §5.1, anticipated here and by the
                    // store's own comments) or any other named event fed to
                    // `StreamFrame.decode` parses — via its lenient defaults — as
                    // an EMPTY v0 snapshot, which blanks the board and stomps the
                    // cursor to 0. Count the surprise on the diagnostics line
                    // instead of dropping it silently.
                    guard Self.route(eventName: event.name) == .frame else {
                        decodeFaults += 1
                        lastDecodeFault = "ignored non-state event: \(event.name)"
                        continue
                    }
                    guard let data = event.data.data(using: .utf8) else { continue }
                    let frame: StreamFrame
                    do {
                        frame = try StreamFrame.decode(data)
                    } catch let error as DecodingError {
                        decodeFaults += 1
                        lastDecodeFault = OrchestraClient.describe(error)
                        continue        // one bad frame is not a dead stream
                    } catch {
                        decodeFaults += 1
                        lastDecodeFault = error.localizedDescription
                        continue
                    }
                    // A server restart is a discontinuity: the held baseline
                    // belongs to the previous boot and a delta applied onto it is
                    // silent corruption. Detected two ways, defensively, so it
                    // works before AND after the v1 stream grows an epoch.
                    let incomingEpoch = Self.epoch(fromEventID: event.id)
                    if let incomingEpoch {
                        if let held = streamEpoch, held != incomingEpoch {
                            applier.reset()        // foreign baseline: start clean
                        }
                        streamEpoch = incomingEpoch
                    }
                    if Self.isBackwardsRestart(frameType: frame.type,
                                               frameVersion: frame.v,
                                               held: applier.version) {
                        // seq is monotonic within a boot, so a lower delta version
                        // can only mean the numbering reset under us. Resnapshot.
                        applier.reset()
                        streamEpoch = incomingEpoch
                        return .gap
                    }
                    if ingest(frame) == .gap { return .gap }
                }
            }
            return .ended
        } catch let error as OrchestraError {
            return .failed(error)
        } catch {
            return .failed(ErrnoCause.classify(error))
        }
    }

    /// Apply one frame. Public so a test can drive the whole applier + store
    /// path with three literals and never touch a socket.
    @discardableResult
    public func ingest(_ frame: StreamFrame) -> FleetApplier.Outcome {
        let outcome = applier.apply(frame)
        guard outcome == .applied else { return outcome }
        framesApplied += 1
        freshness = frame.freshness
        link = .live
        publish(at: Date(), countingAsFrame: true, boardChanged: true)
        return .applied
    }

    /// - Parameters:
    ///   - countingAsFrame: this publish rode a STREAM frame, so it moves the
    ///     liveness and recency clocks. A side fetch never sets it.
    ///   - boardChanged: the composed board CONTENT changed (a frame applied or a
    ///     seed replaced it), so the recency-of-data clock moves. A side fetch
    ///     that only refreshed the side facts leaves it false.
    private func publish(at moment: Date, countingAsFrame: Bool, boardChanged: Bool) {
        guard let composed = applier.composed(side: side) else { return }
        state = composed
        lastGoodAt = moment
        if countingAsFrame {
            lastFrameAt = moment
            lastTokenAt = moment
        }
        if boardChanged {
            lastBoardDataAt = moment
        }
        phase = .loaded
        unknownStatuses = composed.worktrees.reduce(0) { total, card in
            total + card.sessions.filter { $0.status == .unknown }.count
        }
    }

    /// Seed from a `/api/state` body. Public for tests and for anything that has
    /// a board before it has a stream.
    public func apply(_ fresh: FleetState) {
        side = FleetSide(fresh)
        applier.seed(fresh)
        publish(at: Date(), countingAsFrame: false, boardChanged: true)
        lastError = nil
    }

    // MARK: - The clock
    //
    // One 1 s timer with no network of its own: it decides whether anything is
    // owed. The reconnect and the fallback cadence become two comparisons rather
    // than a nest of timers that can each be cancelled independently
    // (`stream.js` `pump()`).

    private func pump() async {
        while !Task.isCancelled {
            await refreshSide(force: false)
            await sleep(1)
        }
    }

    /// `attempt 1: 1s; then 2s, 4s, 8s, 15s, 30s, 60s (cap)`, with ±25 % jitter
    /// on every step (IOS-APP.md §5.7). The counter resets only when a FRAME
    /// arrives, not when a socket connects — a server that accepts and
    /// immediately drops would otherwise produce a hot loop.
    private func backoff(_ attempt: Int) async {
        let ladder: [TimeInterval] = [1, 2, 4, 8, 15, 30, 60]
        let base = ladder[min(max(attempt, 1) - 1, ladder.count - 1)]
        await sleep(base * Double.random(in: 0.75...1.25))
    }

    private func sleep(_ seconds: TimeInterval) async {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }
}

extension LinkState {
    /// The accessory line. Short, lower-case, and it names what is true.
    public var caption: String {
        switch self {
        case .idle: "not connected"
        case .connecting: "connecting…"
        case .live: "live"
        case .reconnecting(let n): "reconnecting… (\(n))"
        case .refused(let why): why
        case .offline(let e): e.headline
        case .unauthorized: "this device is no longer paired"
        }
    }

    public var symbol: String {
        switch self {
        case .idle: "circle.dotted"
        case .connecting: "circle.dashed"
        case .live: "bolt.horizontal.circle.fill"
        case .reconnecting: "arrow.triangle.2.circlepath"
        case .refused: "hand.raised"
        case .offline: "wifi.slash"
        case .unauthorized: "lock"
        }
    }
}
