import Foundation

/// `GET /api/chat?account=&sid=` — the last 40 turns of one session.
///
/// **It answers 200 even when it failed.** `chat.read_chat` returns
/// `{"ok": false, "error": "unknown account x"}` and the handler serialises it
/// with the same 200 as a success, so a client that trusted the status code
/// renders an empty conversation for a real, nameable failure. `ok` is the
/// status here, not the HTTP line.
public struct ChatTranscript: Sendable, Equatable, Decodable {
    public let ok: Bool
    public let error: String?
    public let messages: [ChatMessage]

    public init(ok: Bool, error: String?, messages: [ChatMessage]) {
        self.ok = ok
        self.error = error
        self.messages = messages
    }

    enum CodingKeys: String, CodingKey { case ok, error, messages }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        error = try c.decodeIfPresent(String.self, forKey: .error)
        messages = try c.decodeIfPresent([ChatMessage].self, forKey: .messages) ?? []
    }
}

/// One turn.
///
/// Three facts about the text, all of them the server's doing and all of them
/// visible on screen if they are not designed for:
///
/// 1. **Newlines are destroyed** (`transcripts._clean`). What arrives is one
///    run-on paragraph, so there is no markdown structure left to render and
///    building a renderer for it would be inventing structure the wire threw
///    away. `UX.md` §3.3.3 says so outright.
/// 2. **It is truncated to 900 characters with a trailing `…`.** A bubble that
///    ends in an ellipsis gets a footnote saying the SERVER cut it, so the
///    reader does not think the agent stopped mid-sentence.
/// 3. **`/`-prefixed and `Caveat:`-prefixed user text is filtered out entirely**
///    by `_real_prompt`, so `/compact` and `/model opus` can never appear. The
///    transcript is not a complete record and must not be presented as one.
public struct ChatMessage: Sendable, Equatable, Decodable, Identifiable {
    public enum Role: String, Sendable, Equatable {
        /// The server's spelling. Not `user`.
        case you
        case agent
        case other
    }

    public let role: Role
    public let text: String
    /// ISO-8601 with a `Z`, straight off the transcript entry — and **nullable**,
    /// because it is `e.get("timestamp")` with no fallback.
    public let ts: String?
    /// Position in the returned window. The server ships no ids, so identity is
    /// positional and is assigned at decode.
    public private(set) var index: Int = 0

    public var id: Int { index }

    public init(role: Role, text: String, ts: String?, index: Int = 0) {
        self.role = role
        self.text = text
        self.ts = ts
        self.index = index
    }

    enum CodingKeys: String, CodingKey { case role, text, ts }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = try c.decodeIfPresent(String.self, forKey: .role) ?? ""
        role = Role(rawValue: raw) ?? .other
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        ts = try c.decodeIfPresent(String.self, forKey: .ts)
        // `index` is stamped by `ChatTranscript.numbered`, not by the decoder:
        // a decoder has no idea where in the array it is.
    }

    public var isMine: Bool { role == .you }

    /// True when the SERVER cut this at 900 characters.
    public var serverTruncated: Bool { TextTruncation.alreadyTruncated(text) }

    public var timestamp: Date? { ts.flatMap(ChatMessage.parse) }

    /// `2026-07-22T16:21:22.219Z`. `ISO8601DateFormatter` needs to be told about
    /// the fractional seconds or it returns nil for every message.
    static func parse(_ raw: String) -> Date? {
        let shapes: [ISO8601DateFormatter.Options] = [
            [.withInternetDateTime, .withFractionalSeconds],
            [.withInternetDateTime],
        ]
        for options in shapes {
            let f = ISO8601DateFormatter()
            f.formatOptions = options
            if let d = f.date(from: raw) { return d }
        }
        return nil
    }
}

extension ChatTranscript {
    /// The messages with their positions stamped, which is what makes them
    /// `Identifiable` for a `ForEach` and scroll-targetable by index.
    public var numbered: [ChatMessage] {
        messages.enumerated().map { i, m in
            ChatMessage(role: m.role, text: m.text, ts: m.ts, index: i)
        }
    }
}
