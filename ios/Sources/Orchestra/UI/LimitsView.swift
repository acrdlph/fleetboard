import SwiftUI

/// "Can I afford to start something, and why is that agent parked?"
///
/// **There is no refresh button, and that is the whole design of this screen.**
/// `GET /api/limits` without `refresh=1` is a cache read; with it, the server
/// shells out to `cclimits` for every account under a 90-second timeout while
/// mutating one global dict. `UX.md` §3.6 rules out hiding that behind a cheap
/// gesture, and a read-only build has nothing that needs it — so the screen
/// loads on appear and on an explicit pull, and says how old the numbers are.
public struct LimitsView: View {
    @Bindable private var store: LimitsStore

    @State private var now = Date()
    @State private var path: [String] = []
    /// An account slug to push once, on appear. Nil in every shipping path — it
    /// is how a script reaches Account Detail on a simulator that cannot be
    /// tapped. See `DebugRoute`.
    private let initialAccount: String?
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(store: LimitsStore, initialAccount: String? = nil) {
        self.store = store
        self.initialAccount = initialAccount
    }

    public var body: some View {
        NavigationStack(path: $path) {
            ZStack {
                Palette.canvas.ignoresSafeArea()
                BodyWash()
                content
            }
            .navigationTitle("limits")
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: String.self) { slug in
                if let account = store.report?.accounts.first(where: { $0.slug == slug }) {
                    AccountDetailView(account: account)
                } else {
                    ContentUnavailableView("no such account", systemImage: "person.slash",
                                           description: Text(verbatim: slug))
                }
            }
        }
        .onReceive(ticker) { now = $0 }
        .task {
            await store.load()
            if let initialAccount, path.isEmpty { path = [initialAccount] }
        }
    }

    @ViewBuilder
    private var content: some View {
        if let report = store.report, report.available {
            list(report)
        } else if let report = store.report {
            // `available: false` is the whole page, and it names the consequence
            // rather than only the cause: without cclimits, orchestra cannot tell
            // a limit-parked agent from an idle one.
            ContentUnavailableView {
                Label("usage isn't readable", systemImage: "chart.bar.xaxis")
            } description: {
                Text(verbatim: (report.error ?? "cclimits did not answer")
                     + "\n\nWithout this, orchestra can't tell a limit-parked agent "
                     + "from an idle one.")
            }
        } else if let error = store.error {
            FailureView(error: error) { Task { await store.load() } }
        } else {
            ProgressView().tint(Palette.textTertiary)
        }
    }

    private func list(_ report: LimitsReport) -> some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: Space.md) {
                if let fetched = report.fetched {
                    Text(verbatim: "fetched \(RelativeTime.short(since: fetched, now: now)) ago")
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.textTertiary)
                        .frame(maxWidth: .infinity, alignment: .trailing)
                }
                ForEach(Array(report.ranked.enumerated()), id: \.element.id) { rank, account in
                    NavigationLink(value: account.slug) {
                        AccountCard(account: account, isBest: rank == 0, now: now)
                    }
                    .buttonStyle(.plain)
                }
                Color.clear.frame(height: Space.xxl)
            }
            .padding(.horizontal, Space.lg)
            .padding(.top, Space.sm)
        }
        .scrollIndicators(.hidden)
        .refreshable { await store.load() }
    }
}

struct AccountCard: View {
    let account: AccountLimits
    let isBest: Bool
    let now: Date

    /// The one limit worth putting under the bar: whichever is out, else
    /// whichever resets soonest.
    private var headline: LimitBar? {
        account.exhausted.first
            ?? account.limits.filter { $0.resetsAt != nil }
                             .min { ($0.resetsAt ?? 0) < ($1.resetsAt ?? 0) }
    }

    private var hue: Color {
        if !account.ok { return Palette.textTertiary }
        if account.accountExhausted { return Palette.statusNeeds }
        if account.reserveBlocked { return Palette.statusLimit }
        return Palette.statusWorking
    }

