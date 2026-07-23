import SwiftUI

/// What the map needs about each worktree from the LIVE board, joined by name.
///
/// The topology payload has no status, no session count, and no liveness — those
/// ride the state stream (§5.11). This is the join: computed from `FleetStore` in
/// `FleetView` and handed in, so the map recolours on a board frame without ever
/// re-fetching topology.
public struct MapBoardInfo: Sendable, Equatable {
    public let section: BoardSection
    public let sessionCount: Int
    public let working: Bool
    public init(section: BoardSection, sessionCount: Int, working: Bool) {
        self.section = section
        self.sessionCount = sessionCount
        self.working = working
    }
}

/// The branch map — `UX.md` §5.2's pick: ranked full-width rows, one fork strip
/// each. Chosen over a pan/zoom canvas because hover does not exist, horizontal
/// space is gone, and every target must be 44 pt. One row per worktree eliminates
/// the desktop's label-collision problem entirely; the drawing's one job is to
/// show the CLUSTER STRUCTURE of divergence times, which a column of `144d/5d/2h`
/// labels does not show without arithmetic.
public struct BranchMapView: View {
    @Bindable private var store: TopologyStore
    /// The board join, by worktree name. Read live so tips recolour on a frame.
    private let board: [String: MapBoardInfo]
    /// Every worktree the board knows — to name the ones topology dropped.
    private let boardWorktrees: [String]
    private let onOpenWorktree: (String) -> Void

    @State private var sort: BranchMap.Sort = .status
    @State private var range: BranchMap.Range = .month
    @State private var selected: TopoBranch?
    @State private var collapsedParked: Set<String> = []
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(store: TopologyStore, board: [String: MapBoardInfo],
                boardWorktrees: [String], onOpenWorktree: @escaping (String) -> Void) {
        self.store = store
        self.board = board
        self.boardWorktrees = boardWorktrees
        self.onOpenWorktree = onOpenWorktree
    }

    public var body: some View {
        ZStack {
            Palette.canvas.ignoresSafeArea()
            content
        }
        .navigationTitle("Branches")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) { sortMenu }
            ToolbarItem(placement: .topBarTrailing) { rangeMenu }
        }
        .sheet(item: $selected) { branch in
            MapDetailSheet(branch: branch, group: group(of: branch),
                           info: board[branch.worktree], now: now) {
                selected = nil
                onOpenWorktree(branch.worktree)
            }
            .presentationDetents([.large])
            .preferredColorScheme(.dark)
        }
        .onReceive(ticker) { now = $0 }
        .task { store.load() }
    }

    @ViewBuilder
    private var content: some View {
        switch store.phase {
        case .cold, .loading:
            if store.topology == nil { MapSkeleton() } else { list }
        case .loaded:
            list
        case .failed(let error):
            FailureView(error: error) { Task { await store.refresh() } }
        }
    }

    private var sortMenu: some View {
        Menu {
            Picker("Sort", selection: $sort) {
                ForEach(BranchMap.Sort.allCases) { Text($0.label).tag($0) }
            }
        } label: {
            Label("sort", systemImage: "arrow.up.arrow.down")
        }
    }

    private var rangeMenu: some View {
        Menu {
            Picker("Range", selection: $range) {
                ForEach(BranchMap.Range.allCases) { Text($0.label).tag($0) }
            }
        } label: { Text(range.label).font(OrcFont.status) }
    }

    private func group(of branch: TopoBranch) -> TopoGroup? {
        store.topology?.groups.first { $0.branches.contains { $0.worktree == branch.worktree } }
    }

    private var sections: [String: BoardSection] {
        board.mapValues(\.section)
    }

    @ViewBuilder
    private var list: some View {
        let groups = store.topology?.groups ?? []
        if groups.isEmpty {
            MapEmpty { onOpenWorktreeNil() }
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                    if let stale = staleBanner { stale }
                    ForEach(groups) { grp in
                        groupView(grp)
                    }
                    let dropped = store.unmapped(boardWorktrees: boardWorktrees)
                    if !dropped.isEmpty { MapDroppedFooter(names: dropped) }
                    Color.clear.frame(height: Space.xxl)
                }
            }
            .scrollIndicators(.hidden)
            .refreshable { await store.refresh() }
        }
    }

    private func groupView(_ grp: TopoGroup) -> some View {
        let axis = BranchMap.axis(for: grp, now: now.timeIntervalSince1970)
        let placed = BranchMap.place(grp, sections: sections, sort: sort,
                                     range: range, now: now.timeIntervalSince1970)
        return SwiftUI.Section {
            ForEach(placed.main) { row in
                rowButton(row, grp: grp, axis: axis)
            }
            if !placed.stalled.isEmpty {
                MiniHeader(title: "STALLED", count: placed.stalled.count, hue: Palette.statusLimit)
                ForEach(placed.stalled) { row in
                    rowButton(row, grp: grp, axis: axis)
                }
            }
            if !placed.parked.isEmpty {
                parkedSection(placed.parked)
            }
        } header: {
            MapGroupHeader(group: grp, now: now)
        }
    }

    private func rowButton(_ row: BranchMap.Row, grp: TopoGroup, axis: BranchMap.AxisScale) -> some View {
        Button { selected = row.branch } label: {
            MapRowView(row: row, trunkTs: grp.trunkTs, axis: axis,
                       info: board[row.branch.worktree], now: now)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, Space.lg)
    }

    @ViewBuilder
    private func parkedSection(_ rows: [BranchMap.Row]) -> some View {
        MiniHeader(title: "PARKED AT TIP", count: rows.count, hue: Palette.textTertiary)
        // Collapsed to chips — these are at the trunk tip, clean, and idle. One
        // 44 pt strip of names, tap a name to open its sheet.
        FlowChips(rows: rows) { selected = $0 }
            .padding(.horizontal, Space.lg)
            .padding(.bottom, Space.sm)
    }

    private var staleBanner: MapStaleBanner? {
        guard let loadedAt = store.loadedAt else { return nil }
        let age = now.timeIntervalSince(loadedAt)
        // The map ages truthfully — tip positions are computed against the device
        // clock — so this only warns that behind/dirty/status may have moved.
        return age > 120 ? MapStaleBanner(since: loadedAt, now: now) : nil
    }

    private func onOpenWorktreeNil() {}
}

