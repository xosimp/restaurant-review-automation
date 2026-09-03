import SwiftUI

/// The sign-in screen's living background — the design's "AI" feeling
/// lives here, not in decoration on the form itself.
///
/// Two layers, both wall-clock driven (TimelineView) so they can't stall
/// or snap the way a toggled @State loop can:
///  1. Aurora — three soft ember blooms drifting on 9–18s cycles, the same
///     two ember tokens HomeHeroBackground uses, just full-screen.
///  2. Constellation — a field of points, each on its own slow heading,
///     with hairlines drawn between any two within reach and fading with
///     distance. A network, quietly thinking.
/// A vignette then darkens toward the form so the background stays
/// background. Reduce Motion freezes the drift and leaves a still aurora.
struct LoginBackground: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            Color.cavnarPaper
            LoginAurora(frozen: reduceMotion)
            LoginConstellation(frozen: reduceMotion)
            LinearGradient(
                stops: [
                    .init(color: Color.cavnarPaper.opacity(0), location: 0),
                    .init(color: Color.cavnarPaper.opacity(0.35), location: 0.45),
                    .init(color: Color.cavnarPaper.opacity(0.92), location: 1),
                ],
                startPoint: .top, endPoint: .bottom
            )
        }
        .ignoresSafeArea()
    }
}

// MARK: - Aurora

private struct LoginAurora: View {
    var frozen: Bool

    private struct Bloom {
        let relX: CGFloat, relY: CGFloat, relRadius: CGFloat
        let color: Color, opacity: Double
        let period: Double, phase: Double
    }

    private let blooms: [Bloom] = [
        Bloom(relX: 0.22, relY: 0.05, relRadius: 0.75, color: .cavnarEmber,  opacity: 0.50, period: 14, phase: 0),
        Bloom(relX: 0.95, relY: 0.28, relRadius: 0.60, color: .cavnarEmber2, opacity: 0.30, period: 18, phase: 2.1),
        Bloom(relX: 0.50, relY: 1.05, relRadius: 0.70, color: .cavnarEmber,  opacity: 0.22, period: 9,  phase: 4.2),
    ]

    var body: some View {
        TimelineView(.animation(paused: frozen)) { timeline in
            Canvas { context, size in
                let t = timeline.date.timeIntervalSinceReferenceDate
                context.drawLayer { layer in
                    layer.addFilter(.blur(radius: 60))
                    for bloom in blooms {
                        let w = 2 * .pi / bloom.period
                        let dx = frozen ? 0 : sin(t * w + bloom.phase) * size.width * 0.16
                        let dy = frozen ? 0 : cos(t * w * 0.8 + bloom.phase) * size.height * 0.05
                        let breathe = frozen ? 1 : 0.8 + 0.2 * sin(t * w * 1.3 + bloom.phase)
                        let cx = bloom.relX * size.width + dx
                        let cy = bloom.relY * size.height + dy
                        let r = bloom.relRadius * size.width
                        let gradient = Gradient(stops: [
                            .init(color: bloom.color.opacity(bloom.opacity * breathe), location: 0),
                            .init(color: bloom.color.opacity(0), location: 0.62),
                        ])
                        let rect = CGRect(x: cx - r, y: cy - r, width: r * 2, height: r * 2)
                        layer.fill(
                            Path(ellipseIn: rect),
                            with: .radialGradient(gradient, center: CGPoint(x: cx, y: cy), startRadius: 0, endRadius: r)
                        )
                    }
                }
            }
        }
    }
}

// MARK: - Constellation

private struct LoginConstellation: View {
    var frozen: Bool

    private struct Point {
        let x0: CGFloat, y0: CGFloat   // normalized start
        let vx: CGFloat, vy: CGFloat   // normalized units per second
        let r: CGFloat
        let phase: Double
    }

    private static let count = 36
    private static let linkDistance: CGFloat = 110
    // The top ~72% of the screen only — points never wander into the form.
    private static let fieldHeight: CGFloat = 0.72

