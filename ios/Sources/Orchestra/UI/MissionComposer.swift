import SwiftUI

/// The mission composer — the one screen in this app that spends money.
///
/// Three properties it is built around, all of them the server's:
///
/// 1. **Model and effort have no default and the server refuses to guess one.**
///    `start_dispatch` answers *"pick a model and an effort first — routing is
///    deterministic, nothing is chosen for you"* to any dispatch missing either.
///    So Launch stays disabled until both are set, and the disabled reason is
///    shown inline rather than only as a dimmed button — the app never sends a
///    request it already knows will bounce.
/// 2. **Placement is the server's.** `_pick_defaults` picks the cleanest free
///    worktree and the account with the most headroom for the chosen model.
///    `Auto` here sends `nil` and lets it; this client deliberately does **not**
///    mirror the picker, because `exclude_accounts` is set on the author's machine
///    and is exposed by no endpoint, so a mirror would name an account the server
///    will never choose and every dispatch would show a "picked X" correction.
/// 3. **There is no idempotency key and no way to add one** — see `Actuation`. So
///    the Launch button in the confirmation disables on tap and never re-enables,
///    and a timeout is rendered as "did it launch?", never as "failed".
public struct MissionComposer: View {
    @Bindable private var fleet: FleetStore
    @Bindable private var limits: LimitsStore
    @Bindable private var actions: ActionsStore
    @Environment(\.dismiss) private var dismiss

    @State private var mission = ""
    @State private var worktree: String?      // nil == Auto
    @State private var account: String?       // nil == Auto
    @State private var model: String?         // no default, by design
    @State private var effort: String?        // no default, by design
    @State private var confirming = false
    @State private var forcing: DispatchRefusal?

    /// The four the desktop offers, which are the four `claude --model` takes.
    static let models = ["fable", "opus", "sonnet", "haiku"]
    /// The desktop's own effort list, with its own descriptions.
    static let efforts: [(String, String)] = [
        ("high", "simple task"),
        ("xhigh", "research / medium"),
        ("max", "hard feature"),
        ("ultracode", "hard feature · long-running"),
    ]

    public init(fleet: FleetStore, limits: LimitsStore, actions: ActionsStore) {
        self.fleet = fleet
        self.limits = limits
        self.actions = actions
    }

    private var run: ActionsStore.DispatchRun? { actions.dispatch }

