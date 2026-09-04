import SwiftUI

/// Recoverable Gauge — the money on the table this month as a half-arc,
/// projected to the year beneath once the arc lands. The arc fills with an
/// ember gradient and a hot tip.
struct RecoverableGaugeChart: View {
    let monthly: Double
    let annual: Double
    /// What "full" means — the month's total waste + overstock exposure, so
    /// the arc reads as "this much of what's leaking is recoverable".
    let ceiling: Double

    private var fraction: Double { ceiling > 0 ? min(1, monthly / ceiling) : 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Recoverable", title: "Recoverable Gauge",
                              detail: "Waste and overstock you can claw back with better ordering — the one number to remember.")
            CavnarAnimatedCanvas(duration: 1.4, height: 230, replayKey: "\(Int(monthly))-\(Int(ceiling))") { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let center = CGPoint(x: size.width / 2, y: size.height * 0.72)
        let R = min(size.width * 0.34, size.height * 0.6)
        let s = CavnarChart.easeInOut(min(1, t / 1.0))
        var track = Path()
        track.addArc(center: center, radius: R, startAngle: .degrees(180), endAngle: .degrees(360), clockwise: false)
        ctx.stroke(track, with: .color(Color.white.opacity(0.06)), style: StrokeStyle(lineWidth: 18, lineCap: .round))
        let end = Angle.degrees(180 + 180 * fraction * s)
        var arc = Path()
        arc.addArc(center: center, radius: R, startAngle: .degrees(180), endAngle: end, clockwise: false)
        if fraction * s > 0.002 {
            CavnarChart.glowStroke(&ctx, arc,
                                   shading: .linearGradient(Gradient(colors: [.cavnarEmber, cavnarEmberHot]),
                                                            startPoint: CGPoint(x: center.x - R, y: 0), endPoint: CGPoint(x: center.x + R, y: 0)),
                                   glow: .cavnarEmber, lineWidth: 18, blur: 12)
            let tip = CGPoint(x: center.x + CGFloat(cos(end.radians)) * R, y: center.y + CGFloat(sin(end.radians)) * R)
            ctx.drawLayer { layer in
                layer.addFilter(.shadow(color: cavnarEmberHot.opacity(0.9), radius: 10))
                layer.fill(Path(ellipseIn: CGRect(x: tip.x - 7, y: tip.y - 7, width: 14, height: 14)), with: .color(cavnarEmberHot))
            }
        }
        ctx.drawLayer { layer in
            layer.addFilter(.shadow(color: Color.cavnarEmber.opacity(0.8), radius: 14))
            CavnarChart.text(&layer, CavnarChart.number("$\(Int((monthly * s).rounded()).formatted())", size: 34, weight: 700),
                             at: CGPoint(x: center.x, y: center.y - 16))
        }
        CavnarChart.text(&ctx, CavnarChart.kicker("Recoverable this month"), at: CGPoint(x: center.x, y: center.y + 4))
        let at = CavnarChart.window(t, from: 0.85, length: 0.15)
        ctx.drawLayer { layer in
            layer.opacity = at
            CavnarChart.text(&layer, CavnarChart.number("$\(Int(annual.rounded()).formatted()) / year at this pace", size: 13),
                             at: CGPoint(x: center.x, y: center.y + 30))
        }
        CavnarChart.text(&ctx, CavnarChart.label("$0", size: 10), at: CGPoint(x: center.x - R - 6, y: center.y + 22), anchor: .leading)
        CavnarChart.text(&ctx, CavnarChart.label("$\(Int(ceiling.rounded()).formatted())", size: 10), at: CGPoint(x: center.x + R + 6, y: center.y + 22), anchor: .trailing)
    }
}
