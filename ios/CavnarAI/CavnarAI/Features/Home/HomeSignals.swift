import SwiftUI

/// The trend behind each pulse chip, inside Home's Results (web
/// `renderSignals`, DS §11b step 7; parity audit #1): the weekly rating,
/// labor by weekday against target, and last week's waste by item — each
/// only when that module sent live data. The chips in the header are the
/// at-a-glance row; these are the "why" behind them. Tap a tile for its
/// module. Drawn with the house chart kit (CavnarCharts): an ember glow
/// line with its endpoint lit, bars with the target as a dashed rule.
struct HomeSignals: View {
    let charts: HomeCharts
    var onOpenModule: (String) -> Void

    var body: some View {
        if !charts.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                HomeSectionHeader(kicker: "Signals", title: "The trend behind each number")
                if charts.hasRatingTrend {
                    tile("reviews", kicker: "Rating \u{00B7} weekly",
                         value: String(format: "%.1f\u{2605}", charts.rating.last?.avg ?? 0),
                         caption: "weekly average \u{00B7} \(charts.rating.count) weeks") {
                        HomeSignalLine(values: charts.rating.map(\.avg))
                    }
                }
                if !charts.laborDays.isEmpty {
                    tile("labor", kicker: "Labor \u{00B7} by day",
                         value: nil,
                         caption: "labor % by weekday" + (charts.laborTarget.map { String(format: " \u{00B7} target %.0f%%", $0) } ?? "")) {
                        HomeSignalBars(values: charts.laborDays.map(\.pct), labels: charts.laborDays.map(\.day),
                                       target: charts.laborTarget, format: { String(format: "%.0f%%", $0) })
                    }
                }
                if !charts.waste.isEmpty {
                    tile("inventory", kicker: "Waste \u{00B7} last week",
                         value: "$" + charts.waste.reduce(0) { $0 + $1.cost }.commaFormatted,
                         caption: "$ wasted by item") {
                        HomeSignalBars(values: charts.waste.map(\.cost),
                                       labels: charts.waste.map { String($0.item.split(separator: " ").first ?? "") },
                                       target: nil, format: { "$" + $0.commaFormatted })
                    }
                }
            }
        }
    }

    private func tile<Chart: View>(_ module: String, kicker: String, value: String?, caption: String,
                                   @ViewBuilder chart: () -> Chart) -> some View {
        Button {
            Haptic.light()
            onOpenModule(module)
        } label: {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    Text(kicker.uppercased())
                        .font(.cavnarBody(11, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                    Spacer(minLength: 8)
                    if let value {
                        Text(value)
                            .font(.cavnarNumber(17, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                    }
                }
                chart()
                Text(caption)
                    .font(.cavnarBody(12, weight: 500))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityHint("Opens the module")
    }
}

/// A glow line through a short series, endpoint lit (web `glowLine`).
private struct HomeSignalLine: View {
    let values: [Double]

    var body: some View {
        CavnarAnimatedCanvas(duration: 1.1, height: 86, replayKey: values) { ctx, size, t, _ in
            guard values.count > 1, let lo = values.min(), let hi = values.max() else { return }
            let floor = lo - 0.3, ceil = max(hi, floor + 0.5)
            let plot = CGRect(x: 8, y: 10, width: size.width - 16, height: size.height - 20)
            CavnarChart.grid(&ctx, plot: plot, lines: 2)
            let pts = values.enumerated().map { i, v in
                CGPoint(x: plot.minX + plot.width * CGFloat(i) / CGFloat(values.count - 1),
                        y: plot.maxY - plot.height * CGFloat((v - floor) / (ceil - floor)))
            }
            let e = CavnarChart.easeInOut(t)
            ctx.drawLayer { layer in
                layer.clip(to: Path(CGRect(x: 0, y: 0, width: size.width * e, height: size.height)))
                CavnarChart.glowStroke(&layer, CavnarChart.smoothPath(pts), color: .cavnarEmber2,
                                       glow: .cavnarEmber, lineWidth: 2.4)
            }
            if t >= 1, let last = pts.last {
                CavnarChart.hotDot(&ctx, at: last, radius: 3.5, halo: 9)
            }
        }
    }
}

/// Bars rising in, labelled underneath, a dashed target rule when there is
/// one (web `bars`). A bar over its target turns amber — a status, never
/// ember (§9).
private struct HomeSignalBars: View {
    let values: [Double]
    let labels: [String]
    let target: Double?
    let format: (Double) -> String

    var body: some View {
        CavnarAnimatedCanvas(duration: 0.9, height: 96, replayKey: values) { ctx, size, t, _ in
            guard !values.isEmpty else { return }
            let top = max(values.max() ?? 0, target ?? 0) * 1.12
            guard top > 0 else { return }
            let plot = CGRect(x: 4, y: 6, width: size.width - 8, height: size.height - 24)
            let slot = plot.width / CGFloat(values.count)
            let barW = min(26, slot * 0.6)
            let e = CavnarChart.easeOut(t)
            for (i, v) in values.enumerated() {
                let h = plot.height * CGFloat(v / top) * CGFloat(e)
                let x = plot.minX + slot * CGFloat(i) + (slot - barW) / 2
                let rect = CGRect(x: x, y: plot.maxY - h, width: barW, height: h)
                let over = target.map { v > $0 } ?? false
                CavnarChart.glowFill(&ctx, CavnarChart.roundedRect(rect, radius: 4),
                                     color: over ? .cavnarAmber : .cavnarEmber2,
                                     glow: over ? .cavnarAmber.opacity(0.4) : .cavnarEmber.opacity(0.4), blur: 6)
                if i < labels.count {
                    CavnarChart.text(&ctx, CavnarChart.label(String(labels[i].prefix(6)), size: 9.5),
                                     at: CGPoint(x: x + barW / 2, y: plot.maxY + 9))
                }
            }
            if let target {
                let y = plot.maxY - plot.height * CGFloat(target / top)
                var p = Path(); p.move(to: CGPoint(x: plot.minX, y: y)); p.addLine(to: CGPoint(x: plot.maxX, y: y))
                ctx.stroke(p, with: .color(Color.cavnarInk3.opacity(0.7)),
                           style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
            }
        }
        .accessibilityLabel(zip(labels, values).map { "\($0) \(format($1))" }.joined(separator: ", "))
    }
}
