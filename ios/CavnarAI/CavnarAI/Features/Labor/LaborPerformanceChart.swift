import SwiftUI
import Charts

/// Real bar chart for labor % over time — replaces the plain text list the
/// Analytics tab used to show. Two views toggled by the same branded
/// segmented control used for the Overview/Analytics switch: "8-Week
/// Trend" (one bar per week) and "By Day" (one bar per weekday, averaged
/// across all occurrences) — matching the web dashboard's own Labor %
/// Performance chart and its two tabs.
struct LaborPerformanceChart: View {
    let trend: [LaborTrendWeek]
    let dowSummary: [String: Double]
    let target: Double

    private enum ChartMode: String, CaseIterable, Identifiable, Hashable {
        case trend = "8-Week Trend"
        case byDay = "By Day"
        var id: String { rawValue }
    }

    @State private var mode: ChartMode = .trend

    private struct Bar: Identifiable {
        let id: String
        let label: String
        let pct: Double
    }

    private static let dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    private var bars: [Bar] {
        switch mode {
        case .trend:
            return trend.map { Bar(id: $0.label, label: $0.label, pct: $0.pct) }
        case .byDay:
            return Self.dayOrder.compactMap { day in
                guard let pct = dowSummary[day] else { return nil }
                return Bar(id: day, label: String(day.prefix(3)), pct: pct)
            }
        }
    }

    private func barColor(_ pct: Double) -> Color {
        if pct > target { return .cavnarRed }
        if pct > target - 3 { return .cavnarAmber }
        return .cavnarGreen
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Labor % performance")
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                CavnarSegmentedControl(selection: $mode, options: ChartMode.allCases) { $0.rawValue }
                    .frame(width: 190)
            }

            if bars.isEmpty {
                Text("Not enough data yet")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 36)
            } else {
                Chart {
                    ForEach(bars) { bar in
                        BarMark(x: .value("Period", bar.label), y: .value("Labor %", bar.pct))
                            .foregroundStyle(barColor(bar.pct))
                            .cornerRadius(3)
                    }
                    RuleMark(y: .value("Target", target))
                        .foregroundStyle(Color.cavnarEmber.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("\(Int(target))% target")
                                .font(.cavnarBody(9, weight: 700))
                                .foregroundStyle(Color.cavnarEmber)
                        }
                }
                .frame(height: 180)
                .chartYAxis {
                    AxisMarks(position: .leading) { _ in
                        AxisGridLine().foregroundStyle(Color.cavnarPaper3.opacity(0.4))
                        AxisValueLabel().font(.system(size: 9)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .chartXAxis {
                    AxisMarks { _ in
                        AxisValueLabel().font(.system(size: 9)).foregroundStyle(Color.cavnarInk3)
                    }
                }
            }

            HStack(spacing: 14) {
                legendDot(.cavnarRed, "Over \(Int(target))%")
                legendDot(.cavnarAmber, "\(Int(target - 3))–\(Int(target))%")
                legendDot(.cavnarGreen, "Under \(Int(target - 3))%")
            }
            .font(.cavnarBody(9))
            .foregroundStyle(Color.cavnarInk3)
        }
        .cavnarGlassCard()
    }

    private func legendDot(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(label)
        }
    }
}
