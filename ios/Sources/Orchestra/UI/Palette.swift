import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// The colour tokens of `UX.md` §9.2, with their measured contrast ratios kept
/// beside them.
///
/// **Every variant of a token is resolved in one place.** `UX.md` §9.1 asks for
/// an Asset Catalog carrying four variants of each colour, for the stated reason
/// that a palette with a ternary at the call site is a palette whose
/// high-contrast mode gets applied 60 %. That property is what matters, not the
/// catalog: `UIColor(dynamicProvider:)` resolves light / dark / high-contrast
/// from the real `UITraitCollection` — the same trait the catalog reads — and it
/// does it here, once, for every token. There is still no ternary at any call
/// site. The catalog is the better long-term home (it reaches widgets and
/// notification content, which run out of process); this is the version that
/// does not need twenty-odd JSON directories to compile.
///
/// Ratios in the comments are WCAG 2.x relative luminance with sRGB alpha
/// compositing, taken from `UX.md` §9.2 where they were computed.
public enum Palette {
    // MARK: Surfaces — a child is always lighter than its parent (Night)

    /// scroll background
    public static let canvas = token(dark: 0x0D0D0D, light: 0xF4F2EF, darkHC: 0x000000)
    /// inset wells: session rows, text fields, code blocks
    public static let sunken = token(dark: 0x111111, light: 0xE9E6E2, darkHC: 0x0A0A0A)
    /// the `ended` row ground — the one place opacity was replaced by a darker
    /// ground, because an ended row still carries model, account, age and topic
    public static let sunkenDim = token(dark: 0x0E0E0E, light: 0xEDEBE7, darkHC: 0x050505)
    /// cards, sheets, list containers
    public static let surface = token(dark: 0x161616, light: 0xFCFBFA, darkHC: 0x141414)
    /// card headers, chips, your chat bubble
    public static let raised = token(dark: 0x1C1C1C, light: 0xFFFFFF, darkHC: 0x1E1E1E)

    // MARK: Borders

    /// 1.26:1 — decorative only, never the sole identity of a control
    public static let hairline = token(dark: 0x2A2A2A, light: 0xDCD8D3, darkHC: 0x3A3A3A)
    /// 1.59:1 — outline of a control that also carries a text label
    public static let control = token(dark: 0x3A3A3A, light: 0xB3AEA8, darkHC: 0x585858)
    /// 3.15:1 on `raised`, its worst real ground — outline of any control
    /// identified ONLY by its border or a symbol
    public static let controlStrong = token(dark: 0x6A6A6A, light: 0x8A857F, darkHC: 0x8A8A8A)

    // MARK: Text

    /// 14.53 on `surface`
    public static let textPrimary = token(dark: 0xE8E6E3, light: 0x171614, darkHC: 0xF5F3F1)
    /// 7.31 on `surface` — the human voice, readable rather than quiet
    public static let textSecondary = token(dark: 0xA8A4A0, light: 0x4E4B47, darkHC: 0xC9C6C2)
    /// 5.07 on `surface`
    public static let textTertiary = token(dark: 0x8A8784, light: 0x68645F, darkHC: 0xB4B0AC)
    /// **Fails AA on every surface.** Named `textDisabled` precisely so that
    /// reaching for it to style a timestamp feels wrong — the desktop does
    /// exactly that at 3.0:1 and it is one of the four corrections owed back.
    public static let textDisabled = token(dark: 0x6A6764, light: 0x8E8A85, darkHC: 0x8A8784)

    // MARK: Status hues — five, and each one MEANS something

