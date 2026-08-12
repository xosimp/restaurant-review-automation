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
                    // fabricated one would be lying with a chart.
                    Text("Tracking starts today")
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .font(.cavnarBody(12, weight: 600))
            .padding(.bottom, 10)

            SparklineCanvas(history: history)
                .frame(height: 120)
        }
    }

    private var deltaInfo: (isPositive: Bool, text: String)? {
        guard history.count >= 2, let first = history.first?.value else { return nil }
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
}

/// The sparkline itself — a progressive left-to-right line reveal on first
/// appearance (mirrors the mockup's requestAnimationFrame draw-in), then
/// settles into a single static Canvas draw so it isn't re-rendering every
/// frame forever afterward.
private struct SparklineCanvas: View {
    let history: [ValueSnapshot]

    @State private var startDate = Date()
    @State private var isRevealed = false

    var body: some View {
        Group {
            if history.count < 2 {
                buildingTrendPlaceholder
            } else if isRevealed {
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

    private var buildingTrendPlaceholder: some View {
        VStack {
            Spacer()
            Text("Trend builds as you use the app")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private func draw(context: GraphicsContext, size: CGSize, progress: Double) {
        let values = history.map { Double($0.value) }
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
    }
}
