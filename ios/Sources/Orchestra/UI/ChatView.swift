import SwiftUI

/// One session's conversation — read-only, which is what phase 2 is.
///
/// **The composer is deliberately absent rather than disabled.** A greyed-out
/// text field at the bottom of this screen would say "you can nearly reply", and
/// the reply path is the one place in this app where getting the target wrong
/// means an unattended instruction typed at the wrong agent under
/// `--dangerously-skip-permissions` (`UX.md` §3.3.1). It lands when the receipt
/// path does.
///
/// Three properties of the payload the layout has to respect, all of them the
/// server's doing (`transcripts._clean`):
///
/// * **newlines are destroyed**, so there is no markdown structure left and a
///   renderer here would be inventing one;
/// * **every turn is cut at 900 characters** with a trailing `…`, and a bubble
///   that ends in one says so, or the reader thinks the agent stopped;
/// * **`/`-prefixed user text never appears**, so this is not a complete record
///   and does not present itself as one.
public struct ChatView: View {
    private let worktree: String
    private let account: String
    private let sid: String
    /// The board, so the header's status is LIVE. A chat screen that captured
    /// the session it was pushed with would show a frozen `● WORKING` badge for
    /// as long as somebody sat on it — and sitting on it while an agent works is
    /// the entire use of this screen.
    @Bindable private var fleet: FleetStore
    @State private var chat: ChatStore

    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(worktree: String, account: String, sid: String,
                store: FleetStore, client: OrchestraClient) {
        self.worktree = worktree
        self.account = account
        self.sid = sid
        self.fleet = store
        _chat = State(initialValue: ChatStore(client: client, account: account, sid: sid))
    }

    /// Looked up on every pass. Nil once the session falls off the board — a
    /// transcript older than the observer's window, or a worktree that went
    /// away — and the header says so rather than showing the last status it knew
    /// as if it were still true.
    private var session: Session? {
        fleet.state?.worktrees.first { $0.name == worktree }?
            .sessions.first { $0.sid == sid }
    }

