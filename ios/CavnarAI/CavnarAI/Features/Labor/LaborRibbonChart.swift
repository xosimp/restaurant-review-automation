import SwiftUI

/// Labor Ribbon — daily labor % as a glowing spline against the target
/// band. The band is the goal, not a caption; days over target pulse red
/// as the line passes them. Falls back to the weekly trend when no daily
/// history exists yet.
struct LaborRibbonChart: View {
    struct Point: Identifiable {
        let id: String
        let label: String
        let pct: Double
    }

    let points: [Point]
    let target: Double
    var subtitle: String

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Labor % · \(subtitle)", title: "Labor Ribbon",
                              detail: "Above the dashed line is over target — those days pulse red as the line passes.")
            CavnarAnimatedCanvas(duration: 1.9, height: 250, replayKey: points.map(\.id).joined()) { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        guard points.count > 1 else { return }
        let plot = CGRect(x: 40, y: 22, width: size.width - 56, height: size.height - 52)
        let values = points.map(\.pct)
        let lo = floor(min(values.min() ?? target, target) - 4), hi = ceil(max(values.max() ?? target, target) + 4)
        let n = points.count
        func x(_ i: Int) -> CGFloat { plot.minX + plot.width * CGFloat(i) / CGFloat(n - 1) }
        func y(_ v: Double) -> CGFloat { plot.maxY - CGFloat((v - lo) / (hi - lo)) * plot.height }

        let bt = CavnarChart.easeOut(min(1, t / 0.4))
        ctx.fill(Path(CGRect(x: plot.minX, y: y(target), width: plot.width, height: plot.maxY - y(target))), with: .color(Color.cavnarGreen.opacity(0.10 * bt)))
        ctx.fill(Path(CGRect(x: plot.minX, y: plot.minY, width: plot.width, height: y(target) - plot.minY)), with: .color(Color.cavnarRed.opacity(0.08 * bt)))
        var dash = Path(); dash.move(to: CGPoint(x: plot.minX, y: y(target))); dash.addLine(to: CGPoint(x: plot.maxX, y: y(target)))
        ctx.stroke(dash, with: .color(Color.cavnarInk3.opacity(0.5 * bt)), style: StrokeStyle(lineWidth: 1, dash: [4, 5]))
        ctx.drawLayer { layer in
            layer.opacity = bt
            CavnarChart.text(&layer, CavnarChart.number("\(Int(target))%", size: 10.5), at: CGPoint(x: plot.minX - 6, y: y(target)), anchor: .trailing)
            CavnarChart.text(&layer, CavnarChart.number("\(Int(hi))%", size: 10.5), at: CGPoint(x: plot.minX - 6, y: plot.minY + 4), anchor: .trailing)
            CavnarChart.text(&layer, CavnarChart.number("\(Int(lo))%", size: 10.5), at: CGPoint(x: plot.minX - 6, y: plot.maxY - 4), anchor: .trailing)
        }

        let lt = CavnarChart.window(t, from: 0.3, length: 0.7)
        let ln = CavnarChart.easeOut(lt)
        let pts = (0..<n).map { CGPoint(x: x($0), y: y(values[$0])) }
        let line = CavnarChart.smoothPath(pts)
        ctx.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: plot.minX - 4, y: 0, width: plot.width * ln + 8, height: size.height)))
            CavnarChart.glowStroke(&layer, line,
                                   shading: .linearGradient(Gradient(colors: [.cavnarEmber2, cavnarEmberHot]),
                                                            startPoint: CGPoint(x: plot.minX, y: 0), endPoint: CGPoint(x: plot.maxX, y: 0)),
                                   glow: .cavnarEmber, lineWidth: 2.4, blur: 7)
            var fill = line
            fill.addLine(to: CGPoint(x: plot.maxX, y: plot.maxY)); fill.addLine(to: CGPoint(x: plot.minX, y: plot.maxY)); fill.closeSubpath()
            layer.fill(fill, with: .linearGradient(Gradient(colors: [Color.cavnarEmber.opacity(0.28), Color.cavnarEmber.opacity(0)]),
                                                   startPoint: CGPoint(x: 0, y: plot.minY), endPoint: CGPoint(x: 0, y: plot.maxY)))
        }
        for i in 0..<n where values[i] > target && x(i) <= plot.minX + plot.width * CGFloat(ln) {
            let age = max(0, (Double(plot.width) * lt - Double(x(i) - plot.minX)) / Double(plot.width))
            let pulse = max(0, 1 - age * 6)
            let c = pts[i]
            ctx.fill(Path(ellipseIn: CGRect(x: c.x - (4 + 10 * pulse), y: c.y - (4 + 10 * pulse), width: (4 + 10 * pulse) * 2, height: (4 + 10 * pulse) * 2)),
                     with: .color(Color.cavnarRed.opacity(0.15 + 0.35 * pulse)))
            ctx.fill(Path(ellipseIn: CGRect(x: c.x - 3, y: c.y - 3, width: 6, height: 6)), with: .color(.cavnarRed))
        }
        let step = n > 8 ? 2 : 1
        for i in stride(from: 0, to: n, by: step) {
            CavnarChart.text(&ctx, CavnarChart.label(points[i].label, size: 10), at: CGPoint(x: x(i), y: size.height - 12))
        }
    }
}
