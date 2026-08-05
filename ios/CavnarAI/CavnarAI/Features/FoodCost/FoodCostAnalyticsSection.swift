import SwiftUI

struct FoodCostAnalyticsSection: View {
    let viewModel: FoodCostAnalyticsViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if viewModel.isLoading && viewModel.analytics == nil {
                    ProgressView().padding(.top, 60).frame(maxWidth: .infinity)
                } else if let analytics = viewModel.analytics {
                    if let insight = analytics.insight {
                        Text(insight)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .cavnarCard()
                    }
                    if !analytics.wasteItems.isEmpty {
                        wasteCard(analytics.wasteItems)
                    }
                    if !analytics.overstock.isEmpty {
                        overstockCard(analytics.overstock)
                    }
                }
            }
            .padding(20)
        }
    }

    private func wasteCard(_ items: [WasteItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Waste breakdown")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            ForEach(items) { item in
                HStack {
                    Text(item.item).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                    Text("\(String(format: "%.0f", item.wastePct))%")
                        .font(.cavnarNumber(11))
                        .foregroundStyle(Color.cavnarRed)
                    Text("$\(String(format: "%.0f", item.wasteCost))")
                        .font(.cavnarNumber(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
        }
        .cavnarCard()
    }

    private func overstockCard(_ items: [OverstockItem]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Overstock breakdown")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            ForEach(items) { item in
                HStack {
                    Text(item.item).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Spacer()
                    if let stock = item.currentStock, let par = item.parLevel {
                        Text("\(Int(stock)) / \(Int(par)) par")
                            .font(.cavnarNumber(11))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Text("$\(String(format: "%.0f", item.overstockCost))")
                        .font(.cavnarNumber(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
        }
        .cavnarCard()
    }
}