    public var body: some View {
        NavigationStack {
            Group {
                if let run {
                    DispatchProgressView(run: run) {
                        actions.clearDispatch()
                        dismiss()
                    } reopenDraft: {
                        actions.clearDispatch()
                    }
                } else {
                    editor
                }
            }
            .background(Palette.canvas.ignoresSafeArea())
            .navigationTitle(run == nil ? "New mission" : "Launching")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .foregroundStyle(Palette.textSecondary)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Launch") { confirming = true }
                        .foregroundStyle(canLaunch ? Palette.statusNeeds : Palette.textDisabled)
                        .disabled(!canLaunch)
                }
            }
            .task { if limits.report == nil { await limits.load() } }
        }
        .sheet(isPresented: $confirming) {
            LaunchConfirmSheet(mission: mission, worktree: worktree, account: account,
                               model: model ?? "", effort: effort ?? "",
                               accountLimits: chosenAccountLimits) {
                confirming = false
                actions.launch(mission: WireText.collapsed(mission),
                               worktree: worktree, account: account,
                               model: model ?? "", effort: effort ?? "",
                               forceModel: false)
            } cancel: {
                confirming = false
            }
        }
        .onChange(of: forcingCandidate) { _, refusal in
            forcing = refusal
        }
        .sheet(item: $forcing) { refusal in
            ForceModelSheet(refusal: refusal) {
                actions.clearDispatch()
                forcing = nil
                actions.launch(mission: WireText.collapsed(mission),
                               worktree: worktree, account: account,
                               model: model ?? "", effort: effort ?? "",
                               forceModel: true)
            } useOpus: {
                actions.clearDispatch()
                forcing = nil
                model = "opus"
                actions.launch(mission: WireText.collapsed(mission),
                               worktree: worktree, account: refusal.opusAccount,
                               model: "opus", effort: effort ?? "", forceModel: false)
            } cancel: {
                actions.clearDispatch()
                forcing = nil
            }
        }
    }

    /// The headroom dialog, promoted out of the run into its own sheet — at a
    /// **different detent** from the launch confirm, so muscle memory drilled on
    /// "Launch" cannot land on "use it anyway" (`UX.md` §7.3 rule 3).
    private var forcingCandidate: DispatchRefusal? {
        guard let run, case .refused(let refusal) = run.phase,
              refusal.needsDecision else { return nil }
        return refusal
    }

    private var canLaunch: Bool {
        !WireText.collapsed(mission).isEmpty && model != nil && effort != nil
            && actions.dispatch == nil
    }

    private var disabledReason: String? {
        if actions.dispatch != nil { return "a mission is already launching" }
        if WireText.collapsed(mission).isEmpty { return "the mission is empty" }
        if model == nil && effort == nil { return "pick a model and an effort" }
        if model == nil { return "pick a model" }
        if effort == nil { return "pick an effort" }
        return nil
    }

    private var chosenAccountLimits: AccountLimits? {
        guard let account else { return nil }
        // Join on `fb_label`, NEVER on `slug` — they differ routinely
        // (`slug: "default"` is `fb_label: "main"` on this fleet).
        return limits.report?.accounts.first { $0.label == account }
    }

    // MARK: - Editor

    private var editor: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                TextEditor(text: $mission)
                    .font(OrcFont.body)
                    .foregroundStyle(Palette.textPrimary)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 180)
                    .padding(Space.sm)
                    .background(Palette.sunken)
                    .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                        .stroke(Palette.control, lineWidth: 1))
                    .overlay(alignment: .topLeading) {
                        if mission.isEmpty {
                            Text("what should the agent do?")
                                .font(OrcFont.body)
                                .foregroundStyle(Palette.textDisabled)
                                .padding(Space.md)
                                .allowsHitTesting(false)
                        }
                    }
                HStack {
                    Spacer()
                    Text(verbatim: "\(mission.count) ch")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textDisabled)
                }

                pickers
                if let disabledReason {
                    Text(verbatim: "Launch is off: " + disabledReason)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusLimit)
                }
                Text("placement is deterministic — the server picks the cleanest free "
                     + "worktree and the account with the most headroom. Model and "
                     + "effort are your call; nothing guesses difficulty.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            .padding(Space.lg)
        }
    }

    @ViewBuilder
    private var pickers: some View {
        VStack(spacing: 0) {
            pickerRow("Worktree", value: worktree ?? "Auto") {
                Button("Auto — the server picks") { worktree = nil }
                // The free list is the server's own `free_worktrees`, which is a
                // pure function of the cards. It is not re-derived here.
                ForEach(fleet.state?.freeWorktrees ?? [], id: \.self) { name in
                    Button(name) { worktree = name }
                }
            }
            Divider().overlay(Palette.hairline)
            pickerRow("Account", value: account ?? "Auto") {
                Button("Auto — most headroom") { account = nil }
                ForEach(limits.report?.ranked ?? []) { item in
                    Button(accountLabel(item)) { account = item.label }
                }
            }
            Divider().overlay(Palette.hairline)
            pickerRow("Model", value: model ?? "— pick one —",
                      missing: model == nil) {
                ForEach(Self.models, id: \.self) { name in
                    Button(name) { model = name }
                }
            }
            Divider().overlay(Palette.hairline)
            pickerRow("Effort", value: effort ?? "— pick one —",
                      missing: effort == nil) {
                ForEach(Self.efforts, id: \.0) { name, note in
                    Button("\(name) — \(note)") { effort = name }
                }
            }
        }
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
            .stroke(Palette.hairline, lineWidth: 1))
    }

    private func accountLabel(_ item: AccountLimits) -> String {
        var label = item.label
        if let headroom = item.headroomPercent {
            label += " · \(Int(headroom.rounded()))% left"
        }
        if item.accountExhausted { label += " · exhausted" }
        else if item.reserveBlocked { label += " · below reserve" }
        return label
    }

    private func pickerRow<Content: View>(_ title: String, value: String,
                                          missing: Bool = false,
                                          @ViewBuilder menu: () -> Content) -> some View {
        Menu {
            menu()
        } label: {
            HStack {
                Text(title)
                    .font(OrcFont.bodyCompact)
                    .foregroundStyle(Palette.textSecondary)
                Spacer()
                Text(verbatim: value)
                    .font(OrcFont.meta)
                    .foregroundStyle(missing ? Palette.statusLimit : Palette.textPrimary)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            .padding(.horizontal, Space.md)
            .frame(minHeight: 48)
        }
    }
}

