import SwiftUI

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    @State private var selectedTopic: TopicWeekRow?

    // Each section shows its own skeleton while it's individually still in
    // flight rather than gating the whole page behind one spinner — the 5
    // requests in ReviewsAnalyticsViewModel.load() run concurrently but are
    // awaited (and so become non-nil) in a fixed order, so e.g. the AI
    // insight — the slowest, since it's an LLM call, unlike the plain SQL
    // aggregates behind the other sections — used to visibly "pop in" a
    // couple seconds after everything else had already rendered.
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Picker("Period", selection: Binding(
                    get: { viewModel.windowDays },
                    set: { days in
                        guard days != viewModel.windowDays else { return }
                        Haptic.light()
                        Task { await viewModel.setWindow(days) }
                    })) {
                    Text("30 days").tag(30)
                    Text("90 days").tag(90)
                    Text("6 months").tag(180)
                }
                .pickerStyle(.segmented)

                // Raised above the analytics when the backend could not tie
                // every figure in the AI passage back to this restaurant's
                // own data — see ai_guard.verify_figures.
                if !viewModel.unsupportedFigures.isEmpty {
                    CavnarCaveat.unverifiedFigures(viewModel.unsupportedFigures)
                }

                // The AI read itself. It was fetched on every open and then
                // never rendered — so the app paid for a Haiku call, threw
                // the answer away, and could still show the caveat above
                // referring to a passage that was not on screen.
                if let insight = viewModel.insight, !insight.isEmpty {
                    insightCard(insight)
                } else if viewModel.isLoading {
                    insightSkeleton
                }

                if let performance = viewModel.performance {
                    ResponseRingsChart(performance: performance)
                } else if viewModel.isLoading {
                    performanceSkeleton
                }

                // The weekly grid needs categorised reviews inside the last 8
                // weeks; the period-total cards stay as the fallback.
                if let topicWeeks = viewModel.topicWeeks, !topicWeeks.topics.isEmpty {
                    TopicHeatGridChart(
                        data: topicWeeks,
                        trends: Dictionary(uniqueKeysWithValues: viewModel.heatmap.map { ($0.category, $0.trend) })
                    ) { row in
                        selectedTopic = row
                    }
                } else if !viewModel.heatmap.isEmpty {
                    topicGrid
                } else if viewModel.isLoading {
                    topicGridSkeleton
                }

                if !viewModel.sentimentWeeks.isEmpty {
                    SentimentRiverChart(weeks: viewModel.sentimentWeeks)
                } else if viewModel.isLoading {
                    trendChartSkeleton
                }
            }
            .padding(20)
        }
        .navigationDestination(item: $selectedTopic) { topic in
            FilteredReviewsView(title: topic.label, category: topic.category)
        }
    }

    // MARK: - The AI read

    /// The endpoint writes 3-4 prefixed lines (📊 this week / ⚠️ watch /
    /// ✅ do today / 🔮 next week). Each becomes its own row with a symbol,
    /// and the forecast gets a separate tinted block — the same separation
    /// the web makes, because a prediction sitting in the same visual
    /// weight as a measured figure reads as another measured figure.
    private func insightCard(_ insight: String) -> some View {
        let lines = insight
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return VStack(alignment: .leading, spacing: 12) {
            Text("Cavnar AI's read on your reviews")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1.1)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                let parsed = Self.parseInsightLine(line)
                if parsed.isForecast {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Forecast")
                            .font(.cavnarBody(9.5, weight: 700))
                            .tracking(0.8)
                            .textCase(.uppercase)
                            .foregroundStyle(Color.cavnarEmber)
                        Text(parsed.text)
                            .font(.cavnarBody(14.5))
                            .italic()
                            .foregroundStyle(Color.cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, 10)
                    .padding(.vertical, 6)
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Color.cavnarEmber).frame(width: 2)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Forecast. \(parsed.text)")
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Image(systemName: parsed.symbol)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(parsed.tint)
                            .accessibilityHidden(true)
                        Text(parsed.text)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel(parsed.text)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        .background(Color.cavnarEmber.opacity(0.09))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private struct ParsedInsightLine {
        let symbol: String
        let tint: Color
        let text: String
        let isForecast: Bool
    }

    /// Maps the line's emoji prefix onto an SF Symbol. Unknown prefixes keep
    /// their text and fall back to a neutral bullet rather than being
    /// dropped — the passage is the point, the icon is decoration.
    private static func parseInsightLine(_ line: String) -> ParsedInsightLine {
        let table: [(String, String, Color, Bool)] = [
            ("📊", "chart.bar.fill", .cavnarBlue, false),
            ("⚠️", "exclamationmark.triangle.fill", .cavnarAmber, false),
            ("⚠", "exclamationmark.triangle.fill", .cavnarAmber, false),
            ("✅", "checkmark.circle.fill", .cavnarGreen, false),
            ("🚨", "exclamationmark.octagon.fill", .cavnarRed, false),
            ("💡", "lightbulb.fill", .cavnarAmber, false),
            ("🔮", "sparkles", .cavnarEmber, true),
        ]
        for (prefix, symbol, tint, forecast) in table where line.hasPrefix(prefix) {
            let body = line.dropFirst(prefix.count)
                .trimmingCharacters(in: CharacterSet(charactersIn: " \u{FE0F}"))
            return ParsedInsightLine(symbol: symbol, tint: tint, text: body, isForecast: forecast)
        }
        return ParsedInsightLine(symbol: "circle.fill", tint: .cavnarInk3, text: line, isForecast: false)
    }

    private var insightSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.45)
            CavnarSkeletonBar(height: 12, widthFraction: 0.95)
            CavnarSkeletonBar(height: 12, widthFraction: 0.85)
            CavnarSkeletonBar(height: 12, widthFraction: 0.7)
        }
        .padding(16)
        .background(Color.cavnarEmber.opacity(0.09))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // MARK: - Loading skeletons

    private var performanceSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.5)
            HStack {
                ForEach(0..<3, id: \.self) { _ in
                    VStack(spacing: 6) {
                        CavnarSkeletonBar(height: 20, widthFraction: 0.5)
                        CavnarSkeletonBar(height: 10, widthFraction: 0.8)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }

    private var topicGridSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.35)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(0..<4, id: \.self) { _ in
                    VStack(alignment: .leading, spacing: 8) {
                        CavnarSkeletonBar(height: 12, widthFraction: 0.6)
                        CavnarSkeletonBar(height: 20, widthFraction: 0.3)
                        CavnarSkeletonBar(height: 6, widthFraction: 1.0)
                    }
                    .padding(12)
                    .background(Color.cavnarPaper2.opacity(0.6))
                    .overlay(
                        RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
        }
    }

    private var trendChartSkeleton: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.45)
            CavnarSkeletonBar(height: 190, widthFraction: 1.0)
            HStack(spacing: 16) {
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
            }
        }
        // Unboxed, matching trendChartCard's own now-unboxed container —
        // otherwise the skeleton pops from boxed to unboxed the instant
        // real data arrives.
    }

    // MARK: - Topic sentiment grid

    private var topicGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Topic sentiment")
                .font(.cavnarBody(14, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(viewModel.heatmap.filter { $0.count > 0 }) { entry in
                    NavigationLink {
                        FilteredReviewsView(title: entry.label, category: entry.category)
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
                    .font(.cavnarBody(14, weight: 600))
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
                    .font(.cavnarNumber(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Spacer()
                Text("\(entry.pctNegative)% neg")
                    .font(.cavnarNumber(13.5, weight: 600))
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

}
