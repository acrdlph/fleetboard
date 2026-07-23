import SwiftUI

/// The glyph vocabulary of `UX.md` §9.4 — **two tables, no collisions**.
///
/// Every meaning-bearing mark in this app is an SF Symbol, and the reasons are
/// structural rather than aesthetic:
///
/// * `⛔` U+26D4 has `Emoji_Presentation = Yes`. iOS renders it from Apple Color
///   Emoji as a full-colour bitmap that **ignores `.foregroundStyle`**, ignores
///   weight, does not lift in Contrast+, does not go monochrome in a tinted
///   widget, and carries emoji advance width — which breaks the mono column
///   alignment that is the only reason mono was kept. It is also a *disc*,
///   silhouette-identical to `●` WORKING at 12 pt, so the shape channel fails
///   for LIMIT: exactly the status a protanope most needs separated from
///   WORKING.
/// * `⌁ ⌖ ⌗ ⧗ ⏎` are Miscellaneous Technical / Supplemental Math codepoints that
///   a bundled mono face very likely does not cover, and `Font.custom` falls
///   back **per glyph and silently** to a face with different metrics.
///
/// **The systematic rule: session statuses take `.fill` variants, card
/// availability takes outline variants, and every base shape differs.** Reusing
/// `●` for `working` and `busy`, or `▲` for `needs_input` and `attention`,
/// collapses exactly the pair that is hardest to separate by hue — on rows 8 pt
/// apart. Eleven marks, eleven silhouettes, no cross-table collision.
public struct StatusStyle: Sendable, Equatable {
    public let symbol: String
    public let word: String
    public let hue: Color

    public init(symbol: String, word: String, hue: Color) {
        self.symbol = symbol
        self.word = word
        self.hue = hue
    }

    /// Session status → fill silhouette.
    public static func of(_ status: SessionStatus) -> StatusStyle {
        switch status {
        case .working:
            StatusStyle(symbol: "circle.fill", word: "WORKING", hue: Palette.statusWorking)
        case .needsInput:
            StatusStyle(symbol: "exclamationmark.triangle.fill", word: "NEEDS ANSWER",
                        hue: Palette.statusNeeds)
        case .blocked:
            // The desktop splits this — accent border, yellow text, "the border
            // screams and the text is calmer". At 3 pt on a phone that reads as
            // a bug. `blocked` IS an attention state; the distinction moves to
            // the channel that survives: the square silhouette and the detail
            // line naming the tool.
            StatusStyle(symbol: "square.fill", word: "BLOCKED", hue: Palette.statusNeeds)
        case .waiting:
            StatusStyle(symbol: "diamond.fill", word: "YOUR TURN", hue: Palette.statusTurn)
        case .limit:
            StatusStyle(symbol: "hourglass", word: "LIMIT HIT", hue: Palette.statusLimit)
        case .ended:
            StatusStyle(symbol: "circle", word: "ENDED", hue: Palette.textTertiary)
        case .unknown:
            // NEVER `○ ENDED`. An unreadable process table is not proof that a
            // session stopped, and FREE is what gates dispatch targeting.
            StatusStyle(symbol: "questionmark.circle", word: "UNKNOWN",
                        hue: Palette.textTertiary)
        }
    }

    /// Card section → outline silhouette. Keyed on `BoardSection` rather than on the
    /// server's `availability` because the sections are the five `UX.md` §3.1.2
    /// specifies and the server ships four — see `Triage`.
    public static func of(_ section: BoardSection) -> StatusStyle {
        switch section {
        case .needsYou:
            StatusStyle(symbol: "exclamationmark.triangle", word: "NEEDS YOU",
                        hue: Palette.statusNeeds)
        case .yourTurn:
            StatusStyle(symbol: "diamond", word: "YOUR TURN", hue: Palette.statusTurn)
        case .working:
            StatusStyle(symbol: "waveform", word: "BUSY", hue: Palette.statusWorking)
        case .limited:
            StatusStyle(symbol: "pause.circle", word: "WAITING", hue: Palette.statusLimit)
        case .free:
            StatusStyle(symbol: "circle.dashed", word: "FREE", hue: Palette.statusFree)
        }
    }

    /// The non-status marks, named once so a linter can require string literals
    /// in a mono style to come from here (`UX.md` §9.4, `OrcLiterals`).
    public enum Mark {
        public static let subagent = "gearshape.2.fill"
        public static let waitingOn = "clock"
        public static let agentSaid = "arrow.turn.down.left"
        public static let userSaid = "arrow.right"
        public static let handedTo = "arrow.turn.down.right"
        public static let ahead = "arrow.up"
        public static let behind = "arrow.down"
        public static let pid = "scope"
        public static let brand = "bolt.horizontal"
    }
}

/// A status pill: symbol + word, in one hue, on a tint fill that is capped at
/// α 0.12 and removed entirely in Contrast+ and Daylight.
///
/// **Colour cannot travel alone** (`UX.md` §9.6). Every pill carries its
/// silhouette and its word, so the status survives a protanope, a greyscale
/// screenshot and a tinted widget.
public struct StatusPill: View {
    private let style: StatusStyle
    @Environment(\.colorScheme) private var scheme
    @Environment(\.colorSchemeContrast) private var contrast
    @Environment(\.boardIsStale) private var stale

    public init(_ style: StatusStyle) { self.style = style }

    public init(_ status: SessionStatus) { self.style = .of(status) }

    public init(_ section: BoardSection) { self.style = .of(section) }

    private var fillAlpha: Double {
        if contrast == .increased { return 0 }
        return scheme == .dark ? 0.12 : 0
    }

    public var body: some View {
        HStack(spacing: Space.xs) {
            Image(systemName: style.symbol)
                .imageScale(.small)
            Text(style.word)
                .orcTracking(12)
        }
        .font(OrcFont.status)
        .foregroundStyle(style.hue)
        .padding(.horizontal, Space.sm)
        .padding(.vertical, Space.xxs + 1)
        .background(
            RoundedRectangle(cornerRadius: Radius.xs, style: .continuous)
                .fill(style.hue.opacity(fillAlpha))
                .overlay(
                    RoundedRectangle(cornerRadius: Radius.xs, style: .continuous)
                        .stroke(style.hue.opacity(fillAlpha == 0 ? 0.75 : 0.35), lineWidth: 1)
                )
        )
        // **Statuses dim; ages do not** (IOS-APP.md §5.5). An age derives from an
        // absolute transcript write stamp, so "8m ago" stays literally true with
        // a dead stream and degrades in the SAFE direction — counting up towards
        // "we don't know". A `● WORKING` badge on a four-minute-old board is a
        // lie, and it is the only thing here that is.
        .opacity(stale ? 0.45 : 1)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(stale ? "\(style.word.lowercased()), not current"
                                  : style.word.lowercased())
    }
}

/// Whether the board on screen is known to be behind.
///
/// It travels in the environment rather than as a parameter for the reason
/// `Palette` resolves its variants in one place: a rule threaded by hand through
/// four view initialisers is a rule that gets applied to three of them.
public extension EnvironmentValues {
    @Entry var boardIsStale: Bool = false
}
