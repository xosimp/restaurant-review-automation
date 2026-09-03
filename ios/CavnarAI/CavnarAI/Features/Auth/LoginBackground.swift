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
///
/// Performance: both layers redraw at 30fps, not the display's 120 — the
/// drift is far too slow for the difference to be visible, and the first
/// version at 120fps WITH a full-screen 60pt blur filter re-applied on
/// every frame was what made the whole screen (and the sign-up sheet
/// built on it) stutter under a scroll. The blooms are now soft-edged by
/// their own radial gradient instead of a post-blur, which is nearly free.
struct LoginBackground: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let frameInterval: Double = 1.0 / 30.0

    var body: some View {
        ZStack {
            Color.cavnarPaper
            LoginAurora(frozen: reduceMotion)
            LoginConstellation(frozen: reduceMotion)
            LinearGradient(
                stops: [
                    .init(color: Color.cavnarPaper.opacity(0), location: 0),
                    .init(color: Color.cavnarPaper.opacity(0.28), location: 0.45),
                    .init(color: Color.cavnarPaper.opacity(0.90), location: 1),
                ],
                startPoint: .top, endPoint: .bottom
            )
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
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

    // Opacities up from the first pass (0.50/0.30/0.22) — the ember wash
    // read as a hint rather than a presence against the paper.
    private let blooms: [Bloom] = [
        Bloom(relX: 0.22, relY: 0.05, relRadius: 0.80, color: .cavnarEmber,  opacity: 0.66, period: 14, phase: 0),
        Bloom(relX: 0.95, relY: 0.28, relRadius: 0.64, color: .cavnarEmber2, opacity: 0.42, period: 18, phase: 2.1),
        Bloom(relX: 0.50, relY: 1.05, relRadius: 0.75, color: .cavnarEmber,  opacity: 0.34, period: 9,  phase: 4.2),
    ]

    var body: some View {
        TimelineView(.animation(minimumInterval: LoginBackground.frameInterval, paused: frozen)) { timeline in
            Canvas { context, size in
                let t = timeline.date.timeIntervalSinceReferenceDate
                for bloom in blooms {
                    let w = 2 * .pi / bloom.period
                    let dx = frozen ? 0 : sin(t * w + bloom.phase) * size.width * 0.16
                    let dy = frozen ? 0 : cos(t * w * 0.8 + bloom.phase) * size.height * 0.05
                    let breathe = frozen ? 1 : 0.82 + 0.18 * sin(t * w * 1.3 + bloom.phase)
                    let cx = bloom.relX * size.width + dx
                    let cy = bloom.relY * size.height + dy
                    let r = bloom.relRadius * size.width
                    // A long, eased falloff does the softening a blur used
                    // to — the middle stops keep the edge from ever reading
                    // as a ring.
                    let a = bloom.opacity * breathe
                    let gradient = Gradient(stops: [
                        .init(color: bloom.color.opacity(a), location: 0),
                        .init(color: bloom.color.opacity(a * 0.55), location: 0.22),
                        .init(color: bloom.color.opacity(a * 0.22), location: 0.45),
                        .init(color: bloom.color.opacity(a * 0.06), location: 0.68),
                        .init(color: bloom.color.opacity(0), location: 1),
                    ])
                    let rect = CGRect(x: cx - r, y: cy - r, width: r * 2, height: r * 2)
                    context.fill(
                        Path(ellipseIn: rect),
                        with: .radialGradient(gradient, center: CGPoint(x: cx, y: cy), startRadius: 0, endRadius: r)
                    )
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
            // A touch quicker than the first pass (was 0.006–0.016).
            let speed = 0.009 + next() * 0.013   // normalized/sec
            return Point(
                x0: next(), y0: next(),
                vx: cos(angle) * speed, vy: sin(angle) * speed,
                r: 1 + next() * 1.4,
                phase: Double(next()) * 2 * .pi
            )
        }
    }()

    var body: some View {
        TimelineView(.animation(minimumInterval: LoginBackground.frameInterval, paused: frozen)) { timeline in
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
                            with: .color(Color.cavnarEmber2.opacity(0.24 * Double(1 - d / Self.linkDistance))),
                            lineWidth: 0.8
                        )
                    }
                }
                for (i, p) in Self.points.enumerated() {
                    let glow = 0.55 + 0.45 * sin(t / 0.9 + p.phase)
                    let c = positions[i]
                    context.fill(
                        Path(ellipseIn: CGRect(x: c.x - p.r, y: c.y - p.r, width: p.r * 2, height: p.r * 2)),
                        with: .color(Color.cavnarEmber2.opacity(0.55 * glow))
                    )
                }
            }
        }
        .opacity(0.85)
    }
}

// MARK: - Entrance stagger

/// One fade-and-rise per element, offset by `delay` — the login screen's
/// choreography (wordmark first, then the fields, button, social row,
/// anchor, each ~0.08s apart).
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
///
/// The first version was a three-stop gradient rotated 12° — a visible
/// parallelogram with edges, and it snapped on and off. This is a
/// smooth bell of many stops (opacity follows sin²), on a strip taller
/// than the button so the rotation's corners never land inside the clip,
/// eased in and out over the sweep, and softened once more with a light
/// blur. It reads as light crossing the surface, not a shape sliding by.
struct LoginSheen: ViewModifier {
    var period: Double = 4.5
    var sweep: Double = 1.1
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let stops: [Gradient.Stop] = {
        let n = 24
        return (0...n).map { i in
            let x = Double(i) / Double(n)
            let a = pow(sin(x * .pi), 2) * 0.30
            return .init(color: Color.white.opacity(a), location: x)
        }
    }()

    func body(content: Content) -> some View {
        content.overlay {
            if !reduceMotion {
                TimelineView(.animation(minimumInterval: 1.0 / 60.0)) { timeline in
                    GeometryReader { geo in
                        let phase = timeline.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: period)
                        let start = period - sweep
                        let p = max(0, min(1, (phase - start) / sweep))          // 0→1 across the sweep
                        let eased = p * p * (3 - 2 * p)                            // smoothstep
                        let fade = pow(sin(p * .pi), 0.6)                          // soft in/out
                        let bandWidth = geo.size.width * 0.55
                        let x = -bandWidth + (geo.size.width + bandWidth * 2) * eased - bandWidth
                        LinearGradient(gradient: Gradient(stops: Self.stops), startPoint: .leading, endPoint: .trailing)
                            .frame(width: bandWidth, height: geo.size.height * 3)
                            .rotationEffect(.degrees(14))
                            .blur(radius: 6)
                            .offset(x: x + bandWidth, y: -geo.size.height)
                            .opacity(p > 0 && p < 1 ? fade : 0)
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
