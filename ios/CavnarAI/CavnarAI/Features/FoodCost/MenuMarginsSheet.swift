import SwiftUI
import Observation

@Observable
@MainActor
final class MenuMarginsViewModel {
    var data: MenuProfitability?
    var isLoading = false
    var errorMessage: String?

    /// The dish whose price is being edited, if any.
    var pricingItem: MenuMarginItem?
    var isSavingPrice = false
    var priceError: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Response: Decodable {
        let ok: Bool
        let priced: [MenuMarginItem]
        let unpriced: [MenuMarginItem]
        let unmapped: [MenuMarginItem]
        let averageFoodCostPct: Double?
        let best: MenuMarginItem?
        let worst: MenuMarginItem?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, priced, unpriced, unmapped, best, worst, error
            case averageFoodCostPct = "average_food_cost_pct"
        }
    }

    private struct OKErrorResponse: Decodable {
        let ok: Bool
        let error: String?
    }

    func load() async {
        isLoading = data == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: Response = try await client.send("/mobile/api/food-cost/menu-profitability")
            data = MenuProfitability(priced: r.priced, unpriced: r.unpriced, unmapped: r.unmapped,
                                     averageFoodCostPct: r.averageFoodCostPct, best: r.best, worst: r.worst)
        } catch let error as APIClient.APIError {
            if data == nil { errorMessage = error.message }
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch {
            if data == nil { errorMessage = "Couldn't work out menu margins." }
        }
    }

    private struct PriceBody: Encodable {
        let menuItemId: Int
        let sellPrice: Double?
        enum CodingKeys: String, CodingKey {
            case menuItemId = "menu_item_id"
            case sellPrice = "sell_price"
        }
    }

    @discardableResult
    func setPrice(_ item: MenuMarginItem, to price: Double?) async -> Bool {
        isSavingPrice = true
        priceError = nil
        defer { isSavingPrice = false }
        do {
            let r: OKErrorResponse = try await client.send(
                "/mobile/api/food-cost/menu-item-price", method: .post,
                body: PriceBody(menuItemId: item.id, sellPrice: price))
            if r.ok {
                await load()
                return true
            }
            priceError = r.error ?? "Couldn't save that price."
            return false
        } catch let error as APIClient.APIError {
            priceError = error.message
            return false
        } catch {
            priceError = "Couldn't save that price."
            return false
        }
    }
}

/// What each dish actually earns. Recipes have costed the plate all along;
/// this pairs that cost with the menu price, which is the half that turns a
/// cost into a margin.
struct MenuMarginsSheet: View {
    @State private var viewModel = MenuMarginsViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    if viewModel.isLoading && viewModel.data == nil {
                        CavnarSkeletonLines(widths: [1.0, 0.86, 0.7, 0.55])
                    } else if let error = viewModel.errorMessage, viewModel.data == nil {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                    } else if let data = viewModel.data {
                        if data.isEmpty {
                            emptyState
                        } else {
                            if let average = data.averageFoodCostPct {
                                summaryCard(average: average, data: data)
                            }
                            if !data.priced.isEmpty {
                                pricedCard(data.priced)
                            }
                            if !data.unpriced.isEmpty {
                                unpricedCard(data.unpriced)
                            }
                            if !data.unmapped.isEmpty {
                                unmappedCard(data.unmapped)
                            }
                        }
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle("Menu margins")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Menu margins")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done").font(.cavnarBody(15, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task { await viewModel.load() }
            .sheet(item: $viewModel.pricingItem) { item in
                MenuPriceSheet(viewModel: viewModel, item: item)
            }
        }
    }

