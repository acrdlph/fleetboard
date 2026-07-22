import Foundation

/// **The server offers no idempotency, so this file is what stands in for it —
/// and it says exactly how far that reaches.**
///
/// `UX.md` §7.1 principle 2 makes `Idempotency-Key` *"a precondition for
/// shipping dispatch and finish"* and says both actions must be **disabled**
/// when the server lacks it. That was written against a planned server. The one
/// that exists was driven end to end on 2026-07-22 and there is no key anywhere:
/// `server.do_POST` reads a JSON body, pulls named fields out of it, and calls
/// the module. No header is inspected, no `client_op_id` is stored, `/api/intents`
/// is a 404, and `dispatch._jobs` is an in-memory dict of the last 20 jobs that a
/// restart erases. `POST /api/dispatch` twice with the same body launches **two**
/// agents — the tmux session name embeds `%H%M%S`, so any retry one second later
/// gets a fresh name and a fresh agent.
///
/// Disabling dispatch and finish entirely would ship a phase 3 that cannot act,
/// so this build takes the other honest option: **ship them with the guard the
/// client can actually enforce, and be loud about the two holes it cannot
/// close.**
///
/// | risk | covered here? |
/// |---|---|
/// | the user taps Launch twice | yes — `InFlight` refuses the second, and the button never re-enables (§7.4) |
/// | the app auto-retries a POST | yes — nothing in this app retries a mutation, ever |
/// | a timeout is rendered as failure and the user re-taps | yes — a timeout is `.indeterminate`, and `.indeterminate` never offers a retry |
/// | **URLSession retransmits under us** | **no.** Not app-configurable. A POST whose connection dies before any response byte arrives may be replayed on a fresh connection |
/// | **a second phone, or the desktop board** | **no.** A client-side lock is defeated by two clients, which is the exact case it was written for (§7.1 principle 3) |
///
/// Both open rows are the server's to close and are reported rather than papered
/// over. Sending an `Idempotency-Key` header this server ignores would be the
/// papering: it would look like the guard exists.
public enum Actuation {

    /// What the client is allowed to conclude from how a mutation ended.
    ///
    /// The distinction that matters is not success/failure — it is **"do I know
    /// whether the side effect happened?"** Rendering an unknown as a failure is
    /// the single most dangerous message this app can show, because the brief was
    /// very likely typed (`UX.md` §7.4).
    public enum Outcome: Sendable, Equatable {
        /// The server answered and said it did the thing.
        case succeeded
        /// The server answered and said it did **not** do the thing. Nothing
        /// happened; trying again is safe and is the user's call.
        case refused
        /// The server answered, and its answer does not settle whether the side
        /// effect landed. The tmux send is the real instance of this: `ok` is
        /// `rc1 == 0 and rc2 == 0` over two calls, so a failure of the second
        /// leaves the text **in the composer, un-submitted**.
        case ambiguous(String)
        /// No answer arrived: a timeout, a dropped tunnel, a killed app. The
        /// mutation may have run to completion on the Mac.
        case indeterminate
    }

    /// May the UI offer the user a button that re-sends this?
    ///
    /// Only for a clean refusal. Every other terminal state either needs no
    /// retry or cannot be retried safely, and a disabled-forever button plus a
    /// sentence saying why beats a live button that doubles an agent.
    public static func mayOfferRetry(_ outcome: Outcome) -> Bool {
        if case .refused = outcome { return true }
        return false
    }

    /// The sentence shown when a mutation ends with no answer. It never contains
    /// the word "failed": the request may well have succeeded.
    public static func indeterminateCopy(for action: Kind) -> String {
        switch action {
        case .send:
            "no answer from the Mac. The message may already have been typed — "
            + "check the conversation before sending it again."
        case .dispatch:
            "no answer in 90 seconds. A mission may already be running — open the "
            + "worktree before launching again, because a retry can start a "
            + "SECOND agent in the same worktree."
        case .finish:
            "no answer in 2 minutes. The closeout brief may already have been "
            + "sent — check the session before trying again."
        case .resume:
            "no answer from the Mac. Arming is keyed by worktree and session, so "
            + "arming again replaces rather than duplicates."
        }
    }

    /// The four mutations, as a kind rather than as a payload.
    public enum Kind: Sendable, Equatable, Hashable {
        case send, dispatch, finish, resume
    }