    // Deterministic — same sky every launch, no per-render reshuffle.
    private static let points: [Point] = {
        var seed: UInt64 = 0x9E3779B97F4A7C15
        func next() -> CGFloat {
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return CGFloat((seed >> 33) % 10_000) / 10_000
        }
        return (0..<count).map { _ in
            let angle = next() * 2 * .pi
            let speed = 0.006 + next() * 0.010   // normalized/sec — a slow drift
            return Point(
                x0: next(), y0: next(),
                vx: cos(angle) * speed, vy: sin(angle) * speed,
                r: 1 + next() * 1.4,
                phase: Double(next()) * 2 * .pi
            )
        }
    }()

    var body: some View {
        TimelineView(.animation(paused: frozen)) { timeline in
            Canvas { context, size in
                let t = frozen ? 0 : timeline.date.timeIntervalSinceReferenceDate
                let h = size.height * Self.fieldHeight
                let positions: [CGPoint] = Self.points.map { p in
                    let nx = (p.x0 + p.vx * t).truncatingRemainder(dividingBy: 1)
                    let ny = (p.y0 + p.vy * t).truncatingRemainder(dividingBy: 1)
                    return CGPoint(x: (nx < 0 ? nx + 1 : nx) * size.width, y: (ny < 0 ? ny + 1 : ny) * h)
                }
                // Hairlines first, so the points sit on top of them.
                for i in positions.indices {
                    for j in (i + 1)..<positions.count {
                        let dx = positions[i].x - positions[j].x
                        let dy = positions[i].y - positions[j].y
                        let d = (dx * dx + dy * dy).squareRoot()
                        guard d < Self.linkDistance else { continue }
                        var line = Path()
                        line.move(to: positions[i])
                        line.addLine(to: positions[j])
                        context.stroke(
                            line,
                            with: .color(Color.cavnarEmber2.opacity(0.22 * Double(1 - d / Self.linkDistance))),
                            lineWidth: 0.8
                        )
                    }
                }
                for (i, p) in Self.points.enumerated() {
                    let glow = 0.55 + 0.45 * sin(t / 0.9 + p.phase)
                    let c = positions[i]
                    context.fill(
                        Path(ellipseIn: CGRect(x: c.x - p.r, y: c.y - p.r, width: p.r * 2, height: p.r * 2)),
                        with: .color(Color.cavnarEmber2.opacity(0.5 * glow))
                    )
                }
            }
        }
        .opacity(0.8)
        .allowsHitTesting(false)
    }
}

// MARK: - Entrance stagger

/// One fade-and-rise per element, offset by `delay` — the login screen's
/// choreography (wordmark first, then the greeting, fields, button, social
/// row, anchor, each ~0.08s apart).
struct LoginRise: ViewModifier {
    var delay: Double
    var enabled: Bool = true
    @State private var appeared = false

    func body(content: Content) -> some View {
        content
            .opacity(appeared || !enabled ? 1 : 0)
            .offset(y: appeared || !enabled ? 0 : 14)
            .task {
                guard enabled, !appeared else { return }
                try? await Task.sleep(for: .seconds(delay))
                withAnimation(.timingCurve(0.2, 0.7, 0.2, 1, duration: 0.8)) { appeared = true }
            }
    }
}

extension View {
    func loginRise(_ delay: Double, enabled: Bool = true) -> some View {
        modifier(LoginRise(delay: delay, enabled: enabled))
    }
}

// MARK: - Button sheen

/// A soft diagonal highlight that sweeps across the primary button every
/// few seconds — the one "alive" cue on the form itself.
struct LoginSheen: ViewModifier {
    var period: Double = 4.5
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content.overlay {
            if !reduceMotion {
                TimelineView(.animation) { timeline in
                    GeometryReader { geo in
                        let phase = timeline.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: period) / period
                        // Idle for the first ~55% of the period, then sweep.
                        let sweep = max(0, (phase - 0.55) / 0.25)
                        let x = (-1.2 + 2.4 * min(sweep, 1)) * geo.size.width
                        LinearGradient(
                            colors: [.clear, Color.white.opacity(0.22), .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.5)
                        .rotationEffect(.degrees(12))
                        .offset(x: x)
                        .opacity(sweep > 0 && sweep < 1 ? 1 : 0)
                    }
                }
                .allowsHitTesting(false)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
    }
}

extension View {
    func loginSheen() -> some View { modifier(LoginSheen()) }
}
