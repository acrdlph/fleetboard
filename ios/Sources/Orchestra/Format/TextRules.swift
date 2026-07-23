import Foundation

/// Prose from a transcript arrives with newlines in it. A `lineLimit(1)` on a
/// string containing `\n` renders the first line and drops the rest silently —
/// which reads as an agent that said four words.
public enum SanitizedText {
    public static func oneLine(_ s: String) -> String {
        s.split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespaces)
    }
}

/// What the server will do to a message on its way to the terminal, done here so
/// the composer is WYSIWYG.
///
/// `terminal.send_to_process` runs `re.sub(r"\s*\n\s*", " ", text).strip()`
/// before it types anything, so a message composed with line breaks arrives as
/// one line whatever the composer showed. Rather than let that surprise happen
/// on the far side, the composer applies the same transform **as you type**:
/// Return inserts a space. `UX.md` §3.3.2 — WYSIWYG or nothing.
public enum WireText {
    /// The server's own normalisation, character for character.
    public static func collapsed(_ s: String) -> String {
        let joined = s.replacingOccurrences(of: #"\s*\n\s*"#, with: " ",
                                            options: .regularExpression)
        return joined.trimmingCharacters(in: .whitespaces)
    }

    /// Whether a transcript turn is plausibly the message we sent.
    ///
    /// **Used positive-only.** A match upgrades a receipt; a non-match upgrades
    /// nothing and never produces a warning, because every one of the five known
    /// mismatch paths (`UX.md` §3.3.2) is a false NEGATIVE: a queued message
    /// arrives minutes later, `_real_prompt` drops anything starting `/`,
    /// `_clean` cuts at 899 characters, the window is only 40 turns, and repeated
    /// identical text matches the wrong occurrence. Absence proves nothing, so
    /// absence is never rendered.
    public static func matches(sent: String, turn: String) -> Bool {
        let a = collapsed(sent)
        let b = turn.trimmingCharacters(in: .whitespaces)
        if a == b { return true }
        // `_clean(t, 900)` truncates at 899 characters and appends `…`.
        if b.hasSuffix("…") {
            let head = String(b.dropLast())
            return !head.isEmpty && a.hasPrefix(head)
        }
        return false
    }
}

/// `"haiku-4-5-20251001"` is 18 characters of column for two characters of
/// information. The date suffix and the point release are dropped; the family is
/// kept, because that is the part a person is choosing between.
public enum ModelLabel {
    public static func short(_ raw: String) -> String {
        guard let family = raw.split(separator: "-").first else { return raw }
        return String(family)
    }
}
