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
    let insightIntro: String?
    let insightRecommendations: [String]
    let insightForecast: String?
    let wasteItems: [WasteItem]
    let overstock: [OverstockItem]
    let recoverableMonthly: Double?

    enum CodingKeys: String, CodingKey {
        case ok, overstock
        case insightIntro = "insight_intro"
        case insightRecommendations = "insight_recommendations"
        case insightForecast = "insight_forecast"
        case wasteItems = "waste_items"
        case recoverableMonthly = "recoverable_monthly"
    }

    var insight: AIInsight? {
        guard let insightIntro else { return nil }
        return AIInsight(intro: insightIntro, recommendations: insightRecommendations, forecast: insightForecast)
    }
}
