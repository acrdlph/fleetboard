import Foundation

/// What a live SSE connection produces before it is parsed.
enum SSEChunk: Sendable {
    case head(status: Int)
    case body(Data)
}

/// The `URLSessionDataDelegate` half of the stream.
///
/// **Delegate, not `URLSession.bytes(for:)`** — and the reason is measured, not
/// stylistic. `AsyncBytes.lines` drops the empty line that dispatches an SSE
/// event (see `SSELineSplitter`), and iterating `AsyncBytes` a byte at a time to
/// get the semantics back costs 349 ms per 38 KB frame. A delegate hands over
/// whole `Data` chunks, which is what the socket produced in the first place.
///
/// Every stored property is an immutable `Sendable` value, so this is `Sendable`
/// with nothing suppressed. The callbacks arrive on the session's own delegate
/// queue and do exactly one thing each: hand the value to a continuation, which
/// is itself thread-safe.
final class SSEBridge: NSObject, URLSessionDataDelegate, Sendable {
    private let out: AsyncThrowingStream<SSEChunk, any Error>.Continuation

    init(out: AsyncThrowingStream<SSEChunk, any Error>.Continuation) {
        self.out = out
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        out.yield(.head(status: (response as? HTTPURLResponse)?.statusCode ?? 0))
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        out.yield(.body(data))
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: (any Error)?) {
        if let error {
            out.finish(throwing: error)
        } else {
            // A clean close. orchestra pins HTTP/1.0 and sends
            // `Connection: close`, so the close IS the framing — the stream
            // ending is a normal event, not a failure.
            out.finish()
        }
    }
}

