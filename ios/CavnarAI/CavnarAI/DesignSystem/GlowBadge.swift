import SwiftUI

/// Premium glow-badge icon — ports a well-known 5-layer badge-glow
/// technique (outer blurred bloom → crisp badge shape → white inset plate
/// → tight inner glow → icon on top) onto Cavnar's own brand colors
/// instead of generic gold. The badge silhouette is the real seal mark
/// (Assets/SealRing, a template-rendered vector asset) rather than SF
/// Symbols' generic "seal.fill" scallop — this component was always
/// drawing a stand-in for exactly this shape, so every place it's used
/// (Ask Cavnar's header/FAB/empty state) now shows the actual brand seal
/// instead of a placeholder that merely evoked one.
struct GlowBadge: View {
    var systemImage: String
    var size: CGFloat = 56
    // Rotates only the scalloped "seal" star shape below — the white plate
    // and icon on top stay fixed, like a medallion spinning under a static
    // emblem. Defaults to no rotation so every existing call site (Home KPI
    // tiles, Modules grid) is unaffected; the Ask Cavnar FAB is the one
    // place that drives this to make the badge read as "alive."
    var rotation: Angle = .zero

    private var badgeGradient: RadialGradient {
        RadialGradient(
            colors: [Color.cavnarEmber2, Color.cavnarEmber],
            center: .center, startRadius: 0, endRadius: size * 0.6
        )
    }

    var body: some View {
        ZStack {
            // Outer bloom — same silhouette, heavily blurred, extends past the badge edge.
            Image("SealRing")
                .renderingMode(.template)
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(badgeGradient)
                .blur(radius: size * 0.22)
                .blendMode(.plusLighter)
                .opacity(0.45)
                .rotationEffect(rotation)

            // Crisp badge shape.
            Image("SealRing")
                .renderingMode(.template)
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(badgeGradient)
                .blendMode(.plusLighter)
                .rotationEffect(rotation)

            // White inset plate the icon sits on, for contrast/pop.
            Circle()
                .fill(.white)
                .frame(width: size * 0.54, height: size * 0.54)

            // Tight inner glow directly behind the icon.
            Circle()
                .fill(badgeGradient)
                .frame(width: size * 0.42, height: size * 0.42)
                .blur(radius: size * 0.07)
                .blendMode(.hardLight)
                .opacity(0.7)

            // The icon itself.
            Image(systemName: systemImage)
                .font(.system(size: size * 0.24, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
        }
        .frame(width: size, height: size)
        .compositingGroup()
    }
}
