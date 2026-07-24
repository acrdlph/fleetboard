import SwiftUI

/// One session's conversation — and the one screen in this app that types at an
/// agent.
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
///
/// And one property of the *composer*: the server collapses `\s*\n\s*` to a
/// single space before it types, so this composer does the same **as you type**.
/// Return inserts a space. WYSIWYG or nothing (`UX.md` §3.3.2).
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

    @State private var draft = ""
    @FocusState private var composerFocused: Bool
    /// The connection strip's real height. A bottom-pinned control inside a
    /// PUSHED destination does not receive the `safeAreaInset` the tab applied
    /// outside the `NavigationStack`, so the composer would sit underneath the
    /// bar — which is exactly what the first phase-3 screenshot showed.
    @Environment(\.bottomAccessoryHeight) private var accessoryHeight
    @State private var now = Date()
    /// Whether the transcript is scrolled to (or near) the newest turn. Only then
    /// does new content auto-follow — a reader who has scrolled up into history is
    /// left where they are, not yanked to the bottom on the next frame (`UX.md`
    /// §4.2). Starts true so a freshly opened conversation follows immediately.
    @State private var pinnedToBottom = true
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    /// Text to send once, on appear, through the SAME `ChatStore.send` the
    /// button calls. Nil in every shipping path; it exists because a simulator
    /// cannot be typed into from a script and "a phase ends with something
    /// actually sent to a real agent" is the gate this phase has to pass.
    private let autoSend: String?

    public init(worktree: String, account: String, sid: String,
                store: FleetStore, client: OrchestraClient, autoSend: String? = nil) {
        self.worktree = worktree
        self.account = account
        self.sid = sid
        self.fleet = store
        self.autoSend = autoSend
        _chat = State(initialValue: ChatStore(client: client, worktree: worktree,
                                              account: account, sid: sid))
    }

    private var card: Worktree? {
        fleet.state?.worktrees.first { $0.name == worktree }
    }

    /// Looked up on every pass. Nil once the session falls off the board — a
    /// transcript older than the observer's window, or a worktree that went
    /// away — and the header says so rather than showing the last status it knew
    /// as if it were still true.
    private var session: Session? {
        card?.sessions.first { $0.sid == sid }
    }

    private var canSend: Bool {
        ChatStore.canSend(card: card, sid: sid)
    }

    public var body: some View {
        // `.background`, NOT `ZStack { canvas.ignoresSafeArea(); content }`.
        // A ZStack sizes to its largest child, so a canvas that ignores the safe
        // area makes the whole stack full-bleed and the content lays out into
        // the inset.
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
            .task {
                guard let autoSend, !autoSend.isEmpty else { return }
                // Wait for the board, because `canSend` is decided from the card
                // and a send before the first frame would be refused for the
                // wrong reason.
                for _ in 0..<40 where !canSend {
                    try? await Task.sleep(nanoseconds: 250_000_000)
                }
                await chat.send(autoSend)
            }
            .onDisappear { chat.stop() }
    }

    @ViewBuilder
    private var content: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(Palette.hairline)
            if let message = chat.serverError {
                refusal(message)
            } else if let error = chat.transportError, chat.messages.isEmpty {
                FailureView(error: error) { Task { await chat.load() } }
            } else if chat.messages.isEmpty && chat.outbox.isEmpty && chat.loading {
                ProgressView().tint(Palette.textTertiary).padding(Space.xl)
                Spacer()
            } else if chat.messages.isEmpty && chat.outbox.isEmpty {
                ContentUnavailableView("nothing to show",
                                       systemImage: "text.bubble",
                                       description: Text("this transcript has no readable turns — "
                                                         + "the server filters slash commands and "
                                                         + "machine text out entirely"))
            } else {
                transcript
            }
            Divider().overlay(Palette.hairline)
            if canSend {
                composer
            } else {
                readOnlyNotice
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
                        ChatBubble(message: message,
                                   sentFromHere: chat.wasSentFromHere(message))
                            .id(message.id)
                    }
                    ForEach(chat.outbox) { item in
                        OutgoingBubble(item: item) { chat.dismiss(item.id) }
                            .id(item.id)
                    }
                    Color.clear.frame(height: Space.sm).id("bottom")
                }
                .padding(.horizontal, Space.lg)
                .padding(.vertical, Space.md)
            }
            .scrollIndicators(.hidden)
            // Auto-follow only when the reader is already at (or near) the
            // newest turn. The desktop force-scrolls on every poll and that
            // yanks a reader who has scrolled up into history back down on the
            // next frame; mobile follows only when the last turn is in view
            // (`UX.md` §4.2). `visibleRect.maxY` is the bottom of what is on
            // screen; within one comfortable line of the content's end counts as
            // pinned.
            .onScrollGeometryChange(for: Bool.self) { geo in
                geo.visibleRect.maxY >= geo.contentSize.height - Space.xxl
            } action: { _, atBottom in
                pinnedToBottom = atBottom
            }
            .onChange(of: chat.messages.count) { _, _ in
                if pinnedToBottom { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .onChange(of: chat.outbox.count) { _, _ in
                if pinnedToBottom { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            // A brand-new conversation opens at the newest turn.
            .task {
                proxy.scrollTo("bottom", anchor: .bottom)
                pinnedToBottom = true
            }
        }
    }

    // MARK: - The composer

    /// **No confirmation, no target picker, no pid.**
    ///
    /// The desktop's default — *"the session's own pid if reachable, else the
    /// first reachable proc"* — means typing into a different session's terminal.
    /// A phone makes it worse: a target-changing control directly above the text
    /// field sits in the highest-traffic thumb zone in the app, where an upward
    /// overshoot retargets the message. So the target is this session's own
    /// terminal or nothing, it is addressed by `(account, sid)`, and there is no
    /// control here that can change it (`UX.md` §3.3.1).
    private var composer: some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(alignment: .bottom, spacing: Space.sm) {
                TextField("reply to this agent", text: $draft, axis: .vertical)
                    .font(OrcFont.body)
                    .foregroundStyle(Palette.textPrimary)
                    .lineLimit(1...5)
                    .focused($composerFocused)
                    .textInputAutocapitalization(.sentences)
                    .padding(.horizontal, Space.md)
                    .padding(.vertical, Space.sm)
                    .background(Palette.sunken)
                    .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                        .stroke(Palette.control, lineWidth: 1))
                    // Return inserts a space, exactly as the far side would.
                    .onChange(of: draft) { _, new in
                        if new.contains(where: \.isNewline) {
                            draft = new.replacingOccurrences(of: "\n", with: " ")
                        }
                    }
                Button {
                    let text = draft
                    draft = ""
                    Task { await chat.send(text) }
                } label: {
                    Image(systemName: "arrow.up")
                        .font(OrcFont.button)
                        .foregroundStyle(sendEnabled ? Palette.canvas : Palette.textDisabled)
                        .frame(width: 44, height: 44)
                        .background(sendEnabled ? Palette.statusFree : Palette.raised)
                        .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                }
                .disabled(!sendEnabled)
                .accessibilityLabel("send this reply")
            }
            Text("newlines become spaces on the way to the terminal")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textDisabled)
        }
        .padding(.horizontal, Space.lg)
        .padding(.top, Space.sm)
        .padding(.bottom, Space.xs + accessoryHeight)
    }

    private var sendEnabled: Bool {
        !chat.sending && !WireText.collapsed(draft).isEmpty && session != nil
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

    /// **Replaced, not disabled.** A greyed-out text field at the bottom of this
    /// screen would say "you can nearly reply"; this says what is true and why.
    private var readOnlyNotice: some View {
        HStack(alignment: .top, spacing: Space.sm) {
            Image(systemName: "keyboard")
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: Space.xxs) {
                Text("read-only")
                    .font(OrcFont.status)
                    .foregroundStyle(Palette.textSecondary)
                Text("This session has no terminal that can be typed into. "
                     + "Cursor and VS Code terminals can't be scripted; an ended "
                     + "session has no terminal at all.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, Space.lg)
        .padding(.top, Space.sm)
        .padding(.bottom, Space.sm + accessoryHeight)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// One turn.
///
/// Yours right-aligned on `raised`, the agent's left-aligned on `surface` — and
/// both **selectable**, because a run-on 900-character paragraph regularly has a
/// command in the middle of it and copying that out is worth more than any
/// gesture.
struct ChatBubble: View {
    let message: ChatMessage
    /// This turn is one this phone typed and then watched arrive. The strongest
    /// receipt in the app, and it is on the real transcript turn rather than on a
    /// local echo of it.
    var sentFromHere: Bool = false
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
                    if sentFromHere {
                        Text(verbatim: "✓✓ sent from this phone")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.statusWorking)
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

/// A message this screen sent, until the transcript catches up with it.
///
/// The receipt vocabulary is deliberately shorter than `UX.md` §3.3.2's: there is
/// no `✓ queued` because nothing on this wire distinguishes a queued send from a
/// delivered one, and there is no server-proven `✓✓` because `/api/send` does not
/// prove anything — see `Outgoing`. What is here is what the server actually
/// tells us.
struct OutgoingBubble: View {
    let item: Outgoing
    let dismiss: () -> Void

    private var glyph: (String, Color) {
        switch item.state {
        case .sending: ("circle.dotted", Palette.textTertiary)
        case .typed: ("checkmark", Palette.statusWorking)
        case .inTranscript: ("checkmark.circle.fill", Palette.statusWorking)
        case .refused: ("exclamationmark.triangle.fill", Palette.statusNeeds)
        case .ambiguous: ("questionmark.diamond.fill", Palette.statusLimit)
        case .lost: ("questionmark.diamond.fill", Palette.statusLimit)
        }
    }

    private var note: String? {
        switch item.state {
        case .sending: nil
        case .typed(let m), .inTranscript(let m): m
        case .refused(let m), .ambiguous(let m), .lost(let m): m
        }
    }

    private var isFailure: Bool {
        switch item.state {
        case .sending, .typed, .inTranscript: false
        case .refused, .ambiguous, .lost: true
        }
    }

    var body: some View {
        HStack {
            Spacer(minLength: Space.xxl)
            VStack(alignment: .trailing, spacing: Space.xs) {
                Text(item.text)
                    .font(OrcFont.body)
                    .foregroundStyle(Palette.textPrimary)
                    .multilineTextAlignment(.leading)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .trailing)
                if let note {
                    HStack(alignment: .top, spacing: Space.xs) {
                        Image(systemName: glyph.0)
                            .font(OrcFont.meta)
                            .foregroundStyle(glyph.1)
                        // The server's own prose, verbatim. These messages were
                        // written carefully and they contain the remedy.
                        Text(note)
                            .font(OrcFont.meta)
                            .foregroundStyle(isFailure ? Palette.textSecondary : Palette.textTertiary)
                            .multilineTextAlignment(.leading)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    Image(systemName: glyph.0)
                        .font(OrcFont.meta)
                        .foregroundStyle(glyph.1)
                }
                if isFailure {
                    Button("dismiss", action: dismiss)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusFree)
                        .frame(minHeight: 30)
                }
            }
            .padding(Space.md)
            .background(Palette.raised)
            .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(isFailure ? Palette.statusNeeds.opacity(0.6) : Palette.control,
                        lineWidth: 1))
        }
        .accessibilityElement(children: .combine)
    }
}
