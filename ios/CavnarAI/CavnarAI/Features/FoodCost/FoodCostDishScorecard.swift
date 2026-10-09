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
                            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                                Text(DSRFormat.dash).cavnarText(.figureL, color: .cavnarInk3)
                                Text((card.reason.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? "No dishes to score yet") + ".")
                                    .cavnarText(.body)
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
                        Text(error).cavnarText(.body).cavnarCard().padding(CavnarSpace.gutter)
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

    /// A dish with a decision on the phone: a reprice (or its units check),
    /// a Pass, or a move that changes the plate — Protect, Fix, Cut.
    /// Promote, Review and "no move" are reading, and they are the web's
    /// (re-audit F17: the whole-menu list is a table).
    private func hasDecision(_ dish: DishScorecard.Dish) -> Bool {
        dish.waitsOnUnits || dish.action == "reprice" || viewModel.suggestion(for: dish) != nil
            || ["urgent", "fix", "cut"].contains(dish.action ?? "")
    }

    private func list(_ card: DishScorecard) -> some View {
        let groups = card.groups.compactMap { g -> (title: String, dishes: [DishScorecard.Dish])? in
            let ds = g.dishes.filter(hasDecision)
            return ds.isEmpty ? nil : (g.title, ds)
        }
        let restCount = card.dishes.filter { !hasDecision($0) }.count
        return List {
            Section {
                VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                    Text(groups.isEmpty ? "Nothing on the menu needs a move from you right now."
                         : "The dishes that need a move from you, grouped by the move.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let note = card.note {
                        Text(note).cavnarText(.caption)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let line = viewModel.doneLine {
                        CavnarMixedText(line, role: .secondary, color: .cavnarGreen)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // What each dish sold and what guests said — the
                    // numbers behind the moves — are a table for the web.
                    CavnarWebLinkRow(title: "Every dish\u{2019}s numbers", subtitle: restCount > 0 ? "\(restCount) more dish\(restCount == 1 ? "" : "es") to promote, review or keep, with cost, sales and guest mentions" : "Cost, sales and guest mentions for each",
                                     path: "inventory/menu", actionLabel: "Open on the web")
                }
                .listRowBackground(Color.clear)
            }
            ForEach(groups, id: \.title) { group in
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
                    CavnarKicker(group.title, tint: group.title == "Check units first" ? Color.cavnarAmber : Color.cavnarEmber2)
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

    /// One dish: its name and move, why, its price and food cost on one
    /// line, and — for a dish to reprice — a visible Reprice button and
    /// Pass (the swipe and the long-press menu still work; they used to be
    /// the only way, iOS readability round 10/8/26).
    private func row(_ dish: DishScorecard.Dish) -> some View {
        let canPass = viewModel.suggestion(for: dish)?.recKey != nil && !viewModel.passed.contains(dish.name)
        let showsReprice = dish.action == "reprice" || viewModel.suggestion(for: dish) != nil
        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Text(dish.name).cavnarText(.label)
                Spacer(minLength: CavnarSpace.xs)
                if viewModel.busy.contains(dish.name) {
                    CavnarShimmerText(text: "Saving", color: .cavnarInk)
                } else if let move = dish.moveLabel {
                    Text(move).cavnarText(.tag, color: dish.waitsOnUnits ? Color.cavnarAmber : Color.cavnarEmber2)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background((dish.waitsOnUnits ? Color.cavnarAmber : Color.cavnarEmber).opacity(0.14), in: Capsule())
                }
            }
            if let why = dish.why {
                Text(why).cavnarText(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            if let w = dish.unitWarning {
                Text(w).cavnarText(.secondary, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            CavnarMixedText(Self.priceLine(dish), role: .caption,
                            color: dish.unitWarning != nil ? .cavnarAmber : nil)
            if viewModel.passed.contains(dish.name) {
                Text(RecAnswer.notForUs.confirmation).cavnarText(.caption)
            } else if showsReprice || canPass {
                HStack(spacing: CavnarSpace.m) {
                    if showsReprice {
                        Button {
                            Haptic.light()
                            reprice(dish)
                        } label: {
                            Text(dish.unitWarning != nil ? "Check units first" : "Reprice")
                                .cavnarText(.label, color: dish.unitWarning != nil ? .cavnarAmber : .cavnarEmber2)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.borderless)
                    }
                    if canPass {
                        Button {
                            Haptic.light()
                            passing = dish
                        } label: {
                            Text(RecAnswer.notForUs.label).cavnarText(.label, color: .cavnarInk2).cavnarHitTarget()
                        }
                        .buttonStyle(.borderless)
                    }
                }
                .disabled(viewModel.busy.contains(dish.name))
            }
        }
        .padding(.vertical, CavnarSpace.xxs)
        .accessibilityElement(children: .contain)
    }

    /// "$14.00 · 34.2% food cost" — "?" on a food cost whose units failed
    /// the check.
    static func priceLine(_ dish: DishScorecard.Dish) -> String {
        let pct = dish.foodCostPct.map { String(format: "%.1f%% food cost", $0) + (dish.unitWarning != nil ? " ?" : "") }
        return [DSRFormat.money(dish.sellPrice), pct].compactMap { $0 }.joined(separator: " \u{00B7} ")
    }

    /// The Reprice button and the swipe do the same: a unit warning first,
    /// then the suggested price to confirm, or a price to type.
    private func reprice(_ dish: DishScorecard.Dish) {
        if dish.unitWarning != nil {
            unitsFor = dish
        } else if viewModel.suggestion(for: dish)?.suggestedPrice != nil {
            confirming = dish
        } else {
            pricing = dish
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
                CavnarMixedText("Now \(DSRFormat.money(dish.sellPrice))"
                                + (dish.foodCostPct.map { String(format: " \u{00B7} %.1f%% food cost", $0) } ?? ""),
                                role: .secondary)
                TextField("New menu price", text: $text)
                    .keyboardType(.decimalPad)
                    .font(.cavnar(.figureM))
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
            .padding(CavnarSpace.gutter)
            .accountSheetChrome(dish.name)
            .onAppear {
                if let p = dish.sellPrice { text = String(format: "%.2f", p) }
                focused = true
            }
        }
    }
}
