import SwiftUI
import Observation

// Who each ingredient is ordered from (parity audit #78) — the web's
// Suppliers block (dashboard.html fc2LoadSuppliers): the suppliers with how
// many ingredients each fills, every ingredient's supplier, and one move to
// put every ingredient without a supplier on one. Read from the count sheet
// (GET /food-cost/count-sheet carries each line's supplier), written one
// ingredient at a time through POST /food-cost/ingredient-supplier — so a
// bulk assign is many small saves, each one landing on its own. Once an
// inventory system has synced, its suppliers are read-only here (#8).
//
// And the game-week add-on (#78): a followed game within the week, and the
// extra its kind used here (GET /food-cost/game-week) — said beside the
// order, never added to it.

struct SupplierRef: Hashable, Identifiable {
    let name: String
    let email: String
    let count: Int
    var id: String { email.lowercased() }
}

/// POST /food-cost/ingredient-supplier — addressed by ingredient name.
struct IngredientSupplierBody: Encodable, Equatable {
    let name: String
    let supplierName: String
    let supplierEmail: String
    enum CodingKeys: String, CodingKey {
        case name
        case supplierName = "supplier_name"
        case supplierEmail = "supplier_email"
    }
}

@Observable
@MainActor
final class SupplierOverviewViewModel {
    let sheet = CountSheetViewModel(persistsDraft: false)
    var busy: Set<String> = []
    var bulkBusy = false
    var errorMessage: String?
    var doneLine: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    /// Every supplier on the sheet, by email, with the ingredients it fills —
    /// first seen first, as the web lists them.
    static func suppliers(_ items: [CountSheetItem]) -> [SupplierRef] {
        var order: [String] = []
        var by: [String: (name: String, email: String, n: Int)] = [:]
        for it in items {
            let email = (it.supplierEmail ?? "").trimmingCharacters(in: .whitespaces)
            guard !email.isEmpty else { continue }
            let k = email.lowercased()
            if by[k] == nil {
                order.append(k)
                let name = (it.supplierName ?? "").isEmpty ? email : it.supplierName!
                by[k] = (name, email, 0)
            }
            by[k]!.n += 1
        }
        return order.compactMap { k in by[k].map { SupplierRef(name: $0.name, email: $0.email, count: $0.n) } }
    }

    static func unassigned(_ items: [CountSheetItem]) -> [CountSheetItem] {
        items.filter { ($0.supplierEmail ?? "").trimmingCharacters(in: .whitespaces).isEmpty }
    }

    func load() async { await sheet.load() }

    private func post(_ ingredient: String, _ s: SupplierRef?) async -> String? {
        do {
            let r: APIClient.OKResponse = try await client.send(
                "/mobile/api/food-cost/ingredient-supplier", method: .post,
                body: IngredientSupplierBody(name: ingredient, supplierName: s?.name ?? "", supplierEmail: s?.email ?? ""),
                retryTransient: false)
            return r.ok ? nil : (r.error ?? "Couldn\u{2019}t save that supplier.")
        } catch let error as APIClient.APIError {
            return error.message
        } catch {
            return "Couldn\u{2019}t save that supplier."
        }
    }

    /// One ingredient onto a supplier (nil clears it).
    func assign(_ ingredient: String, to s: SupplierRef?) async {
        busy.insert(ingredient)
        defer { busy.remove(ingredient) }
        errorMessage = nil
        if let err = await post(ingredient, s) {
            errorMessage = err
        } else {
            Haptic.success()
            doneLine = s.map { "\(ingredient) \u{2192} \($0.name)" } ?? "\(ingredient) has no supplier now"
            await load()
        }
    }

    /// Every ingredient without a supplier onto one — each saved on its own.
    func assignAllUnassigned(to s: SupplierRef) async {
        let todo = Self.unassigned(sheet.items).map(\.name)
        guard !todo.isEmpty else { return }
        bulkBusy = true
        defer { bulkBusy = false }
        errorMessage = nil
        var done = 0
        var lastError: String?
        for name in todo {
            if let err = await post(name, s) { lastError = err } else { done += 1 }
        }
        doneLine = "\(done) ingredient\(done == 1 ? "" : "s") \u{2192} \(s.name)"
        if let lastError, done < todo.count {
            errorMessage = "\(todo.count - done) couldn\u{2019}t be saved: \(lastError)"
        }
        if done > 0 { Haptic.success() }
        await load()
    }
}

/// Who you order from (re-audit F17, 10/8/26): the suppliers and how many
/// items each fills, and the ingredients with no supplier yet, named — so
/// the owner knows an order has nowhere to go. Choosing or adding a
/// supplier is a form, and it is the web's: one link opens it there.
struct SupplierOverviewSheet: View {
    @State private var viewModel = SupplierOverviewViewModel()