    var body: some View {
        VStack(alignment: .leading, spacing: Space.sm) {
            HStack(spacing: Space.sm) {
                Text(account.label)
                    .font(OrcFont.cardName)
                    .foregroundStyle(Palette.textPrimary)
                if let plan = account.plan {
                    Text(plan.uppercased())
                        .font(OrcFont.label)
                        .orcTracking(11)
                        .foregroundStyle(Palette.textTertiary)
                }
                Spacer(minLength: 0)
                badge
            }
            HeadroomBar(fraction: (account.headroomPercent ?? 0) / 100,
                        hue: hue,
                        hatched: account.accountExhausted)
            HStack(spacing: Space.sm) {
                Text(verbatim: "\(Int(account.headroomPercent ?? 0))% left")
                    .font(OrcFont.status)
                    .foregroundStyle(hue)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            caption
        }
        .padding(Space.md)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Radius.md, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.md, style: .continuous)
            .stroke(account.accountExhausted ? Palette.statusNeeds.opacity(0.5) : Palette.hairline,
                    lineWidth: 1))
    }

    @ViewBuilder
    private var badge: some View {
        if !account.ok {
            Label("unreadable", systemImage: "questionmark.circle")
                .font(OrcFont.status).foregroundStyle(Palette.textTertiary)
        } else if account.accountExhausted {
            Label("EXHAUSTED", systemImage: "hourglass")
                .font(OrcFont.status).foregroundStyle(Palette.statusNeeds)
        } else if account.reserveBlocked {
            Label("RESERVE", systemImage: "lock")
                .font(OrcFont.status).foregroundStyle(Palette.statusLimit)
        } else if isBest {
            Label("MOST HEADROOM", systemImage: "diamond")
                .font(OrcFont.status).foregroundStyle(Palette.statusWorking)
        }
    }

    @ViewBuilder
    private var caption: some View {
        if !account.ok, let error = account.error {
            Text(error).font(OrcFont.meta).foregroundStyle(Palette.statusLimit).lineLimit(2)
        } else if account.reserveBlocked, let reserve = account.reservePercent {
            // Saying only the first half is the copy trap: auto-dispatch stops,
            // the person does not have to.
            Text(verbatim: "below its \(reserve)% reserve — auto-dispatch won't use it; you still can")
                .font(OrcFont.meta).foregroundStyle(Palette.statusLimit).lineLimit(2)
        } else if let limit = headline, let resets = limit.resets {
            Text(verbatim: "\(limit.label) · resets \(RelativeTime.clock(resets)) · "
                 + RelativeTime.countdown(to: resets, now: now))
                .font(OrcFont.meta).foregroundStyle(Palette.textTertiary)
        }
    }
}

/// The bar. **Hatched when exhausted**, because colour cannot travel alone and
/// "out" is the one state that must survive a greyscale screenshot.
struct HeadroomBar: View {
    let fraction: Double
    let hue: Color
    var hatched = false

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Palette.sunken)
                Capsule()
                    .fill(hue.opacity(hatched ? 0.35 : 1))
                    .frame(width: max(2, geo.size.width * min(1, max(0, fraction))))
                    .overlay {
                        if hatched {
                            Capsule().strokeBorder(hue, style: StrokeStyle(lineWidth: 1, dash: [3, 2]))
                        }
                    }
            }
        }
        .frame(height: 8)
        .accessibilityHidden(true)
    }
}

/// One account, every limit it has, with a countdown on every limit that has a
/// reset.
///
/// The desktop hides countdowns below 50 % used unless exhausted, so a healthy
/// account shows no reset information at all. On a phone "when does this free
/// up" is the whole question, so every one of them is printed.
public struct AccountDetailView: View {
    let account: AccountLimits
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(account: AccountLimits) {
        self.account = account
    }

