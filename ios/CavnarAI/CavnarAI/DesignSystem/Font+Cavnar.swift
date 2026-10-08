import SwiftUI
import UIKit
import CoreText

/// The web app's typography system (per project convention): Clash Display
/// for headlines/words, Apfel Grotezk for UI chrome, Space Grotesk for
/// numbers. All three are addressed by PostScript name through
/// `Font.custom(_:size:relativeTo:)`, so every face follows Dynamic Type —
/// the app's `.xxxLarge` cap (RootView) and `cavnarReadingSize` included.
///
/// Files (CavnarAI/Fonts/, registered by project.yml's UIAppFonts in the app
/// and each extension):
/// - Clash Display and Apfel Grotezk ship as static per-weight .ttf files.
/// - Space Grotesk ships upstream ONLY as a variable font (SpaceGrotesk.ttf,
///   wght 300–700, default instance Light, and its named instances carry no
///   PostScript names). `Font.custom` cannot pick a weight out of it, so the
///   figures use four static instances cut from it with fontTools'
///   `varLib.instancer` (10/8/26, SIL OFL, no reserved name):
///   `SpaceGroteskStatic-Regular` 400, `-Medium` 500, `-SemiBold` 600 and
///   `-Bold` 700, family "Space Grotesk Static" — a family of its own so it
///   can never shadow the variable file that `cavnarUIFont(family:)` (text
///   measurement) still reads by family name. Verified with CoreText: each
///   PostScript name resolves, and the monospaced-numbers selector that
///   `.monospacedDigit()` sends maps onto the font's `tnum` glyphs (every
///   digit 620 units wide).
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

    /// Space Grotesk — numbers and stats (KPI tiles, dollar figures), with
    /// tabular digits so a figure that changes does not jitter and a column
    /// of figures lines up.
    ///
    /// Until 10/8/26 this built a UIFont through the variable font's wght
    /// axis and scaled it with UIFontMetrics, which reads the system text
    /// size directly — it ignored SwiftUI's environment, so figures grew past
    /// the app's `.xxxLarge` cap while the words beside them stopped, and
    /// `cavnarReadingSize` never reached them. Now a figure is a
    /// `Font.custom` on a static instance, relative to the SAME text style
    /// `cavnarBody` picks for that size: words and figures at one size move
    /// together (HomeMixedText relies on it).
    static func cavnarNumber(_ size: CGFloat, weight: CGFloat = 500) -> Font {
        cavnarNumber(size, weight: weight, relativeTo: bodyTextStyle(for: size))
    }

    /// A figure that scales with a named text style — what a role
    /// (`CavnarText`) and a role-sized mixed sentence use.
    static func cavnarNumber(_ size: CGFloat, weight: CGFloat, relativeTo style: Font.TextStyle) -> Font {
        .custom(spaceGroteskStatic(weight), size: size, relativeTo: style).monospacedDigit()
    }

    /// The static Space Grotesk instance nearest a requested weight
    /// (call sites pass 500, 600 or 700; 300 now reads as Regular).
    static func spaceGroteskStatic(_ weight: CGFloat) -> String {
        switch weight {
        case ..<450: return "SpaceGroteskStatic-Regular"
        case ..<550: return "SpaceGroteskStatic-Medium"
        case ..<650: return "SpaceGroteskStatic-SemiBold"
        default:     return "SpaceGroteskStatic-Bold"
        }
    }

    /// Maps a literal point size onto the nearest system text style so scaling
    /// stays proportional to the role the size implies — iOS grows captions
    /// faster than titles, and matching that keeps hierarchy intact at large
    /// sizes instead of everything converging on one size.
    static func bodyTextStyle(for size: CGFloat) -> Font.TextStyle {
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

/// The type scale as named sizes (DESIGN_SYSTEM.md §2, density #41). The
/// census found 18 body sizes, 4 kicker sizes and hero figures from 27 to
/// 56pt; a screen that reaches for these instead of a literal stays on the
/// scale. Sizes only — the face is still the helper's (`cavnarBody` for
/// words, `cavnarHeadline` for titles, `cavnarNumber` for figures), so every
/// call keeps its Dynamic Type mapping. New work uses the roles
/// (`CavnarText`, `.cavnarText(_:)`), which carry face, weight, leading,
/// tracking and ink as well; these sizes are the roles' sizes.
///
/// Raised 10/8/26 (iOS readability round): nothing an owner must read sits
/// under 13pt — caption 12.5→13, secondary 13.5→14, body 15→16, emphasis
/// 16.5→18 — and the two uppercase sizes, the only ones allowed under the
/// floor, went kicker 11.5→12 and tag 10→11.
///
/// One rule rides with `heroNumber`: **one hero figure per screen, and it is
/// the status figure** (labor % against target, food cost % against target,
/// the report's score). Anything else that wants to be big is `cardNumber`.
enum CavnarType {
    /// Uppercase tracked kicker above a section or figure — and the label
    /// over a tile's figure. One size everywhere (parity audit #85: the
    /// census found 14). = `CavnarText.kicker`.
    static let kicker: CGFloat = 12
    /// Uppercase label INSIDE a capsule (a claim tag, a request kind, a
    /// severity pill) — the one size below the kicker. = `CavnarText.tag`.
    static let tag: CGFloat = 11
    /// Meta, timestamps, basis lines — the floor for anything an owner
    /// must read. = `CavnarText.caption`.
    static let caption: CGFloat = 13
    /// Secondary copy under a figure or title; helper lines.
    /// = `CavnarText.secondary`.
    static let secondary: CGFloat = 14
    /// Body copy. = `CavnarText.body`.
    static let body: CGFloat = 16
    /// A lead line — the one sentence a card opens on. = `CavnarText.lead`.
    static let emphasis: CGFloat = 18
    /// A section title (Clash). (`CavnarText.headline` is 21 as well.)
    static let section: CGFloat = 21
    /// The screen's one status figure.
    static let heroNumber: CGFloat = 40
    /// A card's own figure.
    static let cardNumber: CGFloat = 30
    /// A stat-strip or grid tile figure.
    static let tileNumber: CGFloat = 22
}

// MARK: - Text roles

/// The type roles — one per job, each tied to ONE system text style so the
/// whole scale moves together under Dynamic Type (iOS readability round,
/// 10/8/26). A role is face + size + weight + leading + tracking + default
/// ink; a call site names the job, never a point size:
///
///     Text("Labor is over target").cavnarText(.headline)
///     Text(reason).cavnarText(.body)
///     Text("$1,840").cavnarText(.figureL, color: .cavnarGreen)
///     Text("Last night").font(.cavnar(.kicker))   // face and size only
///
/// | Role | Face | pt / leading | Tracking | Ink | Text style |
/// |---|---|---|---|---|---|
/// | display | Clash Medium | 34/40 | -0.4 | Ink | largeTitle |
/// | title | Clash Medium | 26/31 | -0.2 | Ink | title |
/// | headline | Clash Medium | 21/26 | 0 | Ink | title3 |
/// | lead | Apfel Regular | 18/25 | 0 | Ink | body |
/// | body | Apfel Regular | 16/23 | 0 | Ink2 | callout |
/// | label | Apfel Fett | 16/20 | 0 | Ink | callout |
/// | secondary | Apfel Regular | 14/19 | 0 | Ink2 | subheadline |
/// | caption | Apfel Regular | 13/17 | +0.1 | Ink3 | footnote |
/// | kicker | Apfel Fett, UPPERCASE | 12 | +1.2 | Ember2 | caption |
/// | tag | Apfel Fett, UPPERCASE | 11 | +0.6 | Ink2 | caption2 |
/// | figureXL | Space Grotesk 600, tabular | 48 | -1.0 | Ink | largeTitle |
/// | figureL | Space Grotesk 600, tabular | 34 | -0.6 | Ink | largeTitle |
/// | figureM | Space Grotesk 600, tabular | 24 | -0.3 | Ink | title2 |
/// | figureS | Space Grotesk 600, tabular | 17 | 0 | Ink | body |
///
/// Rules (DESIGN_SYSTEM.md §2): caption is the floor for anything an owner
/// must read — only `tag` (inside a capsule) and chart axes go under it;
/// one `figureXL` per screen; Ink3 never for body or label; bold only for
/// label and kicker (Apfel ships Regular and Fett only, so a 600 IS a 700).
enum CavnarText: CaseIterable, Sendable {
    case display, title, headline, lead, body, label, secondary, caption, kicker, tag
    case figureXL, figureL, figureM, figureS

    /// Point size at the default Dynamic Type size.
    var size: CGFloat {
        switch self {
        case .display: return 34
        case .title: return 26
        case .headline: return 21
        case .lead: return 18
        case .body, .label: return 16
        case .secondary: return 14
        case .caption: return 13
        case .kicker: return 12
        case .tag: return 11
        case .figureXL: return 48
        case .figureL: return 34
        case .figureM: return 24
        case .figureS: return 17
        }
    }

    /// The one system text style the role scales with.
    var textStyle: Font.TextStyle {
        switch self {
        case .display, .figureXL, .figureL: return .largeTitle
        case .title: return .title
        case .headline: return .title3
        case .figureM: return .title2
        case .lead, .figureS: return .body
        case .body, .label: return .callout
        case .secondary: return .subheadline
        case .caption: return .footnote
        case .kicker: return .caption
        case .tag: return .caption2
        }
    }

    /// The bundled face (PostScript name).
    var postScriptName: String {
        switch self {
        case .display, .title, .headline: return "ClashDisplay-Medium"
        case .lead, .body, .secondary, .caption: return "ApfelGrotezk-Regular"
        case .label, .kicker, .tag: return "ApfelGrotezk-Fett"
        case .figureXL, .figureL, .figureM, .figureS: return Font.spaceGroteskStatic(600)
        }
    }

    /// Target line height (pt) at the default size; nil = the face's own.
    var leading: CGFloat? {
        switch self {
        case .display: return 40
        case .title: return 31
        case .headline: return 26
        case .lead: return 25
        case .body: return 23
        case .label: return 20
        case .secondary: return 19
        case .caption: return 17
        default: return nil
        }
    }

    /// Letter spacing (pt) at the default size.
    var tracking: CGFloat {
        switch self {
        case .display: return -0.4
        case .title: return -0.2
        case .caption: return 0.1
        case .kicker: return 1.2
        case .tag: return 0.6
        case .figureXL: return -1.0
        case .figureL: return -0.6
        case .figureM: return -0.3
        default: return 0
        }
    }

    var isUppercase: Bool { self == .kicker || self == .tag }
    var isFigure: Bool { self == .figureXL || self == .figureL || self == .figureM || self == .figureS }

    /// The ink a role takes when the caller names none.
    var defaultColor: Color {
        switch self {
        case .body, .secondary, .tag: return .cavnarInk2
        case .caption: return .cavnarInk3
        case .kicker: return .cavnarEmber2
        default: return .cavnarInk
        }
    }

    /// SwiftUI's `lineSpacing` is ADDED to the face's natural line height
    /// (hhea ascent + descent + gap: Apfel 1.08em, Clash 1.23em, Space
    /// Grotesk 1.276em), so the extra is the target leading minus that.
    /// Clash already sets at or above its targets, so titles add nothing.
    var lineSpacing: CGFloat {
        guard let leading else { return 0 }
        let natural: CGFloat
        switch self {
        case .display, .title, .headline: natural = 1.23
        case .figureXL, .figureL, .figureM, .figureS: natural = 1.276
        default: natural = 1.08
        }
        return max(0, (leading - size * natural).rounded(.toNearestOrEven))
    }

    /// The UIKit twin of `textStyle` — for scaling spacing with Dynamic Type.
    var uiTextStyle: UIFont.TextStyle {
        switch textStyle {
        case .largeTitle: return .largeTitle
        case .title: return .title1
        case .title2: return .title2
        case .title3: return .title3
        case .callout: return .callout
        case .subheadline: return .subheadline
        case .footnote: return .footnote
        case .caption: return .caption1
        case .caption2: return .caption2
        default: return .body
        }
    }
}

extension Font {
    /// A role's face and size (scaling with the role's text style; tabular
    /// digits for figures). Leading, tracking, case and ink come with the
    /// view modifier `.cavnarText(_:)` — use that on a view; use this inside
    /// a `Text` concatenation.
    static func cavnar(_ role: CavnarText) -> Font {
        let font = Font.custom(role.postScriptName, size: role.size, relativeTo: role.textStyle)
        return role.isFigure ? font.monospacedDigit() : font
    }
}

/// `.cavnarText(_:color:)` — see `CavnarText`.
struct CavnarTextRole: ViewModifier {
    let role: CavnarText
    var color: Color?
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize

    func body(content: Content) -> some View {
        // Leading and tracking grow with the text, by the role's own style.
        let metrics = UIFontMetrics(forTextStyle: role.uiTextStyle)
        let traits = UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(dynamicTypeSize))
        let scale = metrics.scaledValue(for: 100, compatibleWith: traits) / 100
        return content
            .font(.cavnar(role))
            .lineSpacing(role.lineSpacing * scale)
            .tracking(role.tracking * scale)
            .textCase(role.isUppercase ? .uppercase : nil)
            .foregroundStyle(color ?? role.defaultColor)
    }
}

extension View {
    /// Sets a role's face, size, weight, leading, tracking, case and ink
    /// (`color` overrides the ink). The one way new iOS text is styled.
    func cavnarText(_ role: CavnarText, color: Color? = nil) -> some View {
        modifier(CavnarTextRole(role: role, color: color))
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
