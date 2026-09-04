import SwiftUI

/// Sentiment River — positive, neutral and negative reviews by week as one
/// flowing stacked band, with the average rating riding on top. Replaces
/// the stacked-bar sentiment trend. Bands rise from the baseline over the
/// first 65% of the entrance, then the rating line draws left-to-right and
/// its endpoint breathes.
struct SentimentRiverChart: View {
    let weeks: [SentimentWeek]

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Sentiment trend", title: "Sentiment River",
                              detail: "Positive, neutral and negative by week — the average rating rides on top.")
            CavnarAnimatedCanvas(duration: 1.7, height: 250, replayKey: weeks.map(\.id).joined()) { ctx, size, t, clock in
                draw(&ctx, size: size, t: t, clock: clock)
            } overlay: {
                legend
            }
        }
    }

    private var legend: some View {
        HStack(spacing: 10) {
            legendItem(.cavnarGreen, "Positive")
            legendItem(.cavnarInk3, "Neutral")
            legendItem(.cavnarRed, "Negative")
        }
        .padding(12)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topTrailing)
        .allowsHitTesting(false)
    }

    private func legendItem(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 5) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(label).font(.cavnarBody(10.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double, clock: Double) {
        guard weeks.count > 1 else { return }
        let inset = UIEdgeInsets(top: 42, left: 18, bottom: 30, right: 18)
        let plot = CGRect(x: inset.left, y: inset.top, width: size.width - inset.left - inset.right, height: size.height - inset.top - inset.bottom)
        let maxTotal = max(1, Double(weeks.map(\.total).max() ?? 1)) * 1.15
        let n = weeks.count
        func x(_ i: Int) -> CGFloat { plot.minX + plot.width * CGFloat(i) / CGFloat(n - 1) }
        func y(_ v: Double) -> CGFloat { plot.maxY - CGFloat(v / maxTotal) * plot.height }

        CavnarChart.grid(&ctx, plot: plot)

        let rise = CavnarChart.easeOut(min(1, t / 0.65))
        var base = [Double](repeating: 0, count: n)
        let layers: [(values: [Double], color: Color, alpha: Double)] = [
            (weeks.map { Double($0.negative) }, .cavnarRed, 0.9),
            (weeks.map { Double($0.neutral) }, .cavnarInk3, 0.5),
            (weeks.map { Double($0.positive) }, .cavnarGreen, 0.85),
        ]
        for layer in layers {
            let top = (0..<n).map { base[$0] + layer.values[$0] * rise }
            var path = CavnarChart.smoothPath((0..<n).map { CGPoint(x: x($0), y: y(top[$0])) })
            path.addLine(to: CGPoint(x: x(n - 1), y: y(base[n - 1])))
            CavnarChart.appendReversedSmooth(&path, (0..<n).map { CGPoint(x: x($0), y: y(base[$0])) })
            path.closeSubpath()
            ctx.fill(path, with: .linearGradient(
                Gradient(colors: [layer.color.opacity(layer.alpha), layer.color.opacity(layer.alpha * 0.25)]),
                startPoint: CGPoint(x: 0, y: plot.minY), endPoint: CGPoint(x: 0, y: plot.maxY)
            ))
            base = top
        }

        // Rating line, drawn left-to-right after the bands have risen.
        let ln = CavnarChart.easeOut(CavnarChart.window(t, from: 0.55, length: 0.45))
        let ratings = weeks.map(\.avgRating)
        let lo = 3.8, hi = 5.0
        func ry(_ v: Double) -> CGFloat { plot.maxY - CGFloat((v - lo) / (hi - lo)) * plot.height * 0.9 }
        let linePath = CavnarChart.smoothPath((0..<n).map { CGPoint(x: x($0), y: ry(ratings[$0])) })
        ctx.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: plot.minX - 4, y: 0, width: plot.width * ln + 8, height: size.height)))
            CavnarChart.glowStroke(&layer, linePath, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 2.2, blur: 6)
        }
        if ln >= 1 {
            let end = CGPoint(x: x(n - 1), y: ry(ratings[n - 1]))
            let breathe = 1 + 0.5 * sin(clock * 2.2)
            CavnarChart.hotDot(&ctx, at: end, radius: 3.5, halo: 7 * breathe)
            CavnarChart.text(&ctx, CavnarChart.number(String(format: "%.1f★", ratings[n - 1]), size: 13, weight: 700),
                             at: CGPoint(x: end.x - 10, y: end.y - 12), anchor: .trailing)
        }

        for i in 0..<n {
            CavnarChart.text(&ctx, CavnarChart.label(weeks[i].label, size: 10.5), at: CGPoint(x: x(i), y: size.height - 12))
        }
    }
}