    public var body: some View {
        ZStack {
            Palette.canvas.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: Space.lg) {
                    header
                    ForEach(account.limits) { limit in
                        bar(limit)
                    }
                    reserve
                    Color.clear.frame(height: Space.xxl)
                }
                .padding(.horizontal, Space.lg)
                .padding(.top, Space.sm)
            }
            .scrollIndicators(.hidden)
        }
        .navigationTitle(account.label)
        .navigationBarTitleDisplayMode(.inline)
        .onReceive(ticker) { now = $0 }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: Space.xxs) {
            HStack(spacing: Space.sm) {
                if let plan = account.plan {
                    Text(plan.uppercased()).font(OrcFont.label).orcTracking(11)
                }
                if let dir = account.configDir {
                    Text(dir).lineLimit(1).truncationMode(.head)
                }
            }
            .font(OrcFont.meta)
            .foregroundStyle(Palette.textTertiary)
            Text(verbatim: "\(Int(account.headroomPercent ?? 0))% left overall")
                .font(OrcFont.title)
                .foregroundStyle(Palette.textPrimary)
            // The slug and the label disagree routinely, and every session on the
            // board is tagged with the LABEL. Printing both is what makes this
            // page joinable to the fleet.
            if account.fbLabel != nil, account.fbLabel != account.slug {
                Text(verbatim: "cclimits calls this \(account.slug)")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textDisabled)
            }
        }
    }

    private func bar(_ limit: LimitBar) -> some View {
        VStack(alignment: .leading, spacing: Space.xs) {
            HStack(spacing: Space.sm) {
                Text(limit.label)
                    .font(OrcFont.status)
                    .foregroundStyle(Palette.textPrimary)
                if limit.modelScoped {
                    Text("model cap")
                        .font(OrcFont.label)
                        .orcTracking(11)
                        .padding(.horizontal, Space.sm)
                        .padding(.vertical, 1)
                        .foregroundStyle(Palette.statusTurn)
                        .overlay(RoundedRectangle(cornerRadius: Radius.xs, style: .continuous)
                            .stroke(Palette.statusTurn.opacity(0.6), lineWidth: 1))
                }
                Spacer(minLength: 0)
                Text(verbatim: "\(Int(limit.percent))% used")
                    .font(OrcFont.meta)
                    .foregroundStyle(limit.exhaustedNow ? Palette.statusNeeds : Palette.textTertiary)
            }
            HeadroomBar(fraction: limit.fraction,
                        hue: limit.exhaustedNow ? Palette.statusNeeds : Palette.statusWorking,
                        hatched: limit.exhaustedNow)
            if let resets = limit.resets {
                Text(verbatim: (limit.exhaustedNow ? "exhausted — resets " : "resets ")
                     + "\(RelativeTime.clock(resets)) · "
                     + RelativeTime.countdown(to: resets, now: now))
                    .font(OrcFont.meta)
                    .foregroundStyle(limit.exhaustedNow ? Palette.statusNeeds : Palette.textTertiary)
            } else {
                Text("no reset")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textDisabled)
            }
            if limit.modelScoped, limit.exhaustedNow {
                // A maxed model cap does NOT block the account. Collapsing that
                // distinction is an explicit anti-goal in the server.
                Text("only sessions running this model are blocked; the account itself is not")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textSecondary)
            }
        }
    }

    @ViewBuilder
    private var reserve: some View {
        if let reserve = account.reservePercent {
            VStack(alignment: .leading, spacing: Space.xs) {
                SectionLabel("RESERVE BUFFER")
                Text(verbatim: "\(reserve)%")
                    .font(OrcFont.title)
                    .foregroundStyle(Palette.textPrimary)
                Text(verbatim: reserve == 0
                     ? "no buffer — auto-dispatch will use this account down to empty"
                     : "auto-dispatch stops at \(100 - reserve)% of the weekly limit. "
                       + "You can still launch here by hand.")
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
        }
    }
}
