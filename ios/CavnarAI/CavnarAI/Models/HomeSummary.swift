import Foundation

/// Decodes GET /mobile/api/home — a deliberately trimmed aggregate (see
/// mobile_api.py's _do_mobile_home docstring): just the KPI numbers an
/// owner glances at and the same "needs attention" list the web Home tab
/// shows, not the full desktop dashboard's savings-breakdown/onboarding/
/// marketing-agency-value machinery.
struct HomeSummary: Codable {
    let restaurantName: String
    let locationName: String?
    let brandColor: String?
    let modules: ModuleFlags
    let kpis: KPISet
    let needsAttention: [NeedsAttentionItem]

    enum CodingKeys: String, CodingKey {
        case restaurantName = "restaurant_name"
        case locationName = "location_name"
        case brandColor = "brand_color"
        case modules, kpis
        case needsAttention = "needs_attention"
    }
}

struct ModuleFlags: Codable {
    let reviews: Bool
    let labor: Bool
    let inventory: Bool
    let marketing: Bool
    let intel: Bool
}

struct KPISet: Codable {
    let reviews: ReviewsKPI?
    let labor: LaborKPI?
    let foodCost: FoodCostKPI?
    let marketing: MarketingKPI?

    enum CodingKeys: String, CodingKey {
        case reviews, labor, marketing
        case foodCost = "food_cost"
    }
}

struct ReviewsKPI: Codable {
    let responded: Int
    let total: Int
    let responseRate: Double
    let awaitingApproval: Int
    let needsResponse: Int
    let avgRating: Double

    enum CodingKeys: String, CodingKey {
        case responded, total
        case responseRate = "response_rate"
        case awaitingApproval = "awaiting_approval"
        case needsResponse = "needs_response"
        case avgRating = "avg_rating"
    }
}

struct LaborKPI: Codable {
    let overallLaborPct: Double
    let target: Double
    let onTrack: Bool
    let overtimeCount: Int

    enum CodingKeys: String, CodingKey {
        case overallLaborPct = "overall_labor_pct"
        case target
        case onTrack = "on_track"
        case overtimeCount = "overtime_count"
    }
}

struct FoodCostKPI: Codable {
    let recoverableMonthly: Int

    enum CodingKeys: String, CodingKey {
        case recoverableMonthly = "recoverable_monthly"
    }
}

struct MarketingKPI: Codable {
    let thisMonth: Int

    enum CodingKeys: String, CodingKey {
        case thisMonth = "this_month"
    }
}

struct NeedsAttentionItem: Codable, Identifiable {
    let type: String
    let tab: String
    let title: String
    let detail: String

    var id: String { type }
}