    /// NEEDS ANSWER · BLOCKED · NEEDS YOU · errors. 5.80 on `surface`
    public static let statusNeeds = token(dark: 0xD97757, light: 0x9A553E, darkHC: 0xEB8F6F)
    /// YOUR TURN (idle at the prompt) · commit hash. 10.46 on `surface`.
    /// Changed from the desktop's `#E8A87C` on measured colour-vision grounds.
    public static let statusTurn = token(dark: 0xEDB9AC, light: 0x7D615B, darkHC: 0xF2C7BC)
    /// WORKING · BUSY · armed · ok. 7.60 on `surface`
    public static let statusWorking = token(dark: 0x87B386, light: 0x536E53, darkHC: 0xA3CDA2)
    /// FREE · identifiers: `[account]`, branch, tty, paths. 7.91 on `surface`
    public static let statusFree = token(dark: 0x7FB3C8, light: 0x4D6C79, darkHC: 0x9CCFE4)
    /// LIMIT HIT · WAITING · dirty Δ · caution. 8.80 on `surface`
    public static let statusLimit = token(dark: 0xD4B06A, light: 0x79653D, darkHC: 0xE6C684)

    // MARK: Tint fills

    /// **α = 0.12 is a hard ceiling and it is not a taste.** The worst pair
    /// (`statusNeeds` text on its own fill over `raised`) measures 4.62 at 0.12
    /// and 4.33 at 0.16 — below AA. The desktop currently ships 0.25 behind
    /// accent text on its highest-priority badge, at 3.72:1, which is a live AA
    /// failure worth back-porting.
    ///
    /// In Contrast+ and in Daylight the fill is **removed entirely** rather than
    /// reduced: at α 0.20 the Contrast+ needs pair is 4.93, nowhere near the AAA
    /// the mode exists for, and no Daylight alpha from 0.10 up clears AA at all.
    /// The badge's identity comes from its stroke and its coloured word.
    ///
    /// The rule is applied in exactly one place — `StatusPill.fillAlpha` — and
    /// lives here as prose rather than as a second constant, because a duplicated
    /// threshold is a second thing to edit and a first thing to forget.

    // MARK: - Resolution

    /// One token, four variants. Light high-contrast reuses the Daylight value:
    /// `UX.md` §9.2 gives measured Contrast+ figures for Night only, and
    /// inventing a light-HC ramp here would be a number with no measurement
    /// behind it — which is the one thing this project does not do.
    static func token(dark: UInt32, light: UInt32, darkHC: UInt32) -> Color {
        #if canImport(UIKit)
        return Color(uiColor: UIColor { traits in
            let isDark = traits.userInterfaceStyle == .dark
            let isHC = traits.accessibilityContrast == .high
            switch (isDark, isHC) {
            case (true, true):  return UIColor(rgb: darkHC)
            case (true, false): return UIColor(rgb: dark)
            case (false, _):    return UIColor(rgb: light)
            }
        })
        #else
        return Color(rgb: dark)
        #endif
    }
}

#if canImport(UIKit)
extension UIColor {
    convenience init(rgb: UInt32) {
        self.init(red: Double((rgb >> 16) & 0xFF) / 255,
                  green: Double((rgb >> 8) & 0xFF) / 255,
                  blue: Double(rgb & 0xFF) / 255,
                  alpha: 1)
    }
}
#else
extension Color {
    init(rgb: UInt32) {
        self.init(red: Double((rgb >> 16) & 0xFF) / 255,
                  green: Double((rgb >> 8) & 0xFF) / 255,
                  blue: Double(rgb & 0xFF) / 255)
    }
}
#endif

/// The body wash — the desktop's `radial-gradient(1200px 500px at 70% -10%, …)`.
///
/// `RadialGradient` is circular and cannot express a 2.4:1 ellipse;
/// `EllipticalGradient` can. Anchored to the SCREEN and not to scroll content,
/// and non-interactive, so it can never eat a tap.
public struct BodyWash: View {
    public init() {}
    public var body: some View {
        EllipticalGradient(
            colors: [Palette.statusNeeds.opacity(0.05), .clear],
            center: UnitPoint(x: 0.7, y: -0.1),
            startRadiusFraction: 0,
            endRadiusFraction: 0.6
        )
        .allowsHitTesting(false)
        .ignoresSafeArea()
    }
}
