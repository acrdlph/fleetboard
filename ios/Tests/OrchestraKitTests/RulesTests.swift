import Foundation
import Testing
@testable import OrchestraKit

/// The pure rules: triage, the errno ladder, the pairing ticket, the formatters.
/// No I/O, no clock, no network — every one of these runs in microseconds and
/// none of them can pass by accident.
struct RulesTests {

    // MARK: - Triage

    func card(name: String = "wt", statuses: [SessionStatus],
                     handedTo: [Int: String] = [:], procs: Int = 0) -> Worktree {
        let sessions = statuses.enumerated().map { i, status in
            Session(shortID: "s\(i)", sid: "s\(i)", account: "main", lastWriteAt: 0,
                    cwd: "/x", subdir: nil, branch: "main", model: "", pendingTools: [],
                    pendingWorkflows: 0, pendingBackgroundAgents: 0,
                    pendingBackgroundTools: 0, topic: nil, lastAssistant: nil,
                    lastUser: nil, subagentSaid: nil, subagentsActive: false, pid: nil,
                    pidCertain: false, status: status, turnEnded: nil,
                    limit: status == .limit
                        ? SessionLimit(worst: nil, group: nil, resetsAt: nil) : nil,
                    handedTo: handedTo[i], toolRunning: false, bgShell: false)
        }
        let live = (0..<procs).map {
            LiveProc(pid: Int32(100 + $0), cpu: 0, etime: "01:00", tty: "ttys00",
                     host: "Terminal", account: "main", tmux: nil, reachable: true,
                     subdir: nil)
        }
        return Worktree(name: name, path: "/x/\(name)",
                        git: GitInfo(branch: "main", commit: nil, dirty: 0,
                                     ahead: nil, behind: nil),
                        sessions: sessions, liveProcs: live, availability: .free)
    }

