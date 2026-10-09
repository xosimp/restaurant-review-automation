import SwiftUI

/// The web dashboard's semantic CSS-variable palette (--ink/--paper/--ember/
/// etc., templates/dashboard.html), ported 1:1 as named Color Sets with
/// light/dark appearances baked in — see Assets.xcassets/Colors. Reference
/// these instead of Color(hex:) anywhere in the app so a future palette
/// change only touches the asset catalog.
extension Color {
    /// The three ink tiers (dark appearance — the app forces dark, RootView).
    /// Retuned 10/8/26 (iOS readability round): the old Ink2 #E0D6C6 and
    /// Ink3 #CDBFA9 sat only 1.2x / 1.6x below Ink, so a screen's hierarchy
    /// didn't read and Ink3 had become the default text colour. Each tier is
    /// now ~1.7x below the one above it and all three still clear WCAG AA.
    /// Contrast on Paper #0C0C0C (on a card ground, Paper2 at 60% ≈ #101010,
    /// each is 0.1–0.4 lower — Red 4.4:1, Ink3 5.7:1):
    ///
    /// | Token | Dark | Ratio | Increase Contrast | Ratio |
    /// |---|---|---|---|---|
    /// | Ink | #F0EBE0 | 16.5:1 | — | — |
    /// | Ink2 | #BDB5A8 | 9.6:1 | #D9D2C6 | 13.0:1 |
    /// | Ink3 | #948C80 | 5.9:1 | #B8B0A3 | 9.1:1 |
    /// | Ember | #D4583A | 4.9:1 | #E0664A | 5.8:1 |
    /// | Red | #E3333F | 4.5:1 | #FF7880 | 7.7:1 |
    /// | RedText | #F05A63 | 5.9:1 | #FF7880 | 7.7:1 |
    /// | Ember2 | #E8956A | 8.3:1 | — | — |
    ///
    /// The Increase Contrast variants live in the colorsets themselves
    /// (`contrast: high`), so every use of these tokens follows the setting
    /// without a helper. Ink2 is body text; Ink3 is captions and meta only,
    /// never body or a label; an ink at less than 60% opacity is never text.
    static let cavnarInk = Color("Ink")
    static let cavnarInk2 = Color("Ink2")
    static let cavnarInk3 = Color("Ink3")

    /// Ink3 for text carrying real information. The colorset now carries its
    /// own Increase Contrast variant (#B8B0A3, 9.1:1), so this is the plain
    /// token; kept so existing call sites compile and read as deliberate. It
    /// used to swap in a flat grey (white 0.82) under Increase Contrast
    /// (audit 7.7) because the colorset had no variant of its own.
    static func cavnarInk3(_ contrast: ColorSchemeContrast) -> Color {
        .cavnarInk3
    }

    /// Text a step below Ink3 (an upcoming step, a placeholder, a build
    /// stamp): Ink2 at 70% — #888279 on Paper, 5.1:1, so it still clears AA.
    /// (70% of the retuned Ink3 would be 3.4:1.) Under Increase Contrast it
    /// is full Ink3, whose own high-contrast variant applies.
    static func cavnarInk3Muted(_ contrast: ColorSchemeContrast) -> Color {
        contrast == .increased ? .cavnarInk3 : Color("Ink2").opacity(0.7)
    }

    static let cavnarPaper = Color("Paper")
    static let cavnarPaper2 = Color("Paper2")
    static let cavnarPaper3 = Color("Paper3")

    static let cavnarEmber = Color("Ember")
    static let cavnarEmber2 = Color("Ember2")
    /// Ember for a FILL that carries white text — the primary and split
    /// buttons (`CavnarPremiumButtonSurface`). The look is the brand ember
    /// (#D4583A, white 4.0:1 at the buttons' 16pt bold); its Increase
    /// Contrast variant goes DARKER (#B84529, white 5.4:1), where Ember's
    /// own goes lighter (#E0664A — right for ember text on Paper, but white
    /// on it fell to 3.4:1). Re-audit S1, 10/8/26. Ember stays the brand
    /// colour everywhere else.
    static let cavnarEmberFill = Color("EmberFill")

    static let cavnarGreen = Color("Green")
    static let cavnarGreenBg = Color("GreenBg")
    /// Red for fills, bars, dots and icons. Under 18pt it is 4.5:1 on Paper
    /// and ~4.3:1 on a card — small red TEXT takes `cavnarRedText`.
    static let cavnarRed = Color("Red")
    /// Red for small text (an error line, an over-target figure under
    /// 18pt): #F05A63, 5.9:1 on Paper, 5.8:1 on a card.
    static let cavnarRedText = Color("RedText")
    /// Red for a small FILL that carries white text — the bell's count
    /// badge: #D42C38, white 5.0:1 (white on Red was 4.4:1); Increase
    /// Contrast #C42430, 5.8:1. Re-audit S12, 10/8/26.
    static let cavnarRedFill = Color("RedFill")
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
