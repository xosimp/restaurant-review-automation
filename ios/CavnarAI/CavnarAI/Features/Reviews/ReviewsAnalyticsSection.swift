import SwiftUI
import Charts

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    @State private var selectedWeek: SentimentWeek?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if viewModel.isLoading && viewModel.performance == nil {
                    ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
                } else {
                    if let insight = viewModel.insight {
                        insightCard(insight)
                    }
                    if !viewModel.platforms.isEmpty {
                        platformSection
                    }
                    if let performance = viewModel.performance {
                        performanceCard(performance)
                    }
                    if !viewModel.heatmap.isEmpty {
                        topicGrid
                    }
                    if !viewModel.sentimentWeeks.isEmpty {
                        trendChartCard
                    }
                }
            }
            .padding(20)
        }
    }

    // MARK: - AI insight (emoji lines → branded icon rows)

    // Reviews' AI insight is a distinct web convention from Labor/Food Cost/
    // Marketing's structured AIInsight — a handful of emoji-prefixed lines
    // (see client_api.py's review-insight prompt) rather than intro/
    // recommendations/forecast fields. Parsed here into icon rows instead of
    // rendering the raw emoji characters, matching the app's branded-icon
    // convention everywhere else.
    private static let insightIcons: [(prefix: String, systemImage: String)] = [
        ("📊", "chart.bar.fill"),
        ("⚠️", "exclamationmark.triangle.fill"),
        ("✅", "checkmark.circle.fill"),
        ("🔮", "sparkles"),
    ]

    // No card/gradient container here on purpose — the page already has
    // plenty of boxed sections below. These four lines float directly on
    // the module background; a dark lift-shadow plus a faint ember glow
    // (the same two-shadow technique as .cavnarNumberGlow) keeps the icon
    // and text reading as raised/lit rather than flat against the wash,
    // with hairline dividers standing in for a container edge.
    private func insightCard(_ insight: String) -> some View {
        let lines = insightLines(insight)
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: line.icon)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 20, alignment: .center)
                        .shadow(color: .cavnarEmber.opacity(0.55), radius: 5, x: 0, y: 0)
                    Text(line.text)
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineSpacing(4)
                        .shadow(color: .black.opacity(0.45), radius: 3, x: 0, y: 1)
                }
                .padding(.vertical, 12)
                if index < lines.count - 1 {
                    Rectangle()
                        .fill(Color.cavnarPaper3.opacity(0.25))
                        .frame(height: 1)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func insightLines(_ raw: String) -> [(icon: String, text: String)] {
        raw.split(separator: "\n").compactMap { rawLine in
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { return nil }
            for entry in Self.insightIcons where line.hasPrefix(entry.prefix) {
                let text = line.dropFirst(entry.prefix.count).trimmingCharacters(in: .whitespaces)
                return (entry.systemImage, text)
            }
            return ("circle.fill", line)
        }
    }

    // MARK: - Platform breakdown

    private var platformSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("By platform")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 12) {
                ForEach(viewModel.platforms) { platform in
                    platformCard(platform)
                }
            }
        }
    }

    private func platformCard(_ platform: PlatformBreakdown) -> some View {
        let ratings = viewModel.platforms.map(\.avgRating)
        let isStrongest = viewModel.platforms.count > 1 && platform.avgRating == ratings.max()
        let isWeakest = viewModel.platforms.count > 1 && platform.avgRating == ratings.min() && !isStrongest

        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(platform.platform.uppercased())
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk2)
                Spacer()
                if isStrongest {
                    Text("Strongest")
                        .font(.cavnarBody(9, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                } else if isWeakest {
                    Text("Needs attention")
                        .font(.cavnarBody(9, weight: 700))
                        .foregroundStyle(Color.cavnarRed)
                }
            }

            Text("\(platform.total) reviews")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)

            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(String(format: "%.1f", platform.avgRating))
                    .font(.cavnarNumber(26, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarNumberGlow()
                Image(systemName: "star.fill")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.cavnarAmber)
            }

            HStack(spacing: 12) {
                Label("\(platform.positive)", systemImage: "arrow.up")
                    .font(.cavnarNumber(11, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Label("\(platform.negative)", systemImage: "arrow.down")
                    .font(.cavnarNumber(11, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarGlossyCard()
    }

    // MARK: - Response performance

    private func performanceCard(_ performance: ResponsePerformance) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            (Text("Response performance — last ")
                + Text("\(performance.days)").font(.cavnarNumber(11, weight: 700))
                + Text("d"))
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack {
                statTile("Approved as-is", performance.approvedAsIs)
                statTile("Edited", performance.edited)
                statTile("Regenerated", performance.regenerated)
            }
        }
        .cavnarGlossyCard()
    }

    private func statTile(_ label: String, _ value: Int) -> some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            Text(label)
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Topic sentiment grid

    private var topicGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Topic sentiment")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(viewModel.heatmap.filter { $0.count > 0 }) { entry in
                    NavigationLink {
                        TopicReviewsView(category: entry.category, label: entry.label)
                    } label: {
                        topicCard(entry)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func topicTone(_ entry: TopicHeatmapEntry) -> CavnarTone {
        if entry.pctNegative > 20 { return .bad }
        if entry.pctPositive >= 70 { return .good }
        return .warning
    }

    private func topicCard(_ entry: TopicHeatmapEntry) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                Text(entry.label)
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                trendIcon(entry.trend)
                Spacer()
            }
            Text("\(entry.count)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            StatProgressBar(progress: Double(entry.pctPositive) / 100, tone: topicTone(entry))
            HStack {
                Text("\(entry.pctPositive)% pos")
                    .font(.cavnarNumber(9, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Spacer()
                Text("\(entry.pctNegative)% neg")
                    .font(.cavnarNumber(9, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .padding(12)
        .background(Color.cavnarPaper2.opacity(0.6))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    @ViewBuilder
    private func trendIcon(_ trend: String) -> some View {
        switch trend {
        case "up": Image(systemName: "arrow.up.right").font(.system(size: 9)).foregroundStyle(Color.cavnarRed)
        case "down": Image(systemName: "arrow.down.right").font(.system(size: 9)).foregroundStyle(Color.cavnarGreen)
        default: EmptyView()
        }
    }

    // MARK: - Sentiment trend chart

    private var overallAvgRating: Double {
        let totalReviews = viewModel.sentimentWeeks.reduce(0) { $0 + $1.total }
        guard totalReviews > 0 else { return 0 }
        let weightedSum = viewModel.sentimentWeeks.reduce(0.0) { $0 + $1.avgRating * Double($1.total) }
        return weightedSum / Double(totalReviews)
    }

    /// One continuous bar per week (bottom→top: positive, neutral,
    /// negative) rather than three stacked BarMarks — a single gradient can
    /// blend smoothly across each segment boundary, where three flat-color
    /// marks would always meet at a hard edge. `blend` is the half-width of
    /// that soft transition band; stops are clamped to stay non-decreasing
    /// so a zero-count segment (e.g. no negative reviews that week) just
    /// collapses its band instead of producing an invalid gradient.
    private func barGradient(for week: SentimentWeek) -> LinearGradient {
        let total = Double(week.total)
        guard total > 0 else {
            return LinearGradient(colors: [Color.cavnarPaper3], startPoint: .bottom, endPoint: .top)
        }
        let blend = 0.06
        let boundary1 = Double(week.positive) / total
        let boundary2 = Double(week.positive + week.neutral) / total

        var stops: [Gradient.Stop] = [
            .init(color: .cavnarGreen, location: 0),
            .init(color: .cavnarGreen, location: max(0, boundary1 - blend)),
            .init(color: .cavnarAmber, location: min(1, boundary1 + blend)),
            .init(color: .cavnarAmber, location: max(0, boundary2 - blend)),
            .init(color: .cavnarRed, location: min(1, boundary2 + blend)),
            .init(color: .cavnarRed, location: 1),
        ]
        for i in 1..<stops.count where stops[i].location < stops[i - 1].location {
            stops[i].location = stops[i - 1].location
        }
        return LinearGradient(gradient: Gradient(stops: stops), startPoint: .bottom, endPoint: .top)
    }

    @ChartContentBuilder
    private func trendBars() -> some ChartContent {
        ForEach(viewModel.sentimentWeeks) { week in
            BarMark(x: .value("Week", week.label), y: .value("Reviews", week.total))
                .foregroundStyle(barGradient(for: week))
                .cornerRadius(3)
        }
    }

    private var trendChartCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Sentiment trend — last 8 weeks")
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                (Text("Avg ") + Text(String(format: "%.1f★", overallAvgRating)).font(.cavnarNumber(11, weight: 700)))
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
            }

            // Chart marks aren't Views, so they can't take .shadow() — a
            // blurred, non-interactive duplicate of the same bars sitting
            // directly behind the crisp chart gives each bar its own soft
            // glow silhouette instead of one flat halo behind the whole
            // plot rectangle.
            ZStack {
                Chart { trendBars() }
                    .chartXAxis(.hidden)
                    .chartYAxis(.hidden)
                    .opacity(0.6)
                    .blur(radius: 5)
                    .allowsHitTesting(false)

                Chart { trendBars() }
                    .chartLegend(.hidden)
                    .chartYAxis {
                        AxisMarks(position: .leading) { value in
                            AxisGridLine().foregroundStyle(Color.cavnarPaper3.opacity(0.4))
                            AxisValueLabel {
                                if let v = value.as(Int.self) {
                                    Text("\(v)").font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                        }
                    }
                    .chartXAxis {
                        AxisMarks { _ in
                            AxisValueLabel()
                                .font(.cavnarBody(9))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .chartOverlay { proxy in
                        GeometryReader { geo in
                            weekSelectionOverlay(proxy: proxy, geo: geo)
                        }
                    }
            }
            .frame(height: 190)

            HStack(spacing: 16) {
                legendItem(color: .cavnarGreen, label: "Positive")
                legendItem(color: .cavnarAmber, label: "Neutral")
                legendItem(color: .cavnarRed, label: "Negative")
            }
        }
        .cavnarCard()
    }

    /// Press-and-hold-to-inspect, the touch equivalent of a desktop hover
    /// tooltip: drag (with zero minimum distance, so a plain touch-down
    /// already counts) picks the nearest week under the finger and shows
    /// its exact pos/neutral/negative split; lifting the finger dismisses it.
    @ViewBuilder
    private func weekSelectionOverlay(proxy: ChartProxy, geo: GeometryProxy) -> some View {
        if let selectedWeek, let plotFrame = proxy.plotFrame {
            let frame = geo[plotFrame]
            if let xPosition = proxy.position(forX: selectedWeek.label) {
                let clampedX = min(max(xPosition + frame.origin.x, frame.minX + 60), frame.maxX - 60)
                weekTooltip(selectedWeek)
                    .position(x: clampedX, y: frame.minY + 30)
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
                        selectedWeek = viewModel.sentimentWeeks.first { $0.label == label }
                    }
                    .onEnded { _ in selectedWeek = nil }
            )
    }

    private func weekTooltip(_ week: SentimentWeek) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(week.label)
                .font(.cavnarBody(10, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 10) {
                tooltipStat(week.positive, color: .cavnarGreen)
                tooltipStat(week.neutral, color: .cavnarAmber)
                tooltipStat(week.negative, color: .cavnarRed)
            }
        }
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.95))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .shadow(color: .black.opacity(0.4), radius: 8, x: 0, y: 4)
        .fixedSize()
    }

    private func tooltipStat(_ value: Int, color: Color) -> some View {
        HStack(spacing: 3) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text("\(value)").font(.cavnarNumber(11, weight: 700)).foregroundStyle(Color.cavnarInk)
        }
    }

    private func legendItem(color: Color, label: String) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(label).font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
        }
    }
}
