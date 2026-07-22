import SwiftUI

/// The 20 pt fork strip — one branch's whole geometry, in one `Canvas` (§5.5).
///
/// **The rule, so it is not violated later: the `Canvas` renders geometry ONLY.**
/// The x-positioned fork-age label is a real SwiftUI `Text` in a `ZStack`
/// overlay, at most one per row — which keeps Dynamic Type, localisation and
/// truncation, and structurally removes the desktop's `chars × 6.6` width guess.
///
/// Six primitives, in draw order: a full-width baseline hairline; the trunk-tip
/// caret (the shared reference, in every row); the lane fork→tip; the clipped `⟨`
/// cap or the fork dot; the tip donut with a working pulse; and — as the overlay,
/// not in the `Canvas` — the fork-age text.
struct ForkStrip: View {
    let branch: TopoBranch
    let trunkTs: Double
    let axis: BranchMap.AxisScale
    /// The live tip colour, joined from the board by name. Neutral when the board
    /// has no card for this worktree.
    let hue: Color
    /// Whether to pulse the tip. Scoped so a non-working row does zero per-frame
    /// work (§5.11) — the `TimelineView` is only installed when this is true.
    let working: Bool

    /// Insets: the `⟨` cap needs room on the left, the trunk caret on the right.
    private let leftInset: CGFloat = 14
    private let rightInset: CGFloat = 16
    private let height: CGFloat = 20

    var body: some View {
        GeometryReader { geo in
            let width = geo.size.width
            let a = max(1, width - leftInset - rightInset)
            let clipped = axis.isClipped(branch.forkTs)
            let forkX = clipped ? leftInset
                : CGFloat(axis.x(branch.forkTs, padL: Double(leftInset), width: Double(a)))
            let tipX = CGFloat(axis.x(branch.tipTs, padL: Double(leftInset), width: Double(a)))
            let trunkX = CGFloat(axis.x(trunkTs, padL: Double(leftInset), width: Double(a)))
            let midY = height / 2

            ZStack(alignment: .topLeading) {
                if working {
                    TimelineView(.animation) { timeline in
                        canvas(width: width, forkX: forkX, tipX: tipX, trunkX: trunkX,
                               midY: midY, clipped: clipped,
                               phase: pulsePhase(timeline.date))
                    }
                } else {
                    canvas(width: width, forkX: forkX, tipX: tipX, trunkX: trunkX,
                           midY: midY, clipped: clipped, phase: nil)
                }

                // The one positioned Text — this row's own fork age. Centred under
                // the fork dot, clamped so it never runs off either margin. `.position`
                // centres the view, so this needs no width measurement and no
                // actor-isolated helper in an alignment closure.
                Text(forkAgeLabel)
                    .font(OrcFont.meta)
                    .foregroundStyle(Palette.textDisabled)
                    .fixedSize()
                    .position(x: min(max(forkX, 16), max(16, width - 16)), y: midY + 12)
                    .accessibilityHidden(true)
            }
        }
        .frame(height: height + 14)   // band + room for the age label beneath it
    }

    private func canvas(width: CGFloat, forkX: CGFloat, tipX: CGFloat, trunkX: CGFloat,
                        midY: CGFloat, clipped: Bool, phase: Double?) -> some View {
        Canvas { ctx, _ in
            // 1 — baseline hairline, full width
            var base = Path()
            base.move(to: CGPoint(x: 0, y: midY))
            base.addLine(to: CGPoint(x: width, y: midY))
            ctx.stroke(base, with: .color(Palette.textDisabled.opacity(0.30)), lineWidth: 0.5)

            // 2 — trunk tip caret (the shared reference mark)
            let caret = 4.0
            var tri = Path()
            tri.move(to: CGPoint(x: trunkX - caret, y: midY - caret))
            tri.addLine(to: CGPoint(x: trunkX + caret, y: midY - caret))
            tri.addLine(to: CGPoint(x: trunkX, y: midY + caret))
            tri.closeSubpath()
            ctx.fill(tri, with: .color(Palette.textTertiary.opacity(0.9)))

            // 3 — lane, fork → tip
            if tipX > forkX + 0.5 {
                var lane = Path()
                lane.move(to: CGPoint(x: forkX, y: midY))
                lane.addLine(to: CGPoint(x: tipX, y: midY))
                ctx.stroke(lane, with: .color(hue.opacity(0.85)), lineWidth: 2)
            }

            // 5 (drawn before 4 so the pulse sits under both dots) — working pulse
            if let phase {
                let r = 6.0 + 6.0 * phase
                let alpha = 0.7 * (1 - phase)
                let ring = Path(ellipseIn: CGRect(x: tipX - r, y: midY - r, width: 2 * r, height: 2 * r))
                ctx.stroke(ring, with: .color(hue.opacity(alpha)), lineWidth: 1.5)
            }

            // 4 — clipped cap ⟨, or the fork dot
            if clipped {
                var chevron = Path()
                chevron.move(to: CGPoint(x: leftInset + 3, y: midY - 5))
                chevron.addLine(to: CGPoint(x: leftInset - 3, y: midY))
                chevron.addLine(to: CGPoint(x: leftInset + 3, y: midY + 5))
                ctx.stroke(chevron, with: .color(hue.opacity(0.9)),
                           style: StrokeStyle(lineWidth: 2, lineCap: .round, lineJoin: .round))
            } else {
                let fr = 3.5
                let disc = Path(ellipseIn: CGRect(x: forkX - fr, y: midY - fr, width: 2 * fr, height: 2 * fr))
                ctx.fill(disc, with: .color(Palette.canvas))            // knock out the lane
                ctx.stroke(disc, with: .color(hue), lineWidth: 1.5)
            }

            // 6 — tip donut: 10 pt ring + a filled core
            let outer = Path(ellipseIn: CGRect(x: tipX - 5, y: midY - 5, width: 10, height: 10))
            ctx.fill(outer, with: .color(Palette.canvas))
            ctx.stroke(outer, with: .color(hue), lineWidth: 1.5)
            let core = Path(ellipseIn: CGRect(x: tipX - 2, y: midY - 2, width: 4, height: 4))
            ctx.fill(core, with: .color(hue))
        }
        .frame(width: width, height: height)
    }

    /// A 1.7 s triangle wave in `[0, 1]`, matching the desktop's `r 6 → 12` pulse.
    private func pulsePhase(_ date: Date) -> Double {
        let t = date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.7) / 1.7
        return t
    }

    private var forkAgeLabel: String {
        RelativeTime.short(axis.now - branch.forkTs)
    }
}