    /// Classify a `/api/send` reply into an outcome.
    ///
    /// The tmux clause is the whole reason this is a function and not `reply.ok`.
    /// `terminal.send_to_process` returns the literal string
    /// `"tmux send-keys failed"` for `rc1 != 0 || rc2 != 0`, and those are two
    /// separate `tmux send-keys` calls: the text, then Enter. Half of that
    /// failing is a message sitting in the agent's composer that a "retry" would
    /// duplicate.
    public static func outcome(ofSend reply: SendReply) -> Outcome {
        if reply.ok { return .succeeded }
        if let message = reply.message, message.contains("tmux send-keys failed") {
            return .ambiguous("tmux reported a failure, and it is two calls — the "
                              + "text, then Enter. The message may be sitting in "
                              + "the agent's composer unsent. Attach and look "
                              + "before sending it again.")
        }
        return .refused
    }

    /// Classify a `/api/finish` reply.
    ///
    /// `mode: nudge` with `ok: false` is the one ambiguous finish: the nudge text
    /// went through `send_to_process`, so it carries the same tmux caveat.
    public static func outcome(ofFinish reply: FinishReply) -> Outcome {
        if reply.ok { return .succeeded }
        if let message = reply.message, message.contains("tmux send-keys failed") {
            return .ambiguous("tmux reported a failure part-way through typing the "
                              + "brief. Some of it may be in the agent's composer. "
                              + "Attach and look before finishing again.")
        }
        return .refused
    }
}

/// The button geometry of `UX.md` §7.3, in one place so it cannot be applied to
/// three sheets out of four.
///
/// Three rules, and all three are consequences of one fact — **a fixed detent
/// means a fixed thumb coordinate**:
///
/// 1. **The safe action is bottom-most.** Putting the irreversible,
///    money-spending action in the fattest part of the thumb arc and the safe
///    action below it inverts the whole model.
/// 2. **≥24 pt of dead space** between any two rows whose consequences differ in
///    kind.
/// 3. **Consecutive sheets in one chain never share a detent**, so muscle memory
///    drilled on a safe button cannot later land on a reserve-burning one.
///
/// Rule 3 is why every `presentationDetents` height in this app comes from
/// `SheetHeight` rather than from a number at the call site.
public enum SheetHeight {
    /// The mission launch confirm.
    public static let launch: CGFloat = 380
    /// The force-model / reserve confirm — deliberately NOT `launch`.
    public static let forceModel: CGFloat = 300
    /// Step one of finish.
    public static let finishBrief: CGFloat = 460
    /// Step two of finish — deliberately NOT `finishBrief`.
    public static let finishClose: CGFloat = 340
    /// The auto-resume sheet.
    public static let resume: CGFloat = 520
}


/// The in-flight lock: one attempt per addressable thing at a time.
///
/// A value type with an explicit clock, so the whole rule can be driven in a test
/// with three literals and no waiting. It is deliberately **not** a `Set<Kind>` —
/// two different worktrees may finish concurrently and two different sessions may
/// be replied to concurrently; what must never overlap is two attempts at the
/// *same* target.
public struct InFlight: Sendable, Equatable {

    /// What a lock is taken on. The address, never a pid — for exactly the reason
    /// every mutation is identity-addressed (ADR 0008): the board re-sorts under
    /// you, so a positional key names a different agent a second later.
    public enum Key: Sendable, Equatable, Hashable {
        case send(sid: String)
        case dispatch
        case finish(worktree: String)
        case resume(worktree: String, sid: String)
    }

    private var held: [Key: Date] = [:]

    /// How long a lock survives without being released. It is a leak guard, not a
    /// timeout: every path in this app releases explicitly, and this only matters
    /// if one of them is ever forgotten. Longer than the longest deadline in the
    /// app (finish, 120 s) so it can never expire under a live request.
    public static let staleAfter: TimeInterval = 180

    public init() {}

    /// Take the lock. `false` means an attempt at this exact target is already
    /// running and the caller must do nothing at all — not queue it, not retry.
    public mutating func begin(_ key: Key, at now: Date) -> Bool {
        if let since = held[key], now.timeIntervalSince(since) < Self.staleAfter {
            return false
        }
        held[key] = now
        return true
    }

    public mutating func end(_ key: Key) {
        held.removeValue(forKey: key)
    }

    public func isBusy(_ key: Key, at now: Date) -> Bool {
        guard let since = held[key] else { return false }
        return now.timeIntervalSince(since) < Self.staleAfter
    }
}
