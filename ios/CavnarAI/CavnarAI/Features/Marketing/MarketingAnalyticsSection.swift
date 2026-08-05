import SwiftUI

struct MarketingAnalyticsSection: View {
    let viewModel: MarketingAnalyticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            if viewModel.isLoading && viewModel.performance == nil {
                ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
            } else {
                if let insight = viewModel.insight {
                    Text(insight)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarCard()
                }
                if let perf = viewModel.performance, perf.hasData {
                    HStack(spacing: 0) {
                        statTile(value: "\(perf.totalReach)", label: "Total reach")
                        Divider()
                        statTile(value: "\(perf.totalEngagement)", label: "Engagement")
                        Divider()
                        statTile(value: "\(perf.published)", label: "Published")
                    }
                    .cavnarCard()

                    if let top = perf.topPost {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Top post").font(.cavnarBody(11, weight: 700)).foregroundStyle(Color.cavnarInk3)
                            Text(top.topic ?? "").font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(top.reach) reach · \(top.likes) likes · \(top.comments) comments")
                                .font(.cavnarNumber(11))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .cavnarCard()
                    }
                } else if viewModel.performance != nil {
                    Text("No published post metrics yet.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    private func statTile(value: String, label: String) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(20, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }
}
