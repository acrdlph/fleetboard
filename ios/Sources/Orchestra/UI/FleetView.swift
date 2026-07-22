import SwiftUI

/// The Fleet board. Answer "who needs me" in under a second.
///
/// Phase 1 fetches `/api/state` once and on pull-to-refresh; the stream is the
/// next phase. The 1 s ticker is here already because ages are a client-side
/// animation by design — `age_s` left the wire so the payload is time-invariant,
/// and the phone is what makes it move.
public struct FleetView: View {
    @Bindable private var store: FleetStore
    private let serverLabel: String
    private let onUnpair: () -> Void

    /// Ticks the ages. Mutating a `Date` that only feeds `Text` is cheap; what
    /// would not be cheap is anything that changes a row's SIZE on this tick,
    /// which is why the age slot has a fixed minimum width.
    @State private var now = Date()
    @State private var collapsed: Set<BoardSection> = Set(
        BoardSection.allCases.filter(\.collapsedByDefault)
    )

    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(store: FleetStore, serverLabel: String, onUnpair: @escaping () -> Void) {
        self.store = store
        self.serverLabel = serverLabel
        self.onUnpair = onUnpair
    }

    public var body: some View {
        NavigationStack {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                BodyWash()
                content
            }
            .navigationTitle("orchestra")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("Refresh") { Task { await store.refresh() } }
                        Button("Unpair this device", role: .destructive, action: onUnpair)
                        SwiftUI.Section("Server") { Text(serverLabel) }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                }
            }
        }
        .task { await store.refresh() }
        .onReceive(ticker) { now = $0 }
    }

    @ViewBuilder
    private var content: some View {
        switch store.phase {
        case .cold, .loading:
            if store.state == nil {
                // The ONLY state that may show a skeleton. Never a spinner over
                // blank, and never a skeleton over data that already loaded.
                SkeletonBoard()
            } else {
                board
            }
        case .loaded:
            board
        case .failed(let error):
            FailureView(error: error) { Task { await store.refresh() } }
        }
    }

    private var board: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Space.md, pinnedViews: [.sectionHeaders]) {
                headline
                if let error = store.lastError {
                    // The board stayed on screen; say WHY it is not moving.
                    StaleBanner(error: error, since: store.lastGoodAt, now: now)
                }
                ForEach(store.groups) { group in
                    SwiftUI.Section {
                        if !collapsed.contains(group.section) {
                            ForEach(group.cards) { card in
                                WorktreeCardView(card: card,
                                                 section: group.section,
                                                 now: now,
                                                 resumes: resumes(for: card))
                            }
                        }
                    } header: {
                        header(group)
                    }
                }
                if let others = store.state?.otherProcs, !others.isEmpty {
                    otherAgents(others)
                }
                Color.clear.frame(height: Space.xxl)
            }
            .padding(.horizontal, Space.lg)
        }
        .scrollIndicators(.hidden)
        .refreshable { await store.refresh() }
    }

    private var headline: some View {
        let h = store.headline
        return VStack(alignment: .leading, spacing: Space.xxs) {
            Text(h.text)
                .font(OrcFont.display)
                .foregroundStyle(StatusStyle.of(h.tone).hue)
                .dynamicTypeSize(...DynamicTypeSize.accessibility3)
            if !h.subhead.isEmpty {
                Text(h.subhead)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            if store.unknownStatuses > 0 {
                Text(verbatim: "\(store.unknownStatuses) session(s) reported a status this build "
                     + "does not know")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusLimit)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.top, Space.sm)
    }

    private func header(_ group: Triage.Group) -> some View {
        Button {
            if collapsed.contains(group.section) {
                collapsed.remove(group.section)
            } else {
                collapsed.insert(group.section)
            }
        } label: {
            HStack(spacing: Space.sm) {
                Text(group.section.title)
                    .font(OrcFont.label)
                    .orcTracking(11)
                    .foregroundStyle(StatusStyle.of(group.section).hue)
                Text(verbatim: "\(group.cards.count)")
                    .font(OrcFont.label)
                    .foregroundStyle(Palette.textTertiary)
                Rectangle()
                    .fill(Palette.hairline)
                    .frame(height: 1)
                Image(systemName: collapsed.contains(group.section) ? "chevron.down" : "chevron.up")
                    .font(OrcFont.label)
                    .foregroundStyle(Palette.textTertiary)
            }
            .padding(.vertical, Space.sm)
            .frame(minHeight: 44)          // participates in layout, deliberately
            .background(Palette.canvas)
            .contentShape(.rect)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(group.section.title), \(group.cards.count) worktrees")
        .accessibilityHint(collapsed.contains(group.section) ? "expand" : "collapse")
    }

    private func otherAgents(_ procs: [OtherProc]) -> some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            Text(verbatim: "OTHER AGENTS \(procs.count)")
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(Palette.textTertiary)
            ForEach(procs) { proc in
                HStack(spacing: Space.sm) {
                    Image(systemName: StatusStyle.Mark.pid)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                    Text(verbatim: "\(proc.pid)")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                    Text(proc.cwd ?? "—")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textDisabled)
                        .lineLimit(1)
                        .truncationMode(.head)
                    Spacer(minLength: 0)
                    Text(proc.etime)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                }
            }
        }
        .padding(Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1)
        )
        .padding(.top, Space.sm)
    }

    /// `resumes` is keyed `"{worktree}|{sid}"` with a literal pipe.
    private func resumes(for card: Worktree) -> [ResumeSchedule] {
        (store.state?.resumes ?? [:]).values.filter {
            $0.worktree == card.name && $0.status == "pending"
        }
    }
}

