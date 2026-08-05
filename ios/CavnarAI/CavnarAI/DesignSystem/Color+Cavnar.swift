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

    /// Per-tenant white-label accent (restaurant.brand_color, read from the
    /// /mobile/api/home payload) — falls back to the default ember when a
    /// restaurant hasn't set one. Use this instead of .cavnarEmber for any
    /// "brand accent" usage (FAB, selected state, primary button) so a
    /// white-labeled restaurant's own color shows through; keep using
    /// .cavnarEmber directly only for chrome that's deliberately
    /// Cavnar-branded rather than tenant-branded.
    static func cavnarAccent(brandColorHex: String?) -> Color {
        guard let hex = brandColorHex, let color = Color(hex: hex) else { return .cavnarEmber }
        return color
    }
}

extension Color {
    /// Parses a "#rrggbb" or "rrggbb" string. Returns nil for anything else
    /// rather than silently falling back to black, since a bad brand_color
    /// value should be treated the same as "not set" by the caller.
    init?(hex: String) {
        var hexString = hex.trimmingCharacters(in: .whitespacesAndNewlines)
        if hexString.hasPrefix("#") { hexString.removeFirst() }
        guard hexString.count == 6, let value = UInt32(hexString, radix: 16) else { return nil }
        let r = Double((value >> 16) & 0xFF) / 255.0
        let g = Double((value >> 8) & 0xFF) / 255.0
        let b = Double(value & 0xFF) / 255.0
        self.init(red: r, green: g, blue: b)
    }
}
