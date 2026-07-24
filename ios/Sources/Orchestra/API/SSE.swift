import Foundation

/// One dispatched `text/event-stream` event.
///
/// `id` is the SSE *last event ID buffer*, not "the id line of this event" —
/// the two differ, and the difference matters here. The spec keeps the id
/// across events on a connection: a frame with no `id:` inherits the previous
/// one. orchestra writes an `id:` on every state frame (`server._write_frame`),
/// so today they always agree; modelling it correctly costs one line and means
/// a keepalive between frames can never blank the reconnect cursor.
public struct SSEEvent: Sendable, Equatable {
    /// `event:` — `"message"` when the stream omits it. orchestra always sends
    /// `state`.
    public let name: String
    /// The `data:` lines, joined with `\n` and with the trailing one removed.
    public let data: String
    /// The connection's last-event-ID buffer at dispatch time.
    public let id: String?

    public init(name: String, data: String, id: String?) {
        self.name = name
        self.data = data
        self.id = id
    }
}

/// What one line of the stream produced.
///
/// A comment is surfaced rather than swallowed **because it is the liveness
/// signal**. orchestra writes `: keepalive` after `sse_keepalive_s` (25 s) of a
/// composed view that has not changed — which on a quiet fleet is the only
/// traffic on the socket for minutes. A client that only counted `event:` lines
/// as "the link is alive" would declare a perfectly healthy idle board dead,
/// which is `IOS-APP.md` §5.5's exact warning: liveness and recency are two
/// signals and conflating them makes an idle fleet read stale every 25 seconds.
public enum SSEToken: Sendable, Equatable {
    case comment(String)
    case event(SSEEvent)
    /// `retry:` — the server's reconnect hint, in milliseconds. orchestra never
    /// sends one; parsed rather than ignored so an unknown field cannot be
    /// mistaken for data.
    case retry(Int)
}

/// Bytes in, lines out — **including the empty ones**, which is the entire
/// reason this type exists.
///
/// The obvious way to write the transport is `URLSession.AsyncBytes.lines`, and
/// it is wrong here in a way that is invisible until you run it against the real
/// server. `AsyncLineSequence` **drops empty lines**: its iterator only yields
/// when its accumulated buffer is non-empty. In SSE the empty line is not
/// whitespace, it is the **dispatch instruction** — `data: {…}` followed by a
/// blank line is what makes an event happen — so a client built on `.lines`
/// holds a healthy, established socket, receives every byte of every frame, and
/// never dispatches a single event. Measured against a live `GET /api/events`:
///
/// ```
/// .lines over the first 3 lines — blank line delivered? false
/// ```
///
/// The board looked exactly like a board that could not connect. It was in fact
/// a board that had connected perfectly and could not be told when a frame had
/// ended.
///
/// Byte-at-a-time over `AsyncBytes` fixes the semantics and costs **349 ms for
/// one 38 KB snapshot frame**, also measured — an async `next()` per byte. So
/// the transport hands this whole `Data` chunks off a `URLSessionDataDelegate`
/// and the splitting happens here, synchronously, over bytes already in memory.
///
/// All three SSE terminators are handled (`\n`, `\r\n`, `\r`), because the spec
/// lists three and a `\r` arriving at the end of one chunk with its `\n` at the
/// start of the next is exactly the case a naive splitter turns into a spurious
/// blank line — i.e. into a spurious dispatch.
public struct SSELineSplitter: Sendable {
    private var buffer: [UInt8] = []
    /// A `\r` ended the previous line and its `\n` may still be coming, possibly
    /// in the next chunk.
    private var pendingLF = false
    /// A line has passed `maxLineBytes` with no terminator in sight: its bytes
    /// are being dropped up to the terminator, so the line buffer cannot grow
    /// without bound. The dropped line is NOT emitted — emitting it (even as the
    /// empty string) would be a spurious blank line, i.e. a spurious dispatch.
    private var discarding = false

    /// The largest a single SSE line may reach before it is judged garbage and
    /// dropped. A real state frame arrives on one `data:` line of ~38 KB, so
    /// 4 MB is ~100× the largest legitimate line: no honest frame approaches it,
    /// and a peer that writes bytes without a terminator can no longer grow the
    /// phone's memory until iOS jetsams the app (there is no TLS on this
    /// transport, so any process answering on the port can try).
    static let maxLineBytes = 4 * 1024 * 1024

    public init() {}

