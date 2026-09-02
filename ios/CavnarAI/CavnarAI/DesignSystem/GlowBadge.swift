import SwiftUI

/// The module badge — an "ember tile." A rounded square of obsidian (the
/// same dark-surface family the app icon sits on) with a lit edge running
/// ember-to-dark from its top-left, the module's glyph in cream, and one
/// ember seated on its right edge — the seal's own gap-and-coal, echoed on
/// every tile. Minimal parts, one point of color, no glow: the tile reads
/// as a solid object, not a light source (the first version's ember bloom
/// behind the tile and around the glyph was asked to go).
///
/// An earlier attempt to put the real seal ring here read as a spinning
/// "C" once the FAB's rotation hit it — so nothing on this tile rotates:
/// the FAB's "alive" cue is an optional thin ember halo (`halo`) — a
/// concentric rounded square around the tile whose lit arc travels around
/// it — and the tile itself stays still.
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
            if halo {
                // The same rounded square as the tile, concentric with it
                // (outer radius = tile radius + the gap), so the FAB is one
                // shape, not a square inside a circle. The ring itself never
                // turns — only the ember arc travels around it, via the
                // gradient's own start angle.
                RoundedRectangle(cornerRadius: size * 0.41, style: .continuous)
                    .stroke(
                        AngularGradient(
                            colors: [Color.cavnarEmber2, Color.cavnarEmber.opacity(0), Color.cavnarEmber.opacity(0), Color.cavnarEmber2],
                            center: .center,
                            angle: rotation
                        ),
                        lineWidth: max(1, size * 0.04)
                    )
                    .frame(width: size * 1.22, height: size * 1.22)
                    .opacity(0.7)
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
                .shadow(color: .black.opacity(0.55), radius: size * 0.1, x: 0, y: size * 0.06)

            // The glyph — cream, flat.
            Image(systemName: systemImage)
                .font(.system(size: size * 0.4, weight: .semibold))
                .foregroundStyle(Color.cavnarInk)

            // The ember, seated on the tile's right edge — the seal's coal.
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2, Color.cavnarEmber],
                        center: UnitPoint(x: 0.42, y: 0.38), startRadius: 0, endRadius: size * 0.1
                    )
                )
                .frame(width: size * 0.17, height: size * 0.17)
                .offset(x: size * 0.5 - size * 0.055)
        }
        .frame(width: size, height: size)
        .compositingGroup()
    }
}
