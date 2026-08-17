import SwiftUI
import Charts

/// Real bar chart for labor % over time — replaces the plain text list the
/// Analytics tab used to show. Two views toggled by pills matching the
/// exact style of Home's ValueChartCard range pills (1M/3M/6M/...): "8-Week
/// Trend" (one bar per week) and "By Day" (one bar per weekday, averaged
/// across all occurrences) — matching the web dashboard's own Labor %
/// Performance chart and its two tabs.
///
/// Deliberately unboxed (no .cavnarCard()/.cavnarGlassCard()) and using the
/// same uppercase-eyebrow-label treatment as ValueChartCard, so this reads
/// as the same chart language as Home's hero stat instead of another boxed
/// tile — the Analytics tab had become an unbroken column of bordered
/// cards.
struct LaborPerformanceChart: View {
    let trend: [LaborTrendWeek]
    let dowSummary: [String: Double]
    let target: Double
    // Whether the bars have ever grown up from zero before — see
    // LaborAnalyticsViewModel.hasPlayedBarIntro for why this lives outside
    // this view instead of as its own @State.
    @Binding var hasPlayedIntro: Bool

    private enum ChartMode: String, CaseIterable, Identifiable, Hashable {
        case trend = "8-Week Trend"
        case byDay = "By Day"
        var id: String { rawValue }
    }

    @State private var mode: ChartMode = .trend
    // Drives the bars' own height, independent of `bars`' real values —
    // starts at false only on this view's very first appearance ever
    // (see hasPlayedIntro); every appearance after that snaps straight to
    // true with no animation, so switching back to this tab doesn't replay
    // the reveal. Mode switches (trend ↔ by day) after the first reveal
    // just animate the real value change, same as any other Chart data
    // update — never back through zero.
    @State private var barsVisible = false

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
        VStack(alignment: .leading, spacing: 12) {
            Text("LABOR % PERFORMANCE")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.5)
                .foregroundStyle(Color.cavnarEmber2)

            modePills

            if bars.isEmpty {
                Text("Not enough data yet")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 36)
            } else {
                Chart {
                    ForEach(bars) { bar in
                        BarMark(x: .value("Period", bar.label), y: .value("Labor %", barsVisible ? bar.pct : 0))
                            .foregroundStyle(barColor(bar.pct))
                            .cornerRadius(3)
                    }
                    RuleMark(y: .value("Target", target))
                        .foregroundStyle(Color.cavnarInk.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("\(Int(target))% target")
                                .font(.cavnarBody(9, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
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
                .animation(.easeOut(duration: 0.4), value: mode)
                .onAppear {
                    if hasPlayedIntro {
                        barsVisible = true
                    } else {
                        withAnimation(.easeOut(duration: 0.75)) {
                            barsVisible = true
                        }
                        hasPlayedIntro = true
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
    }

    private var modePills: some View {
        HStack(spacing: 6) {
            ForEach(ChartMode.allCases) { m in
                Button {
                    Haptic.light()
                    mode = m
                } label: {
                    Text(m.rawValue)
                        .font(.cavnarBody(11, weight: 700))
                        .foregroundStyle(m == mode ? Color.cavnarEmber2 : Color.cavnarInk3)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(
                            Capsule().fill(m == mode ? Color.cavnarEmber.opacity(0.16) : .clear)
                        )
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func legendDot(_ color: Color, _ label: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 8, height: 8)
            Text(label)
        }
    }
}