extension DispatchRefusal: Identifiable {
    public var id: String { (message ?? "") + (model ?? "") }
}

/// The launch confirmation. `.height(SheetHeight.launch)`, Cancel bottom-most,
/// 24 pt of dead space between the two.
struct LaunchConfirmSheet: View {
    let mission: String
    let worktree: String?
    let account: String?
    let model: String
    let effort: String
    let accountLimits: AccountLimits?
    let launch: () -> Void
    let cancel: () -> Void

    /// Disables on tap and does not re-enable — see `PrimaryAction`.
    @State private var fired = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                SheetHeader("Launch this mission?", symbol: "bolt.horizontal",
                            hue: Palette.statusNeeds)
                VStack(alignment: .leading, spacing: Space.xs) {
                    ConsequenceRow(worktree ?? "Auto — the cleanest free worktree",
                                   arrow: "folder", hue: Palette.statusFree)
                    ConsequenceRow(account.map { "[\($0)]" } ?? "Auto — most headroom",
                                   detail: accountLimits?.headroomPercent
                                       .map { "\(Int($0.rounded()))% left" },
                                   arrow: "person.crop.circle", hue: Palette.statusFree)
                    ConsequenceRow("\(model) · effort \(effort)", arrow: "cpu",
                                   hue: Palette.textPrimary)
                }
                Text("Spends that account's usage. The agent runs with "
                     + "--dangerously-skip-permissions and can run commands, "
                     + "commit and push.")
                    .font(OrcFont.bodyCompact)
                    .foregroundStyle(Palette.textSecondary)
                // The honest disclosure this server forces: there is no
                // idempotency key, so there is no undo and no safe retry.
                Text("There is no double-fire guard on the server: a second launch "
                     + "in the same worktree starts a second agent. This button "
                     + "fires once and does not come back.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusLimit)
                Text(TextTruncation.clip(WireText.collapsed(mission), to: 220))
                    .font(OrcFont.codeSm)
                    .foregroundStyle(Palette.textTertiary)

                PrimaryAction("Launch mission", symbol: "bolt.fill",
                              tint: Palette.statusNeeds, enabled: !fired) {
                    fired = true
                    launch()
                }
                ConsequenceGap()
                CancelAction(action: cancel)
            }
            .padding(Space.lg)
        }
        .background(Palette.surface.ignoresSafeArea())
        .presentationDetents([.height(SheetHeight.launch), .large])
        .presentationDragIndicator(.visible)
    }
}

/// `needs_decision` — the reserve dialog, at its own detent.
///
/// The server's message names the number (*"best is [work] at 12% left, below its
/// 20% reserve"*), so it is shown verbatim and not paraphrased.
struct ForceModelSheet: View {
    let refusal: DispatchRefusal
    let force: () -> Void
    let useOpus: () -> Void
    let cancel: () -> Void

    @State private var fired = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                SheetHeader("No headroom for that model", symbol: "gauge.with.dots.needle.0percent",
                            hue: Palette.statusLimit)
                ServerSays(refusal.text, tone: .refusal)
                if refusal.canOpus, let opusAccount = refusal.opusAccount {
                    PrimaryAction("Start with opus — [\(opusAccount)]"
                                  + (refusal.opusLeft.map { ", \(Int($0.rounded()))% left" } ?? ""),
                                  symbol: "play.fill",
                                  tint: Palette.statusWorking, enabled: !fired) {
                        fired = true
                        useOpus()
                    }
                } else {
                    Text("No account has opus headroom either.")
                        .font(OrcFont.bodyCompact)
                        .foregroundStyle(Palette.textTertiary)
                }
                ConsequenceGap()
                SecondaryAction("Use \(refusal.model ?? "it") anyway — into the reserve",
                                symbol: "flag.fill", tint: Palette.statusLimit,
                                enabled: !fired) {
                    fired = true
                    force()
                }
                CancelAction(action: cancel)
            }
            .padding(Space.lg)
        }
        .background(Palette.surface.ignoresSafeArea())
        // Deliberately NOT `SheetHeight.launch`: consecutive sheets in one chain
        // never share a detent, so a thumb aimed at "Launch mission" cannot land
        // on "use it anyway".
        .presentationDetents([.height(SheetHeight.forceModel), .large])
        .presentationDragIndicator(.visible)
    }
}

