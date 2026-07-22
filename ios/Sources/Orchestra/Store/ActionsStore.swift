import Foundation
import Observation

/// Everything the app can make the fleet DO, and the memory of what it did.
///
/// It is app-level and not per-screen — the opposite of `ChatStore` — for one
/// reason: **a dispatch outlives the sheet that started it.** The composer can be
/// dismissed, the tab switched, the worktree navigated away from, and the job is
/// still running on the Mac. A store that died with the sheet would lose the job
/// id, and the job id is the only handle on a mission that spends money.
///
/// `@MainActor` for the same reason every store here is: `@Observable` has no
/// isolation of its own, and a write from a background task is a data race that
/// shows up as a corrupted view rather than as a crash. Values cross from the
/// transport actor; this mutates on the main actor; views read.
@MainActor
@Observable
public final class ActionsStore {

    // MARK: - Dispatch

    /// One mission, from the tap that confirmed it to a terminal state.
    public struct DispatchRun: Sendable, Equatable, Identifiable {
        public enum Phase: Sendable, Equatable {
            /// The POST is out and no answer has come back.
            case launching
            /// Accepted; `job` is set and the poll is running.
            case running
            /// The job reached `done: true`.
            case finished(DispatchResult)
            /// The server refused synchronously — nothing was launched. This is
            /// the only phase from which a second attempt is safe.
            case refused(DispatchRefusal)
            /// No answer, or the job id stopped resolving. **This never says
            /// "failed"**: a mission may be running.
            case lost(String)
        }

        public var id: String { key }
        /// A client-side identity for the run. It is NOT an idempotency key — the
        /// server has nowhere to put one — it is how the view finds its own run.
        public let key: String
        public var job: String?
        public var phase: Phase
        public var progress: [String] = []
        public let startedAt: Date
        public let mission: String
        public let worktree: String?
        public let account: String?
        public let model: String
        public let effort: String

        public var isTerminal: Bool {
            switch phase {
            case .launching, .running: false
            case .finished, .refused, .lost: true
            }
        }
    }

    /// The most recent run. One at a time, because the in-flight lock is one at a
    /// time: two concurrent dispatches with `Auto` targeting would very likely
    /// pick the SAME free worktree — the board's own `availability` takes ~30 s to
    /// catch up with a new agent, which is the double-fire `UX.md` §7.4 describes.
    public private(set) var dispatch: DispatchRun?

    /// `UX.md` §4.3 step 8: no terminal phase inside this budget and the run stops
    /// being watched and starts being reconciled.
    public static let dispatchDeadline: TimeInterval = 90

    // MARK: - Finish

    public struct FinishRun: Sendable, Equatable {
        public enum Phase: Sendable, Equatable {
            case running
            case settled(FinishReply)
            case lost(String)
        }
        public let worktree: String
        public var phase: Phase
        public let startedAt: Date
        /// Which of the two steps this attempt was. Decided from the card's
        /// `closeout_sent` at the moment of the tap, so the sheet and the result
        /// agree about what was pressed.
        public let step: FinishStep
    }

    public enum FinishStep: Sendable, Equatable {
        /// `✓ finish` — send the closeout brief.
        case brief
        /// `✕ close` — verify the landing and `/exit`. Never re-sends the brief.
        case close
    }

    /// Keyed by worktree: two different worktrees may be closing out at once.
    public private(set) var finishes: [String: FinishRun] = [:]

    /// **The server-restart hazard, and the only defence a client has.**
    ///
    /// `finish._closeouts` is an in-memory dict. A server restart drops it, the
    /// card stops reporting `closeout_sent`, and the button silently reverts from
    /// `✕ close` to `✓ finish` — pressing which re-types the whole 600-character
    /// brief at an agent that may be mid-closeout. So the client remembers, for
    /// 30 minutes, that it sent a brief for this worktree. If the card stops
    /// saying `closeout_sent` while live procs remain, the sheet warns and demotes
    /// its primary button (`UX.md` §4.4).
    ///
    /// It is honest about its limits: it only knows about briefs **this phone**
    /// sent. A brief sent from the desktop board is invisible to it.
    public private(set) var briefsSentLocally: [String: Date] = [:]
    public static let briefMemory: TimeInterval = 30 * 60

