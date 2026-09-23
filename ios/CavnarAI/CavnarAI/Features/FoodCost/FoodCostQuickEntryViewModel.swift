import Foundation
import Observation

struct FoodCostItem: Identifiable {
    let id = UUID()
    var name: String
    var unit: String
    var priceText: String = ""
    var usageText: String = ""

    /// The same seven ingredients the web dashboard starts a fresh
    /// submission with (templates/dashboard.html's fc-rows default list).
    /// v1 has no GET endpoint to re-fetch last week's saved item list (only
    /// POST /mobile/api/food-cost/quickcount exists), so mobile always
    /// starts from this default set rather than prefilling prior values —
    /// a deliberate v1 scope cut, not an oversight.
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

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    // Returns the new item so the carousel can scroll to reveal it — a
    // blank row appended off the bottom of a 3-card viewport is invisible
    // otherwise, with no indication anything happened.
    @discardableResult
    func addCustomRow() -> FoodCostItem {
        let newItem = FoodCostItem(name: "", unit: "")
        items.append(newItem)
        return newItem
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
