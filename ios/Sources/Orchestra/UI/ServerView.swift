import SwiftUI

/// Where the Server tab can navigate. One destination today — the notification
/// preferences — as a value so a `#if DEBUG` seam can push it without a tap.
enum ServerRoute: Hashable, Sendable {
    case notifications
}

/// "Am I connected, and to what?"
///
/// On the desktop you can see the server's log and the terminals; on a phone
/// this tab is the only place that question has an answer. It is deliberately
/// diagnostic rather than decorative — every number here is one somebody would
/// otherwise have to ask for in a bug report.
public struct ServerView: View {
    @Bindable private var fleet: FleetStore
    @Bindable private var push: PushStore
    private let profile: ServerProfile?
    private let onUnpair: () -> Void
    /// Push the notifications screen on appear. The same `#if DEBUG` seam as the
    /// Fleet tab's `initialRoute`: a simulator cannot tap the row, and a screen
    /// that is only ever reached by a finger is a screen the phase gate cannot
    /// look at.
    private let initialShowSettings: Bool

    @State private var path: [ServerRoute] = []
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(fleet: FleetStore, profile: ServerProfile?, push: PushStore,
                initialShowSettings: Bool = false,
                onUnpair: @escaping () -> Void) {
        self.fleet = fleet
        self.push = push
        self.profile = profile
        self.initialShowSettings = initialShowSettings
        self.onUnpair = onUnpair
    }