    private func summaryCard(average: Double, data: MenuProfitability) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("AVERAGE FOOD COST")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            Text(Self.pct(average))
                .font(.cavnarNumber(38, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
                .cavnarSensitive()
            Text("Across the \(data.priced.count) dish\(data.priced.count == 1 ? "" : "es") with both a recipe and a price. Most kitchens aim for 28–35%.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func pricedCard(_ items: [MenuMarginItem]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("WORST MARGIN FIRST")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        viewModel.pricingItem = item
                    } label: { marginRow(item) }
                    .buttonStyle(.plain)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private func marginRow(_ item: MenuMarginItem) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Rectangle()
                .fill(bandColor(item.costBand))
                .frame(width: 3, height: 30)
                .clipShape(Capsule())
            VStack(alignment: .leading, spacing: 2) {
                Text(item.name)
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                HomeMixedText.make(costLine(item), size: 12.5, weight: 500, color: .cavnarInk3)
                    .cavnarSensitive()
            }
            Spacer(minLength: 8)
            VStack(alignment: .trailing, spacing: 2) {
                Text(Self.pct(item.foodCostPct ?? 0))
                    .font(.cavnarNumber(16, weight: 700))
                    .foregroundStyle(bandColor(item.costBand))
                Text("food cost")
                    .font(.cavnarBody(11.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, 11)
        .contentShape(Rectangle())
    }

    private func costLine(_ item: MenuMarginItem) -> String {
        let cost = Self.money(item.plateCost ?? 0)
        let price = Self.money(item.sellPrice ?? 0)
        let margin = Self.money(item.margin ?? 0)
        return "\(cost) cost · \(price) price · \(margin) margin"
    }

    private func unpricedCard(_ items: [MenuMarginItem]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("NO PRICE YET")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarAmber)
            Text("These have a recipe, so the plate cost is known — add what they sell for and the margin follows.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        viewModel.pricingItem = item
                    } label: {
                        HStack(spacing: 10) {
                            Text(item.name).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk)
                            Spacer(minLength: 8)
                            HomeMixedText.make("\(Self.money(item.plateCost ?? 0)) cost",
                                               size: 13, weight: 600, color: .cavnarInk3)
                                .cavnarSensitive()
                            Image(systemName: "chevron.right")
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                        .padding(.vertical, 11)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    if index < items.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
        .cavnarCard()
    }

    private func unmappedCard(_ items: [MenuMarginItem]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("NO RECIPE MAPPED")
                .font(.cavnarBody(13.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarInk3)
            Text("\(items.count) dish\(items.count == 1 ? "" : "es") can't be costed until their ingredients are mapped: \(items.prefix(6).map(\.name).joined(separator: ", "))\(items.count > 6 ? "…" : "").")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("No menu items yet")
                .font(.cavnarHeadline(17))
                .foregroundStyle(Color.cavnarInk)
            Text("Once dishes are mapped to their ingredients, this shows what each one actually earns.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }

    private func bandColor(_ band: MenuCostBand) -> Color {
        switch band {
        case .healthy: return .cavnarGreen
        case .watch: return .cavnarAmber
        case .high: return .cavnarRed
        case .unknown: return .cavnarInk3
        }
    }

    private static func money(_ value: Double) -> String {
        "$" + String(format: "%.2f", value)
    }

    private static func pct(_ value: Double) -> String {
        String(format: "%.1f", value) + "%"
    }
}

private enum MenuPriceField: Hashable, CaseIterable { case price }

private struct MenuPriceSheet: View {
    let viewModel: MenuMarginsViewModel
    let item: MenuMarginItem

    @Environment(\.dismiss) private var dismiss
    @State private var price = ""
    @FocusState private var focusedField: MenuPriceField?

    private var parsed: Double? { Double(price.trimmingCharacters(in: .whitespaces)) }
    private var canSubmit: Bool { !viewModel.isSavingPrice && (parsed ?? -1) >= 0 }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("What does \(item.name) sell for? The plate costs \(String(format: "$%.2f", item.plateCost ?? 0)) to make, so the price is what turns that into a margin.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "dollarsign", placeholder: "Menu price", text: $price,
                        keyboardType: .decimalPad, focus: $focusedField, field: .price
                    )

                    if let error = viewModel.priceError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.setPrice(item, to: parsed) { dismiss() }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingPrice {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save price")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSubmit))
                        .disabled(!canSubmit)

                        Button { dismiss() } label: {
                            Text("Cancel").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(item.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(item.name) }
            .keyboardNavToolbar($focusedField)
        }
        .onAppear {
            if let existing = item.sellPrice, existing > 0 {
                price = String(format: "%.2f", existing)
            }
            focusedField = .price
        }
    }
}
