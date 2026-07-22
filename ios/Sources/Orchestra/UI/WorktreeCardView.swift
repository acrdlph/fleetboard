import SwiftUI

/// One worktree: an identity row, then up to two session rows, then an overflow
/// line.
///
/// The card is drawn as one rounded silhouette with its rows inside it rather
/// than as a SwiftUI `Section` per worktree, because SwiftUI's `List` supports
/// exactly one level of `Section` — "a section per worktree under sticky
/// severity headers" is not expressible, and reaching for `LazyVStack` to fake
/// two levels loses `.swipeActions`, on which the whole gesture model depends.
struct WorktreeCardView: View {
    let card: Worktree
    let section: BoardSection
    let now: Date
    let resumes: [ResumeSchedule]

    @Environment(\.dynamicTypeSize) private var typeSize

    /// `UX.md` §9.3 — one inline session at `.accessibility1` and above, two
    /// below it. The server already returns them severity-sorted, so "the first
    /// two" is "the two that matter".
    private var inlineSessionCount: Int { typeSize >= .accessibility1 ? 1 : 2 }

    private var visibleSessions: [Session] {
        Array(card.sessions.prefix(inlineSessionCount))
    }

    private var hiddenCount: Int {
        max(0, card.sessions.count - visibleSessions.count)
    }

    var body: some View {
        VStack(spacing: 0) {
            identity
            ForEach(visibleSessions) { session in
                Divider().overlay(Palette.hairline)
                SessionRowView(session: session,
                               isPrimary: session.id == visibleSessions.first?.id,
                               now: now,
                               cardBranch: card.git.branch)
            }
            if hiddenCount > 0 {
                Divider().overlay(Palette.hairline)
                overflow
            }
        }
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(section == .needsYou ? Palette.statusNeeds.opacity(0.55)
                                             : Palette.hairline,
                        lineWidth: 1)
        )
    }

    private var identity: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .firstTextBaseline, spacing: Space.sm) {
                Image(systemName: StatusStyle.of(section).symbol)
                    .font(OrcFont.status)
                    .foregroundStyle(StatusStyle.of(section).hue)
                    .accessibilityHidden(true)
                // MIDDLE truncation: worktree names share long prefixes
                // (`ConfidAI`, `ConfidAI-security-audit`), so head- and
                // tail-truncation both destroy identity. ~18 characters fit at
                // 17 pt on a 393 pt screen and live names reach 23.
                Text(card.name)
                    .font(OrcFont.cardName)
                    .foregroundStyle(Palette.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: Space.xs)
                if !card.liveProcs.isEmpty {
                    Text(verbatim: "\(card.liveProcs.count)")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                        .accessibilityLabel("\(card.liveProcs.count) live terminal(s)")
                }
            }
            gitLine
            if !resumes.isEmpty { resumeLine }
        }
        .padding(.horizontal, Space.md)
        .padding(.vertical, Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.raised)
    }

    /// Note the `Text(verbatim:)` on every number in this file, and it is not
    /// style.
    ///
    /// `Text("\(n)")` takes the `LocalizedStringKey` overload, which formats the
    /// interpolated integer through the locale — so pid **34115** renders as
    /// **`34.115`** on a device set to a European locale, and `Δ998` becomes
    /// `Δ1.203` the moment a worktree gets past a thousand dirty files. Caught in
    /// the first screenshot of the real fleet: every pid in OTHER AGENTS had a
    /// decimal point in it. A pid is an identifier, not a quantity, and neither
    /// is a commit count or a dirty count in a mono column.
    private var gitLine: some View {
        HStack(spacing: Space.sm) {
            Text(card.git.branch)
                .font(OrcFont.code)
                .foregroundStyle(Palette.statusFree)
                .lineLimit(1)
                .truncationMode(.middle)
            if card.git.dirty > 0 {
                Text(verbatim: "Δ\(card.git.dirty)")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusLimit)
                    .accessibilityLabel("\(card.git.dirty) uncommitted")
            }
            // **Omitted ENTIRELY when there is no upstream.** `ahead` is null,
            // not zero — 2 of 9 live worktrees — and `↑null` is not a thing. A
            // zero drawn here would be a measurement this client did not make.
            if card.git.hasUpstream, let ahead = card.git.ahead, let behind = card.git.behind,
               ahead > 0 || behind > 0 {
                HStack(spacing: Space.xxs) {
                    if ahead > 0 {
                        Label { Text(verbatim: "\(ahead)") } icon: {
                            Image(systemName: StatusStyle.Mark.ahead)
                        }
                        .accessibilityLabel("\(ahead) ahead")
                    }
                    if behind > 0 {
                        Label { Text(verbatim: "\(behind)") } icon: {
                            Image(systemName: StatusStyle.Mark.behind)
                        }
                        .accessibilityLabel("\(behind) behind")
                    }
                }
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
            }
            Spacer(minLength: 0)
        }
    }

    private var resumeLine: some View {
        HStack(spacing: Space.xs) {
            Image(systemName: "timer").accessibilityHidden(true)
            Text(resumes.compactMap(\.due).min().map {
                "auto-resume armed · \(RelativeTime.clock($0)) (\(RelativeTime.countdown(to: $0, now: now)))"
            } ?? "auto-resume armed")
            .lineLimit(1)
        }
        .font(OrcFont.meta)
        .foregroundStyle(Palette.statusWorking)
    }

    private var overflow: some View {
        HStack {
            Text(verbatim: "+\(hiddenCount) more session\(hiddenCount == 1 ? "" : "s")")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
            Spacer()
            if card.endedCount > 0 {
                Text(verbatim: "\(card.endedCount) ended")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textDisabled)
            }
        }
        .padding(.horizontal, Space.md)
        .padding(.vertical, Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.sunkenDim)
    }
}
