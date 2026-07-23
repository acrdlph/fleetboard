import SwiftUI

/// The primary, consequence-bearing action of a sheet.
///
/// **It disables on tap and does not re-enable** — including on timeout
/// (`UX.md` §7.4). Recovery from an unanswered mutation is reconciliation, never
/// a button that has become live again while the Mac may still be acting on the
/// first press. With no idempotency key on this server, a re-enabled button is
/// the double-fire.
public struct PrimaryAction: View {
    private let title: String
    private let symbol: String?
    private let tint: Color
    private let enabled: Bool
    private let action: () -> Void

    public init(_ title: String, symbol: String? = nil,
                tint: Color = Palette.statusNeeds, enabled: Bool = true,
                action: @escaping () -> Void) {
        self.title = title
        self.symbol = symbol
        self.tint = tint
        self.enabled = enabled
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            HStack(spacing: Space.sm) {
                if let symbol { Image(systemName: symbol) }
                Text(title)
            }
            .font(OrcFont.button)
            .foregroundStyle(enabled ? Palette.canvas : Palette.textDisabled)
            .frame(maxWidth: .infinity, minHeight: 50)
            .background(enabled ? tint : Palette.raised)
            .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
        }
        .disabled(!enabled)
    }
}

/// A second action whose consequence differs in KIND from the primary — the
/// reserve-burning "use it anyway", the "chat instead". Outlined, never filled.
public struct SecondaryAction: View {
    private let title: String
    private let symbol: String?
    private let tint: Color
    private let enabled: Bool
    private let action: () -> Void

    public init(_ title: String, symbol: String? = nil,
                tint: Color = Palette.statusLimit, enabled: Bool = true,
                action: @escaping () -> Void) {
        self.title = title
        self.symbol = symbol
        self.tint = tint
        self.enabled = enabled
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            HStack(spacing: Space.sm) {
                if let symbol { Image(systemName: symbol) }
                Text(title)
            }
            .font(OrcFont.button)
            .foregroundStyle(enabled ? tint : Palette.textDisabled)
            .frame(maxWidth: .infinity, minHeight: 50)
            .background(tint.opacity(enabled ? 0.10 : 0))
            .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                .stroke(enabled ? tint.opacity(0.7) : Palette.control, lineWidth: 1))
        }
        .disabled(!enabled)
    }
}

/// Cancel. Always the bottom-most control on a confirmation sheet.
public struct CancelAction: View {
    private let title: String
    private let action: () -> Void

    public init(_ title: String = "Cancel", action: @escaping () -> Void) {
        self.title = title
        self.action = action
    }

    public var body: some View {
        Button(action: action) {
            Text(title)
                .font(OrcFont.button)
                .foregroundStyle(Palette.textSecondary)
                .frame(maxWidth: .infinity, minHeight: 50)
                .background(Palette.raised)
                .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
                    .stroke(Palette.control, lineWidth: 1))
        }
    }
}

/// The ≥24 pt of dead space rule 2 asks for, named so a reviewer can see it is
/// there rather than counting padding.
public struct ConsequenceGap: View {
    public init() {}
    public var body: some View { Color.clear.frame(height: Space.xl) }
}

/// A sheet's title and its consequence prose.
public struct SheetHeader: View {
    private let title: String
    private let symbol: String?
    private let hue: Color

    public init(_ title: String, symbol: String? = nil, hue: Color = Palette.textPrimary) {
        self.title = title
        self.symbol = symbol
        self.hue = hue
    }

    public var body: some View {
        HStack(spacing: Space.sm) {
            if let symbol {
                Image(systemName: symbol).foregroundStyle(hue)
            }
            Text(title)
                .font(OrcFont.title)
                .foregroundStyle(Palette.textPrimary)
            Spacer(minLength: 0)
        }
    }
}

/// The server said something and it is shown **verbatim**.
///
/// Every refusal in this app funnels through here rather than through a
/// per-call-site `Text`, because the rule is easy to state and easy to forget:
/// *these messages were written carefully and they contain the remedy.* A
/// generic "failed" throws away the only part of the response worth reading.
public struct ServerSays: View {
    public enum Tone: Sendable { case ok, refusal, unknown }

    private let text: String
    private let tone: Tone

    public init(_ text: String, tone: Tone) {
        self.text = text
        self.tone = tone
    }

    private var hue: Color {
        switch tone {
        case .ok: Palette.statusWorking
        case .refusal: Palette.statusNeeds
        case .unknown: Palette.statusLimit
        }
    }

    private var symbol: String {
        switch tone {
        case .ok: "checkmark.circle.fill"
        case .refusal: "exclamationmark.triangle.fill"
        case .unknown: "questionmark.diamond.fill"
        }
    }

    public var body: some View {
        HStack(alignment: .top, spacing: Space.sm) {
            Image(systemName: symbol)
                .font(OrcFont.meta)
                .foregroundStyle(hue)
                .accessibilityHidden(true)
            Text(text)
                .font(OrcFont.bodyCompact)
                .foregroundStyle(Palette.textSecondary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .textSelection(.enabled)
        }
        .padding(Space.md)
        .background(hue.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: Radius.sm, style: .continuous)
            .stroke(hue.opacity(0.45), lineWidth: 1))
    }
}

/// One line of "here is what this will actually do".
public struct ConsequenceRow: View {
    private let arrow: String
    private let text: String
    private let detail: String?
    private let hue: Color

    public init(_ text: String, detail: String? = nil,
                arrow: String = "arrow.right", hue: Color = Palette.textPrimary) {
        self.text = text
        self.detail = detail
        self.arrow = arrow
        self.hue = hue
    }

    public var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: Space.sm) {
            Image(systemName: arrow)
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
                .accessibilityHidden(true)
            Text(text)
                .font(OrcFont.code)
                .foregroundStyle(hue)
            if let detail {
                Text(detail)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textTertiary)
            }
            Spacer(minLength: 0)
        }
    }
}

/// The elapsed counter that stands in for progress when there is none to show.
///
/// **A staged label on a synchronous call with no job id is a timed fiction.**
/// `POST /api/finish` runs a `git fetch` (30 s), a merge-base, a `git status`, a
/// process scan and an osascript send inside one request, with wildly asymmetric
/// worst cases — so a label reading "typing the brief…" would sit on the wrong
/// stage most of the time. An honest indeterminate spinner and a real elapsed
/// count say exactly as much and never lie (`UX.md` §4.4).
public struct HonestProgress: View {
    private let since: Date
    private let caption: String
    @State private var now = Date()
    private let ticker = Timer.publish(every: 1, on: .main, in: .common).autoconnect()

    public init(since: Date, caption: String) {
        self.since = since
        self.caption = caption
    }

    public var body: some View {
        HStack(spacing: Space.sm) {
            ProgressView().tint(Palette.textTertiary)
            Text(verbatim: caption + " · " + RelativeTime.short(since: since, now: now))
                .font(OrcFont.meta)
                .foregroundStyle(Palette.textTertiary)
            Spacer(minLength: 0)
        }
        .onReceive(ticker) { now = $0 }
    }
}
