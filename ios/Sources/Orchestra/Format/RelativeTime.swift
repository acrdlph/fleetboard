import Foundation

/// Elapsed time, formatted the way the desktop board formats it.
///
/// This is a client-side animation, and that is a deliberate property of the
/// protocol rather than a convenience: `age_s` LEFT the wire (step 5 phase 5) so
/// that the payload is time-invariant end to end. Nothing on a card changes on
/// the clock alone, so a sweep that finds nothing new cannot bump the version,
/// and a phone can hold a board for an hour and still animate every age
/// correctly from `last_write_at`.
///
/// The consequence the layout has to respect: **rows must never change height or
/// width as time passes.** A metadata line re-wrapping from `2h38m` to `59m`
/// under a thumb is a mis-tap. The slot is sized for the longest form.
public enum RelativeTime {
    /// The widest string this can produce, for sizing a fixed slot.
    public static let widest = "999d"

    /// `12s` · `4m` · `2h 38m` · `3d`. Never negative — a server clock a second
    /// ahead of the phone's must read `0s`, not `-1s`.
    public static func short(_ seconds: TimeInterval) -> String {
        let s = max(0, seconds.rounded(.down))
        if s < 60 { return "\(Int(s))s" }
        let m = Int(s / 60)
        if m < 60 { return "\(m)m" }
        let h = m / 60
        if h < 24 {
            let rem = m % 60
            return rem == 0 ? "\(h)h" : "\(h)h \(rem)m"
        }
        return "\(h / 24)d"
    }

    public static func short(since: Date, now: Date) -> String {
        short(now.timeIntervalSince(since))
    }

    /// A countdown to an absolute instant. Past due reads `due`, never a
    /// negative — an auto-resume that should have fired is a different fact from
    /// one that fires in nine minutes, and `-4m` says neither.
    public static func countdown(to target: Date, now: Date) -> String {
        let remaining = target.timeIntervalSince(now)
        return remaining <= 0 ? "due" : short(remaining)
    }

    /// `resets 14:32` — the absolute half, which is what a person actually plans
    /// around. Always paired with the countdown, never instead of it.
    public static func clock(_ date: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }
}

/// Server truncation is upstream and invisible: `topic` and `last_user` at 140,
/// `last_assistant` and `subagent_said` at 240, all `…`-suffixed.
///
/// **Do not add a second ellipsis.** Check for a trailing `…` first, or a row
/// reads `…the old key 24h……`.
public enum TextTruncation {
    public static let ellipsis: Character = "\u{2026}"

    public static func alreadyTruncated(_ s: String) -> Bool {
        s.last == ellipsis
    }

    /// Clip to `limit` characters, adding an ellipsis only if one is not there.
    public static func clip(_ s: String, to limit: Int) -> String {
        if s.count <= limit { return s }
        let head = String(s.prefix(limit)).trimmingCharacters(in: .whitespacesAndNewlines)
        return alreadyTruncated(head) ? head : head + String(ellipsis)
    }
}
