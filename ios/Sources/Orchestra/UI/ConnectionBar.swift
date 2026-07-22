import SwiftUI

/// The one place the app says whether to believe the screen.
///
/// It is a strip pinned above the tab bar rather than a full-screen takeover,
/// because on a phone a connection problem almost never means "you cannot use
/// this" — it means "what you are looking at is four minutes old", and the
/// board's own content is still the most useful thing on the display.
///
/// **The rule the copy has to get right** (IOS-APP.md §5.5): *ages keep ticking
/// while stale; statuses dim.* An age derives from an absolute transcript write
/// stamp, so "8 m ago" stays literally true with a dead stream and degrades in
/// the safe direction — counting up towards "we don't know". A `● WORKING`
/// badge on a four-minute-old snapshot is a lie, so the board behind this bar is
/// dimmed while it shows anything but `live`.
public struct ConnectionBar: View {
    private let link: LinkState
    private let staleness: Staleness
    private let version: Int?
    private let retry: () -> Void

    public init(link: LinkState, staleness: Staleness, version: Int?,
                retry: @escaping () -> Void) {
        self.link = link
        self.staleness = staleness
        self.version = version
        self.retry = retry
    }

    private var hue: Color {
        switch link {
        case .live: staleness.isStale ? Palette.statusLimit : Palette.statusWorking
        case .connecting, .idle: Palette.textTertiary
        case .reconnecting, .refused: Palette.statusLimit
        case .offline, .unauthorized: Palette.statusNeeds
        }
    }

    /// The stale half of the line, and it is deliberately separate from the link
    /// state: a live socket with a board four minutes behind it is a different
    /// fact from a dead socket, and both can be true at once.
    private var ageLine: String? {
        switch staleness {
        case .fresh, .absent: nil
        case .silent(let s): "no frame for \(RelativeTime.short(s)) — the link may be wedged"
        case .stale(let s): "showing data from \(RelativeTime.short(s)) ago"
        }
    }

    public var body: some View {
        HStack(spacing: Space.sm) {
            Image(systemName: link.symbol)
                .font(OrcFont.status)
                .foregroundStyle(hue)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: Space.xs) {
                    Text(link.caption)
                        .font(OrcFont.status)
                        .foregroundStyle(hue)
                        .lineLimit(1)
                    if let version, link.isLive {
                        Text(verbatim: "v\(version)")
                            .font(OrcFont.meta)
                            .foregroundStyle(Palette.textDisabled)
                    }
                }
                if let ageLine {
                    Text(ageLine)
                        .font(OrcFont.meta)
                        .foregroundStyle(Palette.statusLimit)
                        .lineLimit(1)
                }
            }
            Spacer(minLength: 0)
            if !link.isLive || staleness.isStale {
                Button(action: retry) {
                    Image(systemName: "arrow.clockwise")
                        .font(OrcFont.status)
                        .foregroundStyle(Palette.textSecondary)
                        .frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("reconnect now")
            }
        }
        .padding(.horizontal, Space.lg)
        .padding(.vertical, Space.xs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.ultraThinMaterial)
        .overlay(alignment: .top) {
            Rectangle().fill(Palette.hairline).frame(height: 1)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(ageLine.map { "\(link.caption), \($0)" } ?? link.caption)
    }
}
