import SwiftUI
import UIKit

/// "Ember Aurora" — replaces the old rotating-light-ray + drifting-particle
/// system entirely. Three large, heavily-blurred gradient blooms drift and
/// slowly rotate behind the content, plus a tiled grain texture over the
/// top — the actual technique that keeps a soft gradient like this from
/// reading as flat or banded (see css-tricks.com/grainy-gradients), and the
/// same "Aurora UI" pattern behind most current AI-product hero sections.
///
/// Drawn with Canvas + TimelineView instead of animated view modifiers, and
/// with no separately-framed top "wash" band: the blooms are positioned as
/// fractions of the canvas's own size and blurred/faded radially, so there
/// is no fixed-height edge for the gradient to visibly stop at — that fixed
/// band was what produced the seam under the nav bar before.
struct HomeHeroBackground: View {
    private struct Bloom {
        let relX: CGFloat
        let relY: CGFloat
        let relRadius: CGFloat
        let color: Color
        let baseOpacity: Double
        let speed: Double
        let phase: Double
    }

    // Only cavnarEmber/cavnarEmber2 — the app's actual two ember tokens,
    // not a made-up third shade — reused at different positions/opacities
    // for depth instead.
    private let blooms: [Bloom] = [
        Bloom(relX: 0.28, relY: 0.22, relRadius: 0.55, color: .cavnarEmber, baseOpacity: 0.50, speed: 0.00028, phase: 0),
        Bloom(relX: 0.75, relY: 0.30, relRadius: 0.50, color: .cavnarEmber2, baseOpacity: 0.38, speed: 0.00022, phase: 2),
        Bloom(relX: 0.50, relY: 0.55, relRadius: 0.62, color: .cavnarEmber, baseOpacity: 0.42, speed: 0.00018, phase: 4),
    ]

    var body: some View {
        TimelineView(.animation) { timeline in
            Canvas { context, size in
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Color.cavnarPaper))

                let t = timeline.date.timeIntervalSinceReferenceDate * 1000
                context.drawLayer { layer in
                    layer.addFilter(.blur(radius: 42))
                    for bloom in blooms {
                        // Roughly 2x the amplitude and speed of the first
                        // pass — at the old values the drift was real but
                        // essentially imperceptible against blooms this
                        // soft-edged; this is enough to notice without
                        // reading as an obvious sweep.
                        let dx = sin(t * bloom.speed + bloom.phase) * size.width * 0.22
                        let dy = cos(t * bloom.speed * 0.8 + bloom.phase) * size.height * 0.12
                        let cx = bloom.relX * size.width + dx
                        let cy = bloom.relY * size.height + dy
                        let r = bloom.relRadius * size.width

                        let gradient = Gradient(stops: [
                            .init(color: bloom.color.opacity(bloom.baseOpacity), location: 0),
                            .init(color: bloom.color.opacity(0), location: 1),
                        ])
                        layer.fill(
                            Path(ellipseIn: CGRect(x: cx - r, y: cy - r, width: r * 2, height: r * 2)),
                            with: .radialGradient(gradient, center: CGPoint(x: cx, y: cy), startRadius: 0, endRadius: r)
                        )
                    }
                }
            }
        }
        .overlay(
            // Fade to solid black toward the bottom edge. Widened from a
            // 0.78->1.0 ramp (22% of the frame) to 0.55->1.0 (45%) — at the
            // narrower ramp the last stretch went from visible aurora glow
            // to fully opaque cavnarPaper over too short a distance for the
            // shorter (460pt, down from 600) frame this sits in now, which
            // read as a hard seam right where the frame ends rather than a
            // gradual fade — see heroBackgroundHeight's own doc comment.
            LinearGradient(
                stops: [
                    .init(color: .clear, location: 0),
                    .init(color: .clear, location: 0.55),
                    .init(color: Color.cavnarPaper, location: 1.0),
                ],
                startPoint: .top, endPoint: .bottom
            )
        )
        .overlay(
            // Masked with the same fade band as the color overlay above —
            // grain lives only inside this view's own bounds, so without
            // this it would otherwise cut off abruptly right at the frame's
            // bottom edge (grain texture above the line, none below), which
            // read as its own small seam even once the color fade itself
            // was smooth.
            Image(uiImage: CavnarGrainTexture.image)
                .resizable(resizingMode: .tile)
                .opacity(0.05)
                .blendMode(.overlay)
                .mask(
                    LinearGradient(
                        stops: [
                            .init(color: .white, location: 0),
                            .init(color: .white, location: 0.55),
                            .init(color: .clear, location: 1.0),
                        ],
                        startPoint: .top, endPoint: .bottom
                    )
                )
        )
        .allowsHitTesting(false)
        // Lets the canvas paint all the way up behind the status bar and
        // nav bar (where the bell icon sits) instead of stopping at the
        // normal content safe area — without this, that whole top strip
        // just shows the plain black page background behind it, regardless
        // of the nav bar's own material being hidden.
        .ignoresSafeArea(edges: .top)
    }
}

/// A small tileable noise texture, generated once and cached — regenerating
/// per-pixel noise every frame would be wasted work for something that
/// doesn't need to animate; grain reads as "alive" from the blooms moving
/// underneath it, not from the grain itself moving.
private enum CavnarGrainTexture {
    static let image: UIImage = {
        let side = 64
        let renderer = UIGraphicsImageRenderer(size: CGSize(width: side, height: side))
        return renderer.image { ctx in
            let cg = ctx.cgContext
            for y in 0..<side {
                for x in 0..<side {
                    let v = CGFloat.random(in: 0...1)
                    cg.setFillColor(UIColor(white: v, alpha: 1).cgColor)
                    cg.fill(CGRect(x: x, y: y, width: 1, height: 1))
                }
            }
        }
    }()
}
