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
            CavnarAnimatedCanvas(duration: 3.1, height: 250, replayKey: weeks.map(\.id).joined(), ambient: true) { ctx, size, t, clock in
                draw(&ctx, size: size, t: t, clock: clock)
            } overlay: {
                legend
            }
            // A Canvas draws pixels and exposes nothing to VoiceOver, so
            // this chart simply did not exist for a screen-reader user.
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Sentiment by week")
            .accessibilityValue(spokenSummary)
        }
    }

    /// The shape of the trend in a sentence, since the drawing can't be read.
    private var spokenSummary: String {
        guard let latest = weeks.last else { return "No reviews yet." }
        let total = weeks.reduce(0) { $0 + $1.total }
        var parts = ["\(total) reviews over \(weeks.count) weeks"]
        parts.append("most recent week: \(latest.positive) positive, \(latest.neutral) neutral, \(latest.negative) negative")
        if let first = weeks.first(where: { $0.total > 0 }), first.avgRating > 0, latest.avgRating > 0 {
            let direction = latest.avgRating > first.avgRating ? "up" : (latest.avgRating < first.avgRating ? "down" : "flat")
            parts.append("average rating \(direction), \(String(format: "%.1f", first.avgRating)) to \(String(format: "%.1f", latest.avgRating)) stars")
        }
        return parts.joined(separator: ". ") + "."
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
        // Only through weeks that had reviews: a week with none has
        // avg_rating 0, which is a missing measurement, not a rating — it
        // plotted below the chart and could end the line on "0.0★"
        // (CLIENT-54). The line simply bridges the gap.
        let ln = CavnarChart.easeOut(CavnarChart.window(t, from: 0.55, length: 0.45))
        let rated = (0..<n).filter { weeks[$0].total > 0 && weeks[$0].avgRating > 0 }
        guard let lastRated = rated.last else { return drawLabels(&ctx, size: size, n: n, x: x) }
        // The axis reaches down to the lowest real rating rather than
        // clipping a 3.4 week off the bottom.
        let lo = min(3.8, (rated.map { weeks[$0].avgRating }.min() ?? 3.8) - 0.2), hi = 5.0
        func ry(_ v: Double) -> CGFloat { plot.maxY - CGFloat((v - lo) / (hi - lo)) * plot.height * 0.9 }
        if rated.count > 1 {
            let linePath = CavnarChart.smoothPath(rated.map { CGPoint(x: x($0), y: ry(weeks[$0].avgRating)) })
            ctx.drawLayer { layer in
                layer.clip(to: Path(CGRect(x: plot.minX - 4, y: 0, width: plot.width * ln + 8, height: size.height)))
                CavnarChart.glowStroke(&layer, linePath, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 2.2, blur: 6)
            }
        }
        if ln >= 1 {
            let end = CGPoint(x: x(lastRated), y: ry(weeks[lastRated].avgRating))
            let breathe = 1 + 0.5 * sin(clock * 2.2)
            CavnarChart.hotDot(&ctx, at: end, radius: 3.5, halo: 7 * breathe)
            CavnarChart.text(&ctx, CavnarChart.number(String(format: "%.1f★", weeks[lastRated].avgRating), size: 13, weight: 700),
                             at: CGPoint(x: end.x - 10, y: end.y - 12), anchor: .trailing)
        }

        drawLabels(&ctx, size: size, n: n, x: x)
    }

    private func drawLabels(_ ctx: inout GraphicsContext, size: CGSize, n: Int, x: (Int) -> CGFloat) {
        for i in 0..<n {
            CavnarChart.text(&ctx, CavnarChart.label(weeks[i].label, size: 10.5), at: CGPoint(x: x(i), y: size.height - 12))
        }
    }
}
