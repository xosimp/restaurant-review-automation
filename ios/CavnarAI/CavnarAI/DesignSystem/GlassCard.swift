import SwiftUI

/// The hero card for a screen's one status figure (Labor's % against
/// target): a glass panel washed by that figure's TONE — green on track,
/// red over, the neutral ink when the figure can't be judged — at a low
/// opacity, with a hairline of the same tone. A documented exception in
/// DESIGN_SYSTEM §9: the wash repeats the verdict the figure already
/// carries, so it is a status tint, never ember (ember is not a status,
/// and a card is never tinted ember whole). It was an ember-by-default
/// 55%→22% gradient; the tone now sits at 20%→6% over the Paper2 ground,
/// so the figure, not the card, carries the colour. Plain grouped content
/// keeps .cavnarCard().
struct CavnarGlassCardStyle: ViewModifier {
    var tint: Color = CavnarTone.neutral.foreground

    func body(content: Content) -> some View {
        content
            .padding(16)
            .background(
                LinearGradient(
                    colors: [tint.opacity(0.20), tint.opacity(0.06)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .background(Color.cavnarPaper2.opacity(0.6))
            // The hairline follows the card's own tone, so a card that
            // passes .cavnarRed for an over-target figure gets a red edge,
            // not a universally-orange one.
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(tint.opacity(0.4), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    func cavnarGlassCard(tint: Color = CavnarTone.neutral.foreground) -> some View {
        modifier(CavnarGlassCardStyle(tint: tint))
    }
}

/// The same glass language as CavnarGlassButtonStyle/CavnarSegmentedControl
/// — real Liquid Glass (`.glassEffect`) on iOS 26, Material below it — with
/// an ember HAIRLINE. For a stat tile that should read as "premium chrome"
/// matching the buttons/tab switcher. The glass itself is untinted: it was
/// an ember-tinted glass (35% tint, a 22% ember fill on the fallback),
/// which tinted a whole card ember against DESIGN_SYSTEM §9. The edge is
/// the one ember on the card.
struct CavnarGlossyCardStyle: ViewModifier {
    func body(content: Content) -> some View {
        let padded = content.padding(16)
        Group {
            if #available(iOS 26.0, *) {
                padded.glassEffect(
                    .regular.interactive(),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.card)
                )
            } else {
                padded
                    .background {
                        RoundedRectangle(cornerRadius: CavnarRadius.card).fill(.ultraThinMaterial)
                        RoundedRectangle(cornerRadius: CavnarRadius.card).fill(Color.cavnarPaper2.opacity(0.6))
                        RoundedRectangle(cornerRadius: CavnarRadius.card).fill(
                            LinearGradient(
                                colors: [Color.white.opacity(0.10), Color.white.opacity(0)],
                                startPoint: .top, endPoint: .center
                            )
                        )
                    }
                    .shadow(color: .black.opacity(0.2), radius: 4, y: 2)
            }
        }
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarEmber.opacity(0.35), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    func cavnarGlossyCard() -> some View {
        modifier(CavnarGlossyCardStyle())
    }
}

/// Semantic color grouping for StatusPill/StatProgressBar so call sites say
/// what the value *means* ("good", "over target") instead of picking colors
/// by hand each time. `.neutral` is "no verdict" — sample data, not yet
/// measured, a plain count — and reads in the ink on a paper chip, like
/// web's `.hb-chip.neutral`. It used to render ember, which made "no
/// verdict" look like the brand's "do this" (DESIGN_SYSTEM §9: ember is
/// never a status).
enum CavnarTone {
    case good, bad, warning, neutral

    var foreground: Color {
        switch self {
        case .good: return .cavnarGreen
        case .bad: return .cavnarRed
        case .warning: return .cavnarAmber
        case .neutral: return .cavnarInk2
        }
    }

    var background: Color {
        switch self {
        case .good: return .cavnarGreenBg
        case .bad: return .cavnarRedBg
        case .warning: return .cavnarAmberBg
        case .neutral: return .cavnarPaper3.opacity(0.6)
        }
    }
}

/// Small colored capsule status readout — e.g. "On track" / "Over target" —
/// matching the reference card's header-row status pill. Named distinctly
/// from Reviews' own StatusPill (that one maps a fixed set of review-status
/// strings to copy/color internally; this one takes tone+text directly).
struct TonePill: View {
    let text: String
    var tone: CavnarTone

    var body: some View {
        Text(text)
            .font(.cavnarBody(14, weight: 700))
            .foregroundStyle(tone.foreground)
            .padding(.horizontal, 8)
            .padding(.vertical, 4)
            .background(tone.background)
            .clipShape(Capsule())
    }
}

/// Thin rounded value-vs-target bar, color-coded by tone — the reference's
/// most genuinely new pattern for us: several cards already show a number
/// next to a target as text (Labor's %, Reviews' response rate) but none
/// visualize the comparison. `progress` is expected pre-clamped to 0...1.
struct StatProgressBar: View {
    var progress: Double
    var tone: CavnarTone = .neutral

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.7))
                Capsule().fill(tone.foreground)
                    .frame(width: geo.size.width * min(max(progress, 0), 1))
            }
        }
        .frame(height: 6)
    }
}