/// Three breathing sections. Never a spinner over blank — a spinner says
/// "working" and a skeleton says "this is where the board will be".
struct SkeletonBoard: View {
    @State private var breathing = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: Space.md) {
            ForEach(0..<3, id: \.self) { _ in
                RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                    .fill(Palette.surface)
                    .frame(height: 110)
                    .overlay(
                        RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                            .stroke(Palette.hairline, lineWidth: 1)
                    )
            }
            Spacer()
        }
        .padding(Space.lg)
        .opacity(reduceMotion ? 1 : (breathing ? 0.55 : 1))
        .animation(reduceMotion ? nil : .easeInOut(duration: 1.1).repeatForever(autoreverses: true),
                   value: breathing)
        .onAppear { breathing = true }
        .accessibilityLabel("loading the fleet")
    }
}

/// The five transport failures, each with the action that fixes it. One spinner
/// for all of them is the thing this screen exists not to be.
struct FailureView: View {
    let error: OrchestraError
    let retry: () -> Void

    var body: some View {
        VStack(spacing: Space.md) {
            Image(systemName: symbol)
                .font(.system(size: 40))
                .foregroundStyle(Palette.statusNeeds)
            Text(error.headline)
                .font(OrcFont.title)
                .foregroundStyle(Palette.textPrimary)
            Text(error.guidance)
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
                .multilineTextAlignment(.center)
            Button("Try again", action: retry)
                .font(OrcFont.button)
                .foregroundStyle(Palette.statusFree)
                .padding(.horizontal, Space.lg)
                .padding(.vertical, Space.sm)
                .frame(minHeight: 44)
                .overlay(
                    RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                        .stroke(Palette.controlStrong, lineWidth: 1)
                )
        }
        .padding(Space.xl)
    }

    private var symbol: String {
        switch error {
        case .offline: "wifi.slash"
        case .tailnetDown: "network.slash"
        case .macUnreachable: "moon.zzz"
        case .serverStopped: "bolt.slash"
        case .transportBlocked: "hand.raised.slash"
        case .unauthorized, .forbidden: "lock"
        default: "exclamationmark.triangle"
        }
    }
}

/// The board is still on screen and is no longer being updated. Say which, and
/// say how old.
struct StaleBanner: View {
    let error: OrchestraError
    let since: Date?
    let now: Date

    var body: some View {
        HStack(spacing: Space.sm) {
            Image(systemName: "exclamationmark.triangle")
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(error.headline)
                    .font(OrcFont.status)
                if let since {
                    Text(verbatim: "board is \(RelativeTime.short(since: since, now: now)) old")
                        .font(OrcFont.meta)
                }
            }
            Spacer(minLength: 0)
        }
        .foregroundStyle(Palette.statusLimit)
        .padding(Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(Palette.statusLimit.opacity(0.5), lineWidth: 1)
        )
    }
}
