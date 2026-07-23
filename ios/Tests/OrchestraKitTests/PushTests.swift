import Foundation
import Testing
@testable import OrchestraKit

/// Push, tested against the bytes `notify.compose` really produces.
///
/// The two payloads below were emitted by the Python `notify.compose` on
/// 2026-07-23 — a `session.needs_answer` (P1, answerable) and an
/// `account.limit_hit` (P2, no worktree) — and pasted here verbatim, `\u` escapes
/// and all. `METHOD.md` §4: the server is the contract, so this suite decodes
/// what the server sends, not what a document describes. The single most load-
/// bearing fact it pins is a NEGATIVE one: the payload carries **no `category`**,
/// which is why `PushMessage` derives it and why inline reply needs the service
/// extension or a one-line server addition (reported in `ios/README.md`).
struct PushTests {

    /// A real `session.needs_answer` payload, `privacy: structural`.
    static let needsAnswerJSON = """
    {
      "aps": {
        "alert": {
          "subtitle": "\u{25b2} NEEDS ANSWER \u{00b7} [account2]",
          "title": "ConfidAI2 needs an answer"
        },
        "content-available": 1,
        "interruption-level": "time-sensitive",
        "mutable-content": 1,
        "sound": "default",
        "thread-id": "orchestra|ConfidAI2"
      },
      "at": 1721000000.0,
      "counts": { "blocked": 1, "needs_input": 2 },
      "dedupe_key": "session.needs_answer|ConfidAI2|ca1c96e9|3",
      "ev": "session.needs_answer",
      "event_id": "evt_abc123",
      "level": "P1",
      "sid": "ca1c96e9-1111-2222-3333-444455556666",
      "wt": "ConfidAI2"
    }
    """

    /// A real `account.limit_hit` payload — `wt` and `sid` are JSON null.
    static let limitHitJSON = """
    {
      "aps": {
        "alert": {
          "subtitle": "\u{26d4} LIMIT HIT \u{00b7} [account2]",
          "title": "[account2] hit its limit"
        },
        "content-available": 1,
        "interruption-level": "active",
        "mutable-content": 1,
        "sound": "default",
        "thread-id": "orchestra|\u{2014}"
      },
      "at": 1721000500.0,
      "dedupe_key": "account.limit_hit|account2|1",
      "ev": "account.limit_hit",
      "event_id": "evt_lim",
      "level": "P2",
      "sid": null,
      "wt": null
    }
    """

    static func userInfo(_ json: String) throws -> [AnyHashable: Any] {
        let obj = try JSONSerialization.jsonObject(with: Data(json.utf8))
        return try #require(obj as? [AnyHashable: Any])
    }

    // MARK: - decode

    @Test func decodesAnswerablePayloadAndItsAddresses() throws {
        let msg = try #require(PushMessage(userInfo: Self.userInfo(Self.needsAnswerJSON)))
        #expect(msg.event == "session.needs_answer")
        #expect(msg.eventID == "evt_abc123")
        #expect(msg.dedupeKey == "session.needs_answer|ConfidAI2|ca1c96e9|3")
        #expect(msg.worktree == "ConfidAI2")
        #expect(msg.sid == "ca1c96e9-1111-2222-3333-444455556666")
        #expect(msg.level == "P1")
        #expect(msg.at == 1721000000.0)
        #expect(msg.title == "ConfidAI2 needs an answer")
        #expect(msg.subtitle?.contains("NEEDS ANSWER") == true)
        // structural privacy: no prose in the payload — the extension fetches it.
        #expect(msg.body == nil)
    }

    /// The whole point of the reply feature: this event earns the text field, and
    /// the reply is addressed by sid ALONE — the payload never carried an account.
    @Test func answerableEventGetsReplyCategoryAndTarget() throws {
        let msg = try #require(PushMessage(userInfo: Self.userInfo(Self.needsAnswerJSON)))
        #expect(msg.categoryID == PushCategory.reply)
        let target = try #require(msg.replyTarget)
        #expect(target.sid == "ca1c96e9-1111-2222-3333-444455556666")
        #expect(target.worktree == "ConfidAI2")
    }