extension OrchestraClient {
    /// Open `GET /api/events` and yield its tokens until the socket dies.
    ///
    /// The sequence finishes normally when the server closes the stream and
    /// throws an `OrchestraError` for everything else — including the two
    /// refusals orchestra answers *before* a byte of stream is written, both
    /// 503:
    ///
    /// * **no observer.** `--demo` runs no sweep, so there is no version to
    ///   stream and holding the socket open would be a promise the process
    ///   cannot keep.
    /// * **the subscriber cap.** `sse_max_subscribers = 32`.
    ///
    /// Both must be distinguishable from a network failure, because the right
    /// answer to them is "stop reconnecting and poll" and the right answer to a
    /// network failure is "reconnect". `EventSource` reports both as an
    /// `onerror` with no status, which is why `stream.js` has to infer it from
    /// `readyState === 2`. Reading the status directly is most of the reason
    /// this is our own ~90 lines rather than a dependency.
    ///
    /// `lastEventID` is the SSE reconnect cursor and is the WHOLE resync path:
    /// `delta_since` answers a known cursor with a delta, and an unknown, too
    /// old, or ahead-of-the-server cursor with a full snapshot. Passing nil asks
    /// for a snapshot.
    public func openEvents(lastEventID: String?) -> AsyncThrowingStream<SSEToken, any Error> {
        let profile = self.profile
        let tokens = self.tokens
        let configuration = Self.streamConfiguration()
        return AsyncThrowingStream(bufferingPolicy: .unbounded) { continuation in
            let task = Task {
                do {
                    guard let profile, let base = profile.baseURL else {
                        throw OrchestraError.unauthorized(nil)
                    }
                    guard let token = await tokens.bearerToken(), !token.isEmpty else {
                        throw OrchestraError.unauthorized(nil)
                    }
                    var request = try Endpoint.events.urlRequest(base: base, token: token)
                    request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    if let lastEventID, !lastEventID.isEmpty {
                        request.setValue(lastEventID, forHTTPHeaderField: "Last-Event-ID")
                    }

                    // A session PER CONNECTION, because a `URLSession`'s delegate
                    // is fixed at construction. It is invalidated on every exit
                    // path below — a session holds its delegate strongly until
                    // it is, and leaking one per reconnect would leak a socket
                    // with it.
                    let (chunks, bridge) = Self.connect(request, configuration: configuration)
                    // **`invalidateAndCancel`, never `finishTasksAndInvalidate`.**
                    // The latter waits for outstanding tasks to finish, and a
                    // stream never finishes — so backgrounding the app cancelled
                    // this Swift Task, ran this `defer`, and left the socket
                    // open. Measured: background the app, and the Mac still
                    // showed one ESTABLISHED connection; foreground it and there
                    // were TWO, one of them owned by nobody. Each leak burns one
                    // of the server's 32 subscriber slots until the TCP stack
                    // notices, which is exactly the failure `stop()` exists to
                    // prevent.
                    defer { bridge.invalidateAndCancel() }

                    var decoder = SSEDecoder(lastEventID: lastEventID)
                    var splitter = SSELineSplitter()
                    var sawHead = false
                    for try await chunk in chunks {
                        switch chunk {
                        case .head(let status):
                            sawHead = true
                            guard status == 200 else {
                                // orchestra's refusal body is `send_error`'s HTML
                                // page, not a JSON refusal, so there is nothing
                                // to decode — the STATUS is the message, and 503
                                // has its own handling in `FleetStore`.
                                switch status {
                                case 401: throw OrchestraError.unauthorized(nil)
                                case 403: throw OrchestraError.forbidden(nil)
                                default: throw OrchestraError.http(status: status, refusal: nil)
                                }
                            }
                        case .body(let data):
                            for line in splitter.feed(data) {
                                if let token = decoder.feed(line) {
                                    continuation.yield(token)
                                }
                            }
                        }
                    }
                    if !sawHead {
                        throw OrchestraError.decoding("the stream ended before a response")
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish(throwing: OrchestraError.cancelled)
                } catch {
                    continuation.finish(throwing: ErrnoCause.classify(error))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// The stream's own `URLSessionConfiguration`.
    ///
    /// **`timeoutIntervalForRequest` is the most load-bearing number here.** It
    /// is not a deadline on the response; it is the maximum silence between
    /// packets. orchestra writes `: keepalive` only after `sse_keepalive_s` —
    /// 25 s — of a composed view that has not changed, which on a quiet fleet is
    /// the only traffic on the socket. The board's own request session uses a
    /// short request timeout, so a stream sharing it would be torn down by the
    /// phone on every stretch of quiet, reconnected, and torn down again; and
    /// its `timeoutIntervalForResource` is a finite hard cap on a whole
    /// transfer, which a stream is supposed to outlive by hours. This is why the
    /// stream carries its OWN config (below) rather than any number from
    /// `OrchestraClient` — the two deadlines mean opposite things here.
    static func streamConfiguration() -> URLSessionConfiguration {
        let config = URLSessionConfiguration.ephemeral
        config.urlCache = nil
        config.requestCachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        config.waitsForConnectivity = false
        config.timeoutIntervalForRequest = 70          // two keepalives plus slack
        config.timeoutIntervalForResource = .infinity  // a stream has no end to wait for
        config.httpAdditionalHeaders = ["Accept": "text/event-stream"]
        return config
    }

    private static func connect(_ request: URLRequest, configuration: URLSessionConfiguration)
        -> (AsyncThrowingStream<SSEChunk, any Error>, URLSession) {
        var continuation: AsyncThrowingStream<SSEChunk, any Error>.Continuation!
        // .unbounded is deliberate here: these are RAW, ORDERED network chunks,
        // and dropping one (what a bounded policy does on overflow) would splice
        // a partial frame onto the next and corrupt every frame after it — worse
        // than any gap. The genuine jetsam vector — a newline-less flood — is
        // bounded upstream in SSELineSplitter/SSEDecoder (4 MB, drop-and-recover
        // on a frame boundary). This buffer only grows if the CONSUMER stalls,
        // and the producer rate is bounded by the server's frame cadence (a
        // handful/second), so it is not an accumulation risk in practice.
        let stream = AsyncThrowingStream<SSEChunk, any Error>(bufferingPolicy: .unbounded) {
            continuation = $0
        }
        let queue = OperationQueue()
        queue.maxConcurrentOperationCount = 1       // serial: chunks must stay in order
        let session = URLSession(configuration: configuration,
                                 delegate: SSEBridge(out: continuation),
                                 delegateQueue: queue)
        session.dataTask(with: request).resume()
        return (stream, session)
    }
}
