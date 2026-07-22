import SwiftUI

/// The Fleet board. Answer "who needs me" in under a second.
///
/// It is **told**, not asked: `FleetStore` holds one `GET /api/events` socket
/// and this view renders whatever the last frame left behind. The 1 s ticker is
/// not a poll and never was — `age_s` left the wire (step 5 phase 5) so the
/// payload is time-invariant end to end, and the phone is what makes every "12s
/// ago" move. That is why a board can sit for an hour with no traffic and still
/// animate correctly, and it is also why a frozen age is a real symptom rather
/// than a cosmetic one.
public struct FleetView: View {
    @Bindable private var store: FleetStore
    @Bindable private var actions: ActionsStore
    @Bindable private var limits: LimitsStore
    private let client: OrchestraClient
    private let serverLabel: String
    private let onUnpair: () -> Void
    /// Open the mission composer on appear. Same seam as `initialRoute`, and it
    /// exists for the same reason: a simulator cannot be tapped from a script.
    private let openComposer: Bool
    /// A sheet for the initially-pushed worktree, and text to send from the
    /// initially-pushed chat. Both nil in every shipping path.
    private let initialSheet: WorktreeSheet?
    private let initialSend: String?
    @State private var composing = false

    /// Ticks the ages. Mutating a `Date` that only feeds `Text` is cheap; what
    /// would not be cheap is anything that changes a row's SIZE on this tick,
    /// which is why the age slot has a fixed minimum width.
    @State private var now = Date()
    @State private var path: [FleetRoute] = []
    @State private var collapsed: Set<BoardSection> = Set(
        BoardSection.allCases.filter(\.collapsedByDefault)
    )
    /// A destination to push once, on appear. Nil in every shipping path; it is
    /// how a script reaches a screen a simulator cannot be tapped into.
    private let initialRoute: FleetRoute?

    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(store: FleetStore, actions: ActionsStore, limits: LimitsStore,
                client: OrchestraClient, serverLabel: String,
                initialRoute: FleetRoute? = nil, openComposer: Bool = false,
                initialSheet: WorktreeSheet? = nil, initialSend: String? = nil,
                onUnpair: @escaping () -> Void) {
        self.store = store
        self.actions = actions
        self.limits = limits
        self.client = client
        self.serverLabel = serverLabel
        self.initialRoute = initialRoute
        self.openComposer = openComposer
        self.initialSheet = initialSheet
        self.initialSend = initialSend
        self.onUnpair = onUnpair
    }

    private var staleness: Staleness { store.staleness(now: now) }

    public var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                BodyWash()
                content
            }
            .navigationTitle("orchestra")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    // The one control on the board that spends money, and it is
                    // one tap from a confirmation, never from a launch.
                    Button { composing = true } label: {
                        Image(systemName: "plus.circle")
                    }
                    .accessibilityLabel("new mission")
                }
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
            .navigationDestination(for: FleetRoute.self) { route in
                switch route {
                case .worktree(let name):
                    WorktreeDetailView(name: name, store: store, actions: actions,
                                       client: client, initialSheet: initialSheet)
                case .chat(let worktree, let account, let sid):
                    ChatView(worktree: worktree, account: account, sid: sid,
                             store: store, client: client, autoSend: initialSend)
                }
            }
            .sheet(isPresented: $composing) {
                MissionComposer(fleet: store, limits: limits, actions: actions)
            }
        }
        // One environment write, and every status pill on every screen below
        // reads it. A dimming rule threaded by hand through four initialisers is
        // a rule that gets applied to three of them.
        .environment(\.boardIsStale, staleness.isStale)
        .onReceive(ticker) { now = $0 }
        .task {
            if let initialRoute, path.isEmpty { path = [initialRoute] }
            if openComposer { composing = true }
        }
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
                if staleness.isStale, let error = store.lastError {
                    // The board stayed on screen; say WHY it is not moving.
                    StaleBanner(error: error, since: store.lastFrameAt ?? store.lastGoodAt, now: now)
                }
                ForEach(store.groups) { group in
                    SwiftUI.Section {
                        if !collapsed.contains(group.section) {
                            ForEach(group.cards) { card in
                                NavigationLink(value: FleetRoute.worktree(card.name)) {
                                    WorktreeCardView(card: card,
                                                     section: group.section,
                                                     now: now,
                                                     resumes: resumes(for: card))
                                }
                                .buttonStyle(.plain)
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
