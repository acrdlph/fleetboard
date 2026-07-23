import SwiftUI

/// The closeout, as **two steps that look like two steps**.
///
/// The state machine lives entirely server-side and this sheet is a
/// *presentation* of it, never a private client timer. The desktop's version is a
/// 6-second `_armFinish` window on one client; that cannot survive a second
/// phone, a notification action, or an app relaunch, and two clients holding
/// their own arm windows desynchronise instantly.
///
/// **What the server actually publishes is one field:** `closeout_sent`, an epoch
/// on the card, copied from `finish._closeouts` by `observer.py:228` and present
/// only while the card still has a live process. Present → this is step two,
/// `✕ Close`. Absent → step one, `✓ Finish`. There is no phase stream, no
/// `intent_id`, and no `armed` state on the wire — `UX.md` §4.4's *"Finish
/// returns an `intent_id` immediately and phases stream"* describes a server that
/// does not exist. So the two steps are made legible the only honest way
/// available: **two visually different sheets with different words, different
/// heights and different buttons**, so a tap in step two can never be muscle
/// memory from step one.
public struct FinishSheet: View {
    private let worktree: String
    @Bindable private var fleet: FleetStore
    @Bindable private var actions: ActionsStore
    private let onChat: (Session) -> Void
    @Environment(\.dismiss) private var dismiss

    /// **Tapped, and it never becomes false again while this sheet lives.**
    /// `UX.md` §7.4 — the action button disables on tap and does not re-enable on
    /// timeout, because with no idempotency key on this server a re-enabled
    /// button IS the double-fire. Recovery is reconciliation, never a retry.
    @State private var fired = false
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(worktree: String, fleet: FleetStore, actions: ActionsStore,
                onChat: @escaping (Session) -> Void) {
        self.worktree = worktree
        self.fleet = fleet
        self.actions = actions
        self.onChat = onChat
    }

    private var card: Worktree? {
        fleet.state?.worktrees.first { $0.name == worktree }
    }

    /// Which step this is, read off the card on **every** pass rather than
    /// captured when the sheet opened. A brief sent from the desktop while this
    /// sheet is open moves it to step two under the user, which is correct: the
    /// state is the server's.
    private var step: ActionsStore.FinishStep {
        (card?.isCloseoutPending ?? false) ? .close : .brief
    }

