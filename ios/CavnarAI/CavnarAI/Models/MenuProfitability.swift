import Foundation

/// Plate cost and margin per dish — see inventory_ledger.menu_profitability.
/// Deliberately three groups rather than one list: the app should never
/// imply a margin it can't actually compute.
struct MenuProfitability: Decodable {
    /// Has a recipe AND a price — real cost, margin and food cost %.
    /// Worst food cost first, since the dish eating the margin is the one
    /// worth looking at.
    let priced: [MenuMarginItem]
    /// Has a recipe but no price yet — cost is known, margin isn't.
    let unpriced: [MenuMarginItem]
    /// No recipe mapped, so nothing is costable.
    let unmapped: [MenuMarginItem]
    let averageFoodCostPct: Double?
    let best: MenuMarginItem?
    let worst: MenuMarginItem?

    enum CodingKeys: String, CodingKey {
        case priced, unpriced, unmapped, best, worst
        case averageFoodCostPct = "average_food_cost_pct"
    }

    var isEmpty: Bool { priced.isEmpty && unpriced.isEmpty && unmapped.isEmpty }
}

struct MenuMarginItem: Decodable, Identifiable, Hashable {
    let id: Int
    let name: String
    let sellPrice: Double?
    let plateCost: Double?
    let ingredientCount: Int?
    let margin: Double?
    let foodCostPct: Double?
    let marginPct: Double?

    enum CodingKeys: String, CodingKey {
        case id, name, margin
        case sellPrice = "sell_price"
        case plateCost = "plate_cost"
        case ingredientCount = "ingredient_count"
        case foodCostPct = "food_cost_pct"
        case marginPct = "margin_pct"
    }

    /// The industry rule of thumb is roughly 28–35% food cost; below that is
    /// healthy, above ~40% is the dish to look at. Used only for the row's
    /// accent colour, never to state a verdict the number doesn't support.
    var costBand: MenuCostBand {
        guard let pct = foodCostPct else { return .unknown }
        if pct >= 40 { return .high }
        if pct >= 32 { return .watch }
        return .healthy
    }
}

enum MenuCostBand {
    case healthy, watch, high, unknown
}
