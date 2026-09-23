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
    /// A MONTHLY run-rate of measured improvements (value_delivered.headline),
    /// not a lifetime total — H-8.
    let totalValueDelivered: Int
    let valueHistory: [ValueSnapshot]
    /// "measured, per month", and the modules the figure was measured on,
    /// largest first. Optional: an older server omits them.
    let valueLabel: String?
    let valueByModule: [ValueModulePart]?
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
    // The web Home's getting-started card. Empty once every step is done
    // or the owner dismissed it — see mobile_api._setup_checklist.
    let setupChecklist: [HomeSetupStep]?
    // What Cavnar recommends, from home_brief. The server has sent these on
    // every /mobile/api/home response since Home shipped and the app never
    // decoded them, so the one button that starts an outcome tracker
    // ("Track this") existed only on the web — and the owner is on the
    // phone. Without a tracker nothing ever reaches outcomes.record, which
    // is what eventually produces "that one worked, about $420/month".
    let recommendations: [HomeRecommendation]?
    // The first session only. Google's own rating and how it sits against
    // the comparable restaurants nearest this one, for the screen where
    // every other block is empty by definition. The server returns [] the
    // moment the account has data of its own.
    let firstLook: [String]?
    // What is connected and what the product can therefore measure. Per
    // module: connected, what is readable right now, and — when it is not
    // — the one action that would light it up. Never a score.
    let readiness: HomeReadiness?
    /// The restaurant's own clock (ISO), for the day's slot: the close-out
    /// leads it after 8pm, the weekly receipts on Monday. Optional — an
    /// older server omits it and the brief simply leads.
    let localNow: String?
    /// Recommendation kinds that went quieter after the last four passed
    /// unanswered (home_brief / decisions.quiet_kinds), with a way back.
    let quieter: [HomeQuietKind]?
    /// Who a card can be handed to — consented alert contacts; empty for a
    /// login that may not open issues.
    let assignees: [HomeAssignee]?

    var localHour: Int? {
        guard let s = localNow, let t = s.firstIndex(of: "T") else { return nil }
        return Int(s[s.index(after: t)..<s.index(t, offsetBy: 3)])
    }
    var localIsEvening: Bool { (localHour ?? 12) >= 20 }
    var localIsMonday: Bool {
        guard let s = localNow, s.count >= 10 else { return false }
        let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd"; f.timeZone = TimeZone(secondsFromGMT: 0)
        guard let d = f.date(from: String(s.prefix(10))) else { return false }
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(secondsFromGMT: 0)!
        return cal.component(.weekday, from: d) == 2
    }
    /// Nothing connected yet: readiness is the page, so it leads.
    var isFresh: Bool {
        guard let r = readiness, !r.modules.isEmpty else { return false }
        return !r.modules.contains { $0.connected }
    }

    enum CodingKeys: String, CodingKey {
        case username
        case restaurantName = "restaurant_name"
        case locationName = "location_name"
        case brandColor = "brand_color"
        case reviewsAwaitingApproval = "reviews_awaiting_approval"
        case modules
        case needsAttention = "needs_attention"
        case totalValueDelivered = "total_value_delivered"
        case valueLabel = "value_label"
        case valueByModule = "value_by_module"
        case valueHistory = "value_history"
        case quietHoursActive = "quiet_hours_active"
        case alertQuietEnd = "alert_quiet_end"
        case overnight
        case weeklyReceipts = "weekly_receipts"
        case setupChecklist = "setup_checklist"
        case recommendations
        case firstLook = "first_look"
        case readiness
        case localNow = "local_now"
        case quieter, assignees
    }
}

struct HomeQuietKind: Codable, Hashable, Identifiable {
    let kind: String
    let label: String
    var id: String { kind }
}

struct HomeAssignee: Codable, Hashable, Identifiable {
    let id: Int
    let name: String
}

/// home_brief.readiness: which modules have data flowing and what that
/// makes measurable. `complete` hides the card — it exists to name the next
/// step, not to grade a finished setup.
struct HomeReadiness: Codable {
    struct Module: Codable, Identifiable {
        let key: String
        let label: String
        let connected: Bool
        let measurable: [String]?
        let next: String?
        let module: String?
        var id: String { key }
    }
    let modules: [Module]
    let connected: Int?
    let total: Int?
    let complete: Bool?
}

/// One line from home_brief's "Cavnar recommends". `metric` is what makes it
/// trackable: a recommendation that names a metric can be measured before
/// and after, and one that doesn't can only be read.
struct HomeRecommendation: Codable, Identifiable, Hashable {
    let key: String
    /// What to do, verb first.
    let title: String
    /// Why now.
    let why: String?
    let evidence: String?
    let module: String?
    let metric: String?
    /// ONE confidence for the card, from its own evidence (the kind's
    /// measured record here may move it a band). Optional: older servers
    /// omit it, and the card renders exactly as before.
    let confidence: HomeConfidence?
    let timeframe: String?
    let impact: String?
    let strength: String?
    /// Dollars a month at stake — only when measured, never invented.
    let dollarsMonthly: Double?
    let ifIgnored: String?
    let alternative: String?
    /// A one-tap finish (a reprice at the suggested price), when there is one.
    let action: HomeRecAction?
    let timesHidden: Int?
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, title, why, evidence, module, metric, confidence, timeframe, impact, strength, alternative, action
        case dollarsMonthly = "dollars_monthly"
        case ifIgnored = "if_ignored"
        case timesHidden = "times_hidden"
    }
}

struct HomeRecAction: Codable, Hashable {
    let kind: String
    let dish: String?
    let price: Double?
    let label: String?
    let count: Int?
}

struct HomeConfidence: Codable, Hashable {
    let score: Double
    let band: String
    /// "Medium confidence" — the one label the card shows.
    let label: String?
    /// What the band rests on ("6 reviews in 90 days").
    let reason: String?
    let caution: String?
}

struct HomeSetupStep: Codable, Identifiable, Hashable {
    let key: String
    let label: String
    let sub: String?
    let done: Bool
    let module: String?
    var id: String { key }
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

/// One module's share of the measured monthly value (value_delivered.headline).
struct ValueModulePart: Codable, Hashable {
    let module: String
    let label: String
    let monthly: Double
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
    /// The key an answer is recorded against (rec_ledger), whether the item
    /// may be hidden at all (a critical one may not), and how often it has
    /// been hidden before — the second hide asks why.
    let recKey: String?
    let dismissable: Bool?
    let timesHidden: Int?
    /// How many replies a publish tap sends — the number on its label (the
    /// last 30 days' drafts), never 25 including imported history.
    let count: Int?

    var id: String { type }
    var isPublishAction: Bool { action == "publish_replies" }

    enum CodingKeys: String, CodingKey {
        case type, module, title, detail, cta, secondary, action, dismissable, count
        case recKey = "rec_key"
        case timesHidden = "times_hidden"
    }
}
