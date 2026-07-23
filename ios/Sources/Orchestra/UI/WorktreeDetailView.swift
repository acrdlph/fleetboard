import SwiftUI

/// One worktree: its identity, every session on it, and the terminals underneath
/// (`UX.md` §3.2).
///
/// **It reads the card out of the store on every pass rather than holding the
/// copy it was pushed with.** A detail screen that captured its card would show
/// a frozen snapshot while the board behind it streamed — and this is the screen
/// somebody sits on while an agent works, which is exactly when it moves. If the
/// worktree disappears from the fleet the screen says so rather than showing the
/// last thing it knew as if it were still true.
public struct WorktreeDetailView: View {
    private let name: String
    @Bindable private var store: FleetStore
    @Bindable private var actions: ActionsStore
    private let client: OrchestraClient

    /// The connection strip's measured height. This screen's last control is a
    /// money-adjacent button, and the first phase-3 screenshot had it half under
    /// the bar — see `EnvironmentValues.bottomAccessoryHeight`.
    @Environment(\.bottomAccessoryHeight) private var accessoryHeight
    @State private var now = Date()
    @State private var showEnded = false
    @State private var finishing = false
    @State private var resuming: Session?
    /// A pushed chat, addressed the only way anything in this app is addressed:
    /// `(account, sid)`. Not a `Session` value — the board re-sorts and a session
    /// captured now is a different row in a second.
    @State private var chatTarget: ChatTarget?

    struct ChatTarget: Hashable {
        let account: String
        let sid: String
    }
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    /// A sheet to present on appear. Nil in every shipping path — see
    /// `WorktreeSheet`.
    private let initialSheet: WorktreeSheet?

    public init(name: String, store: FleetStore, actions: ActionsStore,
                client: OrchestraClient, initialSheet: WorktreeSheet? = nil) {
        self.name = name
        self.store = store
        self.actions = actions
        self.client = client
        self.initialSheet = initialSheet
    }

    private var card: Worktree? {
        store.state?.worktrees.first { $0.name == name }
    }

    private var resumes: [ResumeSchedule] {
        (store.state?.resumes ?? [:]).values
            .filter { $0.worktree == name && $0.status == "pending" }
            .sorted { ($0.dueAt ?? 0) < ($1.dueAt ?? 0) }
    }