    @Test func answerableEventDeepLinksToItsSession() throws {
        let msg = try #require(PushMessage(userInfo: Self.userInfo(Self.needsAnswerJSON)))
        let link = msg.deepLink
        #expect(link.worktree == "ConfidAI2")
        #expect(link.sid == "ca1c96e9-1111-2222-3333-444455556666")
        #expect(link.isBoardOnly == false)
    }

    /// An account-level event carries no worktree and no session, so it is NOT
    /// answerable and its tap lands on the board — where an account question is
    /// answered anyway.
    @Test func accountEventIsBoardOnlyAndNotAnswerable() throws {
        let msg = try #require(PushMessage(userInfo: Self.userInfo(Self.limitHitJSON)))
        #expect(msg.worktree == nil)
        #expect(msg.sid == nil)
        #expect(msg.categoryID == PushCategory.info)
        #expect(msg.replyTarget == nil)
        #expect(msg.deepLink.isBoardOnly)
    }

    /// `session.your_turn` HAS a session but is a fact, not a prompt: a reply
    /// field on it would type into whatever the agent does next. So even with a
    /// sid present, it is not answerable.
    @Test func aYourTurnEventHasNoReplyEvenWithASession() {
        #expect(PushMessage.isAnswerable(event: "session.your_turn") == false)
        let msg = PushMessage(event: "session.your_turn", worktree: "W", sid: "abc")
        #expect(msg.categoryID == PushCategory.info)
        #expect(msg.replyTarget == nil)
    }

    /// An unknown event is treated as NOT answerable — inventing a reply target
    /// for a payload this build does not understand is how a message ends up
    /// typed at the wrong prompt.
    @Test func anUnknownEventIsNeverAnswerable() {
        #expect(PushMessage.isAnswerable(event: "session.teleported") == false)
        #expect(PushMessage.isAnswerable(event: nil) == false)
    }

    /// No `ev` at all is not a notification this app can route — it must fail the
    /// init rather than decode to a hollow value.
    @Test func aPayloadWithoutAnEventTypeDoesNotDecode() {
        #expect(PushMessage(userInfo: ["aps": ["alert": ["title": "hi"]]]) == nil)
    }

    // MARK: - settings

    /// The defaults mirror `EVENT_TYPES[…]["default"]` exactly. `your_turn`,
    /// `resume.armed` and `worktree.free` are the three that are OFF — the
    /// asymmetry that makes the product "told when needed, not when idle".
    @Test func typeDefaultsMirrorTheServerTable() {
        #expect(PushEventType.needsAnswer.defaultOn)
        #expect(PushEventType.blocked.defaultOn)
        #expect(PushEventType.yourTurn.defaultOn == false)
        #expect(PushEventType.resumeArmed.defaultOn == false)
        #expect(PushEventType.worktreeFree.defaultOn == false)
        #expect(PushEventType.limitHit.defaultOn)
        #expect(PushEventType.sessionDied.defaultOn)
        // and the three OFF ones are the only three off.
        let off = PushEventType.allCases.filter { !$0.defaultOn }.map(\.rawValue).sorted()
        #expect(off == ["resume.armed", "session.your_turn", "worktree.free"])
    }

    @Test func isOnHonoursDefaultThenOverride() {
        var s = PushSettings()
        #expect(s.isOn(.yourTurn) == false)      // default off
        #expect(s.isOn(.needsAnswer))            // default on
        s.set(.yourTurn, on: true)
        #expect(s.isOn(.yourTurn))
        #expect(s.rules["session.your_turn"] == true)
    }