    public var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                ScrollView {
                    VStack(alignment: .leading, spacing: Space.lg) {
                        machine
                        notifications
                        stream
                        freshness
                        Button("Unpair this device", role: .destructive, action: onUnpair)
                            .font(OrcFont.button)
                            .foregroundStyle(Palette.statusNeeds)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                                .stroke(Palette.statusNeeds.opacity(0.5), lineWidth: 1))
                        Color.clear.frame(height: Space.xxl)
                    }
                    .padding(.horizontal, Space.lg)
                    .padding(.top, Space.sm)
                }
                .scrollIndicators(.hidden)
            }
            .navigationTitle("server")
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: ServerRoute.self) { route in
                switch route {
                case .notifications: NotificationSettingsView(push: push)
                }
            }
        }
        .onReceive(ticker) { now = $0 }
        .task {
            if initialShowSettings, path.isEmpty { path = [.notifications] }
        }
    }

    /// A summary of the push pipeline, and the door to the preferences. Every
    /// line is diagnostic: whether the OS let us notify, whether this device has
    /// a token on file, and what the server's last send returned — the three
    /// facts that turn "push just doesn't work" into a specific missing piece.
    private var notifications: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel("NOTIFICATIONS")
            VStack(spacing: Space.xs) {
                Row("permission", authLabel, hue: authHue)
                Row("this device", registrationLabel, hue: registrationHue)
                if let status = push.status {
                    Row("pipeline", status.ready ? "ready"
                        : (status.problems.first ?? status.backend),
                        hue: status.ready ? Palette.statusWorking : Palette.statusLimit)
                    if let last = status.last, !last.isEmpty {
                        Row("last send", last)
                    }
                }
                NavigationLink(value: ServerRoute.notifications) {
                    HStack(spacing: Space.sm) {
                        Text("Preferences & quiet hours")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textSecondary)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textTertiary)
                    }
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(.rect)
                }
                .buttonStyle(.plain)
            }
            .padding(Space.md)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.surface)
            .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1))
        }
    }

    private var authLabel: String {
        switch push.authorizationGranted {
        case .some(true): "granted"
        case .some(false): "denied — enable in iOS Settings"
        case .none: push.authorizationAsked ? "—" : "not asked yet"
        }
    }

    private var authHue: Color? {
        switch push.authorizationGranted {
        case .some(true): Palette.statusWorking
        case .some(false): Palette.statusNeeds
        case .none: nil
        }
    }

    private var registrationLabel: String {
        switch push.registration {
        case .idle: "not registered"
        case .registering: "registering…"
        case .registered(let env, let warnings):
            warnings.isEmpty ? "registered (\(env))" : "registered (\(env)) — \(warnings.count) warning(s)"
        case .failed(let why): why
        }
    }

    private var registrationHue: Color? {
        switch push.registration {
        case .registered(_, let w): w.isEmpty ? Palette.statusWorking : Palette.statusLimit
        case .failed: Palette.statusNeeds
        default: nil
        }
    }

    private var machine: some View {
        Block("MACHINE") {
            Row("host", profile.map { "\($0.host):\($0.port)" } ?? "—")
            Row("name", fleet.state?.hostname ?? profile?.hostname ?? "—")
            Row("user", fleet.state?.user ?? "—")
            Row("device", profile?.deviceID ?? "—")
            // ADR 0013: plain HTTP over the tailnet, deliberately. WireGuard
            // already gives mutual authentication and confidentiality between
            // devices; TLS on top would secure a channel that is already secure,
            // at the cost of a trust store on the phone that fails closed.
            Row("transport", "http over tailscale (ADR 0013)")
        }
    }

    private var stream: some View {
        Block("STREAM") {
            Row("state", fleet.link.caption, hue: fleet.link.isLive ? Palette.statusWorking
                                                                    : Palette.statusLimit)
            Row("version", fleet.version.map { "v\($0)" } ?? "—")
            Row("frames applied", "\(fleet.framesApplied)")
            Row("resyncs", "\(fleet.resyncs)",
                hue: fleet.resyncs > 0 ? Palette.statusLimit : nil)
            if let token = fleet.lastTokenAt {
                Row("last byte", RelativeTime.short(since: token, now: now) + " ago")
            }
            if let frame = fleet.lastFrameAt {
                Row("last frame", RelativeTime.short(since: frame, now: now) + " ago")
            }
            // Never silent. A frame this build cannot read is a server change
            // nobody told the client about, and it belongs on a screen rather
            // than in a `catch {}`.
            if fleet.decodeFaults > 0 {
                Row("decode faults", "\(fleet.decodeFaults)", hue: Palette.statusNeeds)
                if let fault = fleet.lastDecodeFault {
                    Text(fault)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusNeeds)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            if fleet.unknownStatuses > 0 {
                Row("unknown statuses", "\(fleet.unknownStatuses)", hue: Palette.statusLimit)
            }
        }
    }

    /// Per-tier probe ages, which is what makes "the board is fresh but its git
    /// is 40 s old" sayable at all. They ride every frame and move with NO
    /// version bump, so they can never be the cause of a stale card — which is
    /// exactly why they belong here and not on a card.
    private var freshness: some View {
        Block("PROBE AGES") {
            tier("worktrees", fleet.freshness.worktrees)
            tier("processes", fleet.freshness.procs)
            tier("transcripts", fleet.freshness.transcripts)
            tier("git", fleet.freshness.git)
        }
    }

    @ViewBuilder
    private func tier(_ name: String, _ at: Double?) -> some View {
        if let at {
            let age = now.timeIntervalSince(Date(timeIntervalSince1970: at))
            Row(name, RelativeTime.short(age) + " ago",
                hue: age > 120 ? Palette.statusLimit : nil)
        } else {
            Row(name, "—")
        }
    }
}

struct Block<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel(title)
            VStack(spacing: Space.xs) { content }
                .padding(Space.md)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.surface)
                .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                    .stroke(Palette.hairline, lineWidth: 1))
        }
    }
}

struct Row: View {
    let key: String
    let value: String
    var hue: Color?

    init(_ key: String, _ value: String, hue: Color? = nil) {
        self.key = key
        self.value = value
        self.hue = hue
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.md) {
            Text(key)
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
            Spacer(minLength: Space.sm)
            // `Text(verbatim:)` on the value, always: `Text("\(n)")` resolves to
            // the LocalizedStringKey overload and formats an interpolated integer
            // through the locale — which is how every pid on the phase-1 board
            // came out as `34.115`.
            Text(verbatim: value)
                .font(OrcFont.meta)
                .foregroundStyle(hue ?? Palette.textSecondary)
                .multilineTextAlignment(.trailing)
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}
