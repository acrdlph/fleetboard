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

/// `"haiku-4-5-20251001"` is 18 characters of column for two characters of
/// information. The date suffix and the point release are dropped; the family is
/// kept, because that is the part a person is choosing between.
public enum ModelLabel {
    public static func short(_ raw: String) -> String {
        guard let family = raw.split(separator: "-").first else { return raw }
        return String(family)
    }
}
