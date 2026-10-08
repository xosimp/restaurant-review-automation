import SwiftUI
import Observation

// "Your dishes" (parity audit #42) — the web's dish scorecard
// (dashboard.html loadFcMenu; GET /food-cost/dish-scorecard, menu_intelligence
// .dish_scorecard): every costed, priced dish with its food cost %, what it
// sold, what guests said about it, and the move it calls for — Protect,
// Fix, Cut, Reprice, Promote, Review — grouped by that move on the phone.
// A plate cost that fails the unit check (inventory_ledger._unit_sanity)
// cannot justify cutting or repricing a dish: its move waits on the check,
// as on the web.

// MARK: - Model

struct DishScorecard: Decodable {
    struct Dish: Decodable, Identifiable, Hashable {
        /// The menu item's id (the scorecard row's `id`).
        let menuItemId: Int?
        let name: String
        let sellPrice: Double?
        let foodCostPct: Double?
        let unitsSold: Double?
        let positiveMentions: Int
        let negativeMentions: Int
        let action: String?
        let why: String?
        /// Set when the plate cost is far outside any real food cost — a
        /// likely unit mismatch; the % is marked, the move waits.
        let unitWarning: String?

        var id: String { name }

        enum CodingKeys: String, CodingKey {
            case name, action, why
            case menuItemId = "id"
            case sellPrice = "sell_price"
            case foodCostPct = "food_cost_pct"
            case unitsSold = "units_sold"
            case positiveMentions = "positive_mentions"
            case negativeMentions = "negative_mentions"
            case unitWarning = "unit_warning"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            menuItemId = (try? c.decodeIfPresent(Int.self, forKey: .menuItemId)) ?? nil
            name = try c.decode(String.self, forKey: .name)
            sellPrice = (try? c.decodeIfPresent(Double.self, forKey: .sellPrice)) ?? nil
            foodCostPct = (try? c.decodeIfPresent(Double.self, forKey: .foodCostPct)) ?? nil
            unitsSold = (try? c.decodeIfPresent(Double.self, forKey: .unitsSold)) ?? nil
            positiveMentions = (try? c.decodeIfPresent(Int.self, forKey: .positiveMentions)) ?? 0
            negativeMentions = (try? c.decodeIfPresent(Int.self, forKey: .negativeMentions)) ?? 0
            action = (try? c.decodeIfPresent(String.self, forKey: .action)) ?? nil
            why = (try? c.decodeIfPresent(String.self, forKey: .why)) ?? nil
            let w = (try? c.decodeIfPresent(String.self, forKey: .unitWarning)) ?? nil
            unitWarning = (w?.isEmpty ?? true) ? nil : w
        }

        /// The units guard: a dish whose plate cost failed the check can't
        /// be cut or repriced on that cost.
        var waitsOnUnits: Bool { unitWarning != nil && (action == "cut" || action == "reprice") }

        /// The move as the list groups it — "Check units first" when it waits.
        var moveLabel: String? {
            if waitsOnUnits { return "Check units first" }
            return action.flatMap { DishScorecard.moves[$0] } ?? action?.capitalized
        }
    }

    /// The web's words for the moves, in its order (most urgent first).
    static let moves: [String: String] = ["urgent": "Protect", "fix": "Fix", "cut": "Cut", "reprice": "Reprice",
                                          "promote": "Promote", "review": "Review"]
    static let order = ["urgent", "fix", "cut", "reprice", "promote", "review"]

    let ok: Bool
    let available: Bool
    let dishes: [Dish]
    let reason: String?
    let note: String?

    enum CodingKeys: String, CodingKey { case ok, available, dishes, reason, note }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        dishes = (try? c.decodeIfPresent([Dish].self, forKey: .dishes)) ?? []
        available = (try? c.decodeIfPresent(Bool.self, forKey: .available)) ?? !dishes.isEmpty
        reason = (try? c.decodeIfPresent(String.self, forKey: .reason)) ?? nil
        note = (try? c.decodeIfPresent(String.self, forKey: .note)) ?? nil
    }

    /// The dishes grouped by move, in the web's order; a dish waiting on
    /// its units leads its own group; dishes with no move last.
    var groups: [(title: String, dishes: [Dish])] {
        var out: [(String, [Dish])] = []
        let waiting = dishes.filter(\.waitsOnUnits)
        if !waiting.isEmpty { out.append(("Check units first", waiting)) }
        for key in Self.order {
            let ds = dishes.filter { $0.action == key && !$0.waitsOnUnits }
            if !ds.isEmpty { out.append((Self.moves[key] ?? key.capitalized, ds)) }
        }
        let rest = dishes.filter { $0.action == nil }
        if !rest.isEmpty { out.append(("No move needed", rest)) }
        return out
    }
}

