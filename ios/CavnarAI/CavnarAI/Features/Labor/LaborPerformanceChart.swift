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
    // The window "By Day" averaged its weekday buckets across — without
    // this, "Mon–Sun" reads as generic when it's really one specific
    // 2-week (or however long) upload window, and an owner has no way to
    // tell which Monday–Sunday the bars actually represent.
    let dateRangeStart: String?
    let dateRangeEnd: String?
    // Whether the bars have ever grown up from zero before — see
    // LaborAnalyticsViewModel.hasPlayedBarIntro for why this lives outside
    // this view instead of as its own @State. A plain value + callback
    // (not a Binding) so the owning view model's setter can also persist
    // the flag to UserDefaults — a raw Binding could only ever flip the
    // in-memory property, which is exactly what let this replay every time
    // a user left Labor entirely and came back (a fresh LaborView means a
    // fresh LaborAnalyticsViewModel, resetting any in-memory-only flag).
    let hasPlayedIntro: Bool
    let onIntroPlayed: () -> Void

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
    // Press-and-hold selection, same pattern as Reviews Analytics' trend
    // chart — a drag with zero minimum distance so a plain touch-down
    // already registers, showing the exact value under the finger.
    @State private var selectedBar: Bar?

    private struct Bar: Identifiable, Equatable {
        let id: String
        let label: String
        let pct: Double
        // Full "6/1–6/14" period for trend bars — the x-axis label alone
        // is just the period's start date, which reads like "this is one
        // specific day" rather than the (often 2-week) aggregate it
        // actually is. nil for By Day bars, which don't have a single
        // underlying date range to show (each is an average across every
        // occurrence of that weekday).
        let rangeText: String?
    }

    private static let dayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    private var bars: [Bar] {
        switch mode {
        case .trend:
            return trend.map { week in
                Bar(id: week.label, label: week.label, pct: week.pct, rangeText: formattedShortRange(week.start, week.end))
            }
        case .byDay:
            return Self.dayOrder.compactMap { day in
                guard let pct = dowSummary[day] else { return nil }
                return Bar(id: day, label: String(day.prefix(3)), pct: pct, rangeText: nil)
            }
        }
    }

    private func formattedShortRange(_ start: String, _ end: String) -> String? {
        guard let startDate = Self.isoFormatter.date(from: start),
              let endDate = Self.isoFormatter.date(from: end) else { return nil }
        return "\(Self.shortDisplayFormatter.string(from: startDate))–\(Self.shortDisplayFormatter.string(from: endDate))"
    }

    // "By Day" flags a single day for being *close* to target as a real
    // near-term warning (that weekday recurs — it's a heads-up for next
    // time). A trend bar already happened; there's no "next time" to warn
    // about, just over or under. Sharing the 3-tier amber-buffer logic
    // there disagreed with the hero card's own on-track/over-target read
    // of the same underlying number (e.g. 22.3% against a 23% target reads
    // "On track" — green — on the hero pill, but amber on a 3-tier trend
    // bar), so trend gets a plain 2-tier scheme matching that exact
    // on-track definition instead.
    private func barColor(_ pct: Double) -> Color {
        switch mode {
        case .trend:
            return pct > target ? .cavnarRed : .cavnarGreen
        case .byDay:
            if pct > target { return .cavnarRed }
            if pct > target - 3 { return .cavnarAmber }
            return .cavnarGreen
        }
    }

    /// Same bright-base/dark-shadow-top fade as Food Cost's trend chart
    /// (the confirmed mockup style), applied on top of whichever of the
    /// red/amber/green tiers barColor already picked — the on-track/over-
    /// target semantics are unchanged, only the fill goes from flat to gradient.
    private func barGradient(_ pct: Double) -> LinearGradient {
        let hue = barColor(pct)
        return LinearGradient(
            colors: [hue.opacity(0.9), hue.opacity(0.35)],
            startPoint: .bottom, endPoint: .top
        )
    }

    private var dateRangeText: String? {
        switch mode {
        case .byDay:
            guard let start = dateRangeStart, let end = dateRangeEnd,
                  let startDate = Self.isoFormatter.date(from: start),
                  let endDate = Self.isoFormatter.date(from: end) else { return nil }
            return "\(Self.displayFormatter.string(from: startDate)) – \(Self.displayFormatter.string(from: endDate))"
        case .trend:
            guard !trend.isEmpty else { return nil }
            let starts = trend.compactMap { Self.isoFormatter.date(from: $0.start) }
            let ends = trend.compactMap { Self.isoFormatter.date(from: $0.end) }
            guard let earliest = starts.min(), let latest = ends.max() else { return nil }
            return "\(Self.displayFormatter.string(from: earliest)) – \(Self.displayFormatter.string(from: latest))"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("LABOR % PERFORMANCE")
                .font(.cavnarBody(12, weight: 700))
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
                if let dateRangeText {
                    Text(dateRangeText)
                        .font(.cavnarBody(11.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                Chart {
                    ForEach(bars) { bar in
                        BarMark(x: .value("Period", bar.label), y: .value("Labor %", barsVisible ? bar.pct : 0))
                            .foregroundStyle(barGradient(bar.pct))
                            .cornerRadius(3)
                    }
                    RuleMark(y: .value("Target", target))
                        .foregroundStyle(Color.cavnarInk.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            // A dark near-black pill (the previous attempt)
                            // still read as washed-out — it's too close in
                            // tone to the page's own near-black background
                            // to visually pop as its own shape, even at
                            // near-full opacity. Inverted to a solid bright
                            // cream chip with dark text instead: unmissable
                            // against any bar color underneath, and against
                            // the page itself, with zero opacity blending
                            // either way.
                            Text("\(Int(target))% target")
                                .font(.cavnarBody(11, weight: 700))
                                .foregroundStyle(Color.black)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(Color.cavnarInk)
                                .clipShape(Capsule())
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
                .chartOverlay { proxy in
                    GeometryReader { geo in
                        barSelectionOverlay(proxy: proxy, geo: geo)
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
                        onIntroPlayed()
                    }
                }
            }

            legend
        }
        .onChange(of: mode) { _, _ in selectedBar = nil }
    }

    @ViewBuilder
    private var legend: some View {
        switch mode {
        case .byDay:
            HStack(spacing: 14) {
                legendDot(.cavnarRed, "Over \(Int(target))%")
                legendDot(.cavnarAmber, "\(Int(target - 3))–\(Int(target))%")
                legendDot(.cavnarGreen, "Under \(Int(target - 3))%")
            }
            .font(.cavnarBody(11))
            .foregroundStyle(Color.cavnarInk3)
        case .trend:
            HStack(spacing: 14) {
                legendDot(.cavnarGreen, "At or under target")
                legendDot(.cavnarRed, "Over \(Int(target))% target")
            }
            .font(.cavnarBody(11))
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
                        .font(.cavnarBody(12, weight: 700))
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

    /// Press-and-hold-to-inspect, matching Reviews Analytics' trend chart —
    /// a drag with zero minimum distance so a plain touch-down already
    /// counts, picking the nearest bar under the finger and showing its
    /// exact percentage; lifting the finger dismisses it.
    @ViewBuilder
    private func barSelectionOverlay(proxy: ChartProxy, geo: GeometryProxy) -> some View {
        if let selectedBar, let plotFrame = proxy.plotFrame {
            let frame = geo[plotFrame]
            if let xPosition = proxy.position(forX: selectedBar.label) {
                let clampedX = min(max(xPosition + frame.origin.x, frame.minX + 34), frame.maxX - 34)
                barTooltip(selectedBar)
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
                        if let bar = bars.first(where: { $0.label == label }), bar.id != selectedBar?.id {
                            Haptic.selection()
                        }
                        selectedBar = bars.first { $0.label == label }
                    }
                    .onEnded { _ in selectedBar = nil }
            )
    }

    private func barTooltip(_ bar: Bar) -> some View {
        VStack(spacing: 2) {
            Text(bar.rangeText ?? bar.label)
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            Text(String(format: "%.1f%%", bar.pct))
                .font(.cavnarNumber(14, weight: 700))
                .foregroundStyle(barColor(bar.pct))
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

    private static let isoFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "UTC")
        return f
    }()

    private static let displayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "MMM d, yyyy"
        return f
    }()

    private static let shortDisplayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "M/d"
        return f
    }()
}
