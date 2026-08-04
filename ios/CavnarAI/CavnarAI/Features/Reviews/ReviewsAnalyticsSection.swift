import SwiftUI

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if viewModel.isLoading && viewModel.performance == nil {
                    ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
                } else {
                    if let insight = viewModel.insight {
                        insightCard(insight)
                    }
                    if let performance = viewModel.performance {
                        performanceCard(performance)
                    }
                    if !viewModel.heatmap.isEmpty {
                        heatmapCard
                    }
                    if !viewModel.sentimentWeeks.isEmpty {
                        trendCard
                    }
                }
            }
            .padding(20)
        }
    }

    private func insightCard(_ insight: String) -> some View {
        Text(insight)
            .font(.cavnarBody(13))
            .foregroundStyle(Color.cavnarInk)
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
    }

    private func performanceCard(_ performance: ResponsePerformance) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Response performance — last \(performance.days)d")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack {
                statTile("Approved as-is", performance.approvedAsIs)
                statTile("Edited", performance.edited)
                statTile("Regenerated", performance.regenerated)
            }
        }
        .cavnarCard()
    }

    private func statTile(_ label: String, _ value: Int) -> some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text(label)
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private var heatmapCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Topic sentiment")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            ForEach(viewModel.heatmap.filter { $0.count > 0 }) { entry in
                HStack {
                    Text(entry.label).font(.cavnarBody(13, weight: 600))
                    trendIcon(entry.trend)
                    Spacer()
                    Text("\(entry.pctPositive)% pos")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarGreen)
                    Text("\(entry.pctNegative)% neg")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarRed)
                    Text("(\(entry.count))")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .cavnarCard()
    }

    @ViewBuilder
    private func trendIcon(_ trend: String) -> some View {
        switch trend {
        case "up": Image(systemName: "arrow.up.right").font(.system(size: 10)).foregroundStyle(Color.cavnarRed)
        case "down": Image(systemName: "arrow.down.right").font(.system(size: 10)).foregroundStyle(Color.cavnarGreen)
        default: EmptyView()
        }
    }

    private var trendCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Sentiment trend — last 8 weeks")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            ForEach(viewModel.sentimentWeeks) { week in
                HStack {
                    Text(week.label).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3).frame(width: 40, alignment: .leading)
                    Text("\(week.total) reviews").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                    Text(String(format: "%.1f★", week.avgRating))
                        .font(.cavnarNumber(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
        }
        .cavnarCard()
    }
}