    /// **Every rung of `status.card_availability`, in its order.** The client
    /// splits ONE of them (`attention`) into two; every other rung must return
    /// what the server would have returned, and the test that proves it folds the
    /// split back together and compares.
    @Test func theSectionLadderMatchesTheServersAvailabilityLadder() {
        let cases: [(String, Worktree, BoardSection, Availability)] = [
            ("nothing alive, nothing working",
             card(statuses: [.ended, .ended], procs: 0), .free, .free),
            ("a live terminal but only ended sessions",
             card(statuses: [.ended], procs: 1), .working, .busy),
            ("needs_input beats everything",
             card(statuses: [.working, .needsInput], procs: 1), .needsYou, .attention),
            ("blocked is an attention state too",
             card(statuses: [.blocked], procs: 1), .needsYou, .attention),
            ("parked at the prompt with nothing working — THE SPLIT",
             card(statuses: [.waiting, .ended], procs: 1), .yourTurn, .attention),
            ("parked at the prompt WHILE something works is busy",
             card(statuses: [.waiting, .working], procs: 1), .working, .busy),
            ("limit-stuck with nothing working",
             card(statuses: [.limit], procs: 1), .limited, .waiting),
            ("limit-stuck while something works is busy",
             card(statuses: [.limit, .working], procs: 1), .working, .busy),
        ]
        for (label, card, expectedSection, expectedServer) in cases {
            #expect(Triage.section(for: card) == expectedSection, "\(label)")
            #expect(Triage.serverAvailability(for: Triage.section(for: card))
                    == expectedServer,
                    "\(label): folding the split back must reproduce the server")
        }
    }

    /// `working` beats `has_live` at rung 1 — a card with a working session and
    /// no live proc is NOT free. Getting this backwards would hand a busy
    /// worktree to `dispatch.auto_target`, which METHOD.md §7 names as the
    /// expensive direction.
    @Test func aWorkingSessionWithNoLiveProcIsNotFree() {
        #expect(Triage.section(for: card(statuses: [.working], procs: 0)) != .free)
        #expect(Triage.section(for: card(statuses: [.ended], procs: 0)) == .free)
    }

    /// A handed-off limit session is filtered BEFORE the ladder runs, matching
    /// `observer._attention_statuses` — otherwise a card whose work moved to
    /// another account sits in WAITING ON LIMITS forever.
    @Test func aHandedOffLimitDoesNotDecideTheSection() {
        let handed = card(statuses: [.limit, .ended], handedTo: [0: "account3"], procs: 1)
        #expect(Triage.section(for: handed) != .limited)
        let notHanded = card(statuses: [.limit, .ended], procs: 1)
        #expect(Triage.section(for: notHanded) == .limited)
    }

    @Test func groupsComeBackInSeverityOrderAndEmptyOnesAreOmitted() {
        let cards = [
            card(name: "a", statuses: [.ended], procs: 0),               // free
            card(name: "b", statuses: [.needsInput], procs: 1),          // needs you
            card(name: "c", statuses: [.working], procs: 1),             // working
        ]
        let groups = Triage.groups(cards)
        #expect(groups.map(\.section) == [.needsYou, .working, .free])
        #expect(!groups.contains { $0.section == .limited })
    }

    /// The server's `order` is preserved INSIDE a section. The client never
    /// sorts; re-deriving the order on a phone is how two clients start
    /// disagreeing about which agent is worst.
    @Test func orderIsPreservedWithinASection() {
        let cards = ["z", "a", "m"].map { card(name: $0, statuses: [.ended]) }
        let group = try! #require(Triage.groups(cards).first)
        #expect(group.cards.map(\.name) == ["z", "a", "m"])
    }

    @Test func theHeadlineSaysGoodNewsAsGoodNews() {
        #expect(Triage.headline([card(statuses: [.needsInput], procs: 1)]).text
                == "1 needs you")
        #expect(Triage.headline([card(name: "a", statuses: [.needsInput], procs: 1),
                                 card(name: "b", statuses: [.blocked], procs: 1)]).text
                == "2 need you")
        #expect(Triage.headline([card(statuses: [.working], procs: 1)]).text == "all clear")
        #expect(Triage.headline([card(statuses: [.ended])]).text == "nothing running")
        #expect(Triage.headline([]).text == "nothing running")
    }

    /// Line 2 must not repeat line 1. `1 waiting on you · 1 waiting on you` was
    /// the first thing this produced.
    @Test func theSubheadDoesNotRepeatTheHeadline() {
        let h = Triage.headline([card(statuses: [.waiting], procs: 1)])
        #expect(h.text == "1 waiting on you")
        #expect(!h.subhead.contains("waiting on you"))
    }

    // MARK: - The reachability ladder

    @Test func theErrnoLadderNamesTheRungTheUserCanFix() {
        #expect(ErrnoCause.cause(forErrno: 61) == .serverStopped)   // ECONNREFUSED
        #expect(ErrnoCause.cause(forErrno: 60) == .macUnreachable)  // ETIMEDOUT
        #expect(ErrnoCause.cause(forErrno: 65) == .macUnreachable)  // EHOSTUNREACH
        #expect(ErrnoCause.cause(forErrno: 51) == .tailnetDown)     // ENETUNREACH
        if case .unknown(let code, _) = ErrnoCause.cause(forErrno: 99) {
            #expect(code == 99, "an unrecognised errno must keep its number")
        } else {
            Issue.record("an unrecognised errno must not be classified")
        }
    }

    /// The errno is buried under `NSUnderlyingErrorKey`, and it OUTRANKS the
    /// `URLError.Code`: `.cannotConnectToHost` covers both "refused" and "no
    /// route", which are opposite ends of the ladder.
    @Test func theUnderlyingErrnoOutranksTheURLErrorCode() {
        let posix = NSError(domain: NSPOSIXErrorDomain, code: 51)
        let wrapped = NSError(domain: NSURLErrorDomain,
                              code: NSURLErrorCannotConnectToHost,
                              userInfo: [NSUnderlyingErrorKey: posix])
        #expect(ErrnoCause.classify(wrapped) == .tailnetDown)

        let bare = NSError(domain: NSURLErrorDomain, code: NSURLErrorCannotConnectToHost)
        #expect(ErrnoCause.classify(bare) == .serverStopped)
    }

    @Test func atsRefusalIsABuildProblemAndSaysSo() {
        let ats = NSError(domain: NSURLErrorDomain,
                          code: NSURLErrorAppTransportSecurityRequiresSecureConnection)
        #expect(ErrnoCause.classify(ats) == .transportBlocked)
        #expect(OrchestraError.transportBlocked.guidance.contains("Info.plist"))
    }

    @Test func cancellationIsNeverShownToTheUser() {
        let cancelled = NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled)
        #expect(ErrnoCause.classify(cancelled) == .cancelled)
    }

    /// Only `.unauthorized` may tear down a board that is already on screen.
    /// Everything else keeps the last good data and says how old it is.
    @Test func onlyUnauthorizedDiscardsTheLastGoodBoard() {
        #expect(!OrchestraError.unauthorized(nil).keepsLastGoodData)
        for error: OrchestraError in [.offline, .tailnetDown, .macUnreachable,
                                      .serverStopped, .transportBlocked,
                                      .decoding("x"), .http(status: 500, refusal: nil)] {
            #expect(error.keepsLastGoodData)
        }
    }

    // MARK: - The pairing ticket

    /// Must agree with `pairing.normalise` exactly: generous about form, folding
    /// the four glyphs Crockford removed, because the manual fallback is a human
    /// reading a screen.
    @Test func normalisationMatchesTheServers() {
        #expect(PairingTicket.normalise("7k3m-9qp2") == "7K3M9QP2")
        #expect(PairingTicket.normalise("7K3M 9QP2") == "7K3M9QP2")
        #expect(PairingTicket.normalise("7K3M_9QP2") == "7K3M9QP2")
        // Ground truth taken from `orchestra.pairing.normalise` itself, not from
        // reading it: I,L→1 · O→0 · U→V.
        #expect(PairingTicket.normalise("IL0U") == "110V")
        #expect(PairingTicket.normalise("ilou-ILOU") == "110V110V")
        #expect(PairingTicket.normalise("a\tb") == "AB")
        #expect(PairingTicket.normalise("  hb28m1em  ") == "HB28M1EM")
        #expect(PairingTicket.grouped("7ZVTZ9N5") == "7ZVT-Z9N5")
    }

    /// The exact URL `pairing.payload_url` produces, both with and without the
    /// port — it is omitted when it is the default, to save five bytes of a
    /// budget that decides the QR's version.
    @Test func parsesTheServersOwnPairingURLs() throws {
        let withPort = try #require(PairingTicket(url: "orc://p?h=100.113.110.31&p=4269&c=HB28M1EM"))
        #expect(withPort.host == "100.113.110.31")
        #expect(withPort.port == 4269)
        #expect(withPort.code == "HB28M1EM")

        let defaulted = try #require(PairingTicket(url: "orc://p?h=100.113.110.31&c=7ZVTZ9N5"))
        #expect(defaulted.port == PairingTicket.defaultPort)

        let magicDNS = try #require(PairingTicket(
            url: "orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4269&c=7ZVTZ9N5"))
        #expect(magicDNS.host == "achills-macbook-pro.tail1205d9.ts.net")
    }

    /// A scanner pointed at a Wi-Fi QR, a URL, or a shortened code must fail
    /// QUIETLY — never pair, never crash.
    @Test func rejectsAnythingThatIsNotAPairingURL() {
        #expect(PairingTicket(url: "https://example.com") == nil)
        #expect(PairingTicket(url: "WIFI:S=home;T=WPA;P=hunter2;;") == nil)
        #expect(PairingTicket(url: "orc://q?h=1.2.3.4&c=HB28M1EM") == nil)
        #expect(PairingTicket(url: "orc://p?c=HB28M1EM") == nil, "no host")
        #expect(PairingTicket(url: "orc://p?h=1.2.3.4") == nil, "no code")
        #expect(PairingTicket(url: "orc://p?h=1.2.3.4&c=SHORT") == nil, "wrong length")
        #expect(PairingTicket(url: "") == nil)
    }

    // MARK: - Formatters

    @Test func relativeTimeMatchesTheBoardsForms() {
        #expect(RelativeTime.short(0) == "0s")
        #expect(RelativeTime.short(12) == "12s")
        #expect(RelativeTime.short(59) == "59s")
        #expect(RelativeTime.short(60) == "1m")
        #expect(RelativeTime.short(3599) == "59m")
        #expect(RelativeTime.short(3600) == "1h")
        #expect(RelativeTime.short(3600 + 38 * 60) == "1h 38m")
        #expect(RelativeTime.short(86_400) == "1d")
        #expect(RelativeTime.short(86_400 * 3 + 5) == "3d")
    }

    /// A server clock a second ahead of the phone's must read `0s`, not `-1s`.
    @Test func relativeTimeNeverGoesNegative() {
        #expect(RelativeTime.short(-30) == "0s")
        let future = Date(timeIntervalSince1970: 2_000)
        #expect(RelativeTime.short(since: future, now: Date(timeIntervalSince1970: 1_000)) == "0s")
    }

    /// A resume that should have fired already is a different fact from one that
    /// fires in nine minutes, and `-4m` says neither.
    @Test func aPastDueCountdownReadsDue() {
        let now = Date(timeIntervalSince1970: 1_000)
        #expect(RelativeTime.countdown(to: Date(timeIntervalSince1970: 900), now: now) == "due")
        #expect(RelativeTime.countdown(to: Date(timeIntervalSince1970: 1_000), now: now) == "due")
        #expect(RelativeTime.countdown(to: Date(timeIntervalSince1970: 1_120), now: now) == "2m")
    }

    /// Server truncation is upstream and invisible. Adding a second ellipsis
    /// gives `…the old key 24h……`.
    @Test func truncationDoesNotDoubleTheEllipsis() {
        let already = "the JWT one, and keep the old key\u{2026}"
        #expect(TextTruncation.alreadyTruncated(already))
        #expect(TextTruncation.clip(already, to: 10).hasSuffix("\u{2026}"))
        #expect(!TextTruncation.clip(already, to: 10).hasSuffix("\u{2026}\u{2026}"))
        #expect(TextTruncation.clip("short", to: 10) == "short")
    }

    /// Prose from a transcript has newlines in it, and `lineLimit(1)` on a string
    /// containing `\n` silently renders only the first line — which reads as an
    /// agent that said four words.
    @Test func proseIsFlattenedBeforeItIsClamped() {
        #expect(SanitizedText.oneLine("first\nsecond\n\nthird") == "first second third")
        #expect(SanitizedText.oneLine("  padded  ") == "padded")
    }

    @Test func theModelLabelKeepsTheFamilyAndDropsTheDate() {
        #expect(ModelLabel.short("haiku-4-5-20251001") == "haiku")
        #expect(ModelLabel.short("opus-4-8") == "opus")
        #expect(ModelLabel.short("fable-5") == "fable")
        #expect(ModelLabel.short("") == "")
    }
}