    /// Names shown before "+N more".
    private static let unassignedShown = 5

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    let items = viewModel.sheet.items
                    let sups = SupplierOverviewViewModel.suppliers(items)
                    let un = SupplierOverviewViewModel.unassigned(items)
                    header(sups: sups, unassigned: un.count)
                    if viewModel.sheet.isLoading && items.isEmpty {
                        CavnarSkeletonLines(widths: [1, 0.8, 0.9, 0.6]).cavnarCard()
                    } else if items.isEmpty {
                        Text("No ingredients on file yet.").cavnarText(.body).cavnarCard()
                    } else {
                        if !un.isEmpty {
                            AccountSection(kicker: "Without a supplier") {
                                ForEach(Array(un.prefix(Self.unassignedShown).enumerated()), id: \.element.id) { i, it in
                                    ingredientRow(it, showsDivider: i < min(un.count, Self.unassignedShown) - 1)
                                }
                                if un.count > Self.unassignedShown {
                                    CavnarMoreDisclosure(hiddenCount: un.count - Self.unassignedShown) {
                                        ForEach(Array(un.dropFirst(Self.unassignedShown))) { it in
                                            ingredientRow(it, showsDivider: false)
                                        }
                                    }
                                }
                            }
                        }
                        CavnarWebLinkRow(title: viewModel.sheet.isSynced ? "Every ingredient\u{2019}s supplier"
                                                                           : "Assign suppliers",
                                         subtitle: viewModel.sheet.isSynced
                                            ? "As your inventory system has them"
                                            : "Pick or add a supplier for any ingredient, or many at once",
                                         path: "inventory/order",
                                         actionLabel: viewModel.sheet.isSynced ? "Open on the web" : "Edit on the web")
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Suppliers")
            .task { await viewModel.load() }
        }
    }

    private func header(sups: [SupplierRef], unassigned: Int) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Who you order from")
            if let source = viewModel.sheet.source, source.synced {
                CavnarMixedText(source.line("Suppliers") + ". The weekly order groups itself by these.", role: .secondary)
            } else {
                Text("The weekly order groups itself by these; each needs an order email.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !sups.isEmpty || unassigned > 0 {
                AccountFlowLayout(spacing: CavnarSpace.xs) {
                    ForEach(sups) { s in
                        CavnarMixedText("\(s.name) \u{00B7} \(s.count) item\(s.count == 1 ? "" : "s")", role: .secondary)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Color.cavnarPaper3.opacity(0.45), in: Capsule())
                    }
                    if unassigned > 0 {
                        CavnarMixedText("\(unassigned) without a supplier", role: .secondary, color: .cavnarAmber)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Color.cavnarAmber.opacity(0.14), in: Capsule())
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// An ingredient with no supplier: its name and unit, read-only.
    private func ingredientRow(_ it: CountSheetItem, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Text(it.name).cavnarText(.body, color: .cavnarInk)
                Spacer(minLength: CavnarSpace.xs)
                if let u = it.unit, !u.isEmpty { Text(u).cavnarText(.caption) }
            }
            .frame(minHeight: 44)
            if showsDivider { AccountRowDivider() }
        }
    }
}

// MARK: - Game week (parity audit #78)

struct FoodCostGameWeek: Decodable {
    struct Game: Decodable {
        struct Line: Decodable, Hashable {
            let ingredient: String
            let extra: Double?
            let unit: String?
        }
        let describe: String?
        let text: String?
        let lines: [Line]
        let basis: String?

        enum CodingKeys: String, CodingKey { case describe, text, order, basis }
        private enum OrderKeys: String, CodingKey { case lines }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            describe = (try? c.decodeIfPresent(String.self, forKey: .describe)) ?? nil
            text = (try? c.decodeIfPresent(String.self, forKey: .text)) ?? nil
            basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
            if let o = try? c.nestedContainer(keyedBy: OrderKeys.self, forKey: .order) {
                lines = (try? o.decodeIfPresent([Line].self, forKey: .lines)) ?? []
            } else {
                lines = []
            }
        }
    }
    let ok: Bool
    let game: Game?

    enum CodingKeys: String, CodingKey { case ok, game }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        game = (try? c.decodeIfPresent(Game.self, forKey: .game)) ?? nil
    }
}

/// "Game this week" above the order: the game, what its kind used here
/// (or what the last one sold), the extra by ingredient — said, never added.
struct GameWeekCard: View {
    let game: FoodCostGameWeek.Game

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Game this week")
            if let d = game.describe {
                CavnarMixedText(d, role: .label)
            }
            if let t = game.text {
                CavnarMixedText(t, role: .secondary)
            }
            if !game.lines.isEmpty {
                AccountFlowLayout(spacing: 8) {
                    ForEach(Array(game.lines.prefix(6)), id: \.self) { l in
                        CavnarMixedText("+\(l.extra.map { CountSheetViewModel.expectedString($0) } ?? DSRFormat.dash)"
                                        + (l.unit.map { $0.isEmpty ? "" : " \($0)" } ?? "") + " \(l.ingredient)",
                                        role: .caption, color: .cavnarInk2)
                            .padding(.horizontal, 9).padding(.vertical, 4)
                            .background(Color.cavnarEmber.opacity(0.12), in: Capsule())
                    }
                }
            }
            if let b = game.basis {
                Text(b.prefix(1).uppercased() + b.dropFirst() + ".").cavnarText(.caption)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }
}
