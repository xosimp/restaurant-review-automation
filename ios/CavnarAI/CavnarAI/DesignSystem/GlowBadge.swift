import SwiftUI

/// Premium glow-badge icon — ports a well-known 5-layer badge-glow
/// technique (outer blurred bloom → crisp badge shape → white inset plate
/// → tight inner glow → icon on top) onto Cavnar's own brand colors
/// instead of generic gold. Uses SF Symbols' "seal.fill" for the badge's
/// rounded scalloped silhouette rather than hand-built star geometry —
/// same soft "sunburst" read, crisp at any size, zero custom path math.
struct GlowBadge: View {
    var systemImage: String
    var size: CGFloat = 56

    private var badgeGradient: RadialGradient {
        RadialGradient(
            colors: [Color.cavnarEmber2, Color.cavnarEmber],
            center: .center, startRadius: 0, endRadius: size * 0.6
        )
    }

    var body: some View {
        ZStack {
            // Outer bloom — same silhouette, heavily blurred, extends past the badge edge.
            Image(systemName: "seal.fill")
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(badgeGradient)
                .blur(radius: size * 0.22)
                .blendMode(.plusLighter)
                .opacity(0.45)

            // Crisp badge shape.
            Image(systemName: "seal.fill")
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(badgeGradient)
                .blendMode(.plusLighter)

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
