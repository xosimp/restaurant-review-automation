import Foundation

struct WasteItem: Decodable, Identifiable {
    let item: String
    let wasteCost: Double
    let wastePct: Double
    let wasteLastWeek: Double?
    let unit: String?

    var id: String { item }

    enum CodingKeys: String, CodingKey {
        case item
        case wasteCost = "waste_cost"
        case wastePct = "waste_pct"
        case wasteLastWeek = "waste_last_week"
        case unit
    }
}

struct OverstockItem: Decodable, Identifiable {
    let item: String
    let overstockCost: Double
    let currentStock: Double?
    let parLevel: Double?
    let unit: String?

    var id: String { item }

    enum CodingKeys: String, CodingKey {
        case item
        case overstockCost = "overstock_cost"
        case currentStock = "current_stock"
        case parLevel = "par_level"
        case unit
    }
}

struct FoodCostAnalytics: Decodable {
    let ok: Bool
    let insight: String?
    let wasteItems: [WasteItem]
    let overstock: [OverstockItem]
    let recoverableMonthly: Double?

    enum CodingKeys: String, CodingKey {
        case ok, insight, overstock
        case wasteItems = "waste_items"
        case recoverableMonthly = "recoverable_monthly"
    }
}
