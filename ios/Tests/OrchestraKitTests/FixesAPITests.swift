import Foundation
import Testing
@testable import OrchestraKit

/// The transmit path — pinned for the first time.
///
/// `Endpoint.urlRequest` is a pure function whose every property the comments
/// call load-bearing, yet nothing tested any of them: not the `Content-Type`
/// that is the CSRF guard, not the `Authorization` gate, not the per-route
/// deadline, and — the one that shipped a HIGH bug — not the relationship
/// between a route's deadline and the session's resource cap. These pin all of
/// it, plus the two guards added here: the session resource timeout no longer
/// truncates a long `finish`, and every mutation carries a fresh
/// `Idempotency-Key`.
struct EndpointRequestTests {

    static let base = URL(string: "http://127.0.0.1:8787")!

    /// Every endpoint the client can build, reads and mutations alike, so a
    /// fleet-wide invariant can iterate the whole surface.
    static func all() throws -> [(name: String, endpoint: Endpoint)] {
        [
            ("health", .health),
            ("state", .state),
            ("events", .events),
            ("topology", .topology),
            ("limits", .limits),
            ("pushStatus", .pushStatus),
            ("chat", .chat(account: "a", sid: "s")),
            ("dispatchStatus", .dispatchStatus(job: "j")),
            ("pair", try .pair(code: "CODE", label: "iPhone", platform: "ios")),
            ("send", try .send(account: "a", sid: "s", worktree: "w", text: "hi")),
            ("dispatch", try .dispatch(mission: "m", worktree: nil, account: nil,
                                       model: "opus", effort: "high", forceModel: false)),
            ("finish", try .finish(worktree: "w")),
            ("resumeSchedule", try .resumeSchedule(worktree: "w", sid: "s", account: "a",
                                                   delayS: 60, resetsAt: nil, dueAt: nil)),
            ("resumeCancel", try .resumeCancel(worktree: "w", sid: "s")),
            ("reply", try .reply(sid: "s", worktree: "w", text: "ok")),
            ("registerPush", try .registerPush(token: "t", environment: "production",
                                               tzOffsetMin: 0, appVersion: "1", settings: nil)),
            ("pushSettings", try .pushSettings(body: ["quiet": true])),
            ("pushMute", try .pushMute(minutes: 30)),
            ("pushTest", .pushTest()),
        ]
    }

    /// Every route the client can build, exactly the mutations and no reads.
    static func mutations() throws -> [(name: String, endpoint: Endpoint)] {
        try all().filter { $0.endpoint.method == .post && $0.name != "pair" }
    }

    // MARK: - F1 / F3: the timeout invariant that would have caught the HIGH bug

