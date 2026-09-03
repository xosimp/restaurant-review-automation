import SwiftUI

/// Full-page version of LoginBackground's aurora — same drifting-bloom
/// technique (three soft ember radial gradients on slow independent sine/
/// cosine cycles, TimelineView-driven so they can't stall), sized to the
/// whole screen behind Home's scrollable content instead of just being
/// confined to HomeHeroBackground's ~460pt greeting band. That band's own
/// aurora deliberately fades to solid cavnarPaper well before the KPI chart
/// and carousel — an earlier pass tried leaving it vivid and un-faded that
/// far down and it read badly behind real content (see
/// HomeHeroBackground.heroBackgroundHeight's doc comment) — so this is a
/// separate, dimmer layer that sits above the hero band and below the
/// scroll content (see HomeView for why that order matters): opacities here
/// are about half of Login's own, enough to be visibly alive behind the
/// chart/carousel/module grid without competing with them.
struct HomeAmbientAurora: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Bloom {
        let relX: CGFloat, relY: CGFloat, relRadius: CGFloat
        let color: Color, opacity: Double
        let period: Double, phase: Double
    }

    private let blooms: [Bloom] = [
        // Roughly half of Login's own 0.34-0.66: the first pass (0.06-0.09)
        // was invisible on a real device once it sat under the hero band.
        Bloom(relX: 0.15, relY: 0.30, relRadius: 0.55, color: .cavnarEmber,  opacity: 0.24, period: 16, phase: 0),
        Bloom(relX: 0.88, relY: 0.55, relRadius: 0.50, color: .cavnarEmber2, opacity: 0.18, period: 21, phase: 2.4),
        Bloom(relX: 0.40, relY: 0.85, relRadius: 0.60, color: .cavnarEmber,  opacity: 0.16, period: 12, phase: 4.6),
    ]

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            Canvas { context, size in
                let t = timeline.date.timeIntervalSinceReferenceDate
                for bloom in blooms {
                    let w = 2 * .pi / bloom.period
                    let dx = reduceMotion ? 0 : sin(t * w + bloom.phase) * size.width * 0.10
                    let dy = reduceMotion ? 0 : cos(t * w * 0.8 + bloom.phase) * size.height * 0.04
                    let breathe = reduceMotion ? 1 : 0.85 + 0.15 * sin(t * w * 1.3 + bloom.phase)
                    let cx = bloom.relX * size.width + dx
                    let cy = bloom.relY * size.height + dy
                    let r = bloom.relRadius * size.width
                    let a = bloom.opacity * breathe
                    let gradient = Gradient(stops: [
                        .init(color: bloom.color.opacity(a), location: 0),
                        .init(color: bloom.color.opacity(a * 0.5), location: 0.35),
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
        .allowsHitTesting(false)
    }
}
