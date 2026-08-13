import SwiftUI
import UIKit
import CoreText

/// The web app's typography system (per project convention): Fraunces for
/// headlines/words, Plus Jakarta Sans for UI chrome, Space Grotesk for
/// numbers. All three are bundled as variable fonts (single .ttf per family,
/// see CavnarAI/Fonts/ + Info.plist's UIAppFonts) rather than one static
/// file per weight — smaller app bundle, same visual range.
///
/// Fraunces ships proper named-instance PostScript names (e.g.
/// "Fraunces-SemiBold"), so Font.custom addresses those directly. Plus
/// Jakarta Sans and Space Grotesk do NOT — their variable font has only one
/// PostScript name for the whole file, so selecting a specific weight needs
/// a UIFontDescriptor variation-axis dictionary (the `variableFont` helper
/// below), not a plain Font.custom(name:) string.
extension Font {
    /// Fraunces — serif, for headlines and standalone words (matches the
    /// dashboard's `--headline` usage of Fraunces at various weights).
    static func cavnarHeadline(_ size: CGFloat, weight: FrauncesWeight = .semiBold) -> Font {
        .custom(weight.postScriptName, size: size)
    }

    enum FrauncesWeight {
        case regular, semiBold, bold, black

        var postScriptName: String {
            switch self {
            case .regular:  return "Fraunces-Regular"
            case .semiBold: return "Fraunces-SemiBold"
            case .bold:     return "Fraunces-Bold"
            case .black:    return "Fraunces-Black"
            }
        }
    }

    /// Plus Jakarta Sans — UI chrome (labels, buttons, body text).
    static func cavnarBody(_ size: CGFloat, weight: CGFloat = 400) -> Font {
        Font(cavnarUIFont(family: "Plus Jakarta Sans", weight: weight, size: size))
    }

    /// Space Grotesk — numbers and stats (KPI tiles, dollar figures).
    static func cavnarNumber(_ size: CGFloat, weight: CGFloat = 500) -> Font {
        Font(cavnarUIFont(family: "Space Grotesk", weight: weight, size: size))
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
/// font instance `.cavnarBody`/`.cavnarNumber` render with, not an
/// approximation of it.
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