    /// Setting a type back TO its default drops the override, so a later change
    /// to the default is inherited rather than frozen — the sparse-map behaviour
    /// the server relies on by falling through an absent key.
    @Test func settingATypeToItsDefaultRemovesTheOverride() {
        var s = PushSettings()
        s.set(.needsAnswer, on: false)           // non-default -> stored
        #expect(s.rules["session.needs_answer"] == false)
        s.set(.needsAnswer, on: true)            // back to default -> removed
        #expect(s.rules["session.needs_answer"] == nil)
        #expect(s.isOn(.needsAnswer))
    }

    /// The settings body has exactly the four keys the route accepts, shaped as
    /// `prefs_from_device` reads them.
    @Test func settingsBodyHasTheFourAcceptedKeys() throws {
        var s = PushSettings()
        s.quietHours = QuietHours(enabled: true, from: "23:30", to: "07:00", allowP1: true)
        s.privacy = .detail
        s.nudgeMin = 20
        s.set(.yourTurn, on: true)
        let body = s.settingsBody
        #expect(Set(body.keys) == ["quiet_hours", "rules", "privacy", "nudge_min"])
        #expect(body["privacy"] as? String == "detail")
        #expect(body["nudge_min"] as? Int == 20)
        let q = try #require(body["quiet_hours"] as? [String: Any])
        #expect(q["enabled"] as? Bool == true)
        #expect(q["from"] as? String == "23:30")
        #expect(q["allow_p1"] as? Bool == true)
        let rules = try #require(body["rules"] as? [String: Bool])
        #expect(rules["session.your_turn"] == true)
    }

    /// The local mirror round-trips: what the store persists, `fromStored` reads
    /// back the same.
    @Test func storedSettingsRoundTrip() {
        var s = PushSettings()
        s.quietHours = QuietHours(enabled: true, from: "22:00", to: "06:30", allowP1: false)
        s.privacy = .detail
        s.nudgeMin = 30
        s.set(.worktreeFree, on: true)
        let stored: [String: Any] = ["rules": s.rules,
                                     "quiet_hours": s.quietHours.wireBody,
                                     "privacy": s.privacy.rawValue,
                                     "nudge_min": s.nudgeMin]
        let back = PushSettings(fromStored: stored)
        #expect(back.privacy == .detail)
        #expect(back.nudgeMin == 30)
        #expect(back.quietHours.enabled)
        #expect(back.quietHours.to == "06:30")
        #expect(back.quietHours.allowP1 == false)
        #expect(back.isOn(.worktreeFree))
    }

    /// The `_privacy` key `prefs_from_device` injects into `rules` must not be
    /// read back as an event override.
    @Test func storedRulesIgnoreTheUnderscorePrivacyKey() {
        let stored: [String: Any] = ["rules": ["_privacy": "detail",
                                               "session.your_turn": true]]
        let back = PushSettings(fromStored: stored)
        #expect(back.rules["_privacy"] == nil)
        #expect(back.isOn(.yourTurn))
    }

    // MARK: - status

    @Test func decodesPushStatusFromTheNoopSink() throws {
        let json = """
        {"ok": true, "registered": true,
         "push": {"backend": "none", "ready": false,
                  "problems": ["no APNs key configured"]}}
        """
        let status = try JSONDecoder().decode(PushStatus.self, from: Data(json.utf8))
        #expect(status.ok)
        #expect(status.registered)
        #expect(status.backend == "none")
        #expect(status.ready == false)
        #expect(status.problems == ["no APNs key configured"])
        #expect(status.environment == nil)
    }

    /// The registration reply surfaces the one warning that matters — a Focus
    /// that will suppress the P1 this feature exists for.
    @Test func decodesRegistrationReplyWithAWarning() throws {
        let json = """
        {"ok": true, "backend": "apns", "environment": "sandbox",
         "warnings": ["time_sensitive_allowed is false — P1 alerts will be suppressed"]}
        """
        let reply = try JSONDecoder().decode(RegisterPushReply.self, from: Data(json.utf8))
        #expect(reply.ok)
        #expect(reply.environment == "sandbox")
        #expect(reply.warnings.count == 1)
    }
}
