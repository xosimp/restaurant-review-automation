import SwiftUI

/// Ambient animated ember backdrop for Home's greeting hero — a SwiftUI
/// take on the web dashboard's rotating "light ray" + drifting "particle"
/// CSS keyframe animation (templates/dashboard.html's .home-ray/.home-ptcl
/// elements, driven by @keyframes rayA–E/ptclL/R/SL/SR). Not a literal
/// port — SwiftUI has no equivalent to CSS's multi-stage keyframe timing —
/// but the same ambient effect: a handful of softly rotating/pulsing ember
/// beams behind slowly drifting, fading embers, using only the app's own
/// Ember/Ember2 colors.
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
        Ray(x: 0.08, width: 46, baseAngle: -30, swing: 10, duration: 6.5, delay: 0.0),
        Ray(x: 0.22, width: 34, baseAngle: -16, swing: 8, duration: 8.0, delay: 0.6),
        Ray(x: 0.37, width: 58, baseAngle: -6, swing: 12, duration: 5.5, delay: 0.3),
        Ray(x: 0.52, width: 40, baseAngle: 4, swing: 9, duration: 7.2, delay: 0.9),
        Ray(x: 0.66, width: 64, baseAngle: 14, swing: 11, duration: 6.0, delay: 0.2),
        Ray(x: 0.80, width: 38, baseAngle: 24, swing: 8, duration: 7.6, delay: 0.7),
        Ray(x: 0.92, width: 32, baseAngle: -14, swing: 7, duration: 8.4, delay: 0.4),
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
                ForEach(rays.indices, id: \.self) { i in
                    let ray = rays[i]
                    Capsule()
                        .fill(
                            LinearGradient(
                                colors: [.clear, Color.cavnarEmber.opacity(0.26), .clear],
                                startPoint: .top, endPoint: .bottom
                            )
                        )
                        .frame(width: ray.width, height: geo.size.height * 1.5)
                        .rotationEffect(.degrees(animate ? ray.baseAngle + ray.swing : ray.baseAngle - ray.swing))
                        .opacity(animate ? 0.85 : 0.25)
                        .position(x: geo.size.width * ray.x, y: geo.size.height * 0.1)
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
            }
        }
        .allowsHitTesting(false)
        .onAppear { animate = true }
    }
}
