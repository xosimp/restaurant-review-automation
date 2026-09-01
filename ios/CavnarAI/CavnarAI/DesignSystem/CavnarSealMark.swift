import SwiftUI

/// The actual brand seal, composed correctly: the ring (SealRing, a
/// template asset — recolors with `ringColor`) plus the ember dot seated
/// in its gap, always ember-colored regardless of theme (see the brand
/// rationale — the ember is the one constant across every surface, only
/// the paper/ink around it changes). Anywhere the ring was used bare
/// (Assets/SealRing on its own) was missing this — the design was always
/// "an open ring with one ember banked in the gap," never the ring alone.
struct CavnarSealMark: View {
    var size: CGFloat = 24
    var ringColor: Color = Color.cavnarInk

    // Fractions of the seal's own 120x120 source geometry — same ratios
    // as the SVG the mark was built from (brand/assets/seal-color.svg),
    // so this stays proportionally identical at any render size.
    private var emberDiameter: CGFloat { size * (20.0 / 120.0) }
    private var glowDiameter: CGFloat { size * (54.0 / 120.0) }
    private var emberCenter: CGPoint {
        CGPoint(x: size * (99.5 / 120.0), y: size * (60.0 / 120.0))
    }

    var body: some View {
        ZStack {
            Image("SealRing")
                .renderingMode(.template)
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(ringColor)

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0)],
                        center: .center, startRadius: 0, endRadius: glowDiameter / 2
                    )
                )
                .frame(width: glowDiameter, height: glowDiameter)
                .position(emberCenter)

            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: emberDiameter * 0.6
                    )
                )
                .frame(width: emberDiameter, height: emberDiameter)
                .position(emberCenter)
        }
        .frame(width: size, height: size)
    }
}
