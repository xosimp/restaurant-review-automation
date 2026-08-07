import SwiftUI

struct LaborAnalyticsSection: View {
    let viewModel: LaborAnalyticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            AIConsultantView(
                title: "Cavnar AI Labor Consultant",
                insight: viewModel.insight,
                isLoading: viewModel.isLoadingInsight
            )

            if let gap = viewModel.gap {
                gapCard(gap)
            } else if viewModel.isLoading {
                ProgressView().frame(maxWidth: .infinity).padding(.vertical, 20)
            }
            if !viewModel.trend.isEmpty {
                trendCard
            }
        }
    }

    private func gapCard(_ gap: LaborGap) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Monthly labor gap")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack(alignment: .firstTextBaseline) {
                Text("$\(Int(gap.monthlyGap))")
                    .font(.cavnarNumber(24, weight: 600))
                    .foregroundStyle(gap.overTarget ? Color.cavnarRed : Color.cavnarInk)
                    .cavnarNumberGlow(gap.overTarget ? .cavnarRed : .cavnarEmber)
                Text(gap.overTarget ? "over target this month" : "under target this month")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
            }
            (Text("\(String(format: "%.1f", gap.currentPct))%").font(.cavnarNumber(12))
                + Text(" current vs ")
                + Text("\(String(format: "%.0f", gap.targetPct))%").font(.cavnarNumber(12))
                + Text(" target"))
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
        }
        .cavnarCard()
    }

    private var trendCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("8-week trend")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            ForEach(viewModel.trend) { week in
                HStack {
                    Text(week.label).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3).frame(width: 44, alignment: .leading)
                    Text("\(String(format: "%.1f", week.pct))%")
                        .font(.cavnarNumber(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    Spacer()
                    Text("$\(Int(week.labor)) / $\(Int(week.sales))")
                        .font(.cavnarNumber(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
        }
        .cavnarCard()
    }
}
