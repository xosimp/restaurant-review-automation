import SwiftUI

/// Home's ground — the obsidian field. Replaces the ember aurora that used
/// to sit behind the hero (device feedback: the orange wash "looks awful").
/// Nothing here is orange except a handful of embers; the field reads as
/// black stone with light moving across it.
///
/// Three wall-clock layers (TimelineView at 30fps, all frozen under Reduce
/// Motion, none of them a post-blur — same performance profile as the
/// sign-in screen's background, which is proven on device):
///  1. Veils — two very soft, very slow pools of light: cream high on the
///     left, slate low on the right, so the field has depth instead of one
///     flat glow — plus a wide, faint band of light that crosses the top of
///     the screen once every 24 seconds, eased, like light passing over
///     stone.
///  2. Constellation — the sign-in screen's thinking network (points on
///     slow headings, hairlines between neighbours), in cream instead of
///     ember, over the top ~60% only.
///  3. Embers — eight sparks rising slowly up the field and fading, each
///     on its own period and sway. The brand's one ember note, as points
///     of light rather than a wash.
/// A fade to Paper toward the bottom keeps everything below the fold on
/// solid ground.
struct HomeObsidianField: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    static let frameInterval: Double = 1.0 / 30.0

    var body: some View {
        ZStack {
            Color.cavnarPaper
            ObsidianVeils(frozen: reduceMotion)
            ObsidianConstellation(frozen: reduceMotion)
            RisingEmbers(frozen: reduceMotion)
            LinearGradient(
                stops: [
                    .init(color: Color.cavnarPaper.opacity(0), location: 0),
                    .init(color: Color.cavnarPaper.opacity(0.30), location: 0.50),
                    .init(color: Color.cavnarPaper.opacity(0.92), location: 1),
                ],
                startPoint: .top, endPoint: .bottom
            )
        }
        .ignoresSafeArea()
        .allowsHitTesting(false)
    }
}

// MARK: - Veils

private struct ObsidianVeils: View {
    var frozen: Bool

    // A cool counterpart to the cream — never saturated enough to read as
    // blue, just enough to make the lower field a different temperature.
    private static let slate = Color(red: 0.56, green: 0.64, blue: 0.76)

    var body: some View {
        TimelineView(.animation(minimumInterval: HomeObsidianField.frameInterval, paused: frozen)) { timeline in
            Canvas { context, size in
                let t = frozen ? 0 : timeline.date.timeIntervalSinceReferenceDate

                // 1. Cream pool, high left — moonlight on obsidian.
                drift(&context, size: size, t: t,
                      relX: 0.18, relY: 0.06, relRadius: 0.95, color: .cavnarInk, opacity: 0.075,
                      period: 26, phase: 0, driftX: 0.08, driftY: 0.03)
                // 2. Slate pool, low right.
                drift(&context, size: size, t: t,
                      relX: 0.92, relY: 0.58, relRadius: 0.80, color: Self.slate, opacity: 0.065,
                      period: 34, phase: 2.2, driftX: 0.06, driftY: 0.04)

                // 3. The passing light — one slow crossing every 24s,
                // eased at both ends and faded in and out so it never
                // has an edge to notice.
                let cycle = frozen ? 0.5 : (t / 24).truncatingRemainder(dividingBy: 1)
                let eased = cycle * cycle * (3 - 2 * cycle)
                let x = (-0.35 + 1.7 * eased) * size.width
                let fade = pow(sin(cycle * .pi), 1.4)
                veil(&context, center: CGPoint(x: x, y: size.height * 0.16),
                     radius: size.width * 0.62, color: .cavnarInk, opacity: 0.05 * fade)
            }
        }
    }

    private func drift(_ context: inout GraphicsContext, size: CGSize, t: Double,
                       relX: CGFloat, relY: CGFloat, relRadius: CGFloat, color: Color, opacity: Double,
                       period: Double, phase: Double, driftX: CGFloat, driftY: CGFloat) {
        let w = 2 * .pi / period
        let dx = frozen ? 0 : sin(t * w + phase) * size.width * driftX
        let dy = frozen ? 0 : cos(t * w * 0.8 + phase) * size.height * driftY
        let breathe = frozen ? 1 : 0.86 + 0.14 * sin(t * w * 1.3 + phase)
        veil(&context, center: CGPoint(x: relX * size.width + dx, y: relY * size.height + dy),
             radius: relRadius * size.width, color: color, opacity: opacity * breathe)
    }

    private func veil(_ context: inout GraphicsContext, center: CGPoint, radius: CGFloat, color: Color, opacity: Double) {
        guard opacity > 0.0005 else { return }
        // A long, eased falloff — the middle stops keep the edge from ever
        // reading as a ring.
        let gradient = Gradient(stops: [
            .init(color: color.opacity(opacity), location: 0),
            .init(color: color.opacity(opacity * 0.55), location: 0.25),
            .init(color: color.opacity(opacity * 0.20), location: 0.55),
            .init(color: color.opacity(0), location: 1),
        ])
        let rect = CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2)
        context.fill(Path(ellipseIn: rect),
                     with: .radialGradient(gradient, center: center, startRadius: 0, endRadius: radius))
    }
}

