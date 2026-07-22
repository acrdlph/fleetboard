import SwiftUI

/// Arm, change, or disarm an auto-resume for one session.
///
/// **This is the one mutation in the app that is genuinely idempotent**, and it
/// is idempotent by construction rather than by a key the server does not have:
/// `resume._resumes` is a dict keyed `"{worktree}|{sid}"`, so arming twice
/// replaces rather than adds. Driven live to confirm it. That is why this sheet
/// has no confirmation, no disable-on-tap, and an ordinary retry — the friction
/// model is proportional to irreversibility, and this is reversible.
///
/// Two facts the copy is required to state, because both cost real money if the
/// user is wrong about them:
///
/// * **Cancel is not an abort.** If `fire_resume` is already executing, the pop
///   removes the key and the side effect still happens, unreported.
/// * **A re-arm at the armed moment is real.** The board re-checks the limit when
///   it fires; if it still binds it re-arms for the next reset, up to ten times.
public struct ResumeSheet: View {
    private let worktree: String
    private let session: Session
    @Bindable private var fleet: FleetStore
    @Bindable private var actions: ActionsStore
    @Environment(\.dismiss) private var dismiss

    @State private var delay: ResumeDelay = .oneMinute
    @State private var useExactTime = false
    @State private var exactTime = Date().addingTimeInterval(3600)
    @State private var working = false
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(worktree: String, session: Session, fleet: FleetStore,
                actions: ActionsStore) {
        self.worktree = worktree
        self.session = session
        self.fleet = fleet
        self.actions = actions
    }

    /// The live session, so a limit that resolves while this sheet is open is
    /// visible rather than frozen at the value it was pushed with.
    private var live: Session {
        fleet.state?.worktrees.first { $0.name == worktree }?
            .sessions.first { $0.sid == session.sid } ?? session
    }

    private var armed: ResumeSchedule? {
        fleet.state?.resumes[ActionsStore.resumeKey(worktree: worktree, sid: session.sid)]
    }

    private var reply: ResumeReply? {
        actions.notice(worktree: worktree, sid: session.sid)
    }

    /// `resets_at` is genuinely absent for a real and common state: the
    /// transcript-regex fallback fires when the CLI wrote its limit notice but
    /// the cclimits cache is cold, and then nothing knows when it lifts.
    private var resetsAt: Date? { live.limit?.resets }

    /// What the server will actually compute, mirrored so the sheet can state it
    /// in words before the tap rather than after.
    private var resolvedFireTime: Date? {
        if useExactTime { return exactTime }
        guard let resetsAt else { return nil }
        return resetsAt.addingTimeInterval(delay.seconds)
    }

    /// **`_resumes` iterates in insertion order, not due order**, and the loop
    /// fires due keys serially in one pass — so an overdue schedule ahead of this
    /// one blocks it. Only the EARLIEST overdue pending schedule may be firing;
    /// everything behind it is merely queued, freely cancellable, and must not be
    /// told "firing now" (`UX.md` §4.5).
    private var firingBlocker: ResumeSchedule? {
        guard let all = fleet.state?.resumes.values else { return nil }
        let overdue = all
            .filter { $0.status == "pending" && ($0.dueAt ?? .infinity) <= now.timeIntervalSince1970 }
            .sorted { ($0.dueAt ?? 0) < ($1.dueAt ?? 0) }
        guard let earliest = overdue.first else { return nil }
        return earliest.sid == session.sid && earliest.worktree == worktree ? nil : earliest
    }

