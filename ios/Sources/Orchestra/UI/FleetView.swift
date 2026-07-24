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
    @Bindable private var topology: TopologyStore
    @Bindable private var router: PushRouter
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
    /// A notification deep link waiting for the board to load enough to resolve
    /// the session's account. Held rather than dropped — see `tryNavigate`.
    @State private var pendingLink: PushDeepLink?
    @State private var collapsed: Set<BoardSection> = Set(
        BoardSection.allCases.filter(\.collapsedByDefault)
    )
    /// The board order held while a finger is on the grid. `nil` renders the live
    /// order; a snapshot means an interaction is in progress and the order is
    /// frozen to what was on screen when it began — so a card cannot re-sort out
    /// from under a tap (`UX.md` §4.1's hold rule). `boardTouched` is the finger,
    /// `boardScrolling` the momentum tail after it lifts; the order is applied
    /// once both are quiet and a short settle has passed. `holdToken` bumps on
    /// every interaction transition and drives that settle from `.task(id:)`.
    @State private var heldGroups: [Triage.Group]?
    @State private var boardTouched = false
    @State private var boardScrolling = false
    @State private var holdToken = 0
    /// A destination to push once, on appear. Nil in every shipping path; it is
    /// how a script reaches a screen a simulator cannot be tapped into.
    private let initialRoute: FleetRoute?

    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(store: FleetStore, actions: ActionsStore, limits: LimitsStore,
                topology: TopologyStore, router: PushRouter,
                client: OrchestraClient, serverLabel: String,
                initialRoute: FleetRoute? = nil, openComposer: Bool = false,
                initialSheet: WorktreeSheet? = nil, initialSend: String? = nil,
                onUnpair: @escaping () -> Void) {
        self.store = store
        self.actions = actions
        self.limits = limits
        self.topology = topology
        self.router = router
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
                ToolbarItem(placement: .topBarLeading) {
                    NavigationLink(value: FleetRoute.map) {
                        Image(systemName: "arrow.triangle.branch")
                    }
                    .accessibilityLabel("branch map")
                }
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
                case .map:
                    BranchMapView(store: topology, board: boardJoin,
                                  boardWorktrees: store.state?.worktrees.map(\.name) ?? []) { name in
                        path.append(.worktree(name))
                    }
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
            // A tap that arrived before this tab was mounted is waiting in the
            // router; take it now so a cold launch from a notification still
            // lands on the session, not the board.
            if let link = router.consume() { pendingLink = link }
            tryNavigate()
        }
        // A tap that arrives while the app is running. `generation` bumps even
        // when two taps name the SAME session, which a plain `onChange(of: link)`
        // would swallow.
        .onChange(of: router.generation) { _, _ in
            if let link = router.consume() { pendingLink = link }
            tryNavigate()
        }
        // The board arriving is what completes a chat deep link: a tap on a cold
        // launch reaches here before the first frame, so the account cannot be
        // resolved yet. Retrying when the version moves is what makes the tap
        // land on the CONVERSATION rather than settling for the worktree.
        .onChange(of: store.version) { _, _ in tryNavigate() }
    }

    /// Resolve the pending notification deep link against the board, completing
    /// the address the payload could not carry.
    ///
    /// The payload names a worktree and a session but **no account** — it never
    /// had one — so the account the chat screen needs is looked up from the live
    /// board by sid, HERE, where the board is in hand. While the board is still
    /// loading the link is HELD (not discarded), and `onChange(of: store.version)`
    /// retries — so a cold-launch tap lands on the conversation once the frame
    /// arrives. Only once the board has loaded and the session is genuinely
    /// absent does it settle for the worktree. An account-level event with no
    /// worktree leaves the board up.
    private func tryNavigate() {
        guard let link = pendingLink else { return }
        if link.isBoardOnly || (link.worktree ?? "").isEmpty {
            path = []
            pendingLink = nil
            return
        }
        let name = link.worktree ?? ""
        guard let sid = link.sid, !sid.isEmpty else {
            path = [.worktree(name)]
            pendingLink = nil
            return
        }
        if let card = store.state?.worktrees.first(where: { $0.name == name }),
           let session = card.sessions.first(where: { $0.sid == sid }) {
            path = [.worktree(name), .chat(worktree: name, account: session.account, sid: sid)]
            pendingLink = nil
        } else if store.state != nil {
            // The board is loaded and this session is not on it — land on the
            // worktree, the best surviving context for what the notification was
            // about, rather than waiting for a session that will not appear.
            path = [.worktree(name)]
            pendingLink = nil
        }
        // else: board not loaded yet — hold the link and let the version change
        // retry.
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

    /// What the board renders: the frozen snapshot while an interaction is in
    /// progress, the live sectioned board otherwise. The comparator is untouched —
    /// this only defers *applying* a reorder while a finger is down.
    private var displayedGroups: [Triage.Group] {
        heldGroups ?? store.groups
    }

    /// Snapshot the current order the instant an interaction begins, once — later
    /// frames must not slide cards while the snapshot stands.
    private func freezeOrder() {
        if heldGroups == nil { heldGroups = store.groups }
    }

    private var board: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Space.md, pinnedViews: [.sectionHeaders]) {
                headline
                if staleness.isStale, let error = store.lastError {
                    // The board stayed on screen; say WHY it is not moving.
                    StaleBanner(error: error, since: store.lastFrameAt ?? store.lastGoodAt, now: now)
                }
                ForEach(displayedGroups) { group in
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
        // Hold the board order while a finger is down and through the momentum
        // that follows, then apply whatever the store now holds. Freezing on the
        // first touch is what keeps a tap on the card the user aimed at
        // (`UX.md` §4.1). `DragGesture(minimumDistance: 0)` in a
        // `.simultaneousGesture` reads touch-began/ended without swallowing the
        // row taps, the scroll, or the pull-to-refresh; `onScrollPhaseChange`
        // carries the hold across the coast after the finger lifts. Every
        // transition bumps `holdToken`, which re-arms the settle below.
        .simultaneousGesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    guard !boardTouched else { return }
                    boardTouched = true
                    freezeOrder()
                    holdToken &+= 1
                }
                .onEnded { _ in
                    boardTouched = false
                    holdToken &+= 1
                }
        )
        .onScrollPhaseChange { _, newPhase in
            boardScrolling = newPhase != .idle
            if boardScrolling { freezeOrder() }
            holdToken &+= 1
        }
        // The settle. Re-armed on every interaction transition (`holdToken`), it
        // applies the live order only once the finger is up and the list is at
        // rest, after a 700 ms window that also outlasts the tap being consumed
        // (`UX.md` §4.1). A new touch bumps the token and cancels this run.
        .task(id: holdToken) {
            guard !boardTouched, !boardScrolling, heldGroups != nil else { return }
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard !Task.isCancelled, !boardTouched, !boardScrolling else { return }
            heldGroups = nil
        }
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

    /// The board → map join, by worktree name. Computed here because this view
    /// already holds the live board; the map takes it as a value so a status
    /// change recolours a tip with no topology fetch (§5.11).
    private var boardJoin: [String: MapBoardInfo] {
        var out: [String: MapBoardInfo] = [:]
        for card in store.state?.worktrees ?? [] {
            out[card.name] = MapBoardInfo(
                section: Triage.section(for: card),
                sessionCount: card.sessions.count,
                working: card.sessions.contains { $0.status == .working })
        }
        return out
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
