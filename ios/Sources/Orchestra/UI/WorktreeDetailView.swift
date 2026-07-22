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
    private let client: OrchestraClient

    @State private var now = Date()
    @State private var showEnded = false
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(name: String, store: FleetStore, client: OrchestraClient) {
        self.name = name
        self.store = store
        self.client = client
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
    }

    private func content(_ card: Worktree) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: Space.md) {
                identity(card)
                if !resumes.isEmpty { resumeBlock }
                sessions(card)
                if !card.liveProcs.isEmpty { terminals(card) }
                Color.clear.frame(height: Space.xxl)
            }
            .padding(.horizontal, Space.lg)
            .padding(.top, Space.sm)
        }
        .scrollIndicators(.hidden)
        .refreshable { await store.refresh() }
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

    private var resumeBlock: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            SectionLabel("AUTO-RESUME", count: resumes.count)
            ForEach(resumes, id: \.sid) { resume in
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
                        Text(verbatim: "\(resume.attempts) attempt(s)")
                            .foregroundStyle(Palette.textTertiary)
                    }
                }
                .font(OrcFont.meta)
                .foregroundStyle(Palette.statusWorking)
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
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
                .stroke(Palette.hairline, lineWidth: 1))
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
