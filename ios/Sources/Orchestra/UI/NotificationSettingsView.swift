import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// The per-event, quiet-hours, privacy and nudge preferences — every one of
/// which is SERVER state, because a delivered push cannot be filtered on the
/// phone (the payload is already on the lock screen). The screen edits a local
/// copy and POSTs the whole set; the server is the one place it is true.
///
/// **There is no route that returns a device's stored preferences** — `GET
/// /api/v1/push/status` answers the pipeline's health and a `registered` bool,
/// nothing about rules or quiet hours (reported in `ios/README.md`). So this
/// opens on the app's local mirror of the last save, not on a server read, and
/// says as much in one line rather than pretending the two are guaranteed to
/// agree.
public struct NotificationSettingsView: View {
    @Bindable private var push: PushStore
    @State private var working: PushSettings
    @State private var quietFrom: Date
    @State private var quietTo: Date
    @State private var saveError: String?
    @State private var testResult: String?
    @State private var busy = false
    @Environment(\.openURL) private var openURL

    public init(push: PushStore) {
        self.push = push
        let settings = push.settings
        _working = State(initialValue: settings)
        _quietFrom = State(initialValue: Self.date(from: settings.quietHours.from))
        _quietTo = State(initialValue: Self.date(from: settings.quietHours.to))
    }

