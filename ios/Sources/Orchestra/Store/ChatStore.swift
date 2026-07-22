import Foundation
import Observation

/// One outgoing message, from the tap to whatever the server said about it.
///
/// **There is no `✓✓ delivered` in this build, and its absence is the design.**
/// `UX.md` §3.3.2 specifies a server-proven receipt: `/api/send` routing through
/// `deliver_text()` and `_proven_in_transcript()`, then `phase` frames on the
/// event stream. Driven against the real server, none of that exists on this
/// path — `send_to_process` types with `tmux send-keys` or osascript and returns
/// `{"ok": …, "message": …}` synchronously; `_proven_in_transcript` lives in
/// `resume.py` and is called only by the resume daemon; and `server._stream`
/// writes `event: state` and nothing else, so there is no intent frame to carry a
/// phase.
///
/// So the strongest honest receipt is **`✓ typed` — the keystrokes were
/// accepted**, which is exactly what `ok: true` has always meant. The client then
/// looks for the message in the next transcript poll and, if it finds it, upgrades
/// to `✓✓ in the transcript`. That look is **positive-only**: not finding it
/// upgrades nothing and warns about nothing, because all five known mismatch
/// paths are false negatives (see `WireText.matches`).
public struct Outgoing: Sendable, Equatable, Identifiable {
    public enum State: Sendable, Equatable {
        /// `◌` — the request is out.
        case sending
        /// `✓` — `rc == 0`. The server's own message rides along
        /// ("typed into Terminal (ttys008)", "sent via tmux").
        case typed(String)
        /// `✓✓` — and it turned up in the conversation.
        case inTranscript(String)
        /// `⚠` — the server refused, and said why. Safe to try again.
        case refused(String)
        /// `⚠` — it may have half-landed. A retry could duplicate it.
        case ambiguous(String)
        /// `⚠` — no answer. Never rendered as "failed".
        case lost(String)
    }

    public let id: UUID
    public let text: String
    public let at: Date
    public var state: State
    /// How many turns the transcript held when this was sent. The sighting scan
    /// only looks past it, so a `continue` sent twice cannot match the first one.
    public var transcriptBaseline: Int

    public init(id: UUID = UUID(), text: String, at: Date = Date(),
                state: State = .sending, transcriptBaseline: Int = 0) {
        self.id = id
        self.text = text
        self.at = at
        self.state = state
        self.transcriptBaseline = transcriptBaseline
    }

    /// Whether the receipt can still improve, which is what keeps the poll fast.
    public var isSettled: Bool {
        switch state {
        case .sending, .typed: false
        case .inTranscript, .refused, .ambiguous, .lost: true
        }
    }
}

/// One session's conversation, and the one place in this app that types at an
/// agent.
///
/// **Chat is the one thing on this app's screens that does not ride the
/// stream.** Transcript turns are not part of the composed view `publish` diffs,
/// so no version bump carries them and no frame could. So this screen polls, and
/// it polls only while it is on screen — `UX.md` §3.3.3's cadence, now with the
/// send-related rungs live: 5 s while a send is unsettled or less than two
/// minutes old, 15 s otherwise, never backgrounded.
///
/// The store is per-screen and short-lived: it is created by the chat view and
/// dies with it, which is what makes "never poll a screen nobody is looking at"
/// structural rather than a rule somebody has to remember. The **outbox** is the
/// one thing that would be worth outliving it, and does not: a send is settled
/// within seconds and the transcript itself is the durable record.
@MainActor
@Observable
public final class ChatStore {
    public private(set) var messages: [ChatMessage] = []
    public private(set) var loading = false
    /// The server's own refusal, verbatim. `/api/chat` answers **200** with
    /// `{"ok": false, "error": …}`, so this is the only place a failure shows up.
    public private(set) var serverError: String?
    public private(set) var transportError: OrchestraError?
    public private(set) var loadedAt: Date?
    /// Messages this screen has sent, oldest first, appended below the
    /// transcript until the transcript catches up with them.
    public private(set) var outbox: [Outgoing] = []
    /// True while a `/api/send` is in flight. The composer's button is disabled
    /// on it — one message at a time into one agent, always.
    public private(set) var sending = false

    public let account: String
    public let sid: String
    public let worktree: String

    private let client: OrchestraClient
    private var poll: Task<Void, Never>?

    /// 15 s at rest. 5 s when something is in flight or was sent in the last two
    /// minutes — `UX.md` §3.3.3, and the reason is the receipt: a queued message
    /// appears in the transcript only when the agent's turn ends.
    private static let restPeriod: TimeInterval = 15
    private static let activePeriod: TimeInterval = 5
    private static let activeWindow: TimeInterval = 120

    public init(client: OrchestraClient, worktree: String, account: String, sid: String) {
        self.client = client
        self.worktree = worktree
        self.account = account
        self.sid = sid
    }