/// POST /food-cost/menu-item-price — the dish and its new price.
struct DishPriceBody: Encodable, Equatable {
    let menuItemId: Int
    let sellPrice: Double
    enum CodingKeys: String, CodingKey { case menuItemId = "menu_item_id"; case sellPrice = "sell_price" }
}

/// POST /food-cost/reprice/apply — the server's own suggested price.
struct DishRepriceBody: Encodable, Equatable {
    let dish: String
    let price: Double
    let menuItemId: Int?
    enum CodingKeys: String, CodingKey { case dish, price; case menuItemId = "menu_item_id" }
}

@Observable
@MainActor
final class DishScorecardViewModel {
    private(set) var card: DishScorecard?
    private(set) var reprice: [String: RepriceSuggestions.Suggestion] = [:]
    private(set) var isLoading = false
    var errorMessage: String?
    var doneLine: String?
    var busy: Set<String> = []
    /// Dishes the owner passed on, so their row says so in place.
    var passed: Set<String> = []

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = card == nil
        defer { isLoading = false }
        async let rp: RepriceSuggestions? = try? client.send("/mobile/api/food-cost/reprice", hapticOnError: false)
        do {
            let r: DishScorecard = try await client.send("/mobile/api/food-cost/dish-scorecard", hapticOnError: false)
            card = r
            errorMessage = nil
        } catch let error as APIClient.APIError {
            if card == nil { errorMessage = error.message }
        } catch is CancellationError {
        } catch {
            if card == nil { errorMessage = "Couldn\u{2019}t load your dishes." }
        }
        let suggestions = (await rp)?.suggestions ?? []
        reprice = Dictionary(suggestions.map { ($0.dish.lowercased(), $0) }, uniquingKeysWith: { a, _ in a })
    }

    /// The reprice suggestion for a dish, when ingredient costs moved it.
    func suggestion(for dish: DishScorecard.Dish) -> RepriceSuggestions.Suggestion? {
        reprice[dish.name.lowercased()]
    }

    /// Sets a dish's price — the suggestion's (POST /reprice/apply, which
    /// records suggested vs chosen and may start a tracker), or one typed.
    func setPrice(_ dish: DishScorecard.Dish, to price: Double) async -> Bool {
        busy.insert(dish.name)
        defer { busy.remove(dish.name) }
        errorMessage = nil
        do {
            if let s = suggestion(for: dish) {
                let r: RepriceApplyResult = try await client.send(
                    "/mobile/api/food-cost/reprice/apply", method: .post,
                    body: DishRepriceBody(dish: s.dish, price: price, menuItemId: s.menuItemId ?? dish.menuItemId),
                    retryTransient: false)
                guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t set that price."; return false }
            } else {
                guard let id = dish.menuItemId else { errorMessage = "That dish isn\u{2019}t on the menu list."; return false }
                let r: APIClient.OKResponse = try await client.send(
                    "/mobile/api/food-cost/menu-item-price", method: .post,
                    body: DishPriceBody(menuItemId: id, sellPrice: price), retryTransient: false)
                guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t set that price."; return false }
            }
            Haptic.success()
            doneLine = "\(dish.name) is now \(DSRFormat.money(price))."
            await load()
            return true
        } catch let error as APIClient.APIError {
            // A price change whose answer was lost may have landed.
            errorMessage = (error.status == nil && error.mayHaveReachedServer)
                ? "Couldn\u{2019}t confirm the price changed \u{2014} check Menu margins before trying again."
                : error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t set that price."
        }
        return false
    }

    /// "Not for us" on the dish's reprice — the same rec_ledger key as
    /// everywhere else, with the owner's reason.
    func pass(_ dish: DishScorecard.Dish, reason: RecReason) async {
        guard let key = suggestion(for: dish)?.recKey else { return }
        busy.insert(dish.name)
        defer { busy.remove(dish.name) }
        do {
            let r = try await client.answerRecommendation(key: key, answer: .notForUs, surface: "food",
                                                          module: "food", reasonCode: reason.code)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            Haptic.success()
            passed.insert(dish.name)
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }
}

// MARK: - The sheet

