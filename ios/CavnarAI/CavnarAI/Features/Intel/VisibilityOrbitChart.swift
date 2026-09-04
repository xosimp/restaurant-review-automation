import SwiftUI

/// Visibility Orbit — today's AI-visibility score as a ring with an
/// orbiting hot dot at its tip, and the history of every run as a thin
/// line to the right, each run landing as a dot. The drop alert, made
/// visible before it fires.
struct VisibilityOrbitChart: View {
    let score: Int
    let runs: [AIVisibilityRun]

    private var delta: Int? {
        guard runs.count >= 2 else { return nil }
        return runs[runs.count - 1].aiScore - runs[runs.count - 2].aiScore
    }

    var body: some View {
        CavnarAnimatedCanvas(duration: 1.6, height: 230, replayKey: "\(score)-\(runs.count)") { ctx, size, t, clock in
            draw(&ctx, size: size, t: t, clock: clock)
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double, clock: Double) {
        let center = CGPoint(x: size.width * 0.3, y: size.height * 0.46)
        let R: CGFloat = 52
        let s = CavnarChart.easeInOut(min(1, t / 0.9))
        ctx.stroke(Path(ellipseIn: CGRect(x: center.x - R, y: center.y - R, width: R * 2, height: R * 2)), with: .color(Color.white.opacity(0.06)), lineWidth: 12)
        let sweep = 360 * Double(score) / 100 * s
        var arc = Path()
        arc.addArc(center: center, radius: R, startAngle: .degrees(-90), endAngle: .degrees(-90 + sweep), clockwise: false)
        if sweep > 0.5 {
            CavnarChart.glowStroke(&ctx, arc, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 12, blur: 8)
        }
        let orbit = (-90 + sweep) * Double.pi / 180
        let dot = CGPoint(x: center.x + CGFloat(cos(orbit)) * R, y: center.y + CGFloat(sin(orbit)) * R)
        let r = 5 + CGFloat(sin(clock * 3))
        ctx.drawLayer { layer in
            layer.addFilter(.shadow(color: cavnarEmberHot.opacity(0.9), radius: 8))
            layer.fill(Path(ellipseIn: CGRect(x: dot.x - r, y: dot.y - r, width: r * 2, height: r * 2)), with: .color(cavnarEmberHot))
        }
        CavnarChart.text(&ctx, CavnarChart.number("\(Int((Double(score) * s).rounded()))", size: 28, weight: 700), at: CGPoint(x: center.x, y: center.y - 4))
        CavnarChart.text(&ctx, CavnarChart.kicker("AI visibility"), at: CGPoint(x: center.x, y: center.y + 16))

        // History
        let L = size.width * 0.56, Rr = size.width - 18, T: CGFloat = 54, B = size.height - 46
        CavnarChart.text(&ctx, CavnarChart.label("LAST \(runs.count) RUN\(runs.count == 1 ? "" : "S")", size: 10.5, weight: 700), at: CGPoint(x: L, y: T - 16), anchor: .leading)
        guard runs.count >= 2 else {
            CavnarChart.text(&ctx, CavnarChart.label("Run another check to start the trend.", size: 11), at: CGPoint(x: L, y: (T + B) / 2), anchor: .leading)
            return
        }
        let n = runs.count
        let vals = runs.map { Double($0.aiScore) }
        let lo = max(0, (vals.min() ?? 0) - 10), hi = min(100, (vals.max() ?? 100) + 10)
        func x(_ i: Int) -> CGFloat { L + (Rr - L) * CGFloat(i) / CGFloat(n - 1) }
        func y(_ v: Double) -> CGFloat { B - (B - T) * CGFloat((v - lo) / max(1, hi - lo)) }
        let ln = CavnarChart.easeOut(CavnarChart.window(t, from: 0.35, length: 0.65))
        var line = Path()
        for i in 0..<n { let p = CGPoint(x: x(i), y: y(vals[i])); i == 0 ? line.move(to: p) : line.addLine(to: p) }
        ctx.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: L - 4, y: 0, width: (Rr - L) * ln + 8, height: size.height)))
            CavnarChart.glowStroke(&layer, line, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 2, blur: 5)
            var fill = line
            fill.addLine(to: CGPoint(x: Rr, y: B)); fill.addLine(to: CGPoint(x: L, y: B)); fill.closeSubpath()
            layer.fill(fill, with: .linearGradient(Gradient(colors: [Color.cavnarEmber.opacity(0.25), Color.cavnarEmber.opacity(0)]),
                                                   startPoint: CGPoint(x: 0, y: T), endPoint: CGPoint(x: 0, y: B)))
        }
        for i in 0..<n where x(i) <= L + (Rr - L) * ln {
            let p = CGPoint(x: x(i), y: y(vals[i]))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 2.5, y: p.y - 2.5, width: 5, height: 5)), with: .color(i == n - 1 ? cavnarEmberHot : .cavnarEmber2))
        }
        if let delta {
            let text = delta == 0 ? "No change since last run" : (delta > 0 ? "+\(delta) since last run" : "\(delta) since last run")
            ctx.drawLayer { layer in
                layer.opacity = ln
                CavnarChart.text(&layer, CavnarChart.number(text, size: 12, weight: 700, color: delta >= 0 ? .cavnarGreen : .cavnarRed),
                                 at: CGPoint(x: Rr, y: size.height - 16), anchor: .trailing)
            }
        }
    }
}
