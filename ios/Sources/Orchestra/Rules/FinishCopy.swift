import Foundation

/// The `mode` → prose table of `UX.md` §4.4, kept out of the view so it can be
/// read as a table and tested as one.
///
/// The server's own `message` is always shown; this adds what the *card* will do
/// next, which the message does not say and which is the thing the user is about
/// to look for.
public enum FinishCopy {
    public static func result(_ reply: FinishReply) -> String {
        let server = reply.text
        guard let next = whatHappensNext(reply) else { return server }
        return server + "\n\n" + next
    }

    static func whatHappensNext(_ reply: FinishReply) -> String? {
        switch reply.mode {
        case .exit:   "The terminal closes and the card frees itself."
        case .brief:  "The card now shows ✕ close. When the agent reports done, that step verifies the landing."
        case .slim:   "The card now shows ✕ close."
        case .nudge:  "The brief clock restarted. ✕ close works once the agent reports clean."
        case .parked: "No agent was needed — the worktree is on the trunk and pulled."
        case .noop:   "Nothing to do; the card is already free."
        case .pending: "This clears itself the moment the landing verifies — the card watches for it."
        case .chat:   "A typed nudge would collide with the agent's open dialog, so answer it in chat instead."
        case .unknown, .none: nil
        }
    }
}
