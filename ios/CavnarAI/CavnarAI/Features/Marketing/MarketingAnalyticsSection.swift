import SwiftUI

struct MarketingAnalyticsSection: View {
    let viewModel: MarketingAnalyticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            AIConsultantView(
                title: "Cavnar AI Marketing Brief",
                insight: viewModel.insight,
                isLoading: viewModel.isLoadingInsight
            )

            if viewModel.isLoading && viewModel.performance == nil {
                CavnarWorkingLine().padding(.vertical, 20)
            } else {
                if let perf = viewModel.performance, perf.hasData {
                    HStack(spacing: 0) {
                        statTile(value: "\(perf.totalReach)", label: "Total reach")
                        Divider()
                        statTile(value: "\(perf.totalEngagement)", label: "Engagement")
                        Divider()
                        statTile(value: "\(perf.published)", label: "Published")
                    }
                    .cavnarGlassCard()

                    if let top = perf.topPost {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Top post").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk3)
                            Text(top.topic ?? "").font(.cavnarBody(14.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(top.reach) reach · \(top.likes) likes · \(top.comments) comments")
                                .font(.cavnarNumber(14))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .cavnarCard()
                    }
                } else if viewModel.performance != nil {
                    Text("No published post metrics yet.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }

            if !viewModel.recentTopics.isEmpty {
                recentlyGenerated
            }
        }
    }

    /// Per-piece history — what was written, whether it went out, and what it
    /// did. Pull to refresh pulls fresh numbers from Meta first.
    private var recentlyGenerated: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Recently generated")
                    .font(.cavnarBody(14.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if viewModel.isRefreshingMetrics {
                    CavnarShimmerText(text: "Refreshing…")
                }
            }

            ForEach(viewModel.recentTopics) { topic in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .top, spacing: 8) {
                        Text(topic.topic)
                            .font(.cavnarBody(14.5, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        if topic.posted {
                            Label(topic.platformLabel ?? "Posted", systemImage: "checkmark")
                                .font(.cavnarBody(14, weight: 700))
                                .foregroundStyle(Color.cavnarGreen)
                        } else {
                            Text("Draft")
                                .font(.cavnarBody(14, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                    }
                    if let line = topic.metricsLine {
                        Text(line).font(.cavnarNumber(14)).foregroundStyle(Color.cavnarInk3)
                    } else if topic.posted {
                        Text("No numbers back from Meta yet")
                            .font(.cavnarBody(14))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
        }
    }

    private func statTile(value: String, label: String) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(20, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }
}
