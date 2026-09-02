import SwiftUI

/// The module badge — an "ember tile." A rounded square of obsidian (the
/// same dark-surface family the app icon now sits on) with a lit edge
/// running ember-to-dark from its top-left, the module's glyph in cream
/// glowing softly from behind, and one ember seated on its right edge —
/// the seal's own gap-and-coal, echoed on every tile. Minimal parts, one
/// point of color, and the same silhouette language as the mark itself.
///
/// Replaces the previous scalloped "seal.fill" sunburst with a white plate
/// (a generic badge-glow technique in brand colors). An earlier attempt
/// to put the real seal ring here read as a spinning "C" once the FAB's
/// rotation hit it — so nothing on this tile rotates: the FAB's "alive"
/// cue is an optional thin ember halo (`halo`) that turns around the tile
/// instead, and the tile itself stays still.
struct GlowBadge: View {
    var systemImage: String
    var size: CGFloat = 56
    // Only the optional halo ring turns — the tile, glyph, and ember never
    // do. Defaults to zero so every existing call site is unaffected; the
    // Ask Cavnar FAB drives this.
    var rotation: Angle = .zero
    // A thin conic ember halo just outside the tile, for the one badge that
    // should read as alive (the Ask Cavnar FAB). Off for module tiles.
    var halo: Bool = false

    private var radius: CGFloat { size * 0.3 }
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: radius, style: .continuous) }

    // Obsidian — Apple's dark surface family, matching the app icon.
    private static let tileTop = Color(red: 0.173, green: 0.173, blue: 0.180)      // #2C2C2E
    private static let tileBottom = Color(red: 0.086, green: 0.086, blue: 0.094)   // #161618

    var body: some View {
        ZStack {
            // Soft bloom behind the whole tile.
            shape
                .fill(Color.cavnarEmber)
                .frame(width: size, height: size)
                .blur(radius: size * 0.22)
                .opacity(0.32)

            if halo {
                Circle()
                    .stroke(
                        AngularGradient(
                            colors: [Color.cavnarEmber2, Color.cavnarEmber.opacity(0), Color.cavnarEmber.opacity(0), Color.cavnarEmber2],
                            center: .center
                        ),
                        lineWidth: max(1, size * 0.04)
                    )
                    .frame(width: size * 1.22, height: size * 1.22)
                    .opacity(0.7)
                    .rotationEffect(rotation)
            }

            // The tile.
            shape
                .fill(
                    LinearGradient(colors: [Self.tileTop, Self.tileBottom], startPoint: .top, endPoint: .bottom)
                )
                .frame(width: size, height: size)
                // Lit edge: ember at the top-left corner, falling to nothing.
                .overlay(
                    shape.strokeBorder(
                        LinearGradient(
                            stops: [
                                .init(color: Color.cavnarEmber2.opacity(0.95), location: 0),
                                .init(color: Color.cavnarEmber.opacity(0.4), location: 0.45),
                                .init(color: Color.cavnarEmber.opacity(0.12), location: 1),
                            ],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        ),
                        lineWidth: max(1, size * 0.03)
                    )
                )
                // Hairline inner highlight along the top — a lip catching light.
                .overlay(
                    shape
                        .inset(by: max(1, size * 0.03) + 0.5)
                        .strokeBorder(
                            LinearGradient(
                                colors: [Color.white.opacity(0.10), Color.white.opacity(0)],
                                startPoint: .top, endPoint: .center
                            ),
                            lineWidth: 1
                        )
                )
                // Faint ember warmth pooling behind the glyph.
                .overlay(
                    Circle()
                        .fill(
                            RadialGradient(
                                colors: [Color.cavnarEmber.opacity(0.28), Color.cavnarEmber.opacity(0)],
                                center: .center, startRadius: 0, endRadius: size * 0.32
                            )
                        )
                        .frame(width: size * 0.64, height: size * 0.64)
                )
                .shadow(color: .black.opacity(0.5), radius: size * 0.08, x: 0, y: size * 0.05)

            // The glyph — cream, lit from behind.
            Image(systemName: systemImage)
                .font(.system(size: size * 0.4, weight: .semibold))
                .foregroundStyle(Color.cavnarInk)
                .shadow(color: Color.cavnarEmber.opacity(0.7), radius: size * 0.12, x: 0, y: 0)

            // The ember, seated on the tile's right edge — the seal's coal.
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: size * 0.1
                    )
                )
                .frame(width: size * 0.17, height: size * 0.17)
                .shadow(color: Color.cavnarEmber.opacity(0.85), radius: size * 0.09, x: 0, y: 0)
                .offset(x: size * 0.5 - size * 0.055)
        }
        .frame(width: size, height: size)
        .compositingGroup()
    }
}
