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
