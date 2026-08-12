import SwiftUI

/// Ambient animated ember backdrop for Home's greeting hero — a SwiftUI
/// take on the web dashboard's rotating "light ray" + drifting "particle"
/// CSS keyframe animation (templates/dashboard.html's .home-ray/.home-ptcl
/// elements, driven by @keyframes rayA–E/ptclL/R/SL/SR). Not a literal
/// port — SwiftUI has no equivalent to CSS's multi-stage keyframe timing —
/// but the same ambient effect: a handful of softly rotating/pulsing ember
/// light beams behind slowly drifting, fading embers, fading to solid black
/// at the bottom edge so it blends into the rest of the page instead of
/// ending on a hard line.
struct HomeHeroBackground: View {
    @State private var animate = false

    private struct Ray {
        let x: CGFloat
        let width: CGFloat
        let baseAngle: Double
        let swing: Double
        let duration: Double
        let delay: Double
    }

    private struct Particle {
        let x: CGFloat
        let size: CGFloat
        let duration: Double
        let delay: Double
    }

    private let rays: [Ray] = [
        Ray(x: 0.08, width: 50, baseAngle: -30, swing: 10, duration: 6.5, delay: 0.0),
        Ray(x: 0.22, width: 36, baseAngle: -16, swing: 8, duration: 8.0, delay: 0.6),
        Ray(x: 0.37, width: 62, baseAngle: -6, swing: 12, duration: 5.5, delay: 0.3),
        Ray(x: 0.52, width: 42, baseAngle: 4, swing: 9, duration: 7.2, delay: 0.9),
        Ray(x: 0.66, width: 68, baseAngle: 14, swing: 11, duration: 6.0, delay: 0.2),
        Ray(x: 0.80, width: 40, baseAngle: 24, swing: 8, duration: 7.6, delay: 0.7),
        Ray(x: 0.92, width: 34, baseAngle: -14, swing: 7, duration: 8.4, delay: 0.4),
    ]

    private let particles: [Particle] = [
        Particle(x: 0.12, size: 3, duration: 9, delay: 0.0),
        Particle(x: 0.23, size: 2, duration: 12, delay: 1.2),
        Particle(x: 0.36, size: 4, duration: 10, delay: 0.4),
        Particle(x: 0.44, size: 2, duration: 8, delay: 2.0),
        Particle(x: 0.58, size: 3, duration: 13, delay: 0.8),
        Particle(x: 0.67, size: 2, duration: 9, delay: 1.6),
        Particle(x: 0.79, size: 4, duration: 11, delay: 0.2),
        Particle(x: 0.88, size: 2, duration: 10, delay: 2.4),
        Particle(x: 0.17, size: 3, duration: 14, delay: 1.0),
        Particle(x: 0.50, size: 2, duration: 9, delay: 0.6),
        Particle(x: 0.71, size: 3, duration: 12, delay: 1.8),
        Particle(x: 0.94, size: 2, duration: 8, delay: 0.3),
    ]

    var body: some View {
        GeometryReader { geo in
            ZStack {
                // Warm top-anchored wash, the same brand-ember energy as the
                // module tiles' gradient — sits behind everything else in
                // this ZStack so the rays/particles still drift clearly on
                // top of it, giving the page a lit-from-above glow instead
                // of the rays floating over flat black. One continuous taper
                // across the FULL height (not a separately-framed band) —
                // capping it at a sub-height produced a visible seam where
                // the tint stopped dead against the plain black below it.
                // The tail overlaps the existing bottom-fade's own start
                // (0.62) so the two blend into each other instead of
                // leaving a gap where neither contributes any color.
                LinearGradient(
                    stops: [
                        .init(color: Color.cavnarEmber.opacity(0.38), location: 0),
                        .init(color: Color.cavnarEmber.opacity(0.16), location: 0.35),
                        .init(color: Color.cavnarEmber.opacity(0.04), location: 0.62),
                        .init(color: .clear, location: 0.85),
                    ],
                    startPoint: .top, endPoint: .bottom
                )

                ForEach(rays.indices, id: \.self) { i in
                    let ray = rays[i]
                    // An Ellipse, not a Capsule — a capsule's flat rounded
                    // caps still read as a rectangle once you're staring at
                    // it; an ellipse actually tapers to a point at both
                    // ends. The .blur() is what turns that taper into a
                    // soft diffuse beam instead of a shape with a visible
                    // (if rounded) edge — real light rays don't have edges.
                    Ellipse()
                        .fill(
                            LinearGradient(
                                colors: [
                                    .clear,
                                    Color.cavnarEmber.opacity(0.65),
                                    Color.cavnarEmber2.opacity(0.45),
                                    .clear,
                                ],
                                startPoint: .top, endPoint: .bottom
                            )
                        )
                        .frame(width: ray.width, height: geo.size.height * 1.7)
                        .blur(radius: ray.width * 0.55)
                        .rotationEffect(.degrees(animate ? ray.baseAngle + ray.swing : ray.baseAngle - ray.swing))
                        .opacity(animate ? 1.0 : 0.4)
                        .position(x: geo.size.width * ray.x, y: geo.size.height * 0.15)
                        .animation(
                            .easeInOut(duration: ray.duration).repeatForever(autoreverses: true).delay(ray.delay),
                            value: animate
                        )
                }
                ForEach(particles.indices, id: \.self) { i in
                    let p = particles[i]
                    Circle()
                        .fill(Color.cavnarEmber2)
                        .frame(width: p.size, height: p.size)
                        .opacity(animate ? 0 : 0.65)
                        .position(x: geo.size.width * p.x, y: animate ? -20 : geo.size.height * 0.85)
                        .animation(
                            .easeIn(duration: p.duration).repeatForever(autoreverses: false).delay(p.delay),
                            value: animate
                        )
                }
                // Fade to solid black toward the bottom edge — without this
                // the rays/particles just stop dead at the hero's own frame
                // boundary, a hard line against the plain black page below.
                LinearGradient(
                    stops: [
                        .init(color: .clear, location: 0),
                        .init(color: .clear, location: 0.62),
                        .init(color: Color.cavnarPaper, location: 1.0),
                    ],
                    startPoint: .top, endPoint: .bottom
                )
            }
        }
        .allowsHitTesting(false)
        .onAppear { animate = true }
    }
}
