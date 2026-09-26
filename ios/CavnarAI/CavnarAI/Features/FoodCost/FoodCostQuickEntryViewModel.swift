import Foundation
import Observation

struct FoodCostItem: Identifiable {
    let id = UUID()
    var name: String
    var unit: String
    var priceText: String = ""
    var usageText: String = ""
    /// Added by the owner rather than one of the seven defaults — saved to
    /// the account (POST /food-cost/custom-item) so it is there next week,
    /// as the web's "Add custom item" row is.
    var isCustom = false
    /// The custom item is on file server-side (removing it deletes it).
    var customSaved = false

    /// The seven rows a fresh tracker starts from — only until
    /// GET /food-cost/tracker answers (client_api._do_food_cost_tracker),
    /// which serves the same rows the web tracker renders: this week's
    /// saved prices, the pantry for a ledger account, or these seven plus
    /// the owner's custom items. The phone used to start from these every
    /// week, blank, whatever had been saved.
    static let defaults: [FoodCostItem] = [
        FoodCostItem(name: "Chicken Breast", unit: "lb"),
        FoodCostItem(name: "Beef/Steak", unit: "lb"),
        FoodCostItem(name: "Salmon / Fish", unit: "lb"),
        FoodCostItem(name: "Shrimp", unit: "lb"),
        FoodCostItem(name: "Heavy Cream", unit: "qt"),
        FoodCostItem(name: "Butter", unit: "lb"),
        FoodCostItem(name: "Produce (misc)", unit: "case"),
    ]
}

struct DriftItem: Decodable, Identifiable {
    let name: String
    let prevPrice: Double
    let currPrice: Double
    let pctChange: Double
    let weeklyImpact: Double
    let direction: String

    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, direction
        case prevPrice = "prev_price"
        case currPrice = "curr_price"
        case pctChange = "pct_change"
        case weeklyImpact = "weekly_impact"
    }
}

@Observable
@MainActor
final class FoodCostQuickEntryViewModel {
    var items: [FoodCostItem] = FoodCostItem.defaults
    var isSubmitting = false
    var errorMessage: String?
    var drift: [DriftItem] = []
    var totalWeeklyImpact: Double?
    var submittedAt: String?
    var didSubmit = false
    /// A ledger account's rows come from the ingredient list — invoices
    /// keep the prices current and usage comes from sales — so they are
    /// read-only until the owner taps Edit prices (U2-14), as on the web.
    var fromPantry = false
    var isEditing = false
    /// When prices were last typed in by hand on a ledger account.
    var typedAt: String?
    private(set) var hasLoaded = false

    var isReadOnly: Bool { fromPantry && !isEditing }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct TrackerResponse: Decodable {
        struct Row: Decodable {
            let name: String
            let unit: String
            let price: String
            let usage: String
            let custom: Bool?
        }
        let ok: Bool
        let items: [Row]
        let fromPantry: Bool?
        let submittedAt: String?
        let typedAt: String?
        let customItems: [Custom]?
        struct Custom: Decodable { let name: String }
        enum CodingKeys: String, CodingKey {
            case ok, items
            case fromPantry = "from_pantry"
            case submittedAt = "submitted_at"
            case typedAt = "typed_at"
            case customItems = "custom_items"
        }
    }

    /// GET /mobile/api/food-cost/tracker — the rows the web tracker opens
    /// on. Once per screen; a failure keeps the defaults and says so.
    func load() async {
        guard !hasLoaded else { return }
        do {
            let r: TrackerResponse = try await client.send("/mobile/api/food-cost/tracker", hapticOnError: false)
            guard r.ok, !r.items.isEmpty else { return }
            let saved = Set((r.customItems ?? []).map { $0.name.lowercased() })
            items = r.items.map { row in
                let custom = row.custom == true || saved.contains(row.name.lowercased())
                return FoodCostItem(name: row.name, unit: row.unit, priceText: row.price, usageText: row.usage,
                                    isCustom: custom, customSaved: custom)
            }
            fromPantry = r.fromPantry == true
            submittedAt = r.submittedAt
            typedAt = r.typedAt
            hasLoaded = true
        } catch is CancellationError {
        } catch {
            // The defaults stand; the owner can still type this week's prices.
        }
    }

    // Returns the new item so the carousel can scroll to reveal it — a
    // blank row appended off the bottom of a 3-card viewport is invisible
    // otherwise, with no indication anything happened.
    @discardableResult
    func addCustomRow() -> FoodCostItem {
        let newItem = FoodCostItem(name: "", unit: "", isCustom: true)
        items.append(newItem)
        return newItem
    }

    private struct CustomItemBody: Encodable { let name: String; let unit: String }
    private struct NameBody: Encodable { let name: String }

    /// A removed row: a saved custom item is deleted from the account too,
    /// so it does not come back next week.
    func remove(_ item: FoodCostItem) {
        items.removeAll { $0.id == item.id }
        guard item.customSaved else { return }
        let name = item.name.trimmingCharacters(in: .whitespaces)
        Task {
            do {
                let _: APIClient.OKResponse = try await client.send(
                    "/mobile/api/food-cost/custom-item", method: .delete, body: NameBody(name: name))
            } catch {
                errorMessage = "\(name) was removed here but is still saved — try removing it again."
            }
        }
    }

