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

    /// Tertiary text that sits a step below Ink3 (an upcoming step, a
    /// placeholder, a build stamp). 70% of Ink3 is the floor: it still
    /// clears WCAG AA 4.5:1 on Paper, where the 40–60% it replaces fell
    /// below it. Under Increase Contrast it is full Ink3.
    static func cavnarInk3Muted(_ contrast: ColorSchemeContrast) -> Color {
        contrast == .increased ? Color("Ink3") : Color("Ink3").opacity(0.7)
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

    /// The categorical series palette — one colour per series where no
    /// series is good or bad (labor cost by role). Twelve, hue-separated
    /// for the dark ground; the ember leads as the dark token itself
    /// (#d4583a — the role donut used to lead with the light-mode
    /// #c84b2f). The rest have no semantic token on purpose: a role is not
    /// "good" green or "bad" red. A chart takes `cavnarSeries[i % count]`.
    static let cavnarSeries: [Color] = [
        .cavnarEmber,
        Color(red: 0.435, green: 0.812, blue: 0.592),  // #6fcf97 mint
        Color(red: 0.937, green: 0.624, blue: 0.153),  // #ef9f27 amber
        Color(red: 0.376, green: 0.678, blue: 0.961),  // #60adf5 sky blue
        Color(red: 0.702, green: 0.616, blue: 0.953),  // #b39df3 lavender
        Color(red: 0.957, green: 0.447, blue: 0.714),  // #f472b6 pink
        Color(red: 0.302, green: 0.816, blue: 0.882),  // #4dd0e1 cyan
        Color(red: 0.388, green: 0.400, blue: 0.945),  // #6366f1 indigo (lifted from #4338ca, which sank into the dark ground)
        Color(red: 0.851, green: 0.467, blue: 0.024),  // #d97706 burnt amber
        Color(red: 0.063, green: 0.725, blue: 0.506),  // #10b981 emerald (lifted from #059669)
        Color(red: 0.859, green: 0.153, blue: 0.467),  // #db2777 deep rose
        Color(red: 0.580, green: 0.639, blue: 0.722),  // #94a3b8 slate (lifted from #64748b)
    ]

    /// True black — reserved for nav/tab-bar chrome specifically (matches the
    /// web dashboard's own two-tier black system: content backgrounds use the
    /// warm near-black cavnarPaper, while header/nav chrome is forced to pure
    /// #000). Don't use this for content surfaces — use cavnarPaper/Paper2/
    /// Paper3 for those.
    static let cavnarChrome = Color("Chrome")
}