    /// **The fleet-wide invariant the finding says a ten-line test would have
    /// caught.** `timeoutIntervalForResource` is session-level and has no
    /// per-request override, so any route whose own deadline exceeds it is
    /// silently truncated. At the old cap of 20 s, `finish` (120 s) and `send`
    /// (25 s) were aborted mid-mutation and misread as `.macUnreachable`.
    @Test func everyRouteFitsInsideTheSessionResourceCap() throws {
        for (name, endpoint) in try Self.all() {
            #expect(endpoint.timeout <= OrchestraClient.sessionResourceTimeout,
                    "\(name) asks for \(endpoint.timeout)s, over the \(OrchestraClient.sessionResourceTimeout)s session cap — it would be truncated")
        }
    }

    /// `finish` keeps its full 120 s budget and it fits — the exact case the
    /// 20 s cap broke. 120 s is measured off the server's own worst case
    /// (`git fetch` at 30 s + merge-base + status + process scan + osascript).
    @Test func finishGetsItsFullBudgetAndItFits() throws {
        let finish = try Endpoint.finish(worktree: "w")
        #expect(finish.timeout == 120)
        #expect(OrchestraClient.sessionResourceTimeout >= 120)
        // The regression it guards against: the old value would have cut it off.
        #expect(20 < finish.timeout)
    }

    /// The per-route deadline lands on the built request verbatim.
    @Test func theRouteDeadlineIsTheRequestTimeout() throws {
        for (name, endpoint) in try Self.all() {
            let req = try endpoint.urlRequest(base: Self.base, token: "tok")
            #expect(req.timeoutInterval == endpoint.timeout, "\(name)")
        }
    }

    // MARK: - F3: request-construction invariants

    /// Every mutation is a POST with a JSON body and `Content-Type:
    /// application/json` — the CSRF guard, without which the server refuses with
    /// **415 `content_type_required`** before a handler runs.
    @Test func everyMutationDeclaresJSONContentType() throws {
        for (name, endpoint) in try Self.mutations() {
            #expect(endpoint.method == .post, "\(name)")
            #expect(endpoint.body != nil, "\(name) must carry a body")
            let req = try endpoint.urlRequest(base: Self.base, token: "tok")
            #expect(req.value(forHTTPHeaderField: "Content-Type") == "application/json",
                    "\(name)")
        }
    }

    /// `Authorization: Bearer …` is present exactly when the route requires a
    /// token and one is available — and NEVER on the two exempt routes, even if
    /// a token is handed in.
    @Test func bearerIsPresentExactlyWhenRequiredAndNeverOnExemptRoutes() throws {
        for (name, endpoint) in try Self.all() {
            let withToken = try endpoint.urlRequest(base: Self.base, token: "tok")
            let header = withToken.value(forHTTPHeaderField: "Authorization")
            if endpoint.requiresToken {
                #expect(header == "Bearer tok", "\(name) should carry the bearer")
            } else {
                #expect(header == nil, "\(name) is exempt and must not carry a token")
            }
        }
        // health and pair are the exempt pair, by name.
        #expect(Endpoint.health.requiresToken == false)
        #expect(try Endpoint.pair(code: "C", label: "L", platform: "ios").requiresToken == false)
    }

    /// A required-token route with no token builds without an `Authorization`
    /// header rather than an empty one — the empty-string guard in `urlRequest`.
    @Test func aMissingTokenLeavesNoAuthorizationHeader() throws {
        let req = try Endpoint.finish(worktree: "w").urlRequest(base: Self.base, token: nil)
        #expect(req.value(forHTTPHeaderField: "Authorization") == nil)
        let empty = try Endpoint.finish(worktree: "w").urlRequest(base: Self.base, token: "")
        #expect(empty.value(forHTTPHeaderField: "Authorization") == nil)
    }

    /// The board is never cacheable, on the request as well as the session.
    @Test func everyRequestForbidsTheCache() throws {
        for (name, endpoint) in try Self.all() {
            let req = try endpoint.urlRequest(base: Self.base, token: "tok")
            #expect(req.cachePolicy == .reloadIgnoringLocalAndRemoteCacheData, "\(name)")
            #expect(req.value(forHTTPHeaderField: "User-Agent") == "orchestra-ios", "\(name)")
        }
    }

    // MARK: - F3: the wire-idempotency contract (client half)

    /// **Every mutation carries a fresh `Idempotency-Key` and its issued-at.**
    /// This is the client half of the contract the server ships: a retry of the
    /// same request replays the stored result rather than launching a second
    /// agent (API.md §4). A read carries neither.
    @Test func everyMutationCarriesAnIdempotencyKeyAndReadsDoNot() throws {
        for (name, endpoint) in try Self.mutations() {
            let idem = try #require(endpoint.idempotency, "\(name) must have an idempotency identity")
            #expect(UUID(uuidString: idem.key) != nil, "\(name) key must be a UUID")
            let req = try endpoint.urlRequest(base: Self.base, token: "tok")
            #expect(req.value(forHTTPHeaderField: "Idempotency-Key") == idem.key, "\(name)")
            let issuedAt = req.value(forHTTPHeaderField: "Idempotency-Issued-At")
            #expect(issuedAt != nil, "\(name) must send Idempotency-Issued-At")
            // A float epoch, dot-separated, parseable back to the minted instant.
            let parsed = try #require(issuedAt.flatMap(Double.init), "\(name) issued-at must parse")
            #expect(abs(parsed - idem.issuedAt) < 0.01, "\(name)")
        }
    }

    /// Reads and `pair` carry no idempotency header at all — a key on a safe GET
    /// would look like a guarantee and be none, and pairing's code is single-use.
    @Test func readsAndPairCarryNoIdempotencyKey() throws {
        let reads: [Endpoint] = [.health, .state, .events, .topology, .limits, .pushStatus,
                                 .chat(account: "a", sid: "s"), .dispatchStatus(job: "j"),
                                 try .pair(code: "C", label: "L", platform: "ios")]
        for endpoint in reads {
            #expect(endpoint.idempotency == nil)
            let req = try endpoint.urlRequest(base: Self.base, token: "tok")
            #expect(req.value(forHTTPHeaderField: "Idempotency-Key") == nil)
            #expect(req.value(forHTTPHeaderField: "Idempotency-Issued-At") == nil)
        }
    }

    /// **Stable across a `URLSession` retry.** The key is minted once, on the
    /// value; building the request twice from the same `Endpoint` — which is
    /// what a `URLSession`-level resend does — sends the SAME key, so the server
    /// dedupes rather than double-executes. Never regenerate on retry (§4.1).
    @Test func theKeyIsStableAcrossRepeatedRequestBuilds() throws {
        let finish = try Endpoint.finish(worktree: "w")
        let first = try finish.urlRequest(base: Self.base, token: "tok")
        let second = try finish.urlRequest(base: Self.base, token: "tok")
        #expect(first.value(forHTTPHeaderField: "Idempotency-Key")
                == second.value(forHTTPHeaderField: "Idempotency-Key"))
        #expect(first.value(forHTTPHeaderField: "Idempotency-Key") != nil)
    }

    /// **A fresh key per user action.** Two separate builds of the same route are
    /// two distinct intents and must not collide — a shared key would make the
    /// second tap replay the first's result (a real hazard for `pushTest`, whose
    /// second press must fire a second notification, not replay the first).
    @Test func eachUserActionMintsANewKey() throws {
        let a = try Endpoint.dispatch(mission: "m", worktree: nil, account: nil,
                                      model: "opus", effort: "high", forceModel: false)
        let b = try Endpoint.dispatch(mission: "m", worktree: nil, account: nil,
                                      model: "opus", effort: "high", forceModel: false)
        #expect(a.idempotency?.key != b.idempotency?.key)
        #expect(Endpoint.pushTest().idempotency?.key != Endpoint.pushTest().idempotency?.key)
    }
}

