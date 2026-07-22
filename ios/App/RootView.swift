import SwiftUI

/// Paired or not. There is no third state worth a screen: with no token there is
/// nothing to fetch and no request worth making.
struct RootView: View {
    @Bindable var model: AppModel
    @State private var tab = 0

    /// Nil in every shipping build. See `DebugRoute`.
    private var initialFleetRoute: FleetRoute? {
        #if DEBUG
        return DebugRoute.fromEnvironment()?.fleetRoute
        #else
        return nil
        #endif
    }

    private var initialAccount: String? {
        #if DEBUG
        return DebugRoute.fromEnvironment()?.accountSlug
        #else
        return nil
        #endif
    }

    /// `ORC_SCREEN=mission` opens the composer on launch — the only way a script
    /// on a simulator can reach a sheet, and therefore the only way phase 3's
    /// most dangerous screen can be looked at without a finger.
    private var initialComposer: Bool {
        #if DEBUG
        return DebugRoute.fromEnvironment() == .mission
        #else
        return false
        #endif
    }

    private var initialWorktreeSheet: WorktreeSheet? {
        #if DEBUG
        return DebugRoute.fromEnvironment()?.worktreeSheet
        #else
        return nil
        #endif
    }

    /// `ORC_SEND=<text>` types one message at the session `ORC_SCREEN=chat:…`
    /// names, through the same `ChatStore.send` the arrow button calls. It exists
    /// because the gate for this phase is *something actually arrived at a real
    /// agent*, and a simulator cannot be typed into from a script.
    private var initialSend: String? {
        #if DEBUG
        let text = ProcessInfo.processInfo.environment["ORC_SEND"]
        return (text?.isEmpty ?? true) ? nil : text
        #else
        return nil
        #endif
    }

    var body: some View {
        Group {
            if model.pairing.isPaired {
                paired
            } else {
                PairingScreen(store: model.pairing)
            }
        }
        .background(Palette.canvas)
        .onOpenURL { url in
            // The pairing QR is `orc://p?h=…&p=…&c=…`, so scanning it with the
            // SYSTEM camera opens the app straight here. Same ticket, same
            // claim path, no second implementation.
            //
            // It only fires when the phone is UNPAIRED. A link is something
            // anything on the phone can hand us, and "already paired" is the
            // state where accepting one silently would replace a working server
            // with one somebody else chose. Unpaired, the worst a bad link can
            // do is claim a code it does not have.
            guard !model.pairing.isPaired,
                  let ticket = PairingTicket(url: url.absoluteString) else { return }
            Task { await model.pairing.pair(with: ticket, label: AppModel.deviceLabel) }
        }
    }

    /// **Three tabs, not `UX.md` §2.1's four.**
    ///
    /// `Activity` — "what is in flight, and what happened while I was away" — is
    /// the one tab whose content this build cannot produce. Its rows are
    /// dispatches, closeouts and intents; `GET /api/dispatchlog` returns
    /// `{"entries": []}` on this fleet, and the intent stream `UX.md` §3.4 draws
    /// from (`event: intent` frames carrying a `phase`) does not exist in
    /// `server._stream`, which writes `event: state` and nothing else. A tab
    /// spending 25 % of the permanent navigation budget on an empty list would
    /// be worse than its absence — and the two things it could say today, armed
    /// auto-resumes and probe ages, are on the worktree and server screens where
    /// they are already in context.
    private var paired: some View {
        TabView(selection: $tab) {
            Tab("Fleet", systemImage: "square.grid.2x2", value: 0) {
                FleetView(store: model.fleet,
                          actions: model.actions,
                          limits: model.limits,
                          topology: model.topology,
                          client: model.client,
                          serverLabel: model.pairing.profile?.display ?? "—",
                          initialRoute: initialFleetRoute,
                          openComposer: initialComposer,
                          initialSheet: initialWorktreeSheet,
                          initialSend: initialSend) {
                    Task { await model.unpair() }
                }
                .connectionBar(model.fleet)
            }
            Tab("Limits", systemImage: "gauge.with.dots.needle.33percent", value: 1) {
                LimitsView(store: model.limits, initialAccount: initialAccount)
                    .connectionBar(model.fleet)
            }
            Tab("Server", systemImage: "bolt.horizontal", value: 2) {
                ServerView(fleet: model.fleet, profile: model.pairing.profile) {
                    Task { await model.unpair() }
                }
                .connectionBar(model.fleet)
            }
        }
        .tint(Palette.statusFree)
        // Here rather than in `AppModel.start()`, because pairing can happen
        // AFTER launch: this view appears the moment a token exists, and that is
        // the moment the stream should open.
        .task {
            model.fleet.start()
            #if DEBUG
            if let route = DebugRoute.fromEnvironment() { tab = route.tab }
            #endif
        }
    }
}

extension View {
    /// The connection strip, pinned above the tab bar on every tab.
    ///
    /// It rides a `safeAreaInset` rather than sitting inside each screen's
    /// scroll view for one concrete reason: it must not scroll away. A board
    /// four minutes old that says so at the top of a list the user has already
    /// scrolled past is a board that says nothing.
    func connectionBar(_ store: FleetStore) -> some View {
        modifier(ConnectionBarModifier(store: store))
    }
}

private struct ConnectionBarModifier: ViewModifier {
    @Bindable var store: FleetStore
    @State private var now = Date()
    /// Measured, not assumed — see `EnvironmentValues.bottomAccessoryHeight`.
    /// The bar grows a second line on a stale board, so a constant here would be
    /// wrong exactly when the board is worth reading.
    @State private var barHeight: CGFloat = 0
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    func body(content: Content) -> some View {
        content
            .safeAreaInset(edge: .bottom, spacing: 0) {
                ConnectionBar(link: store.link,
                              staleness: store.staleness(now: now),
                              version: store.version) {
                    Task { await store.refresh() }
                }
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: {
                    barHeight = $0
                }
            }
            .environment(\.bottomAccessoryHeight, barHeight)
            .onReceive(ticker) { now = $0 }
    }
}
