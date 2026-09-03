import SwiftUI

/// The actual brand seal, composed correctly: the ring (SealRing, a
/// template asset — recolors with `ringColor`) plus the ember dot seated
/// in its gap, always ember-colored regardless of theme (see the brand
/// rationale — the ember is the one constant across every surface, only
/// the paper/ink around it changes). Anywhere the ring was used bare
/// (Assets/SealRing on its own) was missing this — the design was always
/// "an open ring with one ember banked in the gap," never the ring alone.
///
/// `emberIntensity` (0...1) fans the ember: 0 is the resting mark exactly
/// as drawn in the brand assets; 1 swells the glow halo, brightens it, and
/// lights a hotter Ember2 core — a coal being breathed on. Static call
/// sites leave it at 0; CavnarLoadingSeal animates it.
struct CavnarSealMark: View, Animatable {
    var size: CGFloat = 24
    var ringColor: Color = Color.cavnarInk
    var ringOpacity: Double = 1
    var emberIntensity: CGFloat = 0
    /// 1 is the real ember; 0 is a cold gray coal with no glow at all —
    /// the "nothing here yet" hearth in CavnarEmptyHearth. Animatable
    /// (together with emberIntensity) so a warmth change interpolates per
    /// frame instead of snapping between the two colors.
    var emberWarmth: CGFloat = 1

    // nonisolated: SwiftUI drives animatableData from its own animation
    // machinery, which is not guaranteed to run on the main actor. The
    // property only reads/writes stored value types, so leaving it unisolated
    // is both correct and required for the conformance to be valid in the
    // Swift 6 language mode (audit 2.3).
    nonisolated var animatableData: AnimatablePair<CGFloat, CGFloat> {
        get { AnimatablePair(emberIntensity, emberWarmth) }
        set { emberIntensity = newValue.first; emberWarmth = newValue.second }
    }

    // Fractions of the seal's own 120x120 source geometry — same ratios
    // as the SVG the mark was built from (brand/assets/seal-color.svg),
    // so this stays proportionally identical at any render size.
    private var emberDiameter: CGFloat { size * (20.0 / 120.0) }
    private var restingGlowDiameter: CGFloat { size * (54.0 / 120.0) }
    private var emberCenter: CGPoint {
        CGPoint(x: size * (99.5 / 120.0), y: size * (60.0 / 120.0))
    }

    private var glowDiameter: CGFloat { restingGlowDiameter * (1 + 0.7 * emberIntensity) }
    private var glowPeakOpacity: Double { 0.5 + 0.45 * Double(emberIntensity) }
    private var emberScale: CGFloat { 1 + 0.18 * emberIntensity }

    var body: some View {
        ZStack {
            Image("SealRing")
                .renderingMode(.template)
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(ringColor)
                .opacity(ringOpacity)

            // Halo — the part that visibly "breathes" when fanned.
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber.opacity(glowPeakOpacity * Double(emberWarmth)), Color.cavnarEmber.opacity(0)],
                        center: .center, startRadius: 0, endRadius: glowDiameter / 2
                    )
                )
                .frame(width: glowDiameter, height: glowDiameter)
                .position(emberCenter)

            // The cold coal underneath — only ever visible as the warm one
            // above it fades out (emberWarmth < 1).
            Circle()
                .fill(Color.cavnarInk3.opacity(0.38))
                .frame(width: emberDiameter, height: emberDiameter)
                .scaleEffect(emberScale)
                .position(emberCenter)

            // The coal itself.
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: emberDiameter * 0.6
                    )
                )
                .overlay(
                    // Hot core that only shows while fanned — reads as the
                    // ember going brighter/whiter at the peak of a breath.
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [Color.white.opacity(0.55), Color.cavnarEmber2.opacity(0)],
                                center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: emberDiameter * 0.45
                            )
                        )
                        .opacity(Double(emberIntensity))
                )
                .frame(width: emberDiameter, height: emberDiameter)
                .scaleEffect(emberScale)
                .opacity(Double(emberWarmth))
                .position(emberCenter)
        }
        .frame(width: size, height: size)
    }
}