struct DishScorecardSheet: View {
    @State private var viewModel = DishScorecardViewModel()
    @State private var confirming: DishScorecard.Dish?
    @State private var pricing: DishScorecard.Dish?
    @State private var passing: DishScorecard.Dish?
    @State private var unitsFor: DishScorecard.Dish?

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isLoading {
                    ScrollView { CavnarSkeletonLines(widths: [1, 0.86, 0.7, 0.55]).cavnarCard().padding(20) }
                } else if let card = viewModel.card {
                    if card.dishes.isEmpty {
                        ScrollView {
                            VStack(alignment: .leading, spacing: 8) {
                                Text(DSRFormat.dash).font(.cavnarNumber(34, weight: 600)).foregroundStyle(Color.cavnarInk3)
                                Text((card.reason.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? "No dishes to score yet") + ".")
                                    .font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .cavnarCard()
                            .padding(20)
                        }
                    } else {
                        list(card)
                    }
                } else if let error = viewModel.errorMessage {
                    ScrollView {
                        Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3).cavnarCard().padding(20)
                    }
                }
            }
            .accountSheetChrome("Your dishes")
            .task { await viewModel.load() }
            .cavnarEmberRefreshable { await viewModel.load() }
            // A price change goes out to the menu: asked first, never one swipe.
            .confirmationDialog(confirming.map { "Set \($0.name) to \(DSRFormat.money(viewModel.suggestion(for: $0)?.suggestedPrice))?" } ?? "",
                                isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                                titleVisibility: .visible, presenting: confirming) { dish in
                if let price = viewModel.suggestion(for: dish)?.suggestedPrice {
                    Button("Set \(DSRFormat.money(price))") { Task { _ = await viewModel.setPrice(dish, to: price) } }
                }
                Button("Type a different price") { pricing = dish }
                Button("Cancel", role: .cancel) {}
            } message: { dish in
                Text(viewModel.suggestion(for: dish)?.whyLine.map { "Why: \($0)" }
                     ?? "The price that restores this dish\u{2019}s food cost %.")
            }
            .alert("Check the units first", isPresented: Binding(get: { unitsFor != nil }, set: { if !$0 { unitsFor = nil } }),
                   presenting: unitsFor) { _ in
                Button("OK", role: .cancel) {}
            } message: { dish in
                Text((dish.unitWarning ?? "") + " Fix the recipe\u{2019}s units before cutting or repricing it.")
            }
            .recReasonDialog(isPresented: Binding(get: { passing != nil }, set: { if !$0 { passing = nil } })) { reason in
                guard let dish = passing else { return }
                Task { await viewModel.pass(dish, reason: reason) }
            }
            .sheet(item: $pricing) { dish in
                DishPriceSheet(dish: dish) { price in await viewModel.setPrice(dish, to: price) }
                    .presentationDetents([.medium])
            }
        }
    }

    private func list(_ card: DishScorecard) -> some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Every dish with a recipe and a price: what it costs to make, what it sold, what guests said about it, and the move it calls for. Swipe a dish to reprice it, or to pass on its reprice.")
                        .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if let note = card.note {
                        Text(note).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let line = viewModel.doneLine {
                        HomeMixedText.make(line, size: 13.5, weight: 600, color: .cavnarGreen)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(13)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .listRowBackground(Color.clear)
            }
            ForEach(card.groups, id: \.title) { group in
                Section {
                    ForEach(group.dishes) { dish in
                        row(dish)
                            .listRowBackground(Color.cavnarPaper2.opacity(0.6))
                            .swipeActions(edge: .leading, allowsFullSwipe: false) { repriceAction(dish) }
                            .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                                if viewModel.suggestion(for: dish)?.recKey != nil, !viewModel.passed.contains(dish.name) {
                                    Button { passing = dish } label: { Label(RecAnswer.notForUs.label, systemImage: "hand.raised") }
                                        .tint(Color.cavnarInk3)
                                }
                            }
                            .contextMenu {
                                repriceAction(dish)
                                if viewModel.suggestion(for: dish)?.recKey != nil, !viewModel.passed.contains(dish.name) {
                                    Button { passing = dish } label: { Label(RecAnswer.notForUs.label, systemImage: "hand.raised") }
                                }
                            }
                    }
                } header: {
                    Text(group.title.uppercased())
                        .font(.cavnarBody(CavnarType.kicker, weight: 700)).tracking(1.2)
                        .foregroundStyle(group.title == "Check units first" ? Color.cavnarAmber : Color.cavnarEmber2)
                }
            }
        }
        .scrollContentBackground(.hidden)
        .listStyle(.insetGrouped)
    }

    @ViewBuilder
    private func repriceAction(_ dish: DishScorecard.Dish) -> some View {
        if dish.unitWarning != nil {
            Button { unitsFor = dish } label: { Label("Check units first", systemImage: "exclamationmark.triangle") }
                .tint(Color.cavnarAmber)
        } else {
            Button {
                Haptic.light()
                if viewModel.suggestion(for: dish)?.suggestedPrice != nil { confirming = dish } else { pricing = dish }
            } label: { Label("Reprice", systemImage: "dollarsign.circle") }
                .tint(Color.cavnarEmber)
        }
    }

    private func row(_ dish: DishScorecard.Dish) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(dish.name).font(.cavnarBody(15.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 8)
                if viewModel.busy.contains(dish.name) {
                    CavnarShimmerText(text: "Saving", color: .cavnarInk)
                } else if let move = dish.moveLabel {
                    Text(move).font(.cavnarBody(11.5, weight: 700))
                        .foregroundStyle(dish.waitsOnUnits ? Color.cavnarAmber : Color.cavnarEmber2)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background((dish.waitsOnUnits ? Color.cavnarAmber : Color.cavnarEmber).opacity(0.14), in: Capsule())
                }
            }
            if let why = dish.why {
                Text(why).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3).fixedSize(horizontal: false, vertical: true)
            }
            if let w = dish.unitWarning {
                Text(w).font(.cavnarBody(12.5, weight: 600)).foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 14) {
                stat("Price", DSRFormat.money(dish.sellPrice))
                stat("Food cost", dish.foodCostPct.map { String(format: "%.1f%%", $0) + (dish.unitWarning != nil ? " ?" : "") }
                     ?? DSRFormat.dash, tone: dish.unitWarning != nil ? .cavnarAmber : .cavnarInk)
                stat("Sold", dish.unitsSold.map { DSRFormat.count($0) } ?? DSRFormat.dash)
                stat("Guests", dish.positiveMentions + dish.negativeMentions > 0
                     ? "+\(dish.positiveMentions) / \u{2212}\(dish.negativeMentions)" : DSRFormat.dash)
            }
            if viewModel.passed.contains(dish.name) {
                Text(RecAnswer.notForUs.confirmation).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityHint(dish.unitWarning == nil ? "Swipe right to reprice" : "Its units need checking first")
    }

    private func stat(_ label: String, _ value: String, tone: Color = .cavnarInk) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(label.uppercased()).font(.cavnarBody(CavnarType.kicker, weight: 700)).tracking(0.8).foregroundStyle(Color.cavnarInk3)
            Text(value).font(.cavnarNumber(13.5, weight: 600)).foregroundStyle(value == DSRFormat.dash ? Color.cavnarInk3 : tone)
        }
    }
}