    /// New custom rows with a name are saved to the account before the
    /// count goes up — the web saves each as it is named.
    private func saveNewCustomItems() async {
        for idx in items.indices where items[idx].isCustom && !items[idx].customSaved {
            let name = items[idx].name.trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { continue }
            do {
                let r: APIClient.OKResponse = try await client.send(
                    "/mobile/api/food-cost/custom-item", method: .post,
                    body: CustomItemBody(name: name, unit: items[idx].unit.trimmingCharacters(in: .whitespaces)))
                if r.ok { items[idx].customSaved = true }
            } catch {
                // The price still goes up with the count; only next week's
                // pre-filled row is missed.
            }
        }
    }

    private struct ItemPayload: Encodable {
        let name: String
        let unit: String
        // Strings, deliberately. A blank field coerced to 0 was stored as
        // this week's price of record, became next week's baseline, and then
        // silently suppressed that ingredient's drift alert — the comparison
        // only runs when both prices are above zero. The server validates and
        // names anything it can't use, so an empty string reaches it as an
        // empty string rather than as a confident $0.00.
        let price: String
        let usage: String
    }

    /// A row is submittable when it has a name AND a price that parses.
    /// `canSubmit` used to require only a non-empty name anywhere in the list,
    /// so all seven default ingredients could be submitted with every price
    /// blank.
    // nonisolated: a pure parse with no state, so it doesn't need the main
    // actor and can be exercised directly by tests.
    nonisolated static func parsedPrice(_ text: String) -> Double? {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }
        // Locale-aware first: Double("3,50") is nil on a comma-decimal
        // keyboard, which is exactly where the silent zero came from.
        if let n = NumberFormatter.cavnarDecimal.number(from: trimmed) { return n.doubleValue }
        return Double(trimmed)
    }

    /// The price as the server's float() reads it: a plain "1234.5", from
    /// the number the phone parsed. The typed text used to go up as typed,
    /// so "1,234.50" (a grouping comma) or "3,50" (a decimal comma) failed
    /// float() server-side and the row was dropped (CLIENT-31).
    nonisolated static func wirePrice(_ value: Double) -> String {
        let f = NumberFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.numberStyle = .decimal
        f.usesGroupingSeparator = false
        f.minimumFractionDigits = 0
        f.maximumFractionDigits = 4
        return f.string(from: NSNumber(value: value)) ?? String(value)
    }

    private struct QuickcountBody: Encodable {
        let items: [ItemPayload]
    }

    private struct QuickcountResponse: Decodable {
        let ok: Bool
        let drift: [DriftItem]?
        let totalWeeklyImpact: Double?
        let submittedAt: String?
        let error: String?
        /// Rows the server could not use, each with its reason
        /// (_clean_quickcount_items). An ok:true answer can still carry
        /// these, and they used to vanish without a word (CLIENT-31).
        let rejected: [Rejected]?

        struct Rejected: Decodable {
            let name: String
            let why: String?
        }

        enum CodingKeys: String, CodingKey {
            case ok, drift, error, rejected
            case totalWeeklyImpact = "total_weekly_impact"
            case submittedAt = "submitted_at"
        }
    }

    var canSubmit: Bool {
        !isSubmitting && items.contains { item in
            !item.name.trimmingCharacters(in: .whitespaces).isEmpty
                && Self.parsedPrice(item.priceText) != nil
        }
    }

    /// Rows that carry a name but no usable price — named back to the owner
    /// rather than quietly stored as zeros.
    var rowsMissingAPrice: [String] {
        items.compactMap { item in
            let name = item.name.trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty, Self.parsedPrice(item.priceText) == nil else { return nil }
            return name
        }
    }

    func submit() async {
        let payloadItems: [ItemPayload] = items.compactMap { item in
            let name = item.name.trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { return nil }
            // Only rows with a real price go up; the rest are reported in
            // rowsMissingAPrice so the owner sees what was left out.
            guard let price = Self.parsedPrice(item.priceText) else { return nil }
            return ItemPayload(
                name: name, unit: item.unit,
                price: Self.wirePrice(price),
                usage: item.usageText.trimmingCharacters(in: .whitespaces)
            )
        }
        guard !payloadItems.isEmpty else { return }
        isSubmitting = true
        errorMessage = nil
        defer { isSubmitting = false }
        await saveNewCustomItems()
        do {
            let response: QuickcountResponse = try await client.send(
                "/mobile/api/food-cost/quickcount", method: .post, body: QuickcountBody(items: payloadItems)
            )
            if response.ok {
                drift = response.drift ?? []
                totalWeeklyImpact = response.totalWeeklyImpact
                submittedAt = response.submittedAt
                didSubmit = true
                let rejected = response.rejected ?? []
                if !rejected.isEmpty {
                    errorMessage = "Not saved: " + rejected.map { row in
                        row.why.map { "\(row.name) (\($0))" } ?? row.name
                    }.joined(separator: ", ") + "."
                }
            } else {
                errorMessage = response.error ?? "Couldn't save your prices."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't save — try again."
        }
    }
}