/// A dispatch in flight, and then whatever it became.
///
/// The progress lines are the server's `①②③④⑤`, **rendered verbatim**, with the
/// two-space-prefixed sub-lines indented as sub-lines. They come from
/// `GET /api/dispatch/status?job=…` on a 1.5 s poll — there is no intent frame on
/// this wire, so a poll is what there is.
struct DispatchProgressView: View {
    let run: ActionsStore.DispatchRun
    let done: () -> Void
    let reopenDraft: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                header
                if !run.progress.isEmpty {
                    VStack(alignment: .leading, spacing: Space.xxs) {
                        ForEach(Array(run.progress.enumerated()), id: \.offset) { _, line in
                            Text(line)
                                .font(OrcFont.code)
                                .foregroundStyle(line.hasPrefix("  ") ? Palette.textTertiary
                                                                      : Palette.textSecondary)
                                .padding(.leading, line.hasPrefix("  ") ? Space.lg : 0)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                    .padding(Space.md)
                    .background(Palette.sunken)
                    .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                }
                body(for: run.phase)
            }
            .padding(Space.lg)
        }
    }

    @ViewBuilder
    private var header: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            ConsequenceRow("\(run.model) · effort \(run.effort)", arrow: "cpu",
                           hue: Palette.textPrimary)
            ConsequenceRow(run.worktree ?? "auto", arrow: "folder", hue: Palette.statusFree)
            if let job = run.job {
                ConsequenceRow(job, arrow: "number", hue: Palette.textTertiary)
            }
        }
    }

    @ViewBuilder
    private func body(for phase: ActionsStore.DispatchRun.Phase) -> some View {
        switch phase {
        case .launching:
            HonestProgress(since: run.startedAt, caption: "asking the server")
        case .running:
            HonestProgress(since: run.startedAt, caption: "launching")
            Text("Typically 10–20 s: a tmux session, then claude boots, then the "
                 + "effort command, then the brief.")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
        case .finished(let result):
            ServerSays(result.text, tone: result.ok ? .ok : .refusal)
            if result.ok {
                if result.effortConfirmed == false {
                    // Tri-state on purpose: nil means no effort was asked for.
                    ServerSays("the effort command was not confirmed in the pane — "
                               + "attach and check before trusting it",
                               tone: .unknown)
                }
                if result.kickoffSent == false {
                    ServerSays("the kickoff brief was not confirmed — attach and "
                               + "press Enter", tone: .unknown)
                }
                if let attach = result.attach {
                    VStack(alignment: .leading, spacing: Space.xxs) {
                        Text("attach")
                            .font(OrcFont.label)
                            .orcTracking(11)
                            .foregroundStyle(Palette.textTertiary)
                        Text(attach)
                            .font(OrcFont.codeSm)
                            .foregroundStyle(Palette.statusFree)
                            .textSelection(.enabled)
                    }
                }
                Text("It appears on the board in about 30 seconds.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            ConsequenceGap()
            PrimaryAction(result.ok ? "Done" : "Back to the draft",
                          tint: result.ok ? Palette.statusWorking : Palette.statusFree) {
                result.ok ? done() : reopenDraft()
            }
        case .refused(let refusal):
            // A clean refusal: nothing launched, so a second attempt is safe and
            // is the user's to make. This is the ONLY phase that says so.
            ServerSays(refusal.text, tone: .refusal)
            ConsequenceGap()
            PrimaryAction("Back to the draft", tint: Palette.statusFree) { reopenDraft() }
        case .lost(let why):
            // Never "failed", and never a retry button. `UX.md` §7.4 — rendering
            // a timeout as failure is the most dangerous message available here,
            // because the agent was very likely launched.
            SheetHeader("Did it launch?", symbol: "questionmark.diamond",
                        hue: Palette.statusLimit)
            ServerSays(why, tone: .unknown)
            ConsequenceGap()
            PrimaryAction("Done", tint: Palette.textTertiary) { done() }
        }
    }
}