// MARK: - The sticky group header (§5.2)

struct MapGroupHeader: View {
    let group: TopoGroup
    let now: Date

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(spacing: Space.sm) {
                Text(group.repo.uppercased())
                    .font(OrcFont.cardName)
                    .foregroundStyle(Palette.textPrimary)
                Text(verbatim: "· \(group.base) ·")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusFree)
                Text(verbatim: "tip \(RelativeTime.short(since: group.trunk, now: now)) ago")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                Spacer(minLength: 0)
            }
            Rectangle().fill(Palette.hairline).frame(height: 1)
        }
        .padding(.horizontal, Space.lg)
        .padding(.top, Space.md)
        .padding(.bottom, Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.canvas)
    }
}

// MARK: - One row (§5.4)

struct MapRowView: View {
    let row: BranchMap.Row
    let trunkTs: Double
    let axis: BranchMap.AxisScale
    let info: MapBoardInfo?
    let now: Date

    private var branch: TopoBranch { row.branch }
    private var hue: Color { info.map { StatusStyle.of($0.section).hue } ?? Palette.textTertiary }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            // Line 1 — name · dots · status pill · debt chip
            HStack(alignment: .firstTextBaseline, spacing: Space.sm) {
                Text(branch.worktree)
                    .font(OrcFont.cardName)
                    .foregroundStyle(Palette.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if let dots = multiplicityDots {
                    Text(dots)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                }
                Spacer(minLength: Space.xs)
                if let section = info?.section {
                    StatusPill(section).layoutPriority(1)
                }
                debtChip.layoutPriority(1)
            }

            // Line 2 — the commit subject, the only text the drawing cannot express
            Text(TextTruncation.clip(branch.subject, to: 72))
                .font(OrcFont.codeSm)
                .foregroundStyle(Palette.textSecondary)
                .lineLimit(2)

            // Line 3 — branch · +ahead · Δdirty
            HStack(spacing: Space.sm) {
                if branch.isDetached {
                    Label("detached · \(branch.ahead) unmerged", systemImage: "exclamationmark.triangle")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusLimit)
                        .labelStyle(.titleAndIcon)
                } else {
                    Text(branch.branch)
                        .font(OrcFont.code)
                        .foregroundStyle(Palette.statusFree)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                if branch.ahead > 0 {
                    Text(verbatim: "+\(branch.ahead)")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                }
                if branch.dirty > 0 {
                    Text(verbatim: "Δ\(branch.dirty)")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusLimit)
                }
                Spacer(minLength: 0)
            }

            if row.role == .stale {
                Label("stale checkout", systemImage: "bolt.horizontal")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }

            // The strip — 20 pt band plus its label
            ForkStrip(branch: branch, trunkTs: trunkTs, axis: axis, hue: hue,
                      working: info?.working ?? false)
                .padding(.top, Space.xxs)
        }
        .padding(.vertical, Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(.rect)
        .overlay(alignment: .bottom) {
            Rectangle().fill(Palette.hairline).frame(height: 0.5)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(a11y)
    }

    private var multiplicityDots: String? {
        guard let n = info?.sessionCount, n > 0 else { return nil }
        return n > 4 ? "···· 4+" : String(repeating: "·", count: n)
    }

    @ViewBuilder
    private var debtChip: some View {
        if let tier = row.tier {
            Text(verbatim: "↓\(branch.behind)")
                .font(OrcFont.status)
                .foregroundStyle(tierHue(tier))
        }
    }

    private func tierHue(_ tier: BranchMap.DebtTier) -> Color {
        switch tier {
        case .low: Palette.textDisabled
        case .moderate: Palette.statusLimit
        case .high: Palette.statusTurn
        case .severe: Palette.statusNeeds
        }
    }

    private var a11y: String {
        var parts = [branch.worktree, info?.section.badge ?? ""]
        if branch.behind > 0 { parts.append("\(branch.behind) behind") }
        if branch.ahead > 0 { parts.append("\(branch.ahead) ahead") }
        parts.append("forked \(RelativeTime.short(axis.now - branch.forkTs)) ago")
        parts.append(branch.subject)
        return parts.filter { !$0.isEmpty }.joined(separator: ", ")
    }
}

