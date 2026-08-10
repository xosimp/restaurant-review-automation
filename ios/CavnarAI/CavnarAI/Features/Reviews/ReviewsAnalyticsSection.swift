import SwiftUI
import Charts

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

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

    private func insightCard(_ insight: String) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            ForEach(Array(insightLines(insight).enumerated()), id: \.offset) { _, line in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: line.icon)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 18, alignment: .center)
                    Text(line.text)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk)
                        .lineSpacing(4)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarGlassCard()
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
                    topicCard(entry)
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

    // Same visual language as the web dashboard's own Labor % chart —
    // gradient-toned marks, a clean axis, and a color legend — applied here
    // as a 100%-composition stacked bar per week (positive/neutral/negative
    // review counts) rather than a value-vs-target bar, since that's what
    // this data actually is. Multiple BarMarks sharing the same x category,
    // split out by .foregroundStyle(by:), is Swift Charts' standard stacked-
    // bar pattern — it stacks automatically, no manual layout math.
    private var overallAvgRating: Double {
        let totalReviews = viewModel.sentimentWeeks.reduce(0) { $0 + $1.total }
        guard totalReviews > 0 else { return 0 }
        let weightedSum = viewModel.sentimentWeeks.reduce(0.0) { $0 + $1.avgRating * Double($1.total) }
        return weightedSum / Double(totalReviews)
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

            Chart {
                ForEach(viewModel.sentimentWeeks) { week in
                    BarMark(x: .value("Week", week.label), y: .value("Reviews", week.positive))
                        .foregroundStyle(by: .value("Sentiment", "Positive"))
                    BarMark(x: .value("Week", week.label), y: .value("Reviews", week.neutral))
                        .foregroundStyle(by: .value("Sentiment", "Neutral"))
                    BarMark(x: .value("Week", week.label), y: .value("Reviews", week.negative))
                        .foregroundStyle(by: .value("Sentiment", "Negative"))
                }
            }
            .chartForegroundStyleScale([
                "Positive": Color.cavnarGreen,
                "Neutral": Color.cavnarAmber,
                "Negative": Color.cavnarRed,
            ])
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
            .frame(height: 190)

            HStack(spacing: 16) {
                legendItem(color: .cavnarGreen, label: "Positive")
                legendItem(color: .cavnarAmber, label: "Neutral")
                legendItem(color: .cavnarRed, label: "Negative")
            }
        }
        .cavnarCard()
    }

    private func legendItem(color: Color, label: String) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(label).font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
        }
    }
}
