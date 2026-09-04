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
    // Computed server-side by the exact same is_in_quiet_hours() check
    // notify.py's own alert dispatch gates on, so the Home badge can never
    // disagree with what's actually being held back right now.
    let quietHoursActive: Bool
    let alertQuietEnd: String?
    // Home's hero subline ("Overnight, Cavnar answered 3 reviews and
    // flagged 2 things for you") and its closing "This week" receipt — see
    // mobile_api.py's _home_overnight / _home_weekly_receipts. Both optional
    // on purpose: a summary cached before these shipped still decodes, and
    // the hero simply falls back to its quiet line.
    let overnight: HomeOvernight?
    let weeklyReceipts: [HomeWeeklyReceipt]?

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
        case quietHoursActive = "quiet_hours_active"
        case alertQuietEnd = "alert_quiet_end"
        case overnight
        case weeklyReceipts = "weekly_receipts"
    }
}

/// Drafts written and alerts fired in the last `windowHours` — the numbers
/// the hero subline is built from.
struct HomeOvernight: Codable, Hashable {
    let answered: Int
    let flagged: Int
    let windowHours: Int?

    enum CodingKeys: String, CodingKey {
        case answered, flagged
        case windowHours = "window_hours"
    }
}

/// One line of the "This week — what Cavnar did for you" receipt: a bold
/// `emphasis` ("9 replies") followed by the rest of the sentence. The
/// backend only sends lines whose number is non-zero, so an empty list
/// means the section is hidden, never padded.
struct HomeWeeklyReceipt: Codable, Identifiable, Hashable {
    let module: String
    let emphasis: String
    let text: String

    var id: String { module + "|" + emphasis + "|" + text }
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
    /// Home's pulse-strip chip for this module — the KPI value with a
    /// short label and a semantic tone. Defaulted so the Modules tab's
    /// static coming-soon entries (built with the memberwise init) keep
    /// compiling untouched.
    var pulse: ModulePulse? = nil

    var id: String { key }
    var isAvailable: Bool { status == "available" }
}

struct ModuleKPI: Codable, Hashable {
    let value: String
    let sublabel: String
}

/// "12/14 · replies · 86%" with a breathing dot — `tone` is "good", "warn"
/// or nil (ember), decided server-side from the same thresholds the alerts
/// use (mobile_api.py's _home_pulse).
struct ModulePulse: Codable, Hashable {
    let value: String
    let label: String
    let tone: String?
}

/// `module` names which module a tap should navigate into — a key into the
/// Modules registry, not a literal tab name (the app has no per-module tabs
/// anymore).
struct NeedsAttentionItem: Codable, Identifiable {
    let type: String
    let module: String
    let title: String
    let detail: String
    /// The action deck's buttons — primary label, optional secondary link,
    /// and what the primary does: "publish_replies" (one-tap bulk publish
    /// via /mobile/api/reviews/approve-all) or "open_module" (navigate).
    /// All optional so an older cached summary still decodes.
    let cta: String?
    let secondary: String?
    let action: String?

    var id: String { type }
    var isPublishAction: Bool { action == "publish_replies" }
}