// MARK: - Small parts

/// A trailing sub-section header inside a group — `STALLED · 1`, `PARKED AT TIP · 2`.
struct MiniHeader: View {
    let title: String
    let count: Int
    let hue: Color
    var body: some View {
        HStack(spacing: Space.sm) {
            Text(verbatim: "\(title) · \(count)")
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(hue)
            Rectangle().fill(Palette.hairline).frame(height: 1)
        }
        .padding(.horizontal, Space.lg)
        .padding(.top, Space.md)
        .padding(.bottom, Space.xs)
    }
}

/// Parked worktrees, collapsed to tappable name chips.
struct FlowChips: View {
    let rows: [BranchMap.Row]
    let onTap: (TopoBranch) -> Void
    var body: some View {
        WrapHStack(spacing: Space.sm) {
            ForEach(rows) { row in
                Button { onTap(row.branch) } label: {
                    Text(row.branch.worktree)
                        .font(OrcFont.code)
                        .foregroundStyle(Palette.textSecondary)
                        .padding(.horizontal, Space.sm)
                        .padding(.vertical, Space.xs)
                        .frame(minHeight: 32)
                        .background(
                            RoundedRectangle(cornerRadius: Radius.xs, style: .continuous)
                                .fill(Palette.raised)
                        )
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// A minimal wrapping HStack — chips flow to the next line when they run out of
/// width. Enough for a handful of parked names; not a general layout engine.
struct WrapHStack<Content: View>: View {
    let spacing: CGFloat
    @ViewBuilder let content: () -> Content
    var body: some View {
        // `Layout` would be the general answer; for <= a dozen chips a simple
        // flexible wrap via `.fixedSize` inside an HStack that can grow is enough,
        // but SwiftUI has no built-in flow, so we lean on a lazy grid that wraps.
        FlowLayout(spacing: spacing) { content() }
    }
}

/// A tiny flow layout (iOS 16 `Layout`). Places subviews left to right, wrapping.
struct FlowLayout: Layout {
    var spacing: CGFloat = 8

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let maxWidth = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0
        for sub in subviews {
            let s = sub.sizeThatFits(.unspecified)
            if x + s.width > maxWidth, x > 0 { x = 0; y += rowH + spacing; rowH = 0 }
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
        return CGSize(width: maxWidth == .infinity ? x : maxWidth, height: y + rowH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, rowH: CGFloat = 0
        for sub in subviews {
            let s = sub.sizeThatFits(.unspecified)
            if x + s.width > bounds.maxX, x > bounds.minX { x = bounds.minX; y += rowH + spacing; rowH = 0 }
            sub.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing
            rowH = max(rowH, s.height)
        }
    }
}

struct MapStaleBanner: View {
    let since: Date
    let now: Date
    var body: some View {
        HStack(spacing: Space.sm) {
            Image(systemName: "exclamationmark.triangle").accessibilityHidden(true)
            Text(verbatim: "map data \(RelativeTime.short(since: since, now: now)) old · pull to refresh")
                .font(OrcFont.meta)
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.statusLimit)
        .padding(Space.sm)
        .padding(.horizontal, Space.md)
    }
}

struct MapDroppedFooter: View {
    let names: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            Text(verbatim: "\(names.count) worktree\(names.count == 1 ? "" : "s") can't be placed")
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(Palette.textTertiary)
            Text(names.joined(separator: " · "))
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textDisabled)
            Text("no trunk ref, no merge-base, or bad timestamps — the board still works")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textDisabled)
        }
        .padding(Space.lg)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct MapEmpty: View {
    let openBoard: () -> Void
    var body: some View {
        VStack(spacing: Space.md) {
            Image(systemName: "arrow.triangle.branch")
                .font(.system(size: 36))
                .foregroundStyle(Palette.textTertiary)
            Text("no repos to map")
                .font(OrcFont.title)
                .foregroundStyle(Palette.textPrimary)
            Text("orchestra found worktrees, but none has a trunk ref (origin/HEAD, origin/main, main, master). The board still works.")
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
                .multilineTextAlignment(.center)
        }
        .padding(Space.xl)
    }
}

struct MapSkeleton: View {
    @State private var breathing = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        VStack(alignment: .leading, spacing: Space.md) {
            ForEach(0..<4, id: \.self) { _ in
                RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .fill(Palette.surface)
                    .frame(height: 92)
            }
            Spacer()
        }
        .padding(Space.lg)
        .opacity(reduceMotion ? 1 : (breathing ? 0.55 : 1))
        .animation(reduceMotion ? nil : .easeInOut(duration: 1.1).repeatForever(autoreverses: true),
                   value: breathing)
        .onAppear { breathing = true }
        .accessibilityLabel("loading the branch map")
    }
}
