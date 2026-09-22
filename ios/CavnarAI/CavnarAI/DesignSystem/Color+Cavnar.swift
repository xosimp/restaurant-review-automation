import SwiftUI

/// The web dashboard's semantic CSS-variable palette (--ink/--paper/--ember/
/// etc., templates/dashboard.html), ported 1:1 as named Color Sets with
/// light/dark appearances baked in — see Assets.xcassets/Colors. Reference
/// these instead of Color(hex:) anywhere in the app so a future palette
/// change only touches the asset catalog.
extension Color {
    static let cavnarInk = Color("Ink")
    static let cavnarInk2 = Color("Ink2")
    static let cavnarInk3 = Color("Ink3")

    /// Secondary text, lifted when the user has Increase Contrast on.
    ///
    /// The app is dark-only by design (correct for a dim dining room), but it
    /// is also used at the pass under bright task lighting and outdoors on a
    /// patio — the two environments where low-contrast secondary text on a
    /// near-black ground is hardest to read, and Ink3 on Paper sits below the
    /// WCAG AA 4.5:1 threshold for body text. Increase Contrast is the system
    /// signal that someone is struggling; honour it rather than ignoring it
    /// (audit 7.7).
    ///
    /// Use at call sites carrying real information; decorative chrome can stay
    /// on the plain token.
    static func cavnarInk3(_ contrast: ColorSchemeContrast) -> Color {
        contrast == .increased ? Color(white: 0.82) : Color("Ink3")
    }

    static let cavnarPaper = Color("Paper")
    static let cavnarPaper2 = Color("Paper2")
    static let cavnarPaper3 = Color("Paper3")

    static let cavnarEmber = Color("Ember")
    static let cavnarEmber2 = Color("Ember2")

    static let cavnarGreen = Color("Green")
    static let cavnarGreenBg = Color("GreenBg")
    static let cavnarRed = Color("Red")
    static let cavnarRedBg = Color("RedBg")
    static let cavnarAmber = Color("Amber")
    static let cavnarAmberBg = Color("AmberBg")
    static let cavnarBlue = Color("Blue")
    static let cavnarBlueBg = Color("BlueBg")

    static let cavnarSurface = Color("Surface")

    /// True black — reserved for nav/tab-bar chrome specifically (matches the
    /// web dashboard's own two-tier black system: content backgrounds use the
    /// warm near-black cavnarPaper, while header/nav chrome is forced to pure
    /// #000). Don't use this for content surfaces — use cavnarPaper/Paper2/
    /// Paper3 for those.
    static let cavnarChrome = Color("Chrome")
}
