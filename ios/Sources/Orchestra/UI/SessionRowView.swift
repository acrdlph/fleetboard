import SwiftUI

/// One session, in the desktop's exact field order so a user of both never
/// re-learns it:
///
/// ```
/// ▲ NEEDS ANSWER      [work] opus       2m      ← status · account · model · age
/// Rotate refresh tokens without breaking…       ← topic, 1 line
/// → the JWT one, and keep the old key 24h       ← last_user, 1 line
/// ⏎ I can take either approach — do you want…   ← last_assistant, 2 lines
/// ⚙ subagents running                           ← the busy tag
/// ↳ continued on [spare] — nothing to do        ← handed_to
/// ```
///
/// **`UX.md` §3.1.4 flags all four prose lines as unresolved** — `API.md` §9.3
/// would ship a single 80-character `headline` and move `topic`, `last_user`,
/// `last_assistant` and `subagent_said` to a detail route. The live server ships
/// all four ON THE BOARD, which is resolution (a) and which is also the stronger
/// argument for a streaming client: they change only when the agent speaks, so
/// the delta cost is zero and a list/detail split buys nothing but a
/// partially-loaded-session bug class. This row is built against what is on the
/// wire.
struct SessionRowView: View {
    let session: Session
    /// Whether this is the card's first (most actionable) session. Row 2 is
    /// deliberately thinner — one line — because two full rows per card fills a
    /// phone with three cards.
    let isPrimary: Bool
    let now: Date
    /// The card's branch, so a session on the same branch does not repeat it.
    let cardBranch: String

    @Environment(\.dynamicTypeSize) private var typeSize

    private var style: StatusStyle { .of(session.status) }

    /// `UX.md` §9.3 — what is dropped at each Dynamic Type step. Density is
    /// recovered by dropping COLUMNS, never by shrinking type.
    private var showsModel: Bool { typeSize < .xxxLarge }
    private var showsBranch: Bool { typeSize < .accessibility1 }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            statusLine
            if isPrimary {
                if let topic = session.topic, !topic.isEmpty {
                    Text(topic)
                        .font(OrcFont.bodyCompact)
                        .foregroundStyle(Palette.textSecondary)
                        .lineLimit(1)
                }
                if let user = session.lastUser, !user.isEmpty {
                    prose(SanitizedText.oneLine(user), symbol: StatusStyle.Mark.userSaid,
                          hue: Palette.statusFree, lines: 1)
                }
                if let assistant = session.lastAssistant, !assistant.isEmpty {
                    prose(SanitizedText.oneLine(assistant), symbol: StatusStyle.Mark.agentSaid,
                          hue: Palette.textSecondary, lines: 2)
                }
            }
            if let busy = session.busySignal {
                tag(busy, symbol: StatusStyle.Mark.subagent, hue: Palette.statusWorking)
            }
            if let handed = session.handedTo {
                // The line that EXPLAINS WHY AN ALARM WAS SUPPRESSED, so it is
                // rendered at full contrast. The desktop dims it to 3.20:1.
                tag("continued on [\(handed)] — nothing to do",
                    symbol: StatusStyle.Mark.handedTo, hue: Palette.statusWorking)
            }
            if session.status == .blocked, !session.pendingTools.isEmpty {
                tag("waiting on: " + session.pendingTools.prefix(3).joined(separator: ", "),
                    symbol: StatusStyle.Mark.waitingOn, hue: Palette.statusLimit)
            }
            if let limit = session.limit {
                limitLine(limit)
            }
        }
        .padding(.horizontal, Space.md)
        .padding(.vertical, Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            // `ended` gets a DARKER GROUND, not an opacity. An ended row still
            // carries model, account, age and topic, and `textTertiary` at 0.55
            // over `sunken` measures 2.43:1 — a fail, on a row that is still
            // information.
            session.status == .ended ? Palette.sunkenDim : Palette.sunken
        )
        .accessibilityElement(children: .combine)
    }

    private var statusLine: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.sm) {
            StatusPill(style)
            Text(verbatim: "[\(session.account)]")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.statusFree)
            if showsModel, !session.model.isEmpty {
                Text(ModelLabel.short(session.model))
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                    .lineLimit(1)
            }
            if showsBranch, session.branch != cardBranch, session.branch != "?" {
                Text(session.branch)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: Space.xs)
            ageSlot
        }
    }

    /// A **fixed-width slot sized for the longest form**. The age ticks once a
    /// second and mutates text only; if this slot resized, every row on the
    /// board would reflow under the user's thumb once a second.
    private var ageSlot: some View {
        Text(RelativeTime.short(since: session.lastWrite, now: now))
            .font(OrcFont.meta)
            .foregroundStyle(Palette.textTertiary)
            .lineLimit(1)
            .frame(minWidth: 46, alignment: .trailing)
            .accessibilityLabel("last wrote \(RelativeTime.short(since: session.lastWrite, now: now)) ago")
    }

    private func prose(_ text: String, symbol: String, hue: Color, lines: Int) -> some View {
        HStack(alignment: .top, spacing: Space.xs) {
            Image(systemName: symbol)
                .font(OrcFont.meta)
                .foregroundStyle(hue.opacity(0.8))
                .accessibilityHidden(true)
            Text(text)
                .font(OrcFont.bodyCompact)
                .foregroundStyle(hue)
                .lineLimit(lines)
        }
    }

    private func tag(_ text: String, symbol: String, hue: Color) -> some View {
        HStack(spacing: Space.xs) {
            Image(systemName: symbol)
                .font(OrcFont.meta)
                .accessibilityHidden(true)
            Text(text)
                .font(OrcFont.meta)
                .lineLimit(1)
        }
        .foregroundStyle(hue)
    }

    @ViewBuilder
    private func limitLine(_ limit: SessionLimit) -> some View {
        // ALL THREE FIELDS NULL IS A REAL AND COMMON STATE — the
        // transcript-regex fallback fires when the CLI wrote its limit notice
        // but the cclimits cache was cold. Then the honest row is "reset time
        // unknown", not a countdown to nothing.
        if let resets = limit.resets {
            tag("resets \(RelativeTime.clock(resets)) · \(RelativeTime.countdown(to: resets, now: now))",
                symbol: "clock.arrow.circlepath", hue: Palette.statusLimit)
        } else {
            tag("reset time unknown", symbol: "questionmark.circle",
                hue: Palette.statusLimit)
        }
    }
}