/// A dish's new price, typed — the save is the confirmation.
struct DishPriceSheet: View {
    let dish: DishScorecard.Dish
    let save: (Double) async -> Bool
    @State private var text = ""
    @State private var saving = false
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focused: Bool

    private var price: Double? {
        let v = Double(text.replacingOccurrences(of: "$", with: "").trimmingCharacters(in: .whitespaces))
        return (v ?? -1) > 0 ? v : nil
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                HomeMixedText.make("Now \(DSRFormat.money(dish.sellPrice))"
                                   + (dish.foodCostPct.map { String(format: " \u{00B7} %.1f%% food cost", $0) } ?? ""),
                                   size: 14, color: .cavnarInk3)
                TextField("New menu price", text: $text)
                    .keyboardType(.decimalPad)
                    .font(.cavnarNumber(22, weight: 600))
                    .cavnarTextFieldStyle()
                    .focused($focused)
                Button {
                    guard let price else { return }
                    saving = true
                    Task {
                        if await save(price) { dismiss() }
                        saving = false
                    }
                } label: {
                    Group {
                        if saving { CavnarShimmerText(text: "Saving") } else { Text(price.map { "Set \(DSRFormat.money($0))" } ?? "Set the price") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: price == nil || saving))
                .disabled(price == nil || saving)
                Spacer()
            }
            .padding(20)
            .accountSheetChrome(dish.name)
            .onAppear {
                if let p = dish.sellPrice { text = String(format: "%.2f", p) }
                focused = true
            }
        }
    }
}
