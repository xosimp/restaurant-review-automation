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
    /// What every dish's colour reads against (Benchmarking #35): the
    /// owner's food-cost target, or the top of the published band for the
    /// restaurant's type — nil when there is neither, and then no dish is
    /// coloured. The server decides; the app keeps no band of its own.
    var costReference: MenuCostReference? = nil

    enum CodingKeys: String, CodingKey {
        case priced, unpriced, unmapped, uncosted, best, worst
        case averageFoodCostPct = "average_food_cost_pct"
        case averageBasis = "average_basis"
        case hasSalesData = "has_sales_data"
        case highestFoodCost = "highest_food_cost"
        case costReference = "cost_reference"
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
        case costTone = "cost_tone"
    }

    /// The server's colour for this dish (`cost_tone`: good / warn / bad /
    /// neutral), read against the owner's food-cost target or the published
    /// band for the restaurant's type (Benchmarking #35; BM3-16). The 32/40
    /// band the app used to keep was the same for a steakhouse and a bar.
    var costTone: String? = nil

    /// The row's accent colour — only what the server decided; no tone (an
    /// older server, or no target and no published figure) is neutral.
    var costBand: MenuCostBand {
        guard foodCostPct != nil else { return .unknown }
        switch costTone {
        case "good": return .healthy
        case "warn": return .watch
        case "bad": return .high
        default: return .unknown
        }
    }
}

enum MenuCostBand {
    case healthy, watch, high, unknown
}

/// `cost_reference` — {pct, kind: "target" | "published" | "rule_of_thumb" |
/// "vendor", basis}: what the dish colours were read against.
struct MenuCostReference: Decodable, Hashable {
    let pct: Double?
    let kind: String?
    let basis: String?
}
