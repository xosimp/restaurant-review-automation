import SwiftUI

/// Week Radar — seven spokes, one per weekday, labor % as the radius. The
/// polygon grows from the centre; spokes light one at a time clockwise; a
/// day over target lights red. Replaces the "By Day" bar mode.
struct WeekRadarChart: View {
    let dowSummary: [String: Double]
    let target: Double
    var subtitle: String

    private static let days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    private static let short = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    private var values: [Double] { Self.days.map { dowSummary[$0] ?? target } }
    private var worst: (index: Int, pct: Double)? {
        let present = Self.days.enumerated().compactMap { i, d in dowSummary[d].map { (i, $0) } }
        return present.max { $0.1 < $1.1 }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "By weekday · \(subtitle)", title: "Week Radar",
                              detail: "The week's rhythm as a shape — a spike on Saturday reads as a spike.")
            CavnarAnimatedCanvas(duration: 1.5, height: 260, replayKey: values.map { "\($0)" }.joined()) { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let center = CGPoint(x: size.width / 2, y: size.height / 2 + 6)
        let R = min(size.width, size.height) * 0.36
        let vals = values
        let lo = floor(min(vals.min() ?? target, target) - 6), hi = ceil(max(vals.max() ?? target, target) + 4)
        func angle(_ i: Int) -> Double { -Double.pi / 2 + Double(i) * Double.pi * 2 / 7 }
        func radius(_ v: Double) -> CGFloat { R * CGFloat((v - lo) / max(1, hi - lo)) }
        func polygon(_ r: (Int) -> CGFloat) -> Path {
            var p = Path()
            for i in 0..<7 {
                let a = angle(i), rr = r(i)
                let pt = CGPoint(x: center.x + CGFloat(cos(a)) * rr, y: center.y + CGFloat(sin(a)) * rr)
                i == 0 ? p.move(to: pt) : p.addLine(to: pt)
            }
            p.closeSubpath()
            return p
        }
        for g in 1...4 {
            ctx.stroke(polygon { _ in R * CGFloat(g) / 4 }, with: .color(Color.white.opacity(0.06)), lineWidth: 1)
        }
        ctx.stroke(polygon { _ in radius(target) }, with: .color(Color.cavnarInk3.opacity(0.45)), style: StrokeStyle(lineWidth: 1, dash: [3, 4]))

        let s = CavnarChart.easeInOut(min(1, t / 0.9))
        let shape = polygon { radius(vals[$0]) * CGFloat(s) }
        ctx.fill(shape, with: .radialGradient(Gradient(colors: [Color.cavnarEmber2.opacity(0.45), Color.cavnarEmber.opacity(0.12)]),
                                              center: center, startRadius: 0, endRadius: R))
        CavnarChart.glowStroke(&ctx, shape, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 2, blur: 7)

        for i in 0..<7 {
            let lit = CavnarChart.window(t, from: 0.1 + Double(i) * 0.09, length: 0.3)
            let a = angle(i), rr = radius(vals[i]) * CGFloat(s)
            let over = vals[i] > target
            let pt = CGPoint(x: center.x + CGFloat(cos(a)) * rr, y: center.y + CGFloat(sin(a)) * rr)
            ctx.fill(Path(ellipseIn: CGRect(x: pt.x - 3.5, y: pt.y - 3.5, width: 7, height: 7)), with: .color((over ? Color.cavnarRed : cavnarEmberHot).opacity(lit)))
            let lp = CGPoint(x: center.x + CGFloat(cos(a)) * (R + 18), y: center.y + CGFloat(sin(a)) * (R + 18))
            CavnarChart.text(&ctx, CavnarChart.label(Self.short[i], size: 11, weight: 700, color: over ? .cavnarRed : .cavnarInk3), at: lp)
        }
        if let worst {
            CavnarChart.text(&ctx, CavnarChart.number("\(Self.short[worst.index]) \(Int(worst.pct.rounded()))%", size: 13, weight: 700),
                             at: CGPoint(x: 14, y: 20), anchor: .leading)
            let delta = worst.pct - target
            let line = delta > 0 ? String(format: "%.0f pts over target", delta) : "Every day on target"
            CavnarChart.text(&ctx, CavnarChart.label(line, size: 10, weight: 700, color: delta > 0 ? .cavnarRed : .cavnarGreen),
                             at: CGPoint(x: 14, y: 38), anchor: .leading)
        }
    }
}
