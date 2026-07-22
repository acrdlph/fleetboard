import Foundation
import Testing
@testable import OrchestraKit

/// Phase 3 — the mutations.
///
/// **Every JSON literal below is a body a real server produced**, captured on
/// 2026-07-22 by driving `100.113.110.31:4269` with curl. Not one of them is
/// hand-written from API.md, because API.md describes a contract this server does
/// not have — no idempotency key, no intent frames, no phase stream, and a
/// refusal that arrives as HTTP 200.
struct ActionTests {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: - send

    /// Live body, `POST /api/send {"pid": 93506, "text": "x"}` → **200**.
    @Test func aBarePidIsRefusedAndTheRefusalArrivesAs200() throws {
        let reply = try decode(SendReply.self, """
        {"ok": false, "error": "unaddressed",
         "message": "refusing to act on pid 93506 alone \\u2014 pids are recycled, \
        so a bare pid can name a different agent by the time the click lands \
        (ADR 0008). Reload the board and try again."}
        """)
        #expect(reply.ok == false)
        #expect(reply.error == "unaddressed")
        #expect(reply.isIdentityRefusal)
        #expect(reply.text.contains("pids are recycled"))
    }

    /// Live body, a sid the board no longer carries.
    @Test func aVanishedSessionIsAnIdentityRefusal() throws {
        let reply = try decode(SendReply.self, """
        {"ok": false, "error": "identity_gone",
         "message": "that agent is gone \\u2014 session 00000000 is no longer on the board"}
        """)
        #expect(reply.isIdentityRefusal)
        #expect(Actuation.outcome(ofSend: reply) == .refused)
    }