    private var run: ActionsStore.FinishRun? { actions.finishes[worktree] }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                if let run {
                    outcome(run)
                } else if let card {
                    switch step {
                    case .brief: briefStep(card)
                    case .close: closeStep(card)
                    }
                } else {
                    SheetHeader("this worktree is no longer on the board",
                                symbol: "questionmark.folder", hue: Palette.textTertiary)
                    CancelAction("Close") { dismiss() }
                }
            }
            .padding(Space.lg)
        }
        .background(Palette.surface.ignoresSafeArea())
        .presentationDetents([.height(step == .brief ? SheetHeight.finishBrief
                                                     : SheetHeight.finishClose),
                              .large])
        .presentationDragIndicator(.visible)
        .onReceive(ticker) { now = $0 }
    }

    // MARK: - Step one

    @ViewBuilder
    private func briefStep(_ card: Worktree) -> some View {
        SheetHeader("Close out \(card.name)?", symbol: "checkmark.seal",
                    hue: Palette.statusLimit)
        state(card)

        if actions.serverForgotBrief(card: card, now: now),
           let sent = actions.rememberedBrief(for: card.name, now: now) {
            // The server-restart hazard, and the only defence a client has.
            // `finish._closeouts` is in-memory: a restart drops it, the card
            // stops saying `closeout_sent`, and this button silently reverts
            // from ✕ close to ✓ finish. Pressing it re-types the whole
            // 600-character brief at an agent that may be mid-closeout.
            ServerSays("This phone sent a closeout brief for \(card.name) "
                       + "\(RelativeTime.short(since: sent, now: now)) ago, and the "
                       + "board is no longer reporting one while an agent is still "
                       + "live here. The server most likely restarted — its closeout "
                       + "record is in memory only. Sending again re-types the whole "
                       + "brief at an agent that may already be closing out.",
                       tone: .unknown)
        }

        Text("The agent gets a closeout brief: settle background work, commit what "
             + "matters, land the branch, park on trunk, report. Work that has "
             + "already landed is never re-merged.")
            .font(OrcFont.bodyCompact)
            .foregroundStyle(Palette.textSecondary)

        if card.liveProcs.isEmpty {
            // Not optional copy: `mode: "dispatch"` has no double-fire guard at
            // all server-side, and an agent appearing unannounced is the worst
            // kind of surprise in this app.
            ServerSays("No live terminal here, so this launches a one-shot closeout "
                       + "agent (haiku) instead of typing at one. It runs headless "
                       + "with --dangerously-skip-permissions and can commit, merge "
                       + "and push.", tone: .unknown)
        }

        PrimaryAction("Send the closeout brief", symbol: "paperplane",
                      tint: Palette.statusLimit, enabled: !fired) {
            fired = true
            actions.finish(worktree: card.name, step: .brief)
        }
        ConsequenceGap()
        CancelAction { dismiss() }
        Text("this can take up to a minute — the server runs a git fetch, a "
             + "merge-base and a process scan inside the request")
            .font(OrcFont.meta)
            .foregroundStyle(Palette.textDisabled)
    }

    // MARK: - Step two

    @ViewBuilder
    private func closeStep(_ card: Worktree) -> some View {
        SheetHeader("Close \(card.name)?", symbol: "xmark.circle",
                    hue: Palette.statusNeeds)
        if let sent = card.closeoutSentAt {
            // An absolute epoch, counted up against this phone's own clock. The
            // server deliberately stopped sending an elapsed string: one computed
            // on the Mac and read on a phone minutes later is dead on arrival.
            Text(verbatim: "Brief sent \(RelativeTime.short(since: sent, now: now)) ago.")
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
        }
        state(card)
        Text("This verifies the landing — fetch, merge-base, clean tree — and types "
             + "/exit. It never re-sends the brief.")
            .font(OrcFont.bodyCompact)
            .foregroundStyle(Palette.textSecondary)

        PrimaryAction("Verify and close", symbol: "xmark",
                      tint: Palette.statusNeeds, enabled: !fired) {
            fired = true
            actions.finish(worktree: card.name, step: .close)
        }
        if let session = card.sessions.first(where: { card.isReachable($0) }) {
            SecondaryAction("Chat with the agent", symbol: "text.bubble",
                            tint: Palette.statusFree) {
                dismiss()
                onChat(session)
            }
        }
        ConsequenceGap()
        CancelAction { dismiss() }
    }

    /// What is actually left, from the card — the numbers the decision turns on.
    @ViewBuilder
    private func state(_ card: Worktree) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            ConsequenceRow(card.liveProcs.isEmpty ? "no live agent"
                                                  : "\(card.liveProcs.count) agent(s) live",
                           arrow: "person.wave.2",
                           hue: card.liveProcs.isEmpty ? Palette.textTertiary
                                                       : Palette.statusWorking)
            ConsequenceRow(card.git.dirty == 0 ? "clean tree"
                                               : "Δ\(card.git.dirty) uncommitted",
                           arrow: "doc.badge.ellipsis",
                           hue: card.git.dirty == 0 ? Palette.textTertiary
                                                    : Palette.statusLimit)
            if card.git.hasUpstream, let ahead = card.git.ahead {
                ConsequenceRow("↑\(ahead) ahead of upstream", arrow: "arrow.up",
                               hue: Palette.textTertiary)
            } else {
                // `ahead` is null, not zero, with no upstream — `↑0` would be a
                // measurement this client never made.
                ConsequenceRow("no upstream branch", arrow: "arrow.up",
                               hue: Palette.textDisabled)
            }
            ConsequenceRow(card.git.branch, arrow: "arrow.triangle.branch",
                           hue: Palette.statusFree)
        }
    }

    // MARK: - Outcome

    @ViewBuilder
    private func outcome(_ run: ActionsStore.FinishRun) -> some View {
        SheetHeader(run.step == .brief ? "Closing out \(run.worktree)"
                                       : "Closing \(run.worktree)",
                    symbol: "checkmark.seal", hue: Palette.statusLimit)
        switch run.phase {
        case .running:
            HonestProgress(since: run.startedAt, caption: "working")
            Text("The server is fetching origin, checking whether the branch landed, "
                 + "and then typing. There is no progress to report until it answers "
                 + "— a staged label here would be a timed fiction.")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
        case .settled(let reply):
            ServerSays(FinishCopy.result(reply), tone: reply.ok ? .ok : .refusal)
            if !reply.files.isEmpty {
                VStack(alignment: .leading, spacing: Space.xxs) {
                    Text("what's left")
                        .font(OrcFont.label)
                        .orcTracking(11)
                        .foregroundStyle(Palette.textTertiary)
                    ForEach(reply.files, id: \.self) { line in
                        Text(line)
                            .font(OrcFont.codeSm)
                            .foregroundStyle(Palette.textSecondary)
                            .textSelection(.enabled)
                    }
                }
            }
            if reply.mode == .chat, let card,
               let session = card.sessions.first(where: { card.isReachable($0) }) {
                // A DISTINCT mode, not a flavour of pending: the agent is stuck on
                // a question, and a typed nudge would collide with its open
                // dialog. The server says route the user to chat, so this does.
                SecondaryAction("Answer it in chat", symbol: "text.bubble",
                                tint: Palette.statusFree) {
                    dismiss()
                    onChat(session)
                }
            }
            ConsequenceGap()
            CancelAction("Done") {
                actions.clearFinish(worktree: run.worktree)
                dismiss()
            }
        case .lost(let why):
            // Never "failed". The brief was very likely typed.
            ServerSays(why, tone: .unknown)
            ConsequenceGap()
            CancelAction("Done") {
                actions.clearFinish(worktree: run.worktree)
                dismiss()
            }
        }
    }
}
