import SwiftUI

/// Response Rings — three concentric rings: response rate, drafts sent
/// as-is, drafts edited. The owner's real relationship with the AI, as one
/// shape. The outer ring sweeps first, inner rings follow 120ms apart, and
/// the centre number counts up. Replaces the three flat stat tiles.
struct ResponseRingsChart: View {
    let performance: ResponsePerformance

    private var responded: Int { performance.approvedAsIs + performance.edited + performance.regenerated }
    private var responseRate: Double { performance.total > 0 ? Double(responded) / Double(performance.total) : 0 }
    private var asIsRate: Double { responded > 0 ? Double(performance.approvedAsIs) / Double(responded) : 0 }
    private var editedRate: Double { responded > 0 ? Double(performance.edited + performance.regenerated) / Double(responded) : 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Response performance · last \(performance.days) days", title: "Response Rings",
                              detail: "How much gets answered, and how much of the AI's draft goes out untouched.")
            CavnarAnimatedCanvas(duration: 1.3, height: 230, replayKey: "\(performance.total)-\(performance.approvedAsIs)-\(performance.edited)") { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let center = CGPoint(x: size.width * 0.36, y: size.height / 2)
        let rings: [(value: Double, color: Color, radius: CGFloat, label: String)] = [
            (responseRate, .cavnarEmber2, 88, "Responded"),
            (asIsRate, .cavnarEmber, 68, "As-is"),
            (editedRate, cavnarEmberHot, 48, "Edited"),
        ]
        for (i, ring) in rings.enumerated() {
            let s = CavnarChart.easeInOut(CavnarChart.window(t, from: Double(i) * 0.12, length: 0.7))
            let track = Path(ellipseIn: CGRect(x: center.x - ring.radius, y: center.y - ring.radius, width: ring.radius * 2, height: ring.radius * 2))
            ctx.stroke(track, with: .color(Color.white.opacity(0.06)), lineWidth: 11)
            var arc = Path()
            arc.addArc(center: center, radius: ring.radius, startAngle: .degrees(-90), endAngle: .degrees(-90 + 360 * ring.value * s), clockwise: false)
            if ring.value * s > 0.001 {
                CavnarChart.glowStroke(&ctx, arc, color: ring.color, glow: ring.color, lineWidth: 11, blur: 7)
            }
            let ly = center.y - 34 + CGFloat(i) * 34
            ctx.fill(Path(CGRect(x: size.width * 0.68 - 14, y: ly - 4, width: 8, height: 8)), with: .color(ring.color))
            CavnarChart.text(&ctx, CavnarChart.label(ring.label, size: 11.5), at: CGPoint(x: size.width * 0.68, y: ly), anchor: .leading)
            CavnarChart.text(&ctx, CavnarChart.number("\(Int((ring.value * 100 * s).rounded()))%", size: 16, weight: 700),
                             at: CGPoint(x: size.width - 22, y: ly), anchor: .trailing)
        }
        let n = Int((responseRate * 100 * CavnarChart.easeInOut(min(1, t / 0.7))).rounded())
        ctx.drawLayer { layer in
            layer.addFilter(.shadow(color: Color.cavnarEmber.opacity(0.8), radius: 12))
            CavnarChart.text(&layer, CavnarChart.number("\(n)%", size: 30, weight: 700), at: CGPoint(x: center.x, y: center.y - 4))
        }
        CavnarChart.text(&ctx, CavnarChart.kicker("Responded"), at: CGPoint(x: center.x, y: center.y + 16))
    }
}