    // MARK: - Resume

    /// The last thing an arm/disarm said, per `"{worktree}|{sid}"`. Shown inline
    /// on the sheet that caused it and cleared when that sheet closes.
    public private(set) var resumeNotices: [String: ResumeReply] = [:]

    // MARK: -

    /// The lock. One attempt per target, and the client's whole substitute for
    /// the idempotency the server does not offer — see `Actuation`.
    private var inFlight = InFlight()
    private let client: OrchestraClient
    private weak var fleet: FleetStore?
    private var dispatchPoll: Task<Void, Never>?

    public init(client: OrchestraClient, fleet: FleetStore? = nil) {
        self.client = client
        self.fleet = fleet
    }

    public func attach(fleet: FleetStore) { self.fleet = fleet }

    public func isBusy(_ key: InFlight.Key, now: Date = Date()) -> Bool {
        inFlight.isBusy(key, at: now)
    }

    // MARK: - Dispatch

    /// Launch a mission. Returns immediately; watch `dispatch` for the phases.
    ///
    /// **It refuses to start a second one while one is in flight, and it does not
    /// queue it.** A queued dispatch is a dispatch the user did not confirm at the
    /// moment it ran.
    public func launch(mission: String, worktree: String?, account: String?,
                       model: String, effort: String, forceModel: Bool) {
        let now = Date()
        guard inFlight.begin(.dispatch, at: now) else { return }
        let key = UUID().uuidString
        dispatch = DispatchRun(key: key, job: nil, phase: .launching, startedAt: now,
                               mission: mission, worktree: worktree, account: account,
                               model: model, effort: effort)
        dispatchPoll?.cancel()
        dispatchPoll = Task { [weak self] in
            await self?.runDispatch(key: key, mission: mission, worktree: worktree,
                                    account: account, model: model, effort: effort,
                                    forceModel: forceModel)
        }
    }

    private func runDispatch(key: String, mission: String, worktree: String?,
                             account: String?, model: String, effort: String,
                             forceModel: Bool) async {
        defer { inFlight.end(.dispatch) }
        let start: DispatchStart
        do {
            start = try await client.dispatch(mission: mission, worktree: worktree,
                                              account: account, model: model,
                                              effort: effort, forceModel: forceModel)
        } catch {
            // No answer. **Not a failure** — the mission may be running. The UI
            // never offers a retry from here (`Actuation.mayOfferRetry`).
            settleDispatch(key: key,
                           .lost(Actuation.indeterminateCopy(for: .dispatch)))
            return
        }
        switch start {
        case .refused(let refusal):
            settleDispatch(key: key, .refused(refusal))
        case .accepted(let job):
            guard var run = dispatch, run.key == key else { return }
            run.job = job
            run.phase = .running
            dispatch = run
            await pollDispatch(key: key, job: job)
        }
    }