    public var body: some View {
        ZStack {
            Palette.canvas.ignoresSafeArea()
            if let card {
                content(card)
            } else {
                // The card left the fleet while this screen was open. Saying so
                // is the only honest option: the alternative is a screen that
                // still offers to act on a worktree that is gone.
                ContentUnavailableView("this worktree is no longer on the board",
                                       systemImage: "questionmark.folder",
                                       description: Text(verbatim: name))
            }
        }
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
        .onReceive(ticker) { now = $0 }
        .sheet(isPresented: $finishing) {
            FinishSheet(worktree: name, fleet: store, actions: actions) { session in
                chatTarget = ChatTarget(account: session.account, sid: session.sid)
            }
        }
        .sheet(item: $resuming) { session in
            ResumeSheet(worktree: name, session: session, fleet: store, actions: actions)
        }
        .navigationDestination(item: $chatTarget) { target in
            ChatView(worktree: name, account: target.account, sid: target.sid,
                     store: store, client: client)
        }
        .task {
            // Wait for a board: both sheets are presentations of a card, and a
            // card that has not arrived yet renders the "no longer on the board"
            // state instead of the sheet the screenshot is for.
            guard let initialSheet else { return }
            for _ in 0..<40 where card == nil {
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
            switch initialSheet {
            case .finish:
                finishing = true
            case .resume(let sid):
                resuming = card?.sessions.first { $0.sid == sid }
            }
        }
    }

    private func content(_ card: Worktree) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                identity(card)
                closeoutBlock(card)
                if !resumes.isEmpty { resumeBlock(card) }
                sessions(card)
                if !card.liveProcs.isEmpty { terminals(card) }
                finishFooter(card)
                Color.clear.frame(height: Space.xxl + accessoryHeight)
            }
            .padding(.horizontal, Space.lg)
            .padding(.top, Space.sm)
        }
        .scrollIndicators(.hidden)
        .refreshable { await store.refresh() }
    }

    // MARK: - Finish

    /// The two-step, as a footer that says which step it is on.
    ///
    /// There is one button and its LABEL changes with the server's state — which
    /// is exactly the thing `UX.md` §4.4 warns against ("a single button that
    /// does something different each tap") — so the two are separated the only
    /// way that survives: the step is named above the button, the button's word
    /// changes with it, its colour changes with it, and it opens a sheet whose
    /// height, title and copy are different for each step. Nothing acts on the
    /// tap that opens the sheet.
    @ViewBuilder
    private func finishFooter(_ card: Worktree) -> some View {
        let pending = card.isCloseoutPending
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel(pending ? "CLOSEOUT · STEP 2 OF 2" : "CLOSEOUT · STEP 1 OF 2")
            Text(pending
                 ? "A brief is already with the agent. The next step verifies the "
                   + "landing and types /exit — it never re-sends the brief."
                 : "Step one hands the agent a closeout brief. Step two, later, "
                   + "verifies the landing and closes the terminal.")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
            PrimaryAction(pending ? "✕ Close this worktree" : "✓ Finish this worktree",
                          tint: pending ? Palette.statusNeeds : Palette.statusLimit,
                          enabled: !actions.isBusy(.finish(worktree: name), now: now)) {
                finishing = true
            }
        }
    }

    /// **The refusal, promoted and self-clearing** (`UX.md` §4.4).
    ///
    /// `mode: "pending"` is a six-second toast on the desktop. On a phone you may
    /// look twenty minutes later, so it becomes a persistent row — and its
    /// condition is **recomputed from every frame** rather than remembered, so it
    /// dissolves itself the moment the tree goes clean. A client-local refusal
    /// with no re-verification would be a worse lie than the toast it replaces,
    /// because it persists.
    @ViewBuilder
    private func closeoutBlock(_ card: Worktree) -> some View {
        if let sentAt = card.closeoutSentAt {
            VStack(alignment: .leading, spacing: Space.xs) {
                HStack(spacing: Space.sm) {
                    Image(systemName: "hourglass")
                        .foregroundStyle(Palette.statusLimit)
                    Text(verbatim: "closeout pending — brief sent "
                         + RelativeTime.short(since: sentAt, now: now) + " ago")
                        .font(OrcFont.status)
                        .foregroundStyle(Palette.statusLimit)
                    Spacer(minLength: 0)
                }
                if let blocker = closeoutBlocker(card) {
                    Text(verbatim: blocker)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textSecondary)
                } else {
                    Text("landed and clean — you can close it now")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusWorking)
                }
            }
            .padding(Space.md)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.statusLimit.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.statusLimit.opacity(0.45), lineWidth: 1))
        }
        if actions.serverForgotBrief(card: card, now: now) {
            ServerSays("This phone briefed \(card.name) recently and the board has "
                       + "stopped reporting it while an agent is still live. The "
                       + "server keeps that record in memory only, so a restart "
                       + "loses it — finishing again re-types the whole brief.",
                       tone: .unknown)
        }
    }

    /// What the server would refuse on, recomputed from the card. It is the same
    /// two facts `start_finish` checks: the tree and the landing.
    private func closeoutBlocker(_ card: Worktree) -> String? {
        if card.git.dirty > 0 { return "can't close yet — Δ\(card.git.dirty) uncommitted file(s)" }
        if let ahead = card.git.ahead, ahead > 0 {
            return "can't close yet — ↑\(ahead) not yet on the trunk"
        }
        return nil
    }

    // MARK: - Identity

    private func identity(_ card: Worktree) -> some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            StatusPill(Triage.section(for: card))
            Text(card.git.branch)
                .font(OrcFont.code)
                .foregroundStyle(Palette.statusFree)
                .textSelection(.enabled)
            HStack(spacing: Space.md) {
                if card.git.dirty > 0 {
                    Text(verbatim: "Δ\(card.git.dirty) uncommitted")
                        .foregroundStyle(Palette.statusLimit)
                }
                // Omitted ENTIRELY with no upstream — `ahead` is null, not zero,
                // and `↑0` would be a measurement this client never made.
                if card.git.hasUpstream, let ahead = card.git.ahead, let behind = card.git.behind {
                    Text(verbatim: "↑\(ahead) ahead · ↓\(behind) behind")
                        .foregroundStyle(Palette.textTertiary)
                } else {
                    Text("no upstream")
                        .foregroundStyle(Palette.textDisabled)
                }
            }
            .font(OrcFont.meta)
            if let commit = card.git.commit {
                VStack(alignment: .leading, spacing: Space.xxs) {
                    HStack(spacing: Space.sm) {
                        // `%h` honours `core.abbrev` PER REPOSITORY — 8 chars in
                        // one worktree of this fleet and 9 in the other eight.
                        // Never sliced.
                        Text(commit.hash)
                            .font(OrcFont.codeSm)
                            .foregroundStyle(Palette.statusTurn)
                        Text(verbatim: RelativeTime.short(since: commit.date, now: now) + " ago")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textTertiary)
                    }
                    Text(commit.subject)
                        .font(OrcFont.bodyCompact)
                        .foregroundStyle(Palette.textSecondary)
                }
            }
            Text(card.path)
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
                .lineLimit(1)
                .truncationMode(.head)
                .textSelection(.enabled)
        }
        .padding(Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
            .stroke(Palette.hairline, lineWidth: 1))
    }

    private func resumeBlock(_ card: Worktree) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel("AUTO-RESUME", count: resumes.count)
            ForEach(resumes, id: \.sid) { resume in
                Button {
                    // Change and Disarm live in the same sheet that armed it, so
                    // there is one place where the fire semantics are stated.
                    resuming = card.sessions.first { $0.sid == resume.sid }
                } label: {
                    HStack(spacing: Space.sm) {
                        Image(systemName: "timer").accessibilityHidden(true)
                        if let due = resume.due {
                            Text(verbatim: "\(RelativeTime.clock(due)) · \(RelativeTime.countdown(to: due, now: now))")
                        } else {
                            Text("armed")
                        }
                        Text(verbatim: "[\(resume.account)]")
                            .foregroundStyle(Palette.statusFree)
                        Spacer(minLength: 0)
                        if resume.attempts > 0 {
                            Text(verbatim: "re-armed \(resume.attempts)×")
                                .foregroundStyle(Palette.textTertiary)
                        }
                        Image(systemName: "chevron.right")
                            .foregroundStyle(Palette.textTertiary)
                    }
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusWorking)
                    .frame(minHeight: 40)
                }
                .buttonStyle(.plain)
                .disabled(card.sessions.first { $0.sid == resume.sid } == nil)
            }
        }
    }

    // MARK: - Sessions

    private func sessions(_ card: Worktree) -> some View {
        // The server sorts by severity then freshness and caps at
        // `max_sessions`; the client never re-sorts. `showing N of N` cannot be
        // said honestly — there is no `session_count` on the wire, so a capped
        // card is indistinguishable from a complete one. `UX.md` §3.2 asks for
        // that field; until it exists this says nothing rather than a number it
        // would have to guess.
        let visible = showEnded ? card.sessions
                                : card.sessions.filter { $0.status != .ended }
        return VStack(alignment: .leading, spacing: Space.xs) {
            HStack {
                SectionLabel("SESSIONS", count: card.sessions.count)
                Spacer()
                if card.endedCount > 0 {
                    Button(showEnded ? "hide ended" : "\(card.endedCount) ended") {
                        showEnded.toggle()
                    }
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusFree)
                    .frame(minHeight: 44)
                }
            }
            VStack(spacing: 0) {
                ForEach(Array(visible.enumerated()), id: \.element.id) { index, session in
                    if index > 0 { Divider().overlay(Palette.hairline) }
                    NavigationLink(value: FleetRoute.chat(worktree: card.name,
                                                          account: session.account,
                                                          sid: session.sid)) {
                        // The row paints its own ground and the chevron does not
                        // paint one at all — the ground goes on the HStack. A
                        // background on the glyph alone covers only the glyph's
                        // own height, and the canvas shows through above and
                        // below it as a dark vertical seam down the right edge
                        // of every row. Caught in the screenshot, not in review.
                        HStack(spacing: 0) {
                            SessionRowView(session: session, isPrimary: true, now: now,
                                           cardBranch: card.git.branch)
                            Image(systemName: "chevron.right")
                                .font(OrcFont.meta)
                                .foregroundStyle(Palette.textTertiary)
                                .padding(.trailing, Space.md)
                        }
                        .background(session.status == .ended ? Palette.sunkenDim : Palette.sunken)
                    }
                    .buttonStyle(.plain)
                    if session.status == .limit {
                        limitControls(card, session)
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1))
        }
    }

    /// **Both controls are always present, in fixed positions.**
    ///
    /// A single control that mutates from `⏱ Auto` (opens a sheet) to `▶ Resume`
    /// (a no-confirm write) when the clock passes `resets_at` means a tap begun at
    /// 14:32:59 can land on a different button than the one aimed at. So there are
    /// two, they never move, and `▶ Resume` is disabled-with-a-reason before the
    /// reset instead of absent (`UX.md` §4.5).
    ///
    /// `▶ Resume` sends one word — `continue` — into a session that is idle by
    /// definition, through the same identity-addressed path as a reply. No
    /// confirmation, per §7.5.
    @ViewBuilder
    private func limitControls(_ card: Worktree, _ session: Session) -> some View {
        let resets = session.limit?.resets
        let ready = resets.map { $0 <= now } ?? false
        let reachable = card.isReachable(session)
        HStack(spacing: Space.sm) {
            Button {
                resuming = session
            } label: {
                Label("Auto-resume…", systemImage: "timer")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.statusWorking)
                    .frame(minHeight: 40)
            }
            Spacer(minLength: 0)
            Button {
                Task { await actions.resumeNow(worktree: card.name, session: session) }
            } label: {
                Label(ready ? "Resume now"
                            : "Resume at \(resets.map { RelativeTime.clock($0) } ?? "—")",
                      systemImage: "play.fill")
                    .font(OrcFont.meta)
                    .foregroundStyle(ready && reachable ? Palette.statusFree : Palette.textDisabled)
                    .frame(minHeight: 40)
            }
            .disabled(!ready || !reachable
                      || actions.isBusy(.send(sid: session.sid), now: now))
        }
        .padding(.horizontal, Space.md)
        .background(Palette.sunken)
        if let reply = actions.notice(worktree: card.name, sid: session.sid) {
            ServerSays(reply.text, tone: reply.ok ? .ok : .refusal)
                .padding(.horizontal, Space.md)
                .padding(.bottom, Space.sm)
                .background(Palette.sunken)
        }
    }

    // MARK: - Terminals

    private func terminals(_ card: Worktree) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel("TERMINALS", count: card.liveProcs.count)
            VStack(spacing: Space.sm) {
                ForEach(card.liveProcs) { proc in
                    VStack(alignment: .leading, spacing: Space.xxs) {
                        HStack(spacing: Space.sm) {
                            Image(systemName: StatusStyle.Mark.pid)
                                .accessibilityHidden(true)
                            Text(verbatim: "\(proc.pid)")
                                .foregroundStyle(Palette.textSecondary)
                            if let tty = proc.tty {
                                Text(tty).foregroundStyle(Palette.statusFree)
                            }
                            if let account = proc.account {
                                Text(verbatim: "[\(account)]")
                                    .foregroundStyle(Palette.statusFree)
                            }
                            Spacer(minLength: 0)
                            // `reachable` is THE gate for whether anything could
                            // ever be typed at this agent. Nothing types yet, but
                            // the row that will carry that button is the row that
                            // has to be honest about it now.
                            if !proc.reachable {
                                Text("can't be typed into")
                                    .foregroundStyle(Palette.statusLimit)
                            }
                        }
                        HStack(spacing: Space.sm) {
                            Text(verbatim: "up \(proc.etime)")
                            Text(verbatim: String(format: "%.1f%% cpu", proc.cpu))
                            if let host = proc.host {
                                Text(host).lineLimit(1).truncationMode(.middle)
                            }
                            if let tmux = proc.tmux {
                                Text(tmux).lineLimit(1).truncationMode(.middle)
                            }
                        }
                        .foregroundStyle(Palette.textTertiary)
                    }
                    .font(OrcFont.meta)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            .padding(Space.md)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Palette.surface)
            .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1))
        }
    }
}

/// The uppercase micro-label that heads every block, with its count.
struct SectionLabel: View {
    let title: String
    let count: Int?

    init(_ title: String, count: Int? = nil) {
        self.title = title
        self.count = count
    }

    var body: some View {
        HStack(spacing: Space.sm) {
            Text(title)
                .font(OrcFont.label)
                .orcTracking(11)
                .foregroundStyle(Palette.textTertiary)
            if let count {
                Text(verbatim: "\(count)")
                    .font(OrcFont.label)
                    .foregroundStyle(Palette.textDisabled)
            }
        }
        .padding(.top, Space.xs)
    }
}
