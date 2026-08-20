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

/// Shared shape for critical_low/reorder_soon/order_reduction — all three
/// are just filtered/sorted subsets of the same analyse_inventory() items
/// list (inventory.py), carrying the same computed ordering fields.
struct InventoryActionItem: Decodable, Identifiable {
    let item: String
    let unit: String?
    let daysRemaining: Double?
    let lastOrderQty: Double?
    let suggestedOrderQty: Double?
    let savingsVsLast: Double?

    var id: String { item }

    enum CodingKeys: String, CodingKey {
        case item, unit
        case daysRemaining = "days_remaining"
        case lastOrderQty = "last_order_qty"
        case suggestedOrderQty = "suggested_order_qty"
        case savingsVsLast = "savings_vs_last"
    }

    /// "skip" when a suggested reorder isn't needed at all — same
    /// convention as dashboard.html's own {% if item.suggested_order_qty
    /// == 0 %}skip{% endif %}.
    var suggestedOrderLabel: String {
        guard let suggestedOrderQty, suggestedOrderQty > 0 else { return "skip" }
        let qty = suggestedOrderQty.truncatingRemainder(dividingBy: 1) == 0
            ? String(Int(suggestedOrderQty)) : String(format: "%.1f", suggestedOrderQty)
        return unit.map { "\(qty) \($0)" } ?? qty
    }
}

struct FoodCostTrendWeek: Decodable, Identifiable {
    let label: String
    let start: String
    let end: String
    let waste: Double

    var id: String { end }
}

struct FoodCostTrend: Decodable {
    let ok: Bool
    let weeks: [FoodCostTrendWeek]
}

struct FoodCostAnalytics: Decodable {
    let ok: Bool
    let insightIntro: String?
    let insightRecommendations: [String]
    let insightForecast: String?
    let wasteItems: [WasteItem]
    let overstock: [OverstockItem]
    let criticalLow: [InventoryActionItem]
    let reorderSoon: [InventoryActionItem]
    let orderReduction: [InventoryActionItem]
    let recoverableMonthly: Double?
    let annualRecoverable: Double?
    let totalWasteCostWeek: Double?
    let monthlyWasteProjection: Double?
    let annualWasteProjection: Double?
    let wasteRatePct: Double?
    let benchmarkLabel: String?
    let benchmarkDetail: String?
    let totalStockValue: Double?
    let totalItems: Int?
    let weekStart: String?
    let weekEnd: String?
    let lastUpdated: String?

    enum CodingKeys: String, CodingKey {
        case ok, overstock
        case insightIntro = "insight_intro"
        case insightRecommendations = "insight_recommendations"
        case insightForecast = "insight_forecast"
        case wasteItems = "waste_items"
        case criticalLow = "critical_low"
        case reorderSoon = "reorder_soon"
        case orderReduction = "order_reduction"
        case recoverableMonthly = "recoverable_monthly"
        case annualRecoverable = "annual_recoverable"
        case totalWasteCostWeek = "total_waste_cost_week"
        case monthlyWasteProjection = "monthly_waste_projection"
        case annualWasteProjection = "annual_waste_projection"
        case wasteRatePct = "waste_rate_pct"
        case benchmarkLabel = "benchmark_label"
        case benchmarkDetail = "benchmark_detail"
        case totalStockValue = "total_stock_value"
        case totalItems = "total_items"
        case weekStart = "week_start"
        case weekEnd = "week_end"
        case lastUpdated = "last_updated"
    }

    var insight: AIInsight? {
        guard let insightIntro else { return nil }
        return AIInsight(intro: insightIntro, recommendations: insightRecommendations, forecast: insightForecast)
    }
}
