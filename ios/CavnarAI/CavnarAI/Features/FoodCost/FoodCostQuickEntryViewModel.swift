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

    func addCustomRow() {
        items.append(FoodCostItem(name: "", unit: ""))
    }

    private struct ItemPayload: Encodable {
        let name: String
        let unit: String
        let price: Double
        let usage: Double
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

        enum CodingKeys: String, CodingKey {
            case ok, drift, error
            case totalWeeklyImpact = "total_weekly_impact"
            case submittedAt = "submitted_at"
        }
    }

    var canSubmit: Bool {
        !isSubmitting && items.contains { !$0.name.trimmingCharacters(in: .whitespaces).isEmpty }
    }

    func submit() async {
        let payloadItems: [ItemPayload] = items.compactMap { item in
            let name = item.name.trimmingCharacters(in: .whitespaces)
            guard !name.isEmpty else { return nil }
            return ItemPayload(
                name: name, unit: item.unit,
                price: Double(item.priceText) ?? 0,
                usage: Double(item.usageText) ?? 0
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
