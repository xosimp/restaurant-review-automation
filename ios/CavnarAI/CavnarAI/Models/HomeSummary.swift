import Foundation

/// Decodes GET /mobile/api/home — a deliberately trimmed aggregate (see
/// mobile_api.py's _do_mobile_home docstring): just the KPI numbers an
/// owner glances at and the same "needs attention" list the web Home tab
/// shows, not the full desktop dashboard's savings-breakdown/onboarding/
/// marketing-agency-value machinery.
///
/// `modules` is a generic array (models.get_active_modules() on the
/// backend), not a fixed set of named fields — this is what lets Home and
/// the Modules tab render any number of modules (today: 5; tomorrow:
/// Waitlist, Bar & Alcohol, whatever else) without an app update just to
/// show a new module's tile.
struct HomeSummary: Codable {
    let username: String?
    let restaurantName: String
    let locationName: String?
    let brandColor: String?
    let reviewsAwaitingApproval: Int
    let modules: [ModuleSummary]
    let needsAttention: [NeedsAttentionItem]
    let totalValueDelivered: Int
    let valueHistory: [ValueSnapshot]

    enum CodingKeys: String, CodingKey {
        case username
        case restaurantName = "restaurant_name"
        case locationName = "location_name"
        case brandColor = "brand_color"
        case reviewsAwaitingApproval = "reviews_awaiting_approval"
        case modules
        case needsAttention = "needs_attention"
        case totalValueDelivered = "total_value_delivered"
        case valueHistory = "value_history"
    }
}

/// One day's "Total value delivered" figure — see value_delivered.py's
/// record_value_snapshot(). Ascending by date, oldest first.
struct ValueSnapshot: Codable, Hashable {
    let date: String
    let value: Int
}

/// One entry in the active-modules list. `icon` is a small semantic
/// vocabulary the backend controls (e.g. "reviews", "labor") — NOT a
/// literal SF Symbol name; ModuleIcon.swift owns the actual symbol mapping
/// so either side can change independently (a new backend module needs no
/// app update to show its Home tile; a symbol tweak needs no backend
/// redeploy).
struct ModuleSummary: Codable, Identifiable, Hashable {
    let key: String
    let label: String
    let icon: String
    /// "available" or "coming_soon" — models.get_active_modules() on the
    /// backend. A coming_soon module (Waitlist/Bar today) routes to
    /// ComingSoonView instead of a real screen.
    let status: String
    let kpi: ModuleKPI?

    var id: String { key }
    var isAvailable: Bool { status == "available" }
}

struct ModuleKPI: Codable, Hashable {
    let value: String
    let sublabel: String
}

/// `module` names which module a tap should navigate into — a key into the
/// Modules registry, not a literal tab name (the app has no per-module tabs
/// anymore).
struct NeedsAttentionItem: Codable, Identifiable {
    let type: String
    let module: String
    let title: String
    let detail: String

    var id: String { type }
}
