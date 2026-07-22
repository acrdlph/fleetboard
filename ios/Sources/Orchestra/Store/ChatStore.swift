import Foundation
import Observation

/// One session's conversation.
///
/// **Chat is the one thing on this app's screens that does not ride the
/// stream.** Transcript turns are not part of the composed view `publish`
/// diffs, so no version bump carries them and no frame could. So this screen
/// polls, and it polls only while it is on screen — `UX.md` §3.3.3's cadence,
/// with the send-related rungs dropped because nothing here sends yet.
///
/// The store is per-screen and short-lived: it is created by the chat view and
/// dies with it, which is what makes "never poll a screen nobody is looking at"
/// structural rather than a rule somebody has to remember.
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

    public let account: String
    public let sid: String

    private let client: OrchestraClient
    private var poll: Task<Void, Never>?

    /// 15 s. The desktop's chat drawer polls at 5 s, but it is also the only way
    /// the desktop learns anything; here the board beside it is streaming, and
    /// nothing on this screen can be acted on yet.
    private static let period: TimeInterval = 15

    public init(client: OrchestraClient, account: String, sid: String) {
        self.client = client
        self.account = account
        self.sid = sid
    }

    public func start() {
        guard poll == nil else { return }
        poll = Task { [weak self] in
            while !Task.isCancelled {
                await self?.load()
                try? await Task.sleep(nanoseconds: UInt64(Self.period * 1_000_000_000))
            }
        }
    }

    public func stop() {
        poll?.cancel()
        poll = nil
    }

    public func load() async {
        loading = messages.isEmpty
        do {
            let transcript = try await client.chat(account: account, sid: sid)
            if transcript.ok {
                messages = transcript.numbered
                serverError = nil
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
}