    public var body: some View {
        // `.background`, NOT `ZStack { canvas.ignoresSafeArea(); content }`.
        // A ZStack sizes to its largest child, so a canvas that ignores the safe
        // area makes the whole stack full-bleed and the content lays out into
        // the inset — which put the read-only footer UNDERNEATH the connection
        // bar, hiding the one line that explains why there is no composer.
        // Painting the canvas as a background instead keeps the layout inside
        // the safe area and still fills the screen.
        content
            .background { Palette.canvas.ignoresSafeArea() }
            .navigationTitle(worktree)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    VStack(spacing: 0) {
                        Text(verbatim: "\(worktree) · [\(account)]")
                            .font(OrcFont.status)
                            .foregroundStyle(Palette.textPrimary)
                        if let topic = session?.topic, !topic.isEmpty {
                            Text(SanitizedText.oneLine(topic))
                                .font(OrcFont.meta)
                                .foregroundStyle(Palette.textTertiary)
                                .lineLimit(1)
                        }
                    }
                }
            }
            .onReceive(ticker) { now = $0 }
            .task { chat.start() }
            .onDisappear { chat.stop() }
    }

    @ViewBuilder
    private var content: some View {
        VStack(spacing: 0) {
            header
            readOnlyNotice
            Divider().overlay(Palette.hairline)
            if let message = chat.serverError {
                refusal(message)
            } else if let error = chat.transportError, chat.messages.isEmpty {
                FailureView(error: error) { Task { await chat.load() } }
            } else if chat.messages.isEmpty && chat.loading {
                ProgressView().tint(Palette.textTertiary).padding(Space.xl)
                Spacer()
            } else if chat.messages.isEmpty {
                ContentUnavailableView("nothing to show",
                                       systemImage: "text.bubble",
                                       description: Text("this transcript has no readable turns — "
                                                         + "the server filters slash commands and "
                                                         + "machine text out entirely"))
            } else {
                transcript
            }
        }
    }

    /// The session's own status line, restated here because this screen can be
    /// deep-linked into and because "why am I looking at this" is the first
    /// question.
    @ViewBuilder
    private var header: some View {
        HStack(spacing: Space.sm) {
            if let session {
                StatusPill(session.status)
                Text(ModelLabel.short(session.model))
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                Spacer(minLength: 0)
                Text(verbatim: RelativeTime.short(since: session.lastWrite, now: now) + " ago")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            } else {
                Image(systemName: "questionmark.circle")
                    .foregroundStyle(Palette.textTertiary)
                Text("this session is no longer on the board")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, Space.lg)
        .padding(.vertical, Space.sm)
    }

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: Space.md) {
                    // No fake infinite scroll and **no `.refreshable`** here: the
                    // universal gesture at the top of a transcript is load-older,
                    // and the server has no `before=` cursor to serve one with.
                    Text(verbatim: "— earliest of \(chat.messages.count) loaded turns —")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textDisabled)
                        .frame(maxWidth: .infinity, alignment: .center)
                    ForEach(chat.messages) { message in
                        ChatBubble(message: message).id(message.id)
                    }
                    Color.clear.frame(height: Space.sm).id("bottom")
                }
                .padding(.horizontal, Space.lg)
                .padding(.vertical, Space.md)
            }
            .scrollIndicators(.hidden)
            .onChange(of: chat.messages.count) { _, _ in
                proxy.scrollTo("bottom", anchor: .bottom)
            }
            .task { proxy.scrollTo("bottom", anchor: .bottom) }
        }
    }

    private func refusal(_ message: String) -> some View {
        VStack(spacing: Space.sm) {
            Image(systemName: "exclamationmark.bubble")
                .font(.system(size: 32))
                .foregroundStyle(Palette.statusNeeds)
            // Verbatim, always. The server's own words are the bug report.
            Text(message)
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
                .multilineTextAlignment(.center)
            Text(verbatim: "\(account) · \(sid.prefix(8))")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textDisabled)
        }
        .padding(Space.xl)
        .frame(maxHeight: .infinity)
    }

    /// **At the top, not pinned to the bottom.**
    ///
    /// It began life as a footer where a composer will eventually go, and the
    /// screenshot showed it half-hidden behind the connection bar: a bottom-
    /// pinned row inside a PUSHED navigation destination does not receive the
    /// `safeAreaInset` the tab applied outside the `NavigationStack`, so it laid
    /// itself out against the screen instead. Two attempts to fix it in place
    /// both failed the same way. Since this is a static explanation and not a
    /// control, the honest fix is to stop fighting the layout: it belongs where
    /// it is read, which is before the transcript rather than after it. The
    /// composer, when it lands, is a first-class bottom bar and gets to solve
    /// this properly.
    private var readOnlyNotice: some View {
        HStack(spacing: Space.sm) {
            Image(systemName: "keyboard")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
                .accessibilityHidden(true)
            Text("read-only — replying lands with the delivery receipt")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
                .lineLimit(2)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Space.lg)
        .padding(.bottom, Space.sm)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One turn.
///
/// Yours right-aligned on `raised`, the agent's left-aligned on `surface` — and
/// both **selectable**, because a run-on 900-character paragraph regularly has a
/// command in the middle of it and copying that out is the only thing this
/// screen can do for you today.
struct ChatBubble: View {
    let message: ChatMessage
    @State private var expanded = false

    /// Agent turns collapse at six lines. `UX.md` §3.3.3 — a 900-character
    /// paragraph is the norm, not the exception.
    private var lineLimit: Int? {
        if message.isMine { return nil }
        return expanded ? nil : 6
    }

    var body: some View {
        HStack {
            if message.isMine { Spacer(minLength: Space.xxl) }
            VStack(alignment: message.isMine ? .trailing : .leading, spacing: Space.xxs) {
                Text(message.text)
                    .font(OrcFont.body)
                    .foregroundStyle(message.isMine ? Palette.textPrimary : Palette.textSecondary)
                    .lineLimit(lineLimit)
                    .multilineTextAlignment(.leading)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity,
                           alignment: message.isMine ? .trailing : .leading)
                HStack(spacing: Space.sm) {
                    if !message.isMine, !expanded, message.text.count > 240 {
                        Button("show full") { expanded = true }
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.statusFree)
                    }
                    if message.serverTruncated {
                        // The server cut this at 900 characters. Without this the
                        // reader thinks the agent trailed off mid-sentence.
                        Text("truncated by the server")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textDisabled)
                    }
                    if let stamp = message.timestamp {
                        Text(RelativeTime.clock(stamp))
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textDisabled)
                    }
                }
            }
            .padding(Space.md)
            .background(message.isMine ? Palette.raised : Palette.surface)
            .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(message.isMine ? Palette.control : Palette.hairline, lineWidth: 1))
            if !message.isMine { Spacer(minLength: Space.xxl) }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel((message.isMine ? "you said " : "the agent said ") + message.text)
    }
}