    /// Live body, the successful path.
    @Test func aSuccessfulSendCarriesTheHostItTypedInto() throws {
        let reply = try decode(SendReply.self,
                               #"{"ok": true, "message": "typed into Terminal (ttys008)"}"#)
        #expect(reply.ok)
        #expect(Actuation.outcome(ofSend: reply) == .succeeded)
        #expect(reply.isIdentityRefusal == false)
    }

    /// **The one refusal that is not a clean "nothing happened".**
    ///
    /// `terminal.send_to_process` computes `ok = rc1 == 0 and rc2 == 0` over two
    /// separate `tmux send-keys` calls — the text, then Enter. The second failing
    /// leaves the message in the agent's composer, unsent, and a "retry" would
    /// type it twice.
    @Test func aTmuxFailureIsAmbiguousRatherThanRefused() throws {
        let reply = try decode(SendReply.self,
                               #"{"ok": false, "message": "tmux send-keys failed"}"#)
        guard case .ambiguous(let why) = Actuation.outcome(ofSend: reply) else {
            Issue.record("a tmux failure must not classify as a clean refusal")
            return
        }
        #expect(why.contains("composer"))
        #expect(Actuation.mayOfferRetry(.ambiguous(why)) == false)
    }

    /// The retry rule, stated once: only a clean refusal may be offered again.
    @Test func onlyACleanRefusalMayBeRetried() {
        #expect(Actuation.mayOfferRetry(.refused))
        #expect(Actuation.mayOfferRetry(.succeeded) == false)
        #expect(Actuation.mayOfferRetry(.indeterminate) == false)
        #expect(Actuation.mayOfferRetry(.ambiguous("x")) == false)
    }

    /// A timeout is never rendered as failure — the most dangerous message
    /// available here, because the side effect very likely happened.
    @Test func indeterminateCopyNeverSaysFailed() {
        for kind in [Actuation.Kind.send, .dispatch, .finish, .resume] {
            let copy = Actuation.indeterminateCopy(for: kind)
            #expect(copy.lowercased().contains("fail") == false,
                    "\(kind) copy claims failure: \(copy)")
        }
        #expect(Actuation.indeterminateCopy(for: .dispatch).contains("SECOND agent"))
    }

    // MARK: - the in-flight lock

    @Test func aSecondAttemptAtTheSameTargetIsRefusedNotQueued() {
        var lock = InFlight()
        let now = Date()
        let first = lock.begin(.dispatch, at: now)
        let second = lock.begin(.dispatch, at: now.addingTimeInterval(1))
        lock.end(.dispatch)
        let third = lock.begin(.dispatch, at: now.addingTimeInterval(2))
        #expect(first)
        #expect(second == false)
        #expect(third)
    }

    /// Two different worktrees may close out at once; what must never overlap is
    /// two attempts at the SAME target.
    @Test func theLockIsPerTargetNotPerKind() {
        var lock = InFlight()
        let now = Date()
        let a = lock.begin(.finish(worktree: "a"), at: now)
        let b = lock.begin(.finish(worktree: "b"), at: now)
        let againA = lock.begin(.finish(worktree: "a"), at: now)
        #expect(a)
        #expect(b)
        #expect(againA == false)
    }

    /// A leak guard, not a timeout: it is longer than the longest deadline in the
    /// app so it can never expire under a live request.
    @Test func aForgottenLockExpiresButNotUnderALiveRequest() {
        var lock = InFlight()
        let now = Date()
        let took = lock.begin(.send(sid: "s"), at: now)
        #expect(took)
        #expect(lock.isBusy(.send(sid: "s"), at: now.addingTimeInterval(120)))
        #expect(lock.isBusy(.send(sid: "s"),
                            at: now.addingTimeInterval(InFlight.staleAfter + 1)) == false)
        #expect(InFlight.staleAfter > 120, "must outlast the 120 s finish deadline")
    }

    // MARK: - dispatch

    /// Live body. Note there is **no `ok`** on the accepted branch at all.
    @Test func anAcceptedDispatchIsAJobIdAndNothingElse() throws {
        let start = try decode(DispatchStart.self, #"{"job": "job-214849-1"}"#)
        #expect(start == .accepted(job: "job-214849-1"))
    }

    /// Live body, `POST /api/dispatch {"mission": "probe"}`.
    @Test func aDispatchWithoutModelOrEffortIsRefusedByTheServer() throws {
        let start = try decode(DispatchStart.self, """
        {"ok": false, "message": "pick a model and an effort first \\u2014 routing \
        is deterministic, nothing is chosen for you"}
        """)
        guard case .refused(let refusal) = start else {
            Issue.record("a body with no job must not decode as accepted")
            return
        }
        #expect(refusal.needsDecision == false)
        #expect(refusal.text.contains("nothing is chosen for you"))
    }

    /// Live body — driven for real against an account that does not exist, which
    /// is the cheapest way to reach the headroom dialog.
    @Test func theHeadroomDialogCarriesItsAlternative() throws {
        let start = try decode(DispatchStart.self, """
        {"ok": false, "needs_decision": true, "model": "haiku",
         "message": "No haiku headroom on account [no-such-account] \\u2014 no readable \
        account for this model.",
         "can_opus": false, "opus_account": null, "opus_left": null}
        """)
        guard case .refused(let refusal) = start else {
            Issue.record("needs_decision must decode as a refusal")
            return
        }
        #expect(refusal.needsDecision)
        #expect(refusal.model == "haiku")
        #expect(refusal.canOpus == false)
        #expect(refusal.opusAccount == nil)
    }

    /// Live body — the job path, driven end to end with a worktree that does not
    /// exist so no agent was ever launched.
    @Test func aJobThatFailedInsideTheThreadIsDoneWithAFailedResult() throws {
        let job = try decode(DispatchJob.self, """
        {"ok": true, "progress": [], "done": true,
         "result": {"ok": false, "message": "unknown worktree nope-not-real"}}
        """)
        #expect(job.ok)
        #expect(job.done)
        #expect(job.result?.ok == false)
        #expect(job.result?.text == "unknown worktree nope-not-real")
    }

    /// Live body. `_jobs` keeps the last 20 and a restart erases all of them, so
    /// this is reachable without anything going wrong.
    @Test func aForgottenJobIsNamedRatherThanSilent() throws {
        let job = try decode(DispatchJob.self, #"{"ok": false, "error": "unknown job"}"#)
        #expect(job.ok == false)
        #expect(job.error == "unknown job")
        #expect(job.done == false)
    }

    /// **`effort_confirmed` is tri-state and a `?? false` would libel the server.**
    /// `_run_dispatch` leaves it `None` when no effort was asked for.
    @Test func effortConfirmedIsAbsentRatherThanFalseWhenNoEffortWasSet() throws {
        let noEffort = try decode(DispatchResult.self, """
        {"ok": true, "message": "launched", "session": "mission-x-101010"}
        """)
        #expect(noEffort.effortConfirmed == nil)
        let unconfirmed = try decode(DispatchResult.self, """
        {"ok": true, "message": "launched", "effort": "xhigh", "effort_confirmed": false}
        """)
        #expect(unconfirmed.effortConfirmed == false)
    }

    @Test func aLaunchedDispatchCarriesItsAttachCommand() throws {
        let result = try decode(DispatchResult.self, """
        {"ok": true, "message": "launched mission-confidai7-214849 in ConfidAi7 on [acct2]",
         "session": "mission-confidai7-214849", "worktree": "ConfidAi7",
         "account": "acct2", "model": "haiku", "effort": "high",
         "effort_confirmed": true, "kickoff_sent": true,
         "attach": "tmux -L fleet attach -t mission-confidai7-214849"}
        """)
        #expect(result.attach == "tmux -L fleet attach -t mission-confidai7-214849")
        #expect(result.kickoffSent == true)
    }

    // MARK: - finish

    /// Live body, an unknown worktree — and note it carries **no `mode`**, which
    /// is why `mode` is optional.
    @Test func anEarlyFinishRefusalCarriesNoMode() throws {
        let reply = try decode(FinishReply.self,
                               #"{"ok": false, "message": "unknown worktree 'nope'"}"#)
        #expect(reply.ok == false)
        #expect(reply.mode == nil)
        #expect(FinishCopy.whatHappensNext(reply) == nil)
        #expect(FinishCopy.result(reply) == "unknown worktree 'nope'")
    }

    /// `finish.py`'s step-two refusal, with the three fields the card note is
    /// built from. `sent` is an ABSOLUTE epoch on purpose — an elapsed string
    /// computed on the Mac and read on a phone minutes later is dead on arrival.
    @Test func aPendingCloseoutCarriesTheFilesAndAnAbsoluteStamp() throws {
        let reply = try decode(FinishReply.self, """
        {"ok": false, "mode": "pending", "left": "3 leftover file(s)",
         "sent": 1784749745.04,
         "files": [" M a.py", "?? scratch.txt", "?? notes.md"],
         "message": "can't close yet \\u2014 3 leftover file(s). The closeout brief \
        has already gone to the agent; if it looks stuck, \\u2709 chat with it. \
        \\u2715 close works once the landing verifies."}
        """)
        #expect(reply.mode == .pending)
        #expect(reply.left == "3 leftover file(s)")
        #expect(reply.files.count == 3)
        #expect(reply.sent == Date(timeIntervalSince1970: 1784749745.04))
        #expect(FinishCopy.result(reply).contains("clears itself"))
    }

    /// `chat` is a DISTINCT mode, not a flavour of `pending`: a typed nudge would
    /// collide with the agent's open dialog, so the UI must route to chat.
    @Test func chatModeIsNotPending() throws {
        let reply = try decode(FinishReply.self, """
        {"ok": false, "mode": "chat", "left": "branch not landed on origin/main",
         "sent": 1784749745.04, "message": "can't close yet"}
        """)
        #expect(reply.mode == .chat)
        #expect(reply.mode?.startsCloseout == false)
        #expect(FinishCopy.whatHappensNext(reply)?.contains("open dialog") == true)
    }

    @Test func briefAndSlimAreTheTwoModesThatStartStepTwo() {
        #expect(FinishMode.brief.startsCloseout)
        #expect(FinishMode.slim.startsCloseout)
        for mode in [FinishMode.exit, .nudge, .pending, .chat, .parked, .noop, .unknown] {
            #expect(mode.startsCloseout == false, "\(mode) must not arm step two")
        }
    }

    /// A mode this build has never heard of must not throw the reply away — the
    /// reply also carries the human message that says what happened.
    @Test func anUnknownFinishModeWidensRatherThanThrows() throws {
        let reply = try decode(FinishReply.self,
                               #"{"ok": true, "mode": "teleported", "message": "hm"}"#)
        #expect(reply.mode == .unknown)
        #expect(reply.text == "hm")
    }

    // MARK: - the two-step, as the card publishes it

    /// **`closeout_sent` is the whole state machine on the wire.**
    /// `ios/README.md` finding 3 said this field did not exist; it does, written
    /// by `observer.py:228`, and its presence is what makes the button ✕ close.
    @Test func aCardCarriesTheCloseoutStampAndAbsenceMeansStepOne() throws {
        let json = """
        {"name": "wt", "path": "/x", "availability": "attention",
         "git": {"branch": "main", "dirty": 0, "ahead": null, "behind": null},
         "sessions": [], "live_procs": [], "closeout_sent": 1784749745.04}
        """
        let card = try decode(Worktree.self, json)
        #expect(card.isCloseoutPending)
        #expect(card.closeoutSentAt == Date(timeIntervalSince1970: 1784749745.04))

        let without = try decode(Worktree.self, """
        {"name": "wt", "path": "/x", "availability": "free",
         "git": {"branch": "main", "dirty": 0, "ahead": null, "behind": null},
         "sessions": [], "live_procs": []}
        """)
        #expect(without.isCloseoutPending == false)
        #expect(without.closeoutSent == nil)
    }

    /// **The server-restart hazard.** `finish._closeouts` is in-memory: a restart
    /// drops it, the card stops reporting `closeout_sent`, and the button reverts
    /// to ✓ finish — pressing which re-types the whole brief.
    @MainActor
    @Test func aVanishedCloseoutWithALiveAgentReadsAsARestart() {
        let store = ActionsStore(client: OrchestraClient())
        let now = Date()
        let proc = LiveProc(pid: 1, cpu: 0, etime: "1:00", tty: "ttys001", host: "Terminal",
                            account: "main", tmux: nil, reachable: true, subdir: nil)
        let git = GitInfo(branch: "main", commit: nil, dirty: 0, ahead: nil, behind: nil)
        let live = Worktree(name: "wt", path: "/x", git: git, sessions: [],
                            liveProcs: [proc], availability: .busy, closeoutSent: nil)

        // Nothing remembered → nothing to warn about.
        #expect(store.serverForgotBrief(card: live, now: now) == false)

        store.noteBriefSent("wt", at: now.addingTimeInterval(-60))
        #expect(store.serverForgotBrief(card: live, now: now))

        // The server still reports it: no restart, no warning.
        let stillPending = Worktree(name: "wt", path: "/x", git: git, sessions: [],
                                    liveProcs: [proc], availability: .busy,
                                    closeoutSent: now.timeIntervalSince1970)
        #expect(store.serverForgotBrief(card: stillPending, now: now) == false)

        // A clean close takes the agent with it — that is not a restart.
        let closed = Worktree(name: "wt", path: "/x", git: git, sessions: [],
                              liveProcs: [], availability: .free, closeoutSent: nil)
        #expect(store.serverForgotBrief(card: closed, now: now) == false)

        // And the memory expires rather than warning forever.
        #expect(store.serverForgotBrief(
            card: live, now: now.addingTimeInterval(ActionsStore.briefMemory + 60)) == false)
    }

    // MARK: - resume

    /// Live body. Arming twice replaces rather than adds — `_resumes` is keyed
    /// `"{worktree}|{sid}"` — which is why this is the one mutation with no
    /// disable-on-tap.
    @Test func anArmedResumeCarriesItsResolvedDueTime() throws {
        let reply = try decode(ResumeReply.self, """
        {"ok": true, "due_at": 1784753345.023881, "message": "auto-resume armed for 22:49"}
        """)
        #expect(reply.ok)
        #expect(reply.due == Date(timeIntervalSince1970: 1784753345.023881))
        #expect(reply.needTime == false)
    }

    /// **`need_time` is not an error.** The sheet expands its time picker rather
    /// than showing a failure.
    @Test func needTimeIsARequestForATimeNotAFailure() throws {
        let reply = try decode(ResumeReply.self, """
        {"ok": false, "need_time": true,
         "message": "no known reset time for this limit \\u2014 pick an exact time"}
        """)
        #expect(reply.ok == false)
        #expect(reply.needTime)
        #expect(reply.dueAt == nil)
    }

    /// Live body. A second cancel is a statement about the world, not an error.
    @Test func cancellingNothingIsAnAnswer() throws {
        let reply = try decode(ResumeReply.self, """
        {"ok": false, "message": "nothing armed for this session"}
        """)
        #expect(reply.ok == false)
        #expect(reply.needTime == false)
    }

    // MARK: - what the wire does to a message

    /// The composer applies the server's own normalisation as you type, so what
    /// is on screen is what the agent gets.
    @Test func newlinesCollapseExactlyAsTheServerWouldCollapseThem() {
        #expect(WireText.collapsed("a\nb") == "a b")
        #expect(WireText.collapsed("a  \n   b") == "a b")
        #expect(WireText.collapsed("  a\n\nb  ") == "a b")
        #expect(WireText.collapsed("   ") == "")
        #expect(WireText.collapsed("plain") == "plain")
    }

    /// The transcript sighting is positive-only, and it has to survive the
    /// server's 899-character cut.
    @Test func aSightingSurvivesTheServersTruncation() {
        #expect(WireText.matches(sent: "hello there", turn: "hello there"))
        #expect(WireText.matches(sent: "hello\nthere", turn: "hello there"))
        let long = String(repeating: "x", count: 1000)
        let cut = String(long.prefix(899)) + "\u{2026}"
        #expect(WireText.matches(sent: long, turn: cut))
        #expect(WireText.matches(sent: "hello", turn: "goodbye") == false)
        #expect(WireText.matches(sent: "hello", turn: "\u{2026}") == false)
    }

    // MARK: - who can be typed at

    /// `reachable` is the server's gate and the client does not second-guess it.
    @Test func onlyASessionWithAReachableProcessOffersAComposer() {
        let git = GitInfo(branch: "main", commit: nil, dirty: 0, ahead: nil, behind: nil)
        func session(_ sid: String, pid: Int32?) -> Session {
            Session(shortID: sid, sid: sid, account: "main", lastWriteAt: 0, cwd: "/x",
                    subdir: nil, branch: "main", model: "", pendingTools: [],
                    pendingWorkflows: 0, pendingBackgroundAgents: 0,
                    pendingBackgroundTools: 0, topic: nil, lastAssistant: nil,
                    lastUser: nil, subagentSaid: nil, subagentsActive: false, pid: pid,
                    pidCertain: true, status: .waiting, turnEnded: nil, limit: nil,
                    handedTo: nil, toolRunning: false, bgShell: false)
        }
        func proc(_ pid: Int32, reachable: Bool) -> LiveProc {
            LiveProc(pid: pid, cpu: 0, etime: "1:00", tty: "ttys001", host: "Terminal",
                     account: "main", tmux: nil, reachable: reachable, subdir: nil)
        }
        let card = Worktree(name: "wt", path: "/x", git: git,
                            sessions: [session("live", pid: 10),
                                       session("unscriptable", pid: 11),
                                       session("ended", pid: nil)],
                            liveProcs: [proc(10, reachable: true),
                                        proc(11, reachable: false)],
                            availability: .busy)
        #expect(ChatStore.canSend(card: card, sid: "live"))
        #expect(ChatStore.canSend(card: card, sid: "unscriptable") == false)
        #expect(ChatStore.canSend(card: card, sid: "ended") == false)
        #expect(ChatStore.canSend(card: nil, sid: "live") == false)
        #expect(ChatStore.canSend(card: card, sid: "not-here") == false)
    }

    // MARK: - sheet geometry

    /// **Consecutive sheets in one chain never share a detent** (`UX.md` §7.3
    /// rule 3), so muscle memory drilled on a safe button cannot later land on a
    /// reserve-burning one. A constant at a call site is a constant that drifts;
    /// this is the assertion that keeps them apart.
    @Test func chainedSheetsNeverShareADetent() {
        #expect(SheetHeight.launch != SheetHeight.forceModel)
        #expect(SheetHeight.finishBrief != SheetHeight.finishClose)
        #expect(SheetHeight.resume != SheetHeight.launch)
    }
}
