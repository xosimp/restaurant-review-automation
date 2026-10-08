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

struct SupplierOverviewSheet: View {
    @State private var viewModel = SupplierOverviewViewModel()
    @State private var bulkTarget: SupplierRef?
    @State private var confirmingBulk = false
    @State private var newFor: String?
    @State private var newName = ""
    @State private var newEmail = ""

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    let items = viewModel.sheet.items
                    let sups = SupplierOverviewViewModel.suppliers(items)
                    let un = SupplierOverviewViewModel.unassigned(items)
                    header(sups: sups, unassigned: un.count)
                    if let line = viewModel.doneLine {
                        HomeMixedText.make(line, size: 14, weight: 600, color: .cavnarGreen)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if viewModel.sheet.isLoading && items.isEmpty {
                        CavnarSkeletonLines(widths: [1, 0.8, 0.9, 0.6]).cavnarCard()
                    } else if items.isEmpty {
                        Text("No ingredients on file yet.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3).cavnarCard()
                    } else {
                        if !un.isEmpty, !sups.isEmpty, !viewModel.sheet.isSynced {
                            bulk(sups: sups, count: un.count)
                        }
                        AccountSection(kicker: "Every ingredient") {
                            ForEach(Array(items.enumerated()), id: \.element.id) { i, it in
                                ingredientRow(it, sups: sups, showsDivider: i < items.count - 1)
                            }
                        }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Suppliers")
            .task { await viewModel.load() }
            .confirmationDialog(bulkTarget.map { "Put every ingredient without a supplier on \($0.name)?" } ?? "",
                                isPresented: $confirmingBulk, titleVisibility: .visible, presenting: bulkTarget) { s in
                Button("Assign \(SupplierOverviewViewModel.unassigned(viewModel.sheet.items).count) to \(s.name)") {
                    Task { await viewModel.assignAllUnassigned(to: s) }
                }
                Button("Cancel", role: .cancel) {}
            } message: { s in
                Text("Their orders then go to \(s.email).")
            }
            .alert("New supplier", isPresented: Binding(get: { newFor != nil }, set: { if !$0 { newFor = nil } })) {
                TextField("Supplier name", text: $newName)
                TextField("orders@supplier.com", text: $newEmail)
                    .keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never)
                Button("Save supplier") {
                    guard let ing = newFor else { return }
                    let s = SupplierRef(name: newName.trimmingCharacters(in: .whitespaces),
                                        email: newEmail.trimmingCharacters(in: .whitespaces), count: 0)
                    newFor = nil
                    guard !s.name.isEmpty, !s.email.isEmpty else {
                        viewModel.errorMessage = "A name and the email the order goes to."
                        return
                    }
                    Task { await viewModel.assign(ing, to: s) }
                }
                Button("Cancel", role: .cancel) { newFor = nil }
            } message: {
                Text(newFor.map { "Who \($0) is ordered from, and where its order is emailed." } ?? "")
            }
        }
    }

