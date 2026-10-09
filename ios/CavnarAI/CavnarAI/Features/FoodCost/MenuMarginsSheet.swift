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
        let uncosted: [MenuMarginItem]?
        let averageFoodCostPct: Double?
        let averageBasis: String?
        let hasSalesData: Bool?
        let best: MenuMarginItem?
        let worst: MenuMarginItem?
        let highestFoodCost: MenuMarginItem?
        let costReference: MenuCostReference?
        let error: String?

        enum CodingKeys: String, CodingKey {
            case ok, priced, unpriced, unmapped, uncosted, best, worst, error
            case averageFoodCostPct = "average_food_cost_pct"
            case averageBasis = "average_basis"
            case hasSalesData = "has_sales_data"
            case highestFoodCost = "highest_food_cost"
            case costReference = "cost_reference"
        }
    }

    private typealias OKErrorResponse = APIClient.OKResponse

    func load() async {
        isLoading = data == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let r: Response = try await client.send("/mobile/api/food-cost/menu-profitability")
            data = MenuProfitability(priced: r.priced, unpriced: r.unpriced, unmapped: r.unmapped,
                                     uncosted: r.uncosted ?? [],
                                     averageFoodCostPct: r.averageFoodCostPct,
                                     averageBasis: r.averageBasis, hasSalesData: r.hasSalesData,
                                     best: r.best, worst: r.worst, highestFoodCost: r.highestFoodCost,
                                     costReference: r.costReference)
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
    /// A dish to open on its price — a cost driver's "Look at X's price"
    /// (nav "inventory/menu?dish=X", parity audit #76).
    var focusDish: String? = nil
    @State private var focusSpent = false

    init(focusDish: String? = nil) {
        self.focusDish = focusDish
    }

    /// The dish a focus names, matched without case or edge spaces —
    /// priced first, then unpriced.
    static func match(_ dish: String?, in data: MenuProfitability) -> MenuMarginItem? {
        guard let want = dish?.trimmingCharacters(in: .whitespaces).lowercased(), !want.isEmpty else { return nil }
        return (data.priced + data.unpriced).first { $0.name.trimmingCharacters(in: .whitespaces).lowercased() == want }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.xl) {
                    if viewModel.isLoading && viewModel.data == nil {
                        CavnarSkeletonLines(widths: [1.0, 0.86, 0.7, 0.55])
                    } else if let error = viewModel.errorMessage, viewModel.data == nil {
                        Text(error).cavnarText(.body)
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
                            // Dishes that can't be costed yet — named and
                            // fixed on the web (iOS readability round).
                            if !data.uncosted.isEmpty || !data.unmapped.isEmpty {
                                notCostedCard(uncosted: data.uncosted, unmapped: data.unmapped)
                            }
                        }
                    }
                }
                .padding(CavnarSpace.gutter)
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
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .task {
                await viewModel.load()
                guard !focusSpent, let data = viewModel.data, let item = Self.match(focusDish, in: data) else { return }
                focusSpent = true
                try? await Task.sleep(for: .milliseconds(350))
                viewModel.pricingItem = item
            }
            .sheet(item: $viewModel.pricingItem) { item in
                MenuPriceSheet(viewModel: viewModel, item: item)
            }
        }
    }

    private func summaryCard(average: Double, data: MenuProfitability) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Average food cost")
            Text(Self.pct(average))
                .cavnarText(.figureL)
                .cavnarNumberGlow()
                .cavnarSensitive()
            // The basis matters more than the number. An unweighted mean of
            // per-dish percentages counts a $3 side the same as the entree
            // carrying the night's revenue. The figure it is read against is
            // the server's (the owner's target or the published band for the
            // type; Benchmarking #35) — the app no longer quotes a 28–35%
            // band that was the same for every kind of restaurant.
            Text(data.hasSalesData == true
                 ? "Across the \(data.priced.count) dish\(data.priced.count == 1 ? "" : "es") with a recipe and a price, weighted by what actually sold."
                 : "Across the \(data.priced.count) dish\(data.priced.count == 1 ? "" : "es") with a recipe and a price. No sales data yet, so every dish counts equally.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Text(data.costReference?.basis.map { "Dish colours read against \($0)." }
                 ?? "No food-cost target and no published figure for your type, so dishes are not coloured \u{2014} set a target in Account to colour them.")
                .cavnarText(.caption)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private static let pricedShown = 5

    /// The five thinnest margins, each one tap from its price; every dish's
    /// margin is a table for the web (iOS readability round, 10/8/26).
    private func pricedCard(_ items: [MenuMarginItem]) -> some View {
        let shown = Array(items.prefix(Self.pricedShown))
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Thinnest margins")
            VStack(spacing: 0) {
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        viewModel.pricingItem = item
                    } label: { marginRow(item) }
                    .buttonStyle(.plain)
                    if index < shown.count - 1 {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            CavnarWebLinkRow(title: items.count > Self.pricedShown ? "All \(items.count) dishes" : "Every dish\u{2019}s margin",
                             subtitle: "Plate cost, price and margin for each", path: "inventory/margins",
                             actionLabel: "Open on the web")
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
                Text(item.name).cavnarText(.label)
                CavnarMixedText(costLine(item), role: .caption)
                    .cavnarSensitive()
            }
            Spacer(minLength: CavnarSpace.xs)
            VStack(alignment: .trailing, spacing: 2) {
                Text(item.foodCostPct.map(Self.pct) ?? DSRFormat.dash)
                    .cavnarText(.figureS, color: bandTextColor(item.costBand))
                Text("food cost").cavnarText(.caption)
            }
        }
        .padding(.vertical, CavnarSpace.xs)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }

    private func costLine(_ item: MenuMarginItem) -> String {
        let cost = item.plateCost.map(Self.money) ?? DSRFormat.dash
        let price = item.sellPrice.map(Self.money) ?? DSRFormat.dash
        let margin = item.margin.map(Self.money) ?? DSRFormat.dash
        return "\(cost) cost · \(price) price · \(margin) margin"
    }

    private func unpricedCard(_ items: [MenuMarginItem]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("No price yet", tint: .cavnarAmber)
            Text("These have a recipe, so the plate cost is known — add what they sell for and the margin follows.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                    Button {
                        Haptic.light()
                        viewModel.pricingItem = item
                    } label: {
                        HStack(spacing: CavnarSpace.s) {
                            Text(item.name).cavnarText(.body, color: .cavnarInk)
                            Spacer(minLength: CavnarSpace.xs)
                            CavnarMixedText("\(item.plateCost.map(Self.money) ?? DSRFormat.dash) cost", role: .caption)
                                .cavnarSensitive()
                            Image(systemName: "chevron.right")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarEmber2)
                                .accessibilityHidden(true)
                        }
                        .frame(minHeight: 44)
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

    /// Dishes that can't be costed yet: a recipe with an ingredient that has
    /// no unit cost (costed as zero once, so a dish whose main protein was
    /// never priced showed an excellent margin), or no recipe at all. Said
    /// here as a count; each is fixed on the web.
    private func notCostedCard(uncosted: [MenuMarginItem], unmapped: [MenuMarginItem]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Can\u{2019}t be costed yet", tint: .cavnarAmber)
            if !uncosted.isEmpty {
                CavnarMixedText("\(uncosted.count) dish\(uncosted.count == 1 ? "" : "es") with an ingredient that has no unit cost",
                                role: .secondary)
            }
            if !unmapped.isEmpty {
                CavnarMixedText("\(unmapped.count) dish\(unmapped.count == 1 ? "" : "es") with no recipe yet", role: .secondary)
            }
            CavnarWebLinkRow(title: "Fix their recipes", subtitle: "Add the missing costs and recipes",
                             path: "inventory/menu", actionLabel: "Open on the web")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private var emptyState: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text("No menu items yet").cavnarText(.lead)
            Text("Once dishes are mapped to their ingredients, this shows what each one actually earns.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
    }

    /// The band as text: small red text takes the text-safe red.
    private func bandTextColor(_ band: MenuCostBand) -> Color {
        band == .high ? .cavnarRedText : bandColor(band)
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
                    Text(item.plateCost.map { "What does \(item.name) sell for? The plate costs \(String(format: "$%.2f", $0)) to make, so the price is what turns that into a margin." } ?? "What does \(item.name) sell for? The price is what turns its plate cost into a margin.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)

                    CavnarFloatingField(
                        icon: "dollarsign", placeholder: "Menu price", text: $price,
                        keyboardType: .decimalPad, focus: $focusedField, field: .price
                    )

                    if let error = viewModel.priceError {
                        Text(error).cavnarText(.body, color: .cavnarRedText)
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
