import SwiftUI

/// One branch, opened from a tap (§5.9). `.large` only — `.medium` cannot hold
/// this at default type and a scrolling sheet at `.medium` fights its own
/// drag-to-resize.
///
/// **The action model, stated honestly.** §5.9 draws `✉ chat` and `✓ finish`
/// buttons here. Both are the board's, addressed by a session — which the
/// topology payload does not carry. Rather than duplicate the finish state
/// machine or guess a session, this sheet's single action is *Open on board*,
/// which pushes the full worktree detail where chat and the two-step finish
/// already live, unchanged. One implementation, reached from the map.
struct MapDetailSheet: View {
    let branch: TopoBranch
    let group: TopoGroup?
    let info: MapBoardInfo?
    let now: Date
    let openBoard: () -> Void

    private var axis: BranchMap.AxisScale? {
        group.map { BranchMap.axis(for: $0, now: now.timeIntervalSince1970) }
    }
    private var hue: Color { info.map { StatusStyle.of($0.section).hue } ?? Palette.textTertiary }
    private var role: BranchMap.Role { BranchMap.role(branch) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.lg) {
                header
                Divider().overlay(Palette.hairline)
                commit
                Divider().overlay(Palette.hairline)
                stats
                if let axis, let group {
                    Divider().overlay(Palette.hairline)
                    ForkStrip(branch: branch, trunkTs: group.trunkTs, axis: axis,
                              hue: hue, working: info?.working ?? false)
                        .frame(height: 40)
                    axisLegend
                }
                Divider().overlay(Palette.hairline)
                openButton
            }
            .padding(Space.lg)
        }
        .background(Palette.canvas)
        .presentationBackground(Palette.canvas)
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: Space.xs) {
                Text(branch.worktree)
                    .font(OrcFont.title)
                    .foregroundStyle(Palette.textPrimary)
                if branch.isDetached {
                    Label("detached HEAD · \(branch.ahead) unmerged", systemImage: "exclamationmark.triangle")
                        .font(OrcFont.code)
                        .foregroundStyle(Palette.statusLimit)
                } else {
                    Text(branch.branch)
                        .font(OrcFont.code)
                        .foregroundStyle(Palette.statusFree)
                }
            }
            Spacer(minLength: 0)
            if let section = info?.section { StatusPill(section) }
        }
    }

    private var commit: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .top, spacing: Space.sm) {
                Text(branch.hash)
                    .font(OrcFont.codeSm)
                    .foregroundStyle(Palette.statusTurn)
                Text(branch.subject)
                    .font(OrcFont.codeSm)
                    .foregroundStyle(Palette.textSecondary)
            }
            Text(forkedLine)
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
        }
    }

    private var forkedLine: String {
        var s = "committed \(RelativeTime.short(since: branch.tip, now: now)) ago"
        s += " · forked \(RelativeTime.short(now.timeIntervalSince1970 - branch.forkTs)) ago"
        if let base = group?.base { s += " from \(base)" }
        return s
    }

    private var stats: some View {
        HStack(spacing: 0) {
            stat("+\(branch.ahead)", "ahead", Palette.textPrimary)
            stat("↓\(branch.behind)", "behind",
                 BranchMap.debtTier(branch.behind).map(tierHue) ?? Palette.textTertiary)
            stat("Δ\(branch.dirty)", "uncommitted",
                 branch.dirty > 0 ? Palette.statusLimit : Palette.textTertiary)
        }
    }

    private func stat(_ value: String, _ label: String, _ hue: Color) -> some View {
        VStack(spacing: Space.xxs) {
            Text(value).font(OrcFont.cardName).foregroundStyle(hue)
            Text(label).font(OrcFont.meta).foregroundStyle(Palette.textTertiary)
        }
        .frame(maxWidth: .infinity)
    }

    private var axisLegend: some View {
        HStack {
            Text(verbatim: "\(RelativeTime.short(now.timeIntervalSince1970 - branch.forkTs)) ago")
                .font(OrcFont.meta).foregroundStyle(Palette.textDisabled)
            Spacer()
            if role == .stale {
                Text("at the trunk's old tip — a dispatch here starts \(branch.behind) commits behind")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusLimit)
                    .multilineTextAlignment(.center)
                Spacer()
            }
            Text("now").font(OrcFont.meta).foregroundStyle(Palette.textDisabled)
        }
    }

    private var openButton: some View {
        Button(action: openBoard) {
            HStack {
                Text("open on board")
                Spacer()
                Image(systemName: "chevron.right")
            }
            .font(OrcFont.button)
            .foregroundStyle(Palette.statusFree)
            .padding(Space.md)
            .frame(minHeight: 48)
            .frame(maxWidth: .infinity)
            .overlay(
                RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .stroke(Palette.controlStrong, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
    }

    private func tierHue(_ tier: BranchMap.DebtTier) -> Color {
        switch tier {
        case .low: Palette.textDisabled
        case .moderate: Palette.statusLimit
        case .high: Palette.statusTurn
        case .severe: Palette.statusNeeds
        }
    }
}