    private func header(sups: [SupplierRef], unassigned: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            DSRKicker(text: "Who you order from")
            if let source = viewModel.sheet.source, source.synced {
                HomeMixedText.make(source.line("Suppliers") + ". The weekly order groups itself by these.",
                                   size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("The weekly order groups itself by these; each needs an order email.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !sups.isEmpty || unassigned > 0 {
                AccountFlowLayout(spacing: 8) {
                    ForEach(sups) { s in
                        HomeMixedText.make("\(s.name) \u{00B7} \(s.count) item\(s.count == 1 ? "" : "s")", size: 13, weight: 600,
                                           color: .cavnarInk2)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Color.cavnarPaper3.opacity(0.45), in: Capsule())
                    }
                    if unassigned > 0 {
                        HomeMixedText.make("\(unassigned) without a supplier", size: 13, weight: 600, color: .cavnarAmber)
                            .padding(.horizontal, 10).padding(.vertical, 5)
                            .background(Color.cavnarAmber.opacity(0.14), in: Capsule())
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func bulk(sups: [SupplierRef], count: Int) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Put every ingredient without a supplier on").font(.cavnarBody(14.5, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
            HStack(spacing: 10) {
                Picker("Supplier", selection: Binding(get: { bulkTarget ?? sups.first }, set: { bulkTarget = $0 })) {
                    ForEach(sups) { s in Text(s.name).tag(Optional(s)) }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
                Spacer(minLength: 6)
                Button {
                    Haptic.light()
                    if bulkTarget == nil { bulkTarget = sups.first }
                    confirmingBulk = true
                } label: {
                    Group {
                        if viewModel.bulkBusy { CavnarShimmerText(text: "Assigning", color: .cavnarInk) } else { Text("Assign \(count)") }
                    }
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(viewModel.bulkBusy)
            }
        }
        .cavnarCard()
    }

    private func ingredientRow(_ it: CountSheetItem, sups: [SupplierRef], showsDivider: Bool) -> some View {
        let current = sups.first { $0.email.lowercased() == (it.supplierEmail ?? "").lowercased() }
        return VStack(spacing: 0) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(it.name).font(.cavnarBody(15.5)).foregroundStyle(Color.cavnarInk)
                    if let u = it.unit, !u.isEmpty { Text(u).font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3) }
                }
                Spacer(minLength: 8)
                if viewModel.sheet.isSynced {
                    Text(current?.name ?? DSRFormat.dash).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                } else if viewModel.busy.contains(it.name) {
                    CavnarShimmerText(text: "Saving", color: .cavnarInk)
                } else {
                    Menu {
                        ForEach(sups) { s in
                            Button {
                                guard s != current else { return }
                                Task { await viewModel.assign(it.name, to: s) }
                            } label: {
                                if s == current { Label(s.name, systemImage: "checkmark") } else { Text(s.name) }
                            }
                        }
                        Button("New supplier\u{2026}") {
                            newName = ""
                            newEmail = ""
                            newFor = it.name
                        }
                        if current != nil {
                            Button("No supplier", role: .destructive) { Task { await viewModel.assign(it.name, to: nil) } }
                        }
                    } label: {
                        HStack(spacing: 4) {
                            Text(current?.name ?? "Pick").font(.cavnarBody(14, weight: 700))
                            Image(systemName: "chevron.up.chevron.down").font(.system(size: 10, weight: .bold))
                        }
                        .foregroundStyle(current == nil ? Color.cavnarAmber : Color.cavnarEmber2)
                        .frame(minHeight: 44)
                    }
                }
            }
            .padding(.vertical, 4)
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
        VStack(alignment: .leading, spacing: 8) {
            DSRKicker(text: "Game this week")
            if let d = game.describe {
                HomeMixedText.make(d, size: 15.5, weight: 700, color: .cavnarInk).fixedSize(horizontal: false, vertical: true)
            }
            if let t = game.text {
                HomeMixedText.make(t, size: 13.5, color: .cavnarInk2).fixedSize(horizontal: false, vertical: true)
            }
            if !game.lines.isEmpty {
                AccountFlowLayout(spacing: 8) {
                    ForEach(Array(game.lines.prefix(6)), id: \.self) { l in
                        HomeMixedText.make("+\(l.extra.map { CountSheetViewModel.expectedString($0) } ?? DSRFormat.dash)"
                                           + (l.unit.map { $0.isEmpty ? "" : " \($0)" } ?? "") + " \(l.ingredient)",
                                           size: 12.5, weight: 600, color: .cavnarInk2)
                            .padding(.horizontal, 9).padding(.vertical, 4)
                            .background(Color.cavnarEmber.opacity(0.12), in: Capsule())
                    }
                }
            }
            if let b = game.basis {
                Text(b.prefix(1).uppercased() + b.dropFirst() + ".").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard(.ai)
    }
}
