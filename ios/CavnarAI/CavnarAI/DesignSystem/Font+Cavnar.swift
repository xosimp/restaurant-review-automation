import SwiftUI
import UIKit
import CoreText

/// The web app's typography system (per project convention): Clash Display
/// for headlines/words, Apfel Grotezk for UI chrome, Space Grotesk for
/// numbers. Clash Display and Apfel Grotezk ship as static per-weight .ttf
/// files (not variable fonts — Fontshare/Fontsource only distribute them
/// that way), see CavnarAI/Fonts/ + Info.plist's UIAppFonts. Space Grotesk
/// is still a true variable font, so it alone still goes through the
/// `variableFont` wght-axis helper below.
extension Font {
    /// Clash Display — headlines and standalone words (matches the
    /// dashboard's `--headline` usage).
    ///
    /// `relativeTo:` is what makes a custom font participate in Dynamic Type.
    /// Without it, `.custom(_:size:)` is frozen at `size` no matter what the
    /// user sets under Accessibility -> Larger Text — the app was literally
    /// unreadable-by-design for anyone who needs bigger text, which for
    /// restaurant owners reading small financial figures in dim light is the
    /// single biggest accessibility defect there was (audit 7.1). Because all
    /// typography routes through these three helpers, adding it here fixes
    /// every one of the ~400 call sites at once.
    static func cavnarHeadline(_ size: CGFloat, weight: ClashWeight = .semibold) -> Font {
        .custom(weight.postScriptName, size: size, relativeTo: headlineTextStyle(for: size))
    }

    enum ClashWeight {
        case regular, medium, semibold, bold

        var postScriptName: String {
            switch self {
            case .regular:  return "ClashDisplay-Regular"
            case .medium:   return "ClashDisplay-Medium"
            case .semibold: return "ClashDisplay-Semibold"
            case .bold:     return "ClashDisplay-Bold"
            }
        }
    }

    /// Apfel Grotezk — UI chrome (labels, buttons, body text). Only ships
    /// Regular/Fett (Bold) as real weights, unlike the variable font this
    /// replaced — call sites still pass a CGFloat weight (400...800, same
    /// as before, so none of ~400 existing call sites needed to change),
    /// snapped to whichever of the two real weights it's closer to.
    static func cavnarBody(_ size: CGFloat, weight: CGFloat = 400) -> Font {
        .custom(
            weight >= 550 ? "ApfelGrotezk-Fett" : "ApfelGrotezk-Regular",
            size: size,
            relativeTo: bodyTextStyle(for: size)
        )
    }

    /// Space Grotesk — numbers and stats (KPI tiles, dollar figures).
    ///
    /// This path builds a raw UIFont to reach the variable-weight axis, which
    /// bypasses SwiftUI's scaling entirely — UIFontMetrics is the UIKit-side
    /// equivalent of `relativeTo:` and puts it back.
    static func cavnarNumber(_ size: CGFloat, weight: CGFloat = 500) -> Font {
        let base = cavnarUIFont(family: "Space Grotesk", weight: weight, size: size)
        let metrics = UIFontMetrics(forTextStyle: uiTextStyle(for: size))
        return Font(metrics.scaledFont(for: base))
    }

    /// Maps a literal point size onto the nearest system text style so scaling
    /// stays proportional to the role the size implies — iOS grows captions
    /// faster than titles, and matching that keeps hierarchy intact at large
    /// sizes instead of everything converging on one size.
    private static func bodyTextStyle(for size: CGFloat) -> Font.TextStyle {
        switch size {
        case ..<13:  return .caption
        case ..<15:  return .footnote
        case ..<17:  return .subheadline
        case ..<20:  return .body
        case ..<24:  return .title3
        default:     return .title2
        }
    }

    private static func headlineTextStyle(for size: CGFloat) -> Font.TextStyle {
        switch size {
        case ..<20:  return .headline
        case ..<24:  return .title3
        case ..<30:  return .title2
        default:     return .largeTitle
        }
    }

    private static func uiTextStyle(for size: CGFloat) -> UIFont.TextStyle {
        switch size {
        case ..<13:  return .caption1
        case ..<15:  return .footnote
        case ..<17:  return .subheadline
        case ..<20:  return .body
        case ..<24:  return .title3
        default:     return .title2
        }
    }
}

/// Selects a specific weight out of a variable font by setting its `wght`
/// variation axis directly. UIFontDescriptor.AttributeName has no typed
/// `.variation` case — the variation-axis dictionary is a CoreText-level
/// attribute (kCTFontVariationAttribute), toll-free bridged onto
/// UIFontDescriptor's underlying CTFontDescriptor, which is why this reaches
/// into CoreText's raw attribute key rather than a UIKit-native one.
///
/// Returns the raw UIFont (not wrapped as a SwiftUI Font) and is not
/// private — UIKit-level text measurement (NSString.boundingRect, see
/// `cavnarMeasuredTextWidth` below) needs to measure against the exact same
/// font instance `.cavnarNumber` renders with, not an approximation of it.
/// Only Space Grotesk still uses this — Clash Display/Apfel Grotezk are
/// static per-weight files now, addressed via Font.custom(name:) above.
func cavnarUIFont(family: String, weight: CGFloat, size: CGFloat) -> UIFont {
    let wghtAxis = fourCharCode("wght")
    let variationKey = kCTFontVariationAttribute as String
    let descriptor = UIFontDescriptor(fontAttributes: [
        .family: family,
        UIFontDescriptor.AttributeName(rawValue: variationKey): [wghtAxis: weight],
    ])
    return UIFont(descriptor: descriptor, size: size)
}

/// The real rendered width `text` needs at `font` when wrapped within
/// `maxWidth`, via UIKit's NSString.boundingRect — used to size a chat
/// bubble to its actual content instead of relying on SwiftUI's own
/// frame(maxWidth:)/fixedSize content-hugging, which across three separate
/// attempts did not reliably shrink AskCavnarView's ChatBubble below its cap
/// for short content ("Yes" kept rendering at the full max width regardless
/// of fixedSize/frame-ordering changes). Measuring actual glyph metrics
/// sidesteps that implicit-layout ambiguity entirely.
func cavnarMeasuredTextWidth(_ text: String, font: UIFont, maxWidth: CGFloat) -> CGFloat {
    guard !text.isEmpty else { return 0 }
    let bounds = (text as NSString).boundingRect(
        with: CGSize(width: maxWidth, height: .greatestFiniteMagnitude),
        options: [.usesLineFragmentOrigin, .usesFontLeading],
        attributes: [.font: font],
        context: nil
    )
    return ceil(bounds.width)
}

private func fourCharCode(_ tag: String) -> Int {
    var code: UInt32 = 0
    for scalar in tag.unicodeScalars {
        code = (code << 8) + scalar.value
    }
    return Int(code)
}
