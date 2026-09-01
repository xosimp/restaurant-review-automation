import SwiftUI

/// Frosted-glass hero-card treatment — ports the translucent, colored-glass
/// "glassmorphic stat card" pattern (see the mnowakdesign reference) onto
/// Cavnar's palette: a saturated branded-ember gradient over a dark base,
/// rather than native `.ultraThinMaterial` — the system material reads as
/// neutral gray over our near-black background (it has no way to know our
/// brand color), which was drowning out the warmth and reading as flat gray
/// instead of "frosty branded glass." The gradient alone, at real opacity,
/// gets the reference's actual look — and stays genuinely see-through
/// against cavnarPaper rather than opaque. Reserved for hero/stat cards (a
/// number compared against a target) — plain grouped content keeps the
/// lighter .cavnarCard().
struct CavnarGlassCardStyle: ViewModifier {
    var tint: Color = .cavnarEmber

    func body(content: Content) -> some View {
        content
            .padding(16)
            // Light orange → dark orange, not light orange → black — the
            // previous end stop (0.08) was faint enough that it read as
            // fading to black against the near-black page rather than into
            // a darker shade of the same tint.
            .background(
                LinearGradient(
                    colors: [tint.opacity(0.55), tint.opacity(0.22)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .background(Color.cavnarPaper2.opacity(0.35))
            // Border follows the card's own tint (a lighter shade of it, via
            // the same .opacity(0.5) treatment everywhere else in this
            // file), not a neutral white hairline — matches
            // CavnarGlossyCardStyle's default-ember look for untinted
            // cards, but a card that passes e.g. .cavnarRed for a
            // labor-overtime warning gets a red border to match, not a
            // universally-orange one that doesn't match what the card is
            // actually saying.
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(tint.opacity(0.5), lineWidth: 1.2)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

extension View {
    func cavnarGlassCard(tint: Color = .cavnarEmber) -> some View {
        modifier(CavnarGlassCardStyle(tint: tint))
    }
}

/// The same glass language as CavnarGlassButtonStyle/CavnarSegmentedControl
/// — real Liquid Glass (`.glassEffect`) on iOS 26, Material+ember fallback
/// below it — with a visibly ember-colored border rather than the neutral
/// white hairline CavnarGlassCardStyle uses. For cards that should read as
/// "premium chrome" matching the buttons/tab switcher, not just a tinted
/// panel.
struct CavnarGlossyCardStyle: ViewModifier {
    func body(content: Content) -> some View {
        let padded = content.padding(16)
        Group {
            if #available(iOS 26.0, *) {
                padded.glassEffect(
                    .regular.tint(Color.cavnarEmber.opacity(0.35)).interactive(),
                    in: RoundedRectangle(cornerRadius: CavnarRadius.card)
                )
            } else {
                padded
                    .background {
                        RoundedRectangle(cornerRadius: CavnarRadius.card).fill(.ultraThinMaterial)
                        RoundedRectangle(cornerRadius: CavnarRadius.card).fill(Color.cavnarEmber.opacity(0.22))
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
                .strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1.2)
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
/// by hand each time.
enum CavnarTone {
    case good, bad, warning, neutral

    var foreground: Color {
        switch self {
        case .good: return .cavnarGreen
        case .bad: return .cavnarRed
        case .warning: return .cavnarAmber
        case .neutral: return .cavnarEmber
        }
    }

    var background: Color {
        switch self {
        case .good: return .cavnarGreenBg
        case .bad: return .cavnarRedBg
        case .warning: return .cavnarAmberBg
        case .neutral: return .cavnarEmber.opacity(0.12)
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

/// Small circular "view more" affordance — the reference's top-right
/// chevron button. A display-only visual (the whole card is usually the tap
/// target already); wrap in a Button at the call site when it should be
/// independently tappable.
struct GlassChevronButton: View {
    var body: some View {
        Image(systemName: "chevron.right")
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(Color.cavnarInk2)
            .frame(width: 26, height: 26)
            .background(.ultraThinMaterial, in: Circle())
            .overlay(Circle().strokeBorder(Color.white.opacity(0.12), lineWidth: 1))
    }
}
