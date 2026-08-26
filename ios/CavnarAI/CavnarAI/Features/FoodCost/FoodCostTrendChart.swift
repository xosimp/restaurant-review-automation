import SwiftUI
import Charts

/// Weekly waste-cost trend — same construction as Labor's own
/// LaborPerformanceChart (BarMark + a dashed RuleMark, press-and-hold
/// tooltip via chartOverlay, deliberately unboxed so the Analytics tab
/// doesn't read as another bordered card), simplified to one mode: Food
/// Cost has no "by day" breakdown to toggle to the way Labor's shift data
/// does, so there's no mode-pill row here, just the 8-week trend directly.
struct FoodCostTrendChart: View {
    let weeks: [FoodCostTrendWeek]

    @State private var barsVisible = false
    @State private var selectedWeek: FoodCostTrendWeek?

    private var average: Double {
        guard !weeks.isEmpty else { return 0 }
        return weeks.reduce(0) { $0 + $1.waste } / Double(weeks.count)
    }

    private func barColor(_ waste: Double) -> Color {
        waste > average * 1.15 ? Color.cavnarRed : Color.cavnarEmber
    }

    /// Bright at the base, fading to a dark shadow at the top — matches
    /// the confirmed Food Cost Analytics mockup's bar treatment exactly
    /// (rgba(ember,.9) -> rgba(ember,.4), bottom to top), applied here to
    /// whichever hue barColor already picked for this bar.
    private func barGradient(_ waste: Double) -> LinearGradient {
        let hue = barColor(waste)
        return LinearGradient(
            colors: [hue.opacity(0.9), hue.opacity(0.35)],
            startPoint: .bottom, endPoint: .top
        )
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WASTE — LAST 8 WEEKS")
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)

            if weeks.count < 2 {
                Text("Not enough history yet — check back after a couple more weekly submissions.")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 20)
            } else {
                Chart {
                    ForEach(weeks) { week in
                        BarMark(x: .value("Week", week.label), y: .value("Waste", barsVisible ? week.waste : 0))
                            .foregroundStyle(barGradient(week.waste))
                            .cornerRadius(3)
                    }
                    RuleMark(y: .value("Average", average))
                        .foregroundStyle(Color.cavnarInk.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("avg $\(Int(average))")
                                .font(.cavnarBody(9, weight: 700))
                                .foregroundStyle(Color.black)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(Color.cavnarInk)
                                .clipShape(Capsule())
                        }
                }
                .frame(height: 160)
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
                .chartOverlay { proxy in
                    GeometryReader { geo in
                        weekSelectionOverlay(proxy: proxy, geo: geo)
                    }
                }
                .onAppear {
                    withAnimation(.easeOut(duration: 0.75)) { barsVisible = true }
                }
            }
        }
    }

    /// Press-and-hold-to-inspect, same pattern as LaborPerformanceChart's
    /// own bar selection — a zero-minimum-distance drag so a plain touch
    /// already registers.
    @ViewBuilder
    private func weekSelectionOverlay(proxy: ChartProxy, geo: GeometryProxy) -> some View {
        if let selectedWeek, let plotFrame = proxy.plotFrame {
            let frame = geo[plotFrame]
            if let xPosition = proxy.position(forX: selectedWeek.label) {
                let clampedX = min(max(xPosition + frame.origin.x, frame.minX + 34), frame.maxX - 34)
                weekTooltip(selectedWeek)
                    .position(x: clampedX, y: frame.minY + 20)
            }
        }
        Rectangle()
            .fill(Color.clear)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard let plotFrame = proxy.plotFrame else { return }
                        let originX = geo[plotFrame].origin.x
                        guard let label: String = proxy.value(atX: value.location.x - originX) else { return }
                        if let week = weeks.first(where: { $0.label == label }), week.id != selectedWeek?.id {
                            Haptic.selection()
                        }
                        selectedWeek = weeks.first { $0.label == label }
                    }
                    .onEnded { _ in selectedWeek = nil }
            )
    }

    private func weekTooltip(_ week: FoodCostTrendWeek) -> some View {
        VStack(spacing: 2) {
            Text(week.label)
                .font(.cavnarBody(9, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            Text("$\(Int(week.waste))")
                .font(.cavnarNumber(14, weight: 700))
                .foregroundStyle(barColor(week.waste))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(Color.cavnarPaper2.opacity(0.95))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .shadow(color: .black.opacity(0.4), radius: 8, x: 0, y: 4)
        .fixedSize()
    }
}
