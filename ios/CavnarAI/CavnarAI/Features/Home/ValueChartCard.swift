import SwiftUI

/// Robinhood/finance-app-style hero stat: eyebrow label, big Space Grotesk
/// number, a colored delta line, and a Canvas-drawn sparkline with a glowing
/// endpoint — matches the approved mockup 1:1, in the app's ember palette
/// instead of gold. Deliberately unboxed (no .cavnarCard() wrapper), sitting
/// flush on the page like the reference screenshot rather than looking like
/// another KPI tile.
struct ValueChartCard: View {
    let totalValue: Int
    let history: [ValueSnapshot]

    // Real trend needs both enough points AND actual variation between
    // them — two identical snapshots (nothing changed day over day, common
    // for a brand-new restaurant) technically satisfy "count >= 2" but plot
    // as a flat line at the bottom of the canvas with none of the fill/
    // glow/curve that makes the chart read as a chart. Either case falls
    // back to the same illustrative curve below.
    private var hasRealTrend: Bool {
        guard history.count >= 2 else { return false }
        let values = history.map(\.value)
        return (values.max() ?? 0) != (values.min() ?? 0)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("TOTAL VALUE DELIVERED")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.5)
                .foregroundStyle(Color.cavnarEmber2)

            Text(Self.currencyText(totalValue))
                .font(.cavnarNumber(38, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow(.cavnarEmber)

            Group {
                if let delta = deltaInfo {
                    HStack(spacing: 4) {
                        Text(delta.isPositive ? "▲" : "▼")
                        Text(delta.text)
                    }
                    .foregroundStyle(delta.isPositive ? Color.cavnarGreen : Color.cavnarRed)
                } else {
                    // No baseline yet to compare against (first day using the
                    // app) — an empty delta line would look broken, and a
                    // fabricated one would be lying about this restaurant's
                    // own numbers.
                    Text("Tracking starts today")
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .font(.cavnarBody(12, weight: 600))
            .padding(.bottom, 10)

            // With no real trend yet, the chart still draws — just from a
            // representative sample curve with its own illustrative axis
            // numbers, instead of sitting blank or (worse) a flat line with
            // no context — so a brand-new restaurant sees what the chart
            // becomes rather than an empty box.
            SparklineCanvas(
                values: hasRealTrend ? history.map { Double($0.value) } : Self.sampleTrend,
                axisLabels: hasRealTrend ? nil : Self.sampleAxisLabels
            )
            .frame(height: 120)

            if !hasRealTrend {
                HStack {
                    Text(Self.sampleAxisLabels.xLeft)
                    Spacer()
                    Text(Self.sampleAxisLabels.xRight)
                }
                .font(.cavnarBody(9, weight: 600))
                .tracking(0.5)
                .foregroundStyle(Color.cavnarInk3.opacity(0.7))
                .padding(.top, 2)

                Text("Example — shows the trend a typical restaurant sees over time")
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                    .padding(.top, 4)
            }
        }
    }

    private var deltaInfo: (isPositive: Bool, text: String)? {
        guard hasRealTrend, let first = history.first?.value else { return nil }
        let delta = totalValue - first
        let isPositive = delta >= 0
        let dollarText = Self.currencyText(abs(delta))
        if first != 0 {
            let pct = abs(Double(delta) / Double(first) * 100)
            return (isPositive, "\(dollarText) (\(String(format: "%.1f", pct))%) this month")
        }
        return (isPositive, "\(dollarText) this month")
    }

    private static func currencyText(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        let digits = formatter.string(from: NSNumber(value: value)) ?? "\(value)"
        return "$\(digits)"
    }

    // A mostly-up, occasional-dip cumulative curve — the shape real usage
    // tends to produce — normalized 0-1 so SparklineCanvas can plot it the
    // same way it plots real dollar history. Purely illustrative: never
    // shown next to a real dollar figure implying it IS this restaurant's
    // data (see the "Example" caption above).
    private static let sampleTrend: [Double] = [
        0.10, 0.11, 0.11, 0.22, 0.20, 0.30, 0.31, 0.30, 0.42, 0.48,
        0.47, 0.55, 0.60, 0.58, 0.66, 0.70, 0.74, 0.80, 0.86, 0.90, 1.0,
    ]

    // Representative low/high dollar figures for a typical restaurant's
    // Total Value Delivered over a 30-day window — gives the illustrative
    // curve real axis numbers instead of an unlabeled line, without
    // implying either figure is this restaurant's own data (see the
    // "Example" caption, always shown alongside).
    private static let sampleAxisLabels = SparklineCanvas.AxisLabels(
        yTop: "$4.8K", yBottom: "$800", xLeft: "30 DAYS AGO", xRight: "TODAY"
    )
}

/// The sparkline itself — a progressive left-to-right line reveal on first
/// appearance (mirrors the mockup's requestAnimationFrame draw-in), then
/// settles into a single static Canvas draw so it isn't re-rendering every
/// frame forever afterward. Takes plain plot values rather than
/// ValueSnapshot directly, so the same drawing code serves both real
/// history and the synthetic sample curve.
private struct SparklineCanvas: View {
    struct AxisLabels {
        let yTop: String
        let yBottom: String
        let xLeft: String
        let xRight: String
    }

    let values: [Double]
    var axisLabels: AxisLabels? = nil

    @State private var startDate = Date()
    @State private var isRevealed = false

    var body: some View {
        Group {
            if isRevealed {
                Canvas { context, size in draw(context: context, size: size, progress: 1) }
            } else {
                TimelineView(.animation) { timeline in
                    Canvas { context, size in
                        let progress = min(1, timeline.date.timeIntervalSince(startDate) / 0.9)
                        draw(context: context, size: size, progress: progress)
                    }
                }
                .task {
                    try? await Task.sleep(for: .seconds(0.95))
                    isRevealed = true
                }
            }
        }
    }

    private func draw(context: GraphicsContext, size: CGSize, progress: Double) {
        guard let minV = values.min(), let maxV = values.max() else { return }
        let range = max(maxV - minV, 1)
        let pad: CGFloat = 6
        let stepX = values.count > 1 ? (size.width - pad * 2) / CGFloat(values.count - 1) : 0

        let coords: [CGPoint] = values.enumerated().map { index, v in
            let normalized = (v - minV) / range
            let x = pad + CGFloat(index) * stepX
            let y = pad + (1 - CGFloat(normalized)) * (size.height - pad * 2 - 14)
            return CGPoint(x: x, y: y)
        }

        let visibleCount = max(2, Int(Double(coords.count) * progress))
        let visible = Array(coords.prefix(visibleCount))
        guard visible.count >= 2, let last = visible.last, let first = visible.first else { return }

        var fillPath = Path()
        fillPath.move(to: CGPoint(x: first.x, y: size.height))
        visible.forEach { fillPath.addLine(to: $0) }
        fillPath.addLine(to: CGPoint(x: last.x, y: size.height))
        fillPath.closeSubpath()
        context.fill(
            fillPath,
            with: .linearGradient(
                Gradient(colors: [Color.cavnarEmber.opacity(0.28), Color.cavnarEmber.opacity(0)]),
                startPoint: CGPoint(x: 0, y: 0),
                endPoint: CGPoint(x: 0, y: size.height)
            )
        )

        var linePath = Path()
        linePath.move(to: first)
        visible.dropFirst().forEach { linePath.addLine(to: $0) }
        context.stroke(linePath, with: .color(Color.cavnarEmber2), lineWidth: 2)

        let glowRect = CGRect(x: last.x - 16, y: last.y - 16, width: 32, height: 32)
        context.fill(
            Path(ellipseIn: glowRect),
            with: .radialGradient(
                Gradient(colors: [Color.cavnarEmber2.opacity(0.9), Color.cavnarEmber2.opacity(0)]),
                center: last, startRadius: 0, endRadius: 16
            )
        )
        let dotRect = CGRect(x: last.x - 3, y: last.y - 3, width: 6, height: 6)
        context.fill(Path(ellipseIn: dotRect), with: .color(Color.cavnarInk))

        // Only the Y-axis (dollar) labels draw inside the canvas, in its
        // top-left/bottom-left corners — the X-axis (date-range) labels
        // render as a plain SwiftUI row below the chart instead (see
        // ValueChartCard.body) since bottom-left is already spoken for here.
        if let axisLabels, progress >= 1 {
            let labelFont = Font.cavnarBody(9, weight: 600)
            let labelColor = Color.cavnarInk3.opacity(0.8)
            context.draw(
                Text(axisLabels.yTop).font(labelFont).foregroundStyle(labelColor),
                at: CGPoint(x: 2, y: 2), anchor: .topLeading
            )
            context.draw(
                Text(axisLabels.yBottom).font(labelFont).foregroundStyle(labelColor),
                at: CGPoint(x: 2, y: size.height - 2), anchor: .bottomLeading
            )
        }
    }
}