    public func start() {
        guard poll == nil else { return }
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.load()
                let period = self?.pollPeriod ?? Self.restPeriod
                try? await Task.sleep(nanoseconds: UInt64(period * 1_000_000_000))
            }
        }
    }

    public func stop() {
        poll?.cancel()
        poll = nil
    }

    /// 5 s while something is unsettled or freshly sent, 15 s otherwise.
    var pollPeriod: TimeInterval {
        let now = Date()
        let busy = outbox.contains { !$0.isSettled || now.timeIntervalSince($0.at) < Self.activeWindow }
        return busy ? Self.activePeriod : Self.restPeriod
    }

    public func load() async {
        loading = messages.isEmpty
        do {
            let transcript = try await client.chat(account: account, sid: sid)
            if transcript.ok {
                messages = transcript.numbered
                serverError = nil
                sightOutgoing()
            } else {
                // `unknown account …` is worth its own copy: `server.do_GET`
                // pulls `account` out of the raw path with
                // `re.search(r"account=([^&]+)")` and never percent-decodes it,
                // so an account label with a space or a `+` in it arrives at
                // `read_chat` still encoded and cannot match. No label on this
                // fleet needs escaping, so this is a latent bug rather than an
                // observed one — but it is the reason this string is shown
                // rather than replaced with "no messages".
                serverError = transcript.error ?? "the server refused, without saying why"
            }
            transportError = nil
            loadedAt = Date()
        } catch let error as OrchestraError {
            if case .cancelled = error { return }
            transportError = error
        } catch {
            transportError = ErrnoCause.classify(error)
        }
        loading = false
    }

    // MARK: - Sending

    /// Whether this session can be typed into at all, decided from the board
    /// rather than from anything this screen holds.
    ///
    /// `reachable` is the server's own gate: a tmux pane, or a Terminal/iTerm2
    /// tty. A Cursor or VS Code terminal cannot be scripted and an ended session
    /// has no terminal — in both cases the composer is **replaced** by a read-only
    /// notice rather than disabled, because a greyed-out field says "you can
    /// nearly reply" (`UX.md` §3.3).
    /// `nonisolated` because it is a pure function of two values and reads
    /// nothing on this actor — the view calls it during layout and a test drives
    /// it with literals.
    public nonisolated static func canSend(card: Worktree?, sid: String) -> Bool {
        guard let card, let session = card.sessions.first(where: { $0.sid == sid })
        else { return false }
        return card.isReachable(session)
    }

    /// Send `text` to this session's own terminal.
    ///
    /// **No confirmation dialog, ever** — it is the primary job of the app and a
    /// dialog here kills it (`UX.md` §3.3.2). The safety is elsewhere and it is
    /// server-side: the message is addressed by `(account, sid)` and
    /// `identity.resolve` re-resolves it to a process *at the instant it types*,
    /// refusing outright if the session moved, changed account, or lost its
    /// terminal in between.
    ///
    /// The one client-side guard is the in-flight flag: one message at a time.
    @discardableResult
    public func send(_ raw: String) async -> Outgoing.State? {
        let text = WireText.collapsed(raw)
        guard !text.isEmpty, !sending else { return nil }
        sending = true
        defer { sending = false }

        var item = Outgoing(text: text, transcriptBaseline: messages.count)
        outbox.append(item)

        let reply: SendReply
        do {
            reply = try await client.send(account: account, sid: sid,
                                          worktree: worktree, text: text)
        } catch let error as OrchestraError {
            if case .cancelled = error {
                // The screen went away mid-request. The keystrokes may well have
                // landed; saying "cancelled" would imply they did not.
                item.state = .lost(Actuation.indeterminateCopy(for: .send))
            } else {
                item.state = .lost(error.headline + " — "
                                   + Actuation.indeterminateCopy(for: .send))
            }
            update(item)
            return item.state
        } catch {
            item.state = .lost(Actuation.indeterminateCopy(for: .send))
            update(item)
            return item.state
        }

        switch Actuation.outcome(ofSend: reply) {
        case .succeeded:
            item.state = .typed(reply.text)
            // Look for it straight away: on a Terminal send the CLI writes the
            // user turn to the transcript within a second or two.
            update(item)
            await load()
            return outbox.last?.state
        case .refused:
            item.state = .refused(reply.text)
        case .ambiguous(let why):
            item.state = .ambiguous(reply.text + " — " + why)
        case .indeterminate:
            item.state = .lost(Actuation.indeterminateCopy(for: .send))
        }
        update(item)
        return item.state
    }

    private func update(_ item: Outgoing) {
        guard let index = outbox.firstIndex(where: { $0.id == item.id }) else { return }
        outbox[index] = item
    }

    /// Upgrade `✓ typed` to `✓✓ in the transcript` where the turn actually turned
    /// up, then hand the bubble over to the transcript.
    ///
    /// Positive-only: nothing is ever downgraded or warned about here.
    private func sightOutgoing() {
        for index in outbox.indices {
            guard case .typed = outbox[index].state else { continue }
            let sent = outbox[index].text
            let baseline = outbox[index].transcriptBaseline
            let seen = messages.contains { message in
                message.isMine && message.index >= baseline
                    && WireText.matches(sent: sent, turn: message.text)
            }
            if seen { sightedTexts.insert(WireText.collapsed(sent)) }
        }
        // A sighted message IS in the transcript now, and the transcript renders
        // it with the server's own `_clean`ing. Keeping the outbox bubble too
        // would show one message twice, so the bubble is dropped and the
        // transcript turn carries the receipt instead — see `wasSentFromHere`.
        outbox.removeAll { item in
            if case .typed = item.state {
                return sightedTexts.contains(WireText.collapsed(item.text))
            }
            return false
        }
    }

    /// Sent from this phone, in this sitting, and seen to arrive.
    ///
    /// **Stored as text rather than as a transcript index on purpose.** The
    /// server returns a 40-turn window with no ids, so a turn's index shifts down
    /// every time the agent speaks; an index-keyed receipt would slide onto
    /// somebody else's message.
    private var sightedTexts: Set<String> = []

    /// Whether this transcript turn is one this screen typed and watched arrive.
    /// The `✓✓` lives here, on the real turn, and never on a guess.
    public func wasSentFromHere(_ message: ChatMessage) -> Bool {
        guard message.isMine else { return false }
        return sightedTexts.contains { WireText.matches(sent: $0, turn: message.text) }
    }

    /// Drop a settled failure the user has read.
    public func dismiss(_ id: UUID) {
        outbox.removeAll { $0.id == id && $0.isSettled }
    }
}