    public mutating func feed(_ data: Data) -> [String] {
        var lines: [String] = []
        for byte in data {
            if pendingLF {
                pendingLF = false
                if byte == 0x0A {                  // the LF of a CRLF already spent
                    if discarding { discarding = false }
                    continue
                }
            }
            switch byte {
            case 0x0D:                             // CR — a terminator on its own
                if discarding {
                    discarding = false             // the overlong line ends here
                    buffer.removeAll(keepingCapacity: true)
                } else {
                    lines.append(take())
                }
                pendingLF = true
            case 0x0A:                             // LF
                if discarding {
                    discarding = false
                    buffer.removeAll(keepingCapacity: true)
                } else {
                    lines.append(take())
                }
            default:
                if discarding { continue }
                if buffer.count >= Self.maxLineBytes {
                    // Give up on this line: drop what we have and every further
                    // byte until its terminator, rather than grow forever.
                    discarding = true
                    buffer.removeAll(keepingCapacity: false)
                    continue
                }
                buffer.append(byte)
            }
        }
        return lines
    }

    private mutating func take() -> String {
        defer { buffer.removeAll(keepingCapacity: true) }
        return String(decoding: buffer, as: UTF8.self)
    }
}

/// A line-fed `text/event-stream` parser, and deliberately nothing else.
///
/// **No third-party EventSource.** The format is ~60 lines of state machine, and
/// on this transport the client needs three things a wrapper hides: the raw
/// `Last-Event-ID` it will reconnect with, the comment frames that prove the
/// socket is alive, and the HTTP status of the *initial* response — orchestra
/// answers 503 with a body naming the reason (no sweep running under `--demo`,
/// or the 32-subscriber cap), and a client that cannot read that says "network
/// error" for a server that answered clearly.
///
/// It takes lines, not bytes, so it can be tested with string literals and has
/// no opinion about where the bytes came from. Feeding it the transcript of a
/// real capture is the whole test.
public struct SSEDecoder: Sendable {
    /// The reconnect cursor: what goes in `Last-Event-ID` on the next attempt.
    public private(set) var lastEventID: String?

    private var eventName = ""
    private var data = ""
    /// A running byte count for `data`, so the size check is O(1) per line
    /// rather than an O(n) `data.utf8.count` on every `data:` line (which would
    /// make a many-line frame O(n²)).
    private var dataBytes = 0
    /// The accumulated event passed `maxEventBytes`: stop appending and drop the
    /// whole event on dispatch. A blank line spanning many `data:` lines with no
    /// dispatch is the other unbounded-growth path the splitter's per-line cap
    /// does not close.
    private var overflowed = false

    /// The most an event's joined `data` may accumulate before it is dropped.
    /// Same budget and reasoning as `SSELineSplitter.maxLineBytes`: ~100× the
    /// ~38 KB the server actually writes.
    static let maxEventBytes = 4 * 1024 * 1024

    public init(lastEventID: String? = nil) {
        self.lastEventID = lastEventID
    }

    /// Feed one line, stripped of its terminator. Returns a token when the line
    /// completed one.
    public mutating func feed(_ line: String) -> SSEToken? {
        if line.isEmpty { return dispatch() }
        if line.hasPrefix(":") { return .comment(String(line.dropFirst())) }

        let field: String
        let rawValue: String
        if let colon = line.firstIndex(of: ":") {
            field = String(line[line.startIndex..<colon])
            rawValue = String(line[line.index(after: colon)...])
        } else {
            field = line
            rawValue = ""
        }
        // Exactly ONE leading space is removed. `data:  x` carries " x".
        let value = rawValue.hasPrefix(" ") ? String(rawValue.dropFirst()) : rawValue

        switch field {
        case "event":
            eventName = value
        case "data":
            if !overflowed {
                let add = value.utf8.count + 1        // +1 for the joining "\n"
                if dataBytes + add > Self.maxEventBytes {
                    overflowed = true                 // drop the rest; dispatch will discard
                } else {
                    data += value
                    data += "\n"
                    dataBytes += add
                }
            }
        case "id":
            // The spec ignores an id containing U+0000. orchestra sends an
            // integer, so this is defensive rather than observed.
            if !value.contains("\0") { lastEventID = value }
        case "retry":
            if let ms = Int(value), ms >= 0 { return .retry(ms) }
        default:
            break                       // an unknown field is ignored, per spec
        }
        return nil
    }

    /// A blank line ends an event. An event with no `data:` at all is NOT
    /// dispatched — that is the spec, and it is also what keeps a stray `id:`
    /// line from being delivered as an empty frame.
    private mutating func dispatch() -> SSEToken? {
        defer {
            data = ""
            eventName = ""
            dataBytes = 0
            overflowed = false
        }
        // An event that blew the size cap is garbage: drop it and recover on the
        // next frame rather than deliver a truncated payload the applier can only
        // fail to decode.
        guard !overflowed else { return nil }
        guard !data.isEmpty else { return nil }
        let payload = data.hasSuffix("\n") ? String(data.dropLast()) : data
        return .event(SSEEvent(name: eventName.isEmpty ? "message" : eventName,
                               data: payload,
                               id: lastEventID))
    }
}
