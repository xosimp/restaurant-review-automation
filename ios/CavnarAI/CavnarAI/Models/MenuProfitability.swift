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
    /// Has a recipe, but at least one of its ingredients has no unit cost —
    /// so the plate cost would be understated by exactly the ingredients
    /// nobody priced. Reported separately rather than costed as if the
    /// missing prices were zero.
    let uncosted: [MenuMarginItem]
    let averageFoodCostPct: Double?
    /// How the average was arrived at — weighted by units sold, or, without
    /// sales data, an unweighted mean that counts a side dish the same as the
    /// entree. The reader needs to know which before comparing it to 28–35%.
    let averageBasis: String?
    let hasSalesData: Bool?
    let best: MenuMarginItem?
    let worst: MenuMarginItem?
    /// Highest food cost percentage. Still worth surfacing — just not as
    /// "worst dish", which now means the smallest contribution.
    let highestFoodCost: MenuMarginItem?

    enum CodingKeys: String, CodingKey {
        case priced, unpriced, unmapped, uncosted, best, worst
        case averageFoodCostPct = "average_food_cost_pct"
        case averageBasis = "average_basis"
        case hasSalesData = "has_sales_data"
        case highestFoodCost = "highest_food_cost"
    }

    var isEmpty: Bool { priced.isEmpty && unpriced.isEmpty && unmapped.isEmpty && uncosted.isEmpty }
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
    /// Units sold in the popularity window — the other half of menu
    /// engineering, and the weight behind the menu's average food cost %.
    let unitsSold: Double?
    /// margin x unitsSold: what this dish actually contributes.
    let totalContribution: Double?
    /// How many of this dish's ingredients have no unit cost set.
    let uncostedIngredients: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, margin
        case sellPrice = "sell_price"
        case plateCost = "plate_cost"
        case ingredientCount = "ingredient_count"
        case foodCostPct = "food_cost_pct"
        case marginPct = "margin_pct"
        case unitsSold = "units_sold"
        case totalContribution = "total_contribution"
        case uncostedIngredients = "uncosted_ingredients"
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