    private func pollDispatch(key: String, job: String) async {
        let deadline = Date().addingTimeInterval(Self.dispatchDeadline)
        while !Task.isCancelled {
            if Date() >= deadline {
                settleDispatch(key: key,
                               .lost(Actuation.indeterminateCopy(for: .dispatch)))
                return
            }
            do {
                let status = try await client.dispatchStatus(job: job)
                guard var run = dispatch, run.key == key else { return }
                if !status.ok {
                    // `unknown job` — `_jobs` keeps the last 20 and a restart
                    // erases all of them. The mission is not necessarily gone.
                    settleDispatch(key: key, .lost(
                        "the server no longer knows this job (\(status.error ?? "unknown job")). "
                        + "It keeps only the last 20 and forgets them all on restart — "
                        + "the mission may well be running. Check the worktree."))
                    return
                }
                run.progress = status.progress
                dispatch = run
                if status.done {
                    if let result = status.result {
                        settleDispatch(key: key, .finished(result))
                    } else {
                        // `_run_dispatch` has no try/except, so a raise inside it
                        // strands the job at `done: false, result: null` forever.
                        // `done: true` with no result would be a new way to be
                        // stranded; say so rather than spin.
                        settleDispatch(key: key, .lost(
                            "the job finished without a result — the server's "
                            + "dispatch thread ended without recording an outcome."))
                    }
                    return
                }
            } catch {
                // A poll is a GET and is safe to repeat, so a transport hiccup
                // here is not terminal — the deadline above is.
            }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
    }

    private func settleDispatch(key: String, _ phase: DispatchRun.Phase) {
        guard var run = dispatch, run.key == key else { return }
        run.phase = phase
        dispatch = run
        if case .finished = phase {
            // A new agent takes ~30 s to register as busy; nudge the board so the
            // card is not still claiming FREE when the user goes to look.
            Task { [weak fleet] in await fleet?.refresh() }
        }
    }

    /// Forget a terminal run, so the composer can be used again. A run that is
    /// still in flight is never cleared — that is the lock.
    public func clearDispatch() {
        guard let run = dispatch, run.isTerminal else { return }
        dispatch = nil
    }

    // MARK: - Finish

    /// Step one or step two of the closeout, decided by the caller from the
    /// card's own `closeout_sent`.
    public func finish(worktree: String, step: FinishStep) {
        let now = Date()
        guard inFlight.begin(.finish(worktree: worktree), at: now) else { return }
        finishes[worktree] = FinishRun(worktree: worktree, phase: .running,
                                       startedAt: now, step: step)
        Task { [weak self] in await self?.runFinish(worktree: worktree, step: step) }
    }

    private func runFinish(worktree: String, step: FinishStep) async {
        defer { inFlight.end(.finish(worktree: worktree)) }
        do {
            let reply = try await client.finish(worktree: worktree)
            guard var run = finishes[worktree] else { return }
            run.phase = .settled(reply)
            finishes[worktree] = run
            if reply.ok, reply.mode?.startsCloseout == true {
                noteBriefSent(worktree)
            }
            if reply.ok {
                // `start_finish` calls `observer.nudge`, so the board already
                // knows — but the phone is holding a stream, and a refresh here
                // closes the gap between "the sheet says done" and "the card
                // agrees".
                await fleet?.refresh()
            }
        } catch {
            guard var run = finishes[worktree] else { return }
            run.phase = .lost(Actuation.indeterminateCopy(for: .finish))
            finishes[worktree] = run
        }
    }

    public func clearFinish(worktree: String) {
        guard let run = finishes[worktree] else { return }
        if case .running = run.phase { return }
        finishes.removeValue(forKey: worktree)
    }

    /// Record that a brief went out. One writer in production —
    /// `runFinish` — and it is a named method rather than an inline assignment so
    /// the restart detector can be driven in a test without a network.
    func noteBriefSent(_ worktree: String, at moment: Date = Date()) {
        briefsSentLocally[worktree] = moment
    }

    /// **Did the server forget a brief this phone sent?**
    ///
    /// True when we remember briefing this worktree recently, the card no longer
    /// reports `closeout_sent`, and an agent is still live on it. That combination
    /// is the restart — a genuine clean close removes the live proc too.
    public func serverForgotBrief(card: Worktree, now: Date = Date()) -> Bool {
        guard let sent = briefsSentLocally[card.name],
              now.timeIntervalSince(sent) < Self.briefMemory else { return false }
        return !card.isCloseoutPending && !card.liveProcs.isEmpty
    }

    public func rememberedBrief(for worktree: String, now: Date = Date()) -> Date? {
        guard let sent = briefsSentLocally[worktree],
              now.timeIntervalSince(sent) < Self.briefMemory else { return nil }
        return sent
    }

    // MARK: - Resume

    public static func resumeKey(worktree: String, sid: String) -> String {
        "\(worktree)|\(sid)"
    }

    public func notice(worktree: String, sid: String) -> ResumeReply? {
        resumeNotices[Self.resumeKey(worktree: worktree, sid: sid)]
    }

    public func clearNotice(worktree: String, sid: String) {
        resumeNotices.removeValue(forKey: Self.resumeKey(worktree: worktree, sid: sid))
    }

    /// Arm or re-arm. Idempotent server-side (`_resumes` is keyed
    /// `"{worktree}|{sid}"`), which is why this one is allowed to be retried.
    public func armResume(worktree: String, sid: String, account: String,
                          delayS: Double?, resetsAt: Double?, dueAt: Double?) async {
        let key = InFlight.Key.resume(worktree: worktree, sid: sid)
        guard inFlight.begin(key, at: Date()) else { return }
        defer { inFlight.end(key) }
        let store = Self.resumeKey(worktree: worktree, sid: sid)
        do {
            let reply = try await client.armResume(worktree: worktree, sid: sid,
                                                   account: account, delayS: delayS,
                                                   resetsAt: resetsAt, dueAt: dueAt)
            resumeNotices[store] = reply
            // Schedules ride only on `/api/state` — `resume.py` is not watched by
            // the observer, so arming moves no version and NO frame will ever
            // carry it. Without this the sheet says "armed" and the board does
            // not show it for up to 20 s.
            if reply.ok { await fleet?.refresh() }
        } catch {
            resumeNotices[store] = ResumeReply(
                ok: false, message: Actuation.indeterminateCopy(for: .resume))
        }
    }

    /// **Manual resume: one word, `continue`, into a session idle by definition.**
    ///
    /// It goes through `POST /api/send` and the same identity-addressed path as a
    /// reply, so a session that lost its terminal gets the identity guard's
    /// refusal rather than a keystroke at whoever holds that pid now. No
    /// confirmation — `UX.md` §7.5 lists it among the things that must NOT have
    /// friction — and the reply lands in the same notice slot as an arm.
    public func resumeNow(worktree: String, session: Session) async {
        let key = InFlight.Key.send(sid: session.sid)
        guard inFlight.begin(key, at: Date()) else { return }
        defer { inFlight.end(key) }
        let store = Self.resumeKey(worktree: worktree, sid: session.sid)
        do {
            let reply = try await client.send(account: session.account, sid: session.sid,
                                              worktree: worktree, text: "continue")
            switch Actuation.outcome(ofSend: reply) {
            case .succeeded:
                resumeNotices[store] = ResumeReply(ok: true, message: reply.text)
            case .refused:
                resumeNotices[store] = ResumeReply(ok: false, message: reply.text)
            case .ambiguous(let why):
                resumeNotices[store] = ResumeReply(ok: false,
                                                   message: reply.text + " — " + why)
            case .indeterminate:
                resumeNotices[store] = ResumeReply(
                    ok: false, message: Actuation.indeterminateCopy(for: .send))
            }
        } catch {
            resumeNotices[store] = ResumeReply(
                ok: false, message: Actuation.indeterminateCopy(for: .send))
        }
    }

    public func cancelResume(worktree: String, sid: String) async {
        let key = InFlight.Key.resume(worktree: worktree, sid: sid)
        guard inFlight.begin(key, at: Date()) else { return }
        defer { inFlight.end(key) }
        let store = Self.resumeKey(worktree: worktree, sid: sid)
        do {
            let reply = try await client.cancelResume(worktree: worktree, sid: sid)
            resumeNotices[store] = reply
            await fleet?.refresh()
        } catch {
            resumeNotices[store] = ResumeReply(
                ok: false, message: Actuation.indeterminateCopy(for: .resume))
        }
    }
}