    private var mayBeFiring: Bool {
        guard let armed, armed.status == "pending", let due = armed.dueAt else { return false }
        return due <= now.timeIntervalSince1970 && firingBlocker == nil
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                header
                if let armed { armedBlock(armed) } else { picker }
                if let reply {
                    ServerSays(reply.text,
                               tone: reply.ok ? .ok : (reply.needTime ? .unknown : .refusal))
                }
                buttons
                footnote
            }
            .padding(Space.lg)
        }
        .background(Palette.surface.ignoresSafeArea())
        .presentationDetents([.height(SheetHeight.resume), .large])
        .presentationDragIndicator(.visible)
        .onReceive(ticker) { now = $0 }
        .onAppear {
            // `need_time` is not an error: it means this limit carries no reset
            // time, so the time is the user's to pick. Start there rather than on
            // a delay ladder that cannot resolve.
            if resetsAt == nil {
                useExactTime = true
                exactTime = Date().addingTimeInterval(3600)
            }
        }
        .onDisappear { actions.clearNotice(worktree: worktree, sid: session.sid) }
    }

    @ViewBuilder
    private var header: some View {
        SheetHeader("Auto-resume · \(worktree)", symbol: "timer",
                    hue: Palette.statusWorking)
        VStack(alignment: .leading, spacing: Space.xxs) {
            Text(verbatim: "[\(live.account)] · \(live.shortID)")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.statusFree)
            if let resetsAt {
                Text(verbatim: "resets \(RelativeTime.clock(resetsAt)) · in "
                     + RelativeTime.countdown(to: resetsAt, now: now))
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            } else {
                Text("this limit carries no reset time — the time is yours to pick")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusLimit)
            }
        }
    }

    @ViewBuilder
    private var picker: some View {
        Text("Resume")
            .font(OrcFont.label)
            .orcTracking(11)
            .foregroundStyle(Palette.textTertiary)
        if resetsAt != nil {
            // The delay ladder mirrors the desktop select exactly: 60 / 300 /
            // 900 / 3600, plus an exact time.
            HStack(spacing: Space.sm) {
                ForEach(ResumeDelay.allCases, id: \.self) { option in
                    Button {
                        delay = option
                        useExactTime = false
                    } label: {
                        Text(verbatim: option.label)
                            .font(OrcFont.meta)
                            .foregroundStyle(!useExactTime && delay == option
                                             ? Palette.canvas : Palette.textSecondary)
                            .padding(.horizontal, Space.sm)
                            .frame(minHeight: 40)
                            .frame(maxWidth: .infinity)
                            .background(!useExactTime && delay == option
                                        ? Palette.statusWorking : Palette.raised)
                            .clipShape(RoundedRectangle(cornerRadius: Radius.xs,
                                                        style: .continuous))
                    }
                }
            }
            Text("after the reset")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textDisabled)
        }
        Toggle(isOn: $useExactTime) {
            Text("At an exact time")
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
        }
        .tint(Palette.statusWorking)
        .disabled(resetsAt == nil)
        if useExactTime {
            DatePicker("fires at", selection: $exactTime,
                       displayedComponents: [.date, .hourAndMinute])
                .datePickerStyle(.compact)
                .font(OrcFont.bodyCompact)
                .tint(Palette.statusWorking)
        }
        if let fire = resolvedFireTime {
            ConsequenceRow("fires \(RelativeTime.clock(fire))",
                           detail: "in " + RelativeTime.countdown(to: fire, now: now),
                           arrow: "clock", hue: Palette.statusWorking)
        }
    }

    @ViewBuilder
    private func armedBlock(_ schedule: ResumeSchedule) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            if let due = schedule.due {
                ConsequenceRow("armed for \(RelativeTime.clock(due))",
                               detail: RelativeTime.countdown(to: due, now: now),
                               arrow: "timer", hue: Palette.statusWorking)
            }
            if schedule.attempts > 0 {
                ConsequenceRow("re-armed \(schedule.attempts)×", arrow: "arrow.clockwise",
                               hue: Palette.textTertiary)
            }
            if let message = schedule.message, !message.isEmpty {
                ServerSays(message, tone: schedule.status == "failed" ? .refusal : .ok)
            }
            if let blocker = firingBlocker {
                ServerSays("queued behind \(blocker.worktree) — the resume loop fires "
                           + "due schedules one at a time, in insertion order, and "
                           + "that one is overdue. This is still freely cancellable.",
                           tone: .unknown)
            } else if mayBeFiring {
                ServerSays("this may be firing now. Cancelling won't stop a resume "
                           + "already in progress — the key is removed but the side "
                           + "effect still happens, and it is never reported.",
                           tone: .unknown)
            }
        }
    }

    @ViewBuilder
    private var buttons: some View {
        if armed != nil {
            PrimaryAction(mayBeFiring ? "Change (blocked while firing)" : "Change the time",
                          symbol: "slider.horizontal.3",
                          tint: Palette.statusWorking,
                          enabled: !working && !mayBeFiring) {
                // Re-arming while firing is a lost-update race server-side, so
                // the control is blocked rather than merely discouraged.
                Task { await arm() }
            }
            SecondaryAction("Disarm", symbol: "xmark", tint: Palette.statusNeeds,
                            enabled: !working) {
                Task {
                    working = true
                    await actions.cancelResume(worktree: worktree, sid: session.sid)
                    working = false
                }
            }
        } else {
            PrimaryAction("Arm auto-resume", symbol: "timer",
                          tint: Palette.statusWorking,
                          enabled: !working && resolvedFireTime != nil) {
                Task { await arm() }
            }
        }
        ConsequenceGap()
        CancelAction("Close") { dismiss() }
    }

    private var footnote: some View {
        Text("At the armed moment the board re-checks the limit. If it still binds "
             + "it re-arms for the next reset, up to 10 times. Then it types "
             + "\"continue\" into this session's own terminal. If no terminal can "
             + "be typed into, the conversation is reopened in tmux with "
             + "claude --resume.")
            .font(OrcFont.meta)
            .foregroundStyle(Palette.textTertiary)
    }

    private func arm() async {
        working = true
        defer { working = false }
        if useExactTime {
            await actions.armResume(worktree: worktree, sid: session.sid,
                                    account: live.account, delayS: nil,
                                    resetsAt: nil, dueAt: exactTime.timeIntervalSince1970)
        } else {
            await actions.armResume(worktree: worktree, sid: session.sid,
                                    account: live.account, delayS: delay.seconds,
                                    resetsAt: resetsAt?.timeIntervalSince1970,
                                    dueAt: nil)
        }
    }
}