    public var body: some View {
        ZStack {
            Palette.canvas.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: Space.lg) {
                    if push.authorizationGranted == false {
                        deniedBanner
                    }
                    events
                    quietHours
                    delivery
                    nudge
                    diagnostics
                    Color.clear.frame(height: Space.xxl)
                }
                .padding(.horizontal, Space.lg)
                .padding(.top, Space.sm)
            }
            .scrollIndicators(.hidden)
        }
        .navigationTitle("notifications")
        .navigationBarTitleDisplayMode(.inline)
        .tint(Palette.statusFree)
    }

    // MARK: - denied

    private var deniedBanner: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            Text("Notifications are turned off for orchestra")
                .font(OrcFont.status)
                .foregroundStyle(Palette.statusNeeds)
            Text("These preferences are saved to the server, but nothing will "
                 + "reach this phone until you allow notifications in iOS Settings.")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textSecondary)
            #if canImport(UIKit)
            Button("Open iOS Settings") {
                if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
            }
            .font(OrcFont.button)
            .foregroundStyle(Palette.statusFree)
            .frame(minHeight: 44)
            #endif
        }
        .padding(Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
            .stroke(Palette.statusNeeds.opacity(0.5), lineWidth: 1))
    }

    // MARK: - events

    private var events: some View {
        SettingsBlock("WHAT TO NOTIFY", footer:
            "Off by default: your turn, auto-resume armed, worktree freed — the "
            + "quiet ones. A tap on a question you can answer is what this is for.") {
            ForEach(PushEventType.allCases) { type in
                Toggle(isOn: binding(for: type)) {
                    HStack(spacing: Space.sm) {
                        Text(type.label)
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textPrimary)
                        Text(type.level)
                            .font(OrcFont.label)
                            .foregroundStyle(Palette.textTertiary)
                    }
                }
                .tint(Palette.statusWorking)
                .frame(minHeight: 40)
            }
        }
    }

    private func binding(for type: PushEventType) -> Binding<Bool> {
        Binding(
            get: { working.isOn(type) },
            set: { working.set(type, on: $0); commit() }
        )
    }

    // MARK: - quiet hours

    private var quietHours: some View {
        SettingsBlock("QUIET HOURS", footer:
            "Evaluated in this phone's timezone. With P1 allowed, a blocked agent "
            + "still reaches you through the quiet window — the 2 a.m. case this exists for.") {
            Toggle(isOn: Binding(
                get: { working.quietHours.enabled },
                set: { working.quietHours.enabled = $0; commit() })) {
                Text("Silence non-urgent notifications at night")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textPrimary)
            }
            .tint(Palette.statusWorking)
            .frame(minHeight: 40)

            if working.quietHours.enabled {
                DatePicker("From", selection: $quietFrom, displayedComponents: .hourAndMinute)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textSecondary)
                    .onChange(of: quietFrom) { _, new in
                        working.quietHours.from = Self.string(from: new); commit()
                    }
                DatePicker("To", selection: $quietTo, displayedComponents: .hourAndMinute)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textSecondary)
                    .onChange(of: quietTo) { _, new in
                        working.quietHours.to = Self.string(from: new); commit()
                    }
                Toggle(isOn: Binding(
                    get: { working.quietHours.allowP1 },
                    set: { working.quietHours.allowP1 = $0; commit() })) {
                    Text("Let urgent (P1) through anyway")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textPrimary)
                }
                .tint(Palette.statusWorking)
                .frame(minHeight: 40)
            }
        }
    }

    // MARK: - delivery / privacy

    private var delivery: some View {
        SettingsBlock("ON THE LOCK SCREEN", footer: working.privacy.explanation) {
            Picker("", selection: Binding(
                get: { working.privacy },
                set: { working.privacy = $0; commit() })) {
                ForEach(PushPrivacy.allCases, id: \.self) { p in
                    Text(p.label).tag(p)
                }
            }
            .pickerStyle(.segmented)
        }
    }

    // MARK: - nudge

    private var nudge: some View {
        SettingsBlock("STALLED CLOSEOUT", footer:
            "How long a closeout waits before it nudges the agent again.") {
            Stepper(value: Binding(
                get: { working.nudgeMin },
                set: { working.nudgeMin = $0; commit() }), in: 5...60, step: 5) {
                Text(verbatim: "Nudge after \(working.nudgeMin) min")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textPrimary)
            }
            .frame(minHeight: 40)
        }
    }

    // MARK: - diagnostics / actions

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            SectionLabel("PROVE IT")
            VStack(spacing: Space.sm) {
                Button {
                    busy = true
                    Task { testResult = await push.sendTest(); busy = false }
                } label: {
                    Text(busy ? "sending…" : "Send a test notification")
                        .font(OrcFont.button)
                        .foregroundStyle(Palette.statusFree)
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .disabled(busy)
                .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .stroke(Palette.controlStrong, lineWidth: 1))

                Button {
                    Task { _ = await push.mute(minutes: 60) }
                } label: {
                    Text("Mute all for 1 hour")
                        .font(OrcFont.button)
                        .foregroundStyle(Palette.textSecondary)
                        .frame(maxWidth: .infinity, minHeight: 44)
                }
                .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .stroke(Palette.hairline, lineWidth: 1))

                if let until = push.mutedUntil, until > Date() {
                    Text(verbatim: "muted until \(Self.clock(until))")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusLimit)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let result = testResult {
                    // The server's own words — including the `403
                    // InvalidProviderToken` that a working transport with no
                    // registered key correctly returns. Shown verbatim.
                    Text(result)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textSecondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                if let error = saveError {
                    Text(verbatim: "save failed: \(error)")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusNeeds)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    // MARK: - save

    /// Every edit POSTs the whole preference set. The server merges and echoes;
    /// a refusal surfaces in `saveError` rather than being swallowed.
    private func commit() {
        Task { saveError = await push.save(working) }
    }

    // MARK: - time helpers

    private static func date(from hhmm: String) -> Date {
        let parts = hhmm.split(separator: ":").compactMap { Int($0) }
        var c = DateComponents()
        c.hour = parts.count == 2 ? parts[0] : 23
        c.minute = parts.count == 2 ? parts[1] : 0
        return Calendar.current.date(from: c) ?? Date()
    }

    private static func string(from date: Date) -> String {
        let c = Calendar.current.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", c.hour ?? 0, c.minute ?? 0)
    }

    private static func clock(_ date: Date) -> String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: date)
    }
}

/// A titled card with an optional footer caption, matching `ServerView`'s
/// `Block` but with the explanatory line these settings need.
private struct SettingsBlock<Content: View>: View {
    let title: String
    let footer: String?
    @ViewBuilder let content: Content

    init(_ title: String, footer: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.footer = footer
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel(title)
            VStack(alignment: .leading, spacing: Space.xs) { content }
                .padding(Space.md)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Palette.surface)
                .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                    .stroke(Palette.hairline, lineWidth: 1))
            if let footer {
                Text(footer)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                    .padding(.horizontal, Space.xs)
            }
        }
    }
}
