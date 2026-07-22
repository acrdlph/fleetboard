import SwiftUI

/// The type ramp of `UX.md` §9.3 — eleven tokens, two voices.
///
/// **Mono is for machine tokens; SF for human language.** Worktree name, branch,
/// commit hash, `[account]`, model, pid, tty, ages, countdowns, `↑ahead`,
/// `Δdirty`, counts, status words, badges, buttons — all mono. Topic,
/// `last_assistant`, `last_user`, chat, control labels — all SF Pro.
///
/// **Deviation from the spec, stated rather than hidden.** §9.3 bundles four
/// weights of IBM Plex Mono and resolves them from `\.legibilityWeight`, because
/// `Font.custom(_:size:relativeTo:)` does not respond to Bold Text and would
/// leave the entire machine voice thin while the human voice went bold. This
/// build uses the SYSTEM monospaced face (`design: .monospaced`) instead, which
/// gets all of that for free — it honours Bold Text, it honours Dynamic Type,
/// and it cannot fall back per-glyph to a face with different metrics, which is
/// the silent failure §9.4 spends a page on. The cost is that it is SF Mono
/// rather than Plex. Bundling Plex is additive and does not change a call site.
///
/// **Everything uses `relativeTo:`.** A fixed-size initialiser does not
/// participate in Dynamic Type at all, and at AX5 a fixed 12 pt `meta` would
/// render larger than the 20 pt `title` above it — the hierarchy inverted by the
/// setting that exists to preserve it.
public enum OrcFont {
    /// headline numbers only. Sans at display size because proportional figures
    /// read better than tabular mono digits at 34 pt.
    public static let display = Font.system(.largeTitle, design: .default).weight(.semibold)
    /// sheet titles, section heads
    public static let title = Font.system(.title3, design: .default).weight(.semibold)
    /// worktree name
    public static let cardName = Font.system(.headline, design: .monospaced).weight(.semibold)
    /// mission composer, chat bubbles
    public static let body = Font.system(.body, design: .default)
    /// topic, last_assistant, last_user, notes
    public static let bodyCompact = Font.system(.subheadline, design: .default)
    /// branch, path, attach commands, progress lines
    public static let code = Font.system(.subheadline, design: .monospaced)
    /// commit subject, session identifiers
    public static let codeSm = Font.system(.footnote, design: .monospaced)
    /// age, model, tty, etime, %cpu
    public static let meta = Font.system(.caption, design: .monospaced)
    /// UPPERCASE micro-labels
    public static let label = Font.system(.caption2, design: .monospaced).weight(.semibold)
    /// status words and availability badges. **12 pt, not the desktop's 10** —
    /// the densest and most-glanced element in the app; on a phone it goes up.
    public static let status = Font.system(.caption, design: .monospaced).weight(.semibold)
    /// all button labels
    public static let button = Font.system(.callout, design: .monospaced).weight(.semibold)

    /// Tracking for the two uppercase tokens, as a fraction of the RENDERED
    /// size. A constant computed from the shipped size means an 11 pt label at
    /// AX5 carries .03em instead of .08em — the tracking vanishing exactly where
    /// "uppercase mono without tracking reads as a wall" bites hardest.
    public static let uppercaseTracking = 0.08
}

/// Spacing — 4 pt base, seven steps (`UX.md` §9.5).
public enum Space {
    public static let xxs: CGFloat = 2
    public static let xs: CGFloat = 4
    public static let sm: CGFloat = 8
    public static let md: CGFloat = 12
    public static let lg: CGFloat = 16
    public static let xl: CGFloat = 24
    public static let xxl: CGFloat = 32
}

/// Radii (`UX.md` §9.5). `.continuous` throughout.
public enum Radius {
    /// chips, badges, pills
    public static let xs: CGFloat = 5
    /// rows, bubbles, fields
    public static let sm: CGFloat = 8
    /// cards, tiles
    public static let md: CGFloat = 12
    /// sheets, toasts
    public static let lg: CGFloat = 16
}

extension View {
    /// Uppercase micro-label styling, with tracking that scales with the
    /// rendered point size rather than being frozen at the shipped one.
    func orcTracking(_ size: CGFloat) -> some View {
        tracking(size * OrcFont.uppercaseTracking)
    }
}