/// F2: the SSE accumulation points now refuse to grow without bound.
///
/// A wedged peer that writes bytes with no newline, or `data:` lines with no
/// dispatching blank line, used to grow the phone's memory until iOS jetsammed
/// the app. Both accumulation points now cap at ~100× the largest legitimate
/// frame and drop the garbage, and — critically — recover on the next frame.
struct SSEBufferCapTests {

    /// An overlong unterminated line is dropped, not accumulated, and the
    /// stream recovers: a real frame arriving after it parses normally.
    @Test func anOverlongLineIsDroppedAndTheStreamRecovers() {
        var splitter = SSELineSplitter()
        // More than the per-line cap, with NO terminator: the old code held all
        // of it; the new code discards past the cap.
        let flood = Data(repeating: 0x41, count: SSELineSplitter.maxLineBytes + 4096)
        #expect(splitter.feed(flood).isEmpty)
        // The terminator that ends the discarded line must NOT surface as a
        // spurious blank line (that would be a spurious dispatch), and the real
        // frame after it must parse exactly.
        let out = splitter.feed(Data("\ndata: real\n\n".utf8))
        #expect(out == ["data: real", ""])
    }

    /// The dropped bytes and the good frame can even arrive split at an awkward
    /// boundary; the splitter still recovers to a clean frame.
    @Test func theSplitterRecoversAcrossChunkBoundaries() {
        var splitter = SSELineSplitter()
        _ = splitter.feed(Data(repeating: 0x42, count: SSELineSplitter.maxLineBytes + 1))
        _ = splitter.feed(Data(repeating: 0x42, count: 10))     // still discarding
        var lines = splitter.feed(Data("\nid: 7".utf8))          // terminator, then a new line begins
        lines += splitter.feed(Data("\ndata: x\n\n".utf8))
        var decoder = SSEDecoder()
        var events: [SSEEvent] = []
        for line in lines { if case .event(let e)? = decoder.feed(line) { events.append(e) } }
        #expect(events.count == 1)
        #expect(events.first?.id == "7")
        #expect(events.first?.data == "x")
    }

    /// An event whose accumulated `data` blows the size cap is dropped whole —
    /// not delivered truncated, which the applier could only fail to decode —
    /// and the decoder recovers on the next event.
    @Test func anOversizedEventIsDroppedAndTheDecoderRecovers() {
        var decoder = SSEDecoder(lastEventID: "5")
        let huge = String(repeating: "a", count: SSEDecoder.maxEventBytes + 16)
        #expect(decoder.feed("data: \(huge)") == nil)   // accumulating (over cap)
        #expect(decoder.feed("") == nil, "the oversized event is dropped, not dispatched")
        // The cursor from before is untouched, and the next real event lands.
        #expect(decoder.feed("id: 6") == nil)
        #expect(decoder.feed("data: ok") == nil)
        guard case .event(let e)? = decoder.feed("") else { Issue.record("no recovery event"); return }
        #expect(e.data == "ok")
        #expect(e.id == "6")
    }

    /// The cap is generous: a real ~38 KB snapshot data line is nowhere near it
    /// and passes through untouched.
    @Test func aLegitimateSnapshotLineIsWellUnderTheCap() throws {
        let body = try DecodeTests.fixture("snapshot-frame")
        #expect(body.count < SSEDecoder.maxEventBytes)
        let json = try #require(String(data: body, encoding: .utf8))
        var decoder = SSEDecoder()
        _ = decoder.feed("data: \(json.replacingOccurrences(of: "\n", with: ""))")
        guard case .event(let e)? = decoder.feed("") else { Issue.record("no event"); return }
        #expect(!e.data.isEmpty)
    }
}