// MARK: - Constellation

private struct ObsidianConstellation: View {
    var frozen: Bool

    private struct Point {
        let x0: CGFloat, y0: CGFloat   // normalized start
        let vx: CGFloat, vy: CGFloat   // normalized units per second
        let r: CGFloat
        let phase: Double
    }

    private static let count = 34
    private static let linkDistance: CGFloat = 105
    // The top ~60% of the screen — the hero and the pulse strip sit on the
    // network; the deck and everything below it sit on quieter ground.
    private static let fieldHeight: CGFloat = 0.60

    // Deterministic — the same sky every launch, no per-render reshuffle.
    private static let points: [Point] = {
        var seed: UInt64 = 0xC2B2AE3D27D4EB4F
        func next() -> CGFloat {
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return CGFloat((seed >> 33) % 10_000) / 10_000
        }
        return (0..<count).map { _ in
            let angle = next() * 2 * .pi
            let speed = 0.008 + next() * 0.012
            return Point(
                x0: next(), y0: next(),
                vx: cos(angle) * speed, vy: sin(angle) * speed,
                r: 0.9 + next() * 1.3,
                phase: Double(next()) * 2 * .pi
            )
        }
    }()

    var body: some View {
        TimelineView(.animation(minimumInterval: HomeObsidianField.frameInterval, paused: frozen)) { timeline in
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
                            with: .color(Color.cavnarInk.opacity(0.13 * Double(1 - d / Self.linkDistance))),
                            lineWidth: 0.7
                        )
                    }
                }
                for (i, p) in Self.points.enumerated() {
                    let glow = 0.55 + 0.45 * sin(t / 0.9 + p.phase)
                    let c = positions[i]
                    context.fill(
                        Path(ellipseIn: CGRect(x: c.x - p.r, y: c.y - p.r, width: p.r * 2, height: p.r * 2)),
                        with: .color(Color.cavnarInk.opacity(0.42 * glow))
                    )
                }
            }
        }
    }
}

// MARK: - Embers

private struct RisingEmbers: View {
    var frozen: Bool

    private struct Spark {
        let x0: CGFloat       // normalized column
        let period: Double    // seconds for one full rise
        let phase: Double     // 0…1 offset into the rise
        let r: CGFloat
        let sway: CGFloat     // normalized side-to-side amplitude
        let swayPeriod: Double
    }

    private static let sparks: [Spark] = {
        var seed: UInt64 = 0x5851F42D4C957F2D
        func next() -> CGFloat {
            seed = seed &* 6364136223846793005 &+ 1442695040888963407
            return CGFloat((seed >> 33) % 10_000) / 10_000
        }
        return (0..<8).map { _ in
            Spark(
                x0: 0.08 + next() * 0.84,
                period: 18 + Double(next()) * 14,
                phase: Double(next()),
                r: 1.1 + next() * 1.1,
                sway: 0.015 + next() * 0.02,
                swayPeriod: 5 + Double(next()) * 4
            )
        }
    }()

    var body: some View {
        TimelineView(.animation(minimumInterval: HomeObsidianField.frameInterval, paused: frozen)) { timeline in
            Canvas { context, size in
                let t = frozen ? 0 : timeline.date.timeIntervalSinceReferenceDate
                for s in Self.sparks {
                    // 0 at the bottom of the rise, 1 at the top. Frozen:
                    // spread the sparks up the field by phase instead.
                    let p = frozen ? (0.2 + s.phase * 0.6) : (t / s.period + s.phase).truncatingRemainder(dividingBy: 1)
                    let y = size.height * (0.92 - 0.86 * p)
                    let x = size.width * s.x0 + sin(t * 2 * .pi / s.swayPeriod + s.phase * 6) * s.sway * size.width
                    // Fade in low, fade out high; a slow flicker on top.
                    let envelope = pow(sin(p * .pi), 1.6)
                    let flicker = frozen ? 0.5 : 0.5 + 0.5 * sin(t * 3.1 + s.phase * 6)
                    let alpha = envelope * (0.35 + 0.35 * flicker)
                    guard alpha > 0.01 else { continue }
                    let c = CGPoint(x: x, y: y)
                    let halo = s.r * 4.5
                    context.fill(
                        Path(ellipseIn: CGRect(x: c.x - halo, y: c.y - halo, width: halo * 2, height: halo * 2)),
                        with: .radialGradient(
                            Gradient(colors: [Color.cavnarEmber2.opacity(alpha * 0.35), Color.cavnarEmber2.opacity(0)]),
                            center: c, startRadius: 0, endRadius: halo
                        )
                    )
                    context.fill(
                        Path(ellipseIn: CGRect(x: c.x - s.r, y: c.y - s.r, width: s.r * 2, height: s.r * 2)),
                        with: .color(Color.cavnarEmber2.opacity(alpha))
                    )
                }
            }
        }
    }
}
