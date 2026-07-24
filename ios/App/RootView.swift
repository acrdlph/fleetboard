import SwiftUI

/// Paired or not. There is no third state worth a screen: with no token there is
/// nothing to fetch and no request worth making.
struct RootView: View {
    @Bindable var model: AppModel
    @State private var tab = 0

    /// The device-owner check in front of the paired app. Held here, at the one
    /// place the board is revealed, so every tab and every mutation lives behind
    /// a single gate. See `BiometricGate` — it is advisory (threat T10); the real
    /// controls are server-side.
    @State private var gate = BiometricGate()
    @Environment(\.scenePhase) private var scenePhase

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

    private var initialShowSettings: Bool {
        #if DEBUG
        return DebugRoute.fromEnvironment()?.showsNotificationSettings ?? false
        #else
        return false
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
                // The board — and with it every mutation (dispatch / send /
                // finish / resume, all reachable only from inside these tabs) —
                // is behind a successful device-owner check. Nothing paired is
                // shown or tappable until `gate.isUnlocked`.
                if gate.isUnlocked {
                    paired
                } else {
                    LockView(gate: gate)
                        // Cold-launch prompt. `authenticateIfNeeded` only fires
                        // from `locked`, so this is a no-op once unlocked or after
                        // a cancel, and cannot race the foreground re-prompt below.
                        .task { await gate.authenticateIfNeeded() }
                }
            } else {
                PairingScreen(store: model.pairing)
            }
        }
        .background(Palette.canvas)
        // Re-lock when the phone leaves the foreground, so a device left unlocked
        // and handed to someone else must re-authenticate on return. The trigger
        // is `.background`, **not `.inactive`** — the app switcher, Control Center
        // and a notification banner must not force a re-prompt. This mirrors the
        // stream-teardown rule in `AppModel.scenePhaseChanged`.
        .onChange(of: scenePhase) { _, phase in
            guard model.pairing.isPaired else { return }
            switch phase {
            case .background:
                gate.lock()
            case .active:
                Task { await gate.authenticateIfNeeded() }
            default:
                break
            }
        }
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
                          router: model.router,
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
                ServerView(fleet: model.fleet, profile: model.pairing.profile,
                           push: model.push,
                           initialShowSettings: initialShowSettings) {
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
            model.ensurePushStarted()
            #if DEBUG
            if let route = DebugRoute.fromEnvironment() { tab = route.tab }
            #endif
        }
        // A notification tap deposits a deep link and bumps the router's
        // generation. Selecting the Fleet tab here — and resolving the exact
        // session in `FleetView` — is what makes a tap land on the agent it is
        // about rather than on whatever tab was last open.
        .onChange(of: model.router.generation) { _, _ in
            if model.router.pendingDeepLink != nil { tab = 0 }
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

/// The "unlock to continue" state, shown in place of the whole paired app while
/// `BiometricGate` is anything but `unlocked`.
///
/// It always offers an explicit button — the system sheet can be dismissed, an
/// evaluation can be cancelled, and a screen with no way forward is a trap. On a
/// failure it names what happened without blaming the user and lets them retry.
/// The footnote is deliberately modest: this lock lives on the phone, and it says
/// so, because the controls that actually hold the fleet are on the Mac (T10).
private struct LockView: View {
    let gate: BiometricGate

    var body: some View {
        ZStack {
            Palette.canvas.ignoresSafeArea()
            BodyWash()
            VStack(spacing: Space.lg) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 40, weight: .semibold))
                    .foregroundStyle(Palette.statusFree)
                    .accessibilityHidden(true)

                VStack(spacing: Space.sm) {
                    Text("orchestra is locked")
                        .font(OrcFont.title)
                        .foregroundStyle(Palette.textPrimary)
                    Text("Unlock with Face ID, Touch ID, or your passcode to reach the fleet.")
                        .font(OrcFont.bodyCompact)
                        .foregroundStyle(Palette.textSecondary)
                        .multilineTextAlignment(.center)
                }

                if case .failed(let reason) = gate.phase {
                    Text(reason)
                        .font(OrcFont.status)
                        .foregroundStyle(Palette.statusNeeds)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: .infinity)
                        .padding(Space.md)
                        .background(
                            RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                                .stroke(Palette.statusNeeds.opacity(0.5), lineWidth: 1)
                        )
                }

                unlockButton

                Text("This lock lives on this phone. What guards the fleet is on your "
                     + "Mac — a per-device token you can revoke at any time.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                    .multilineTextAlignment(.center)
            }
            .padding(Space.xl)
            .frame(maxWidth: 420)
        }
    }

    private var unlockButton: some View {
        Button {
            Task { await gate.authenticate() }
        } label: {
            Text(buttonTitle)
                .font(OrcFont.button)
                .frame(maxWidth: .infinity, minHeight: 44)
        }
        .foregroundStyle(Palette.statusWorking)
        .background(Palette.statusWorking.opacity(0.12))
        .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(Palette.statusWorking, lineWidth: 1)
        )
        .disabled(gate.isAuthenticating)
    }

    private var buttonTitle: String {
        switch gate.phase {
        case .authenticating: return "unlocking…"
        case .failed: return "Try again"
        default: return "Unlock"
        }
    }
}
