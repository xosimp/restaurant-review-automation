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

    private static func fmt(_ v: Double) -> String {
        v.truncatingRemainder(dividingBy: 1) == 0 ? String(Int(v)) : String(format: "%.1f", v)
    }

    /// "ORDER" alone doesn't say whether this is more or less than last
    /// time — computed against last_order_qty so the caption states the
    /// delta directly ("ORDER 4 MORE"/"ORDER 6 LESS") instead of making
    /// the reader do that subtraction themselves against the number below.
    /// Falls back to plain "ORDER" when there's nothing to compare (a
    /// skipped order, no prior order on file, or an unchanged quantity).
    var orderCaption: String {
        guard let suggestedOrderQty, suggestedOrderQty > 0,
              let lastOrderQty else { return "ORDER" }
        let delta = suggestedOrderQty - lastOrderQty
        if abs(delta) < 0.05 { return "ORDER" }
        return delta > 0 ? "ORDER \(Self.fmt(delta)) MORE" : "ORDER \(Self.fmt(-delta)) LESS"
    }
}

/// One entry from inventory.build_price_watch() — either a single-week
/// price spike or a sustained multi-week rise, already deduped/classified
/// server-side so the client only has to render, not judge.
struct PriceWatchItem: Decodable, Identifiable {
    let item: String
    let kind: String  // "spike" | "trend"
    let changePct: Double
    let weeks: Int?
    let oldPrice: Double
    let newPrice: Double
    let isBig8: Bool
    let actionHint: String

    var id: String { item }

    enum CodingKeys: String, CodingKey {
        case item, kind, weeks
        case changePct = "change_pct"
        case oldPrice = "old_price"
        case newPrice = "new_price"
        case isBig8 = "is_big_8"
        case actionHint = "action_hint"
    }

    var isTrend: Bool { kind == "trend" }

    var timeframeLabel: String {
        if let weeks, isTrend { return "\(weeks) week\(weeks == 1 ? "" : "s")" }
        return "this week"
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
    /// The target the SERVER computed, from the same history the bars come
    /// from. The chart used to derive its own from this week's analytics —
    /// a different endpoint, a different table, and a divisor already rounded
    /// to one decimal — and draw it across these bars as if they matched.
    let target: FoodCostTrendTarget?
}

struct FoodCostTrendTarget: Decodable {
    /// Target waste as a share of purchases. Per-restaurant, falling back to
    /// the industry figure — not a constant the client keeps its own copy of.
    let pct: Double?
    /// That percentage applied to what this restaurant actually buys, in
    /// dollars per week. Nil when there is no purchase history to imply it.
    let weekly: Double?
    /// "live" or "history" — which source the figure came from.
    let basis: String?
}

struct FoodCostAnalytics: Decodable {
    let ok: Bool
    let insightIntro: String?
    let insightRecommendations: [String]
    /// rec_ledger keys aligned index for index with `insightRecommendations`
    /// (null = no answer controls for that line). Optional: older servers
    /// don't send it.
    let insightRecKeys: [String?]?
    let insightForecast: String?
    let wasteItems: [WasteItem]
    let overstock: [OverstockItem]
    let criticalLow: [InventoryActionItem]
    let reorderSoon: [InventoryActionItem]
    let orderReduction: [InventoryActionItem]
    let priceWatch: [PriceWatchItem]
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
    /// Totals over EVERY item, not just the truncated lists above. The client
    /// used to sum the visible five overstock rows and present that as the
    /// restaurant's tied-up capital, which undercounts whenever a sixth item
    /// is overstocked.
    let overstockTotal: Double?
    let wasteItemsTotal: Double?
    /// Figures the server could not trace back to the data it gave the model.
    /// Reviews renders this; Food Cost declared no such key, so unsupported
    /// numbers were shown at full authority.
    let insightUnverified: String?
    /// False when the backend served the built-in example pantry because this
    /// restaurant has no inventory connected. Optional so a response from an
    /// older server (which didn't send it) still decodes, and defaults to
    /// live — absent means the old behaviour, not "assume it's fake".
    let isLive: Bool?
    /// Actual food cost % — COGS over net sales against the restaurant's own
    /// target. The module's namesake number: computed by `cogs.py` all along,
    /// exposed on its own endpoint, and never once requested by this app, so
    /// the phone showed a waste analysis with no way to see the percentage it
    /// was about.
    let cogs: FoodCostCOGS?
    /// The share of what sold that a recipe actually accounts for. A dish
    /// with no recipe depletes nothing, so its ingredients read as never
    /// used — this says how far the rest of this payload can be trusted.
    let recipeCoverage: RecipeCoverage?
    /// Counted waste versus waste inferred from a count coming in under
    /// expectation. An owner can act on the first and can only count more
    /// carefully on the second; they were indistinguishable here.
    let wasteSplit: WasteSplit?
    /// Whether the window label came from real count dates, and how old the
    /// newest count is. It used to be hardcoded to "today back six days"
    /// regardless of when anything was counted.
    let windowFromCounts: Bool?
    let windowAgeDays: Int?
    let projectionBasis: String?
    let purchasesBasis: String?
    let totalPurchased: Double?

    struct FoodCostCOGS: Decodable {
        let ok: Bool
        let pct: Double?
        let target: Double?
        let label: String?
        let tone: String?
        let variancePts: Double?
        let basis: String?
        let missing: [MissingComponent]?

        struct MissingComponent: Decodable, Hashable {
            let component: String
            let why: String
        }
        enum CodingKeys: String, CodingKey {
            case ok, pct, target, label, tone, basis, missing
            case variancePts = "variance_pts"
        }
    }

    struct RecipeCoverage: Decodable {
        let coveragePct: Double?
        let uncoveredCount: Int?
        let windowDays: Int?
        let hasData: Bool?
        enum CodingKeys: String, CodingKey {
            case coveragePct = "coverage_pct"
            case uncoveredCount = "uncovered_count"
            case windowDays = "window_days"
            case hasData = "has_data"
        }
    }

    struct WasteSplit: Decodable {
        let counted: Double?
        let inferred: Double?
        let inferredPct: Double?
        let hasData: Bool?
        enum CodingKeys: String, CodingKey {
            case counted, inferred
            case inferredPct = "inferred_pct"
            case hasData = "has_data"
        }
    }

    enum CodingKeys: String, CodingKey {
        case ok, overstock, cogs
        case recipeCoverage = "recipe_coverage"
        case wasteSplit = "waste_split"
        case windowFromCounts = "window_from_counts"
        case windowAgeDays = "window_age_days"
        case projectionBasis = "projection_basis"
        case purchasesBasis = "purchases_basis"
        case totalPurchased = "total_purchased"
        case insightIntro = "insight_intro"
        case insightRecommendations = "insight_recommendations"
        case insightRecKeys = "insight_rec_keys"
        case insightForecast = "insight_forecast"
        case wasteItems = "waste_items"
        case criticalLow = "critical_low"
        case reorderSoon = "reorder_soon"
        case orderReduction = "order_reduction"
        case priceWatch = "price_watch"
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
        case isLive = "is_live"
        case overstockTotal = "overstock_total"
        case wasteItemsTotal = "waste_items_total"
        case insightUnverified = "insight_unverified"
    }

    /// Example data must never be read as the owner's own numbers.
    var showsExampleData: Bool { isLive == false }

    var insight: AIInsight? {
        guard let insightIntro else { return nil }
        return AIInsight(intro: insightIntro, recommendations: insightRecommendations, forecast: insightForecast,
                         recKeys: insightRecKeys)
    }

    /// True when the server flagged figures in the narrative that it could not
    /// tie back to the data — the caveat the reader needs before acting on it.
    var hasUnverifiedFigures: Bool { !unverifiedFigureList.isEmpty }

    /// The individual figures the server could not support, for the caveat.
    var unverifiedFigureList: [String] {
        guard let insightUnverified else { return [] }
        return insightUnverified
            .split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    /// Annualised figures come from a single week's count. Below a few weeks
    /// on file they are an extrapolation from one data point, and the chart
    /// already refuses to draw on less — the hero had no equivalent test.
    func annualFiguresAreSupported(weeksOfHistory: Int) -> Bool { weeksOfHistory >= 4 }
}


// MARK: - The CFO read

/// GET /mobile/api/food-cost/cfo — the step after the position.
///
/// Everything else in this module answers "what is my food cost". This
/// answers "what is driving it, what is it worth, and what do I do first" —
/// the questions an operator actually opens the module with, and the ones
/// the app had no payload for.
struct FoodCostCFO: Decodable {
    let ok: Bool
    let brief: Brief?
    let diagnosis: Diagnosis?
    let drivers: Drivers?
    let profitability: Profitability?
    /// Which kind of claim each part is making — measured, computed,
    /// inferred, forecast or suggestion. A measured percentage, an inferred
    /// cause and a month-end projection used to arrive as prose of equal
    /// authority.
    let claimKinds: [String: String]?

    enum CodingKeys: String, CodingKey {
        case ok, brief, diagnosis, drivers, profitability
        case claimKinds = "claim_kinds"
    }

    struct Drivers: Decodable {
        let available: Bool
        let drivers: [Driver]
        let totalMonthly: Double?
        /// The same dollars with no ingredient counted twice — the figure the
        /// web card shows. The plain sum counts one ingredient up to four
        /// times (M-10).
        let totalMonthlyDeduplicated: Double?
        let reason: String?
        enum CodingKeys: String, CodingKey {
            case available, drivers, reason
            case totalMonthly = "total_monthly"
            case totalMonthlyDeduplicated = "total_monthly_deduplicated"
        }
        /// The one "at stake" figure to state.
        var atStake: Double? { totalMonthlyDeduplicated ?? totalMonthly }
    }

    /// One driver of cost movement. Ranked server-side by dollars, then
    /// confidence, then ease — the app renders that order and never re-sorts.
    struct Driver: Decodable, Identifiable, Hashable {
        let kind: String
        let label: String
        let dollarsMonthly: Double
        /// K1 (a percentage with "Why?"); an older server's bare band
        /// ("high") still decodes, and so does a driver with none.
        let confidence: TrustConfidence?
        let difficulty: String
        let evidence: String
        let ifIgnored: String
        /// The driver's rec_ledger key, when the server sends one — what
        /// "Why?" records the evidence look against.
        let recKey: String?
        var id: String { "\(kind)-\(label)" }
        enum CodingKeys: String, CodingKey {
            case kind, label, confidence, difficulty, evidence
            case dollarsMonthly = "dollars_monthly"
            case ifIgnored = "if_ignored"
            case recKey = "rec_key"
        }
        /// The band, for anything that still ranks or words by it.
        var confidenceBand: String { confidence?.effectiveBand ?? "low" }
    }

    struct Diagnosis: Decodable {
        let headline: String?
        let cause: String?
        let alternativeCause: String?
        let whatWouldConfirm: String?
        let recommendedAction: String?
        let expectedOutcome: String?
        let dollarsAtStake: Double?
        let operationalEvidence: [OperationalEvidence]?
        let ageHours: Double?
        let stale: Bool?
        /// K6 — the K1 object (a percentage with "Why?"); an older server's
        /// bare band still decodes. Declared apart from the others so the
        /// doc above stays with its fields.
        let confidence: TrustConfidence?
        /// The recommended action's rec_ledger key, whether the owner already
        /// answered it (the action and its controls then drop), when the
        /// read was written (M/D/YY) and the server's note for a read that
        /// hasn't been refreshed. All optional: older servers omit them.
        let recKey: String?
        let answered: Bool?
        let asOf: String?
        let staleNote: String?
        /// Figures in the cause the server could not trace to the data (M-17).
        let unsupportedFigures: [String]?

        struct OperationalEvidence: Decodable, Hashable {
            let module: String
            let metric: String
            let value: String
        }
        enum CodingKeys: String, CodingKey {
            case headline, cause, confidence, stale, answered
            case recKey = "rec_key"
            case asOf = "as_of"
            case staleNote = "stale_note"
            case unsupportedFigures = "unsupported_figures"
            case alternativeCause = "alternative_cause"
            case whatWouldConfirm = "what_would_confirm"
            case recommendedAction = "recommended_action"
            case expectedOutcome = "expected_outcome"
            case dollarsAtStake = "dollars_at_stake"
            case operationalEvidence = "operational_evidence"
            case ageHours = "age_hours"
        }
        /// The band — K1's, else the legacy one, else low.
        var confidenceBand: String { confidence?.effectiveBand ?? "low" }
    }

    /// Month-to-date prime cost projected to month end. Always a forecast,
    /// always with its basis, and absent entirely when any of COGS, labor or
    /// POS sales is missing — a projection built on a substituted zero looks
    /// exactly like a real one.
    struct Profitability: Decodable {
        let available: Bool
        let reason: String?
        let primeCostPct: Double?
        let foodCostPct: Double?
        let laborPct: Double?
        let projectedSales: Double?
        let projectedPrimeCost: Double?
        let prevMonthPrimePct: Double?
        let dollarsVsLastMonth: Double?
        let direction: String?
        let daysElapsed: Int?
        let basis: String?
        enum CodingKeys: String, CodingKey {
            case available, reason, direction, basis
            case primeCostPct = "prime_cost_pct"
            case foodCostPct = "food_cost_pct"
            case laborPct = "labor_pct"
            case projectedSales = "projected_sales"
            case projectedPrimeCost = "projected_prime_cost"
            case prevMonthPrimePct = "prev_month_prime_pct"
            case dollarsVsLastMonth = "dollars_vs_last_month"
            case daysElapsed = "days_elapsed"
        }
    }

    struct Brief: Decodable {
        let trust: Trust?
        /// K8 — how the waste forecasts have held up. Read from the brief,
        /// else from `trust.forecast_accuracy` where the server carried it
        /// before K8.
        let forecastAccuracy: ForecastAccuracy?

        enum CodingKeys: String, CodingKey {
            case trust
            case forecastAccuracy = "forecast_accuracy"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            trust = try? c.decodeIfPresent(Trust.self, forKey: .trust)
            forecastAccuracy = (try? c.decodeIfPresent(ForecastAccuracy.self, forKey: .forecastAccuracy))
                ?? trust?.forecastAccuracy
        }

        struct Trust: Decodable {
            let recipeCoveragePct: Double?
            let inferredWastePct: Double?
            let forecastAccuracy: ForecastAccuracy?
            enum CodingKeys: String, CodingKey {
                case recipeCoveragePct = "recipe_coverage_pct"
                case inferredWastePct = "inferred_waste_pct"
                case forecastAccuracy = "forecast_accuracy"
            }
            init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                recipeCoveragePct = try? c.decodeIfPresent(Double.self, forKey: .recipeCoveragePct)
                inferredWastePct = try? c.decodeIfPresent(Double.self, forKey: .inferredWastePct)
                forecastAccuracy = try? c.decodeIfPresent(ForecastAccuracy.self, forKey: .forecastAccuracy)
            }
        }
    }
}

/// How a forecast has held up here (K8): `{reading, mean_error_pct,
/// n_weeks, withheld}`. The pre-K8 server shape (`available`, `scored`) is
/// read too. Every field lenient.
struct ForecastAccuracy: Decodable, Equatable, Sendable {
    let reading: String?
    let meanErrorPct: Double?
    let nWeeks: Int?
    let withheld: Bool

    enum CodingKeys: String, CodingKey {
        case reading, withheld, available, scored
        case meanErrorPct = "mean_error_pct"
        case nWeeks = "n_weeks"
    }

    init(reading: String?, meanErrorPct: Double?, nWeeks: Int?, withheld: Bool = false) {
        self.reading = reading; self.meanErrorPct = meanErrorPct; self.nWeeks = nWeeks; self.withheld = withheld
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        reading = try? c.decodeIfPresent(String.self, forKey: .reading)
        meanErrorPct = try? c.decodeIfPresent(Double.self, forKey: .meanErrorPct)
        nWeeks = (try? c.decodeIfPresent(Int.self, forKey: .nWeeks)) ?? (try? c.decodeIfPresent(Int.self, forKey: .scored)) ?? nil
        let available = try? c.decodeIfPresent(Bool.self, forKey: .available)
        withheld = ((try? c.decodeIfPresent(Bool.self, forKey: .withheld)) ?? nil) ?? (available == false)
    }

    /// "Past forecasts here have been roughly right (22% mean error over 6
    /// weeks)" — nil when withheld or there is no reading.
    var sentence: String? {
        guard !withheld, let reading, !reading.isEmpty else { return nil }
        var detail: [String] = []
        if let e = meanErrorPct { detail.append("\(Int(e.rounded()))% mean error") }
        if let n = nWeeks, n > 0 { detail.append("over \(n) week\(n == 1 ? "" : "s")") }
        return "Past forecasts here have been \(reading)" + (detail.isEmpty ? "" : " (\(detail.joined(separator: " ")))")
    }
}

// MARK: - Prices to revisit

/// GET /mobile/api/food-cost/reprice — for every dish an ingredient price
/// rise hit, the price that restores its food cost % (menu_intelligence
/// .reprice_suggestions). A suggestion the owner already answered (applied,
/// or said no to) never arrives.
struct RepriceSuggestions: Decodable {
    let ok: Bool
    let available: Bool?
    let suggestions: [Suggestion]?
    /// Stated with every result: the costs already carry the new price, and
    /// the suggestion is a starting point.
    let assumption: String?
    /// Why there is nothing to show, when there isn't.
    let reason: String?

    struct Suggestion: Decodable, Identifiable, Equatable {
        let dish: String
        let menuItemId: Int?
        let sellPrice: Double?
        /// Nil when no price could be computed — then there is nothing to
        /// set in one tap.
        let suggestedPrice: Double?
        let priceChange: Double?
        /// Dollars a month, or nil when there is no sales mix yet (the
        /// per-plate figure is then the only one) — never read as $0.
        let monthlyMarginLost: Double?
        let monthlyBasis: String?
        let increasePerPlate: Double?
        let foodCostPctBefore: Double?
        let foodCostPctNow: Double?
        let drivers: [Driver]?
        let recKey: String?

        var id: String { dish }

        struct Driver: Decodable, Equatable, Hashable {
            let ingredient: String
            let oldPrice: Double?
            let newPrice: Double?
            let changePct: Double?
            let perPlate: Double?
            enum CodingKeys: String, CodingKey {
                case ingredient
                case oldPrice = "old_price"
                case newPrice = "new_price"
                case changePct = "change_pct"
                case perPlate = "per_plate"
            }
        }

        enum CodingKeys: String, CodingKey {
            case dish, drivers
            case menuItemId = "menu_item_id"
            case sellPrice = "sell_price"
            case suggestedPrice = "suggested_price"
            case priceChange = "price_change"
            case monthlyMarginLost = "monthly_margin_lost"
            case monthlyBasis = "monthly_basis"
            case increasePerPlate = "increase_per_plate"
            case foodCostPctBefore = "food_cost_pct_before"
            case foodCostPctNow = "food_cost_pct_now"
            case recKey = "rec_key"
        }

        /// "Parmesan +18%" — the ingredient that moved most, or nil.
        var whyLine: String? {
            let ranked = (drivers ?? []).sorted { ($0.perPlate ?? 0) > ($1.perPlate ?? 0) }
            guard let top = ranked.first else { return nil }
            var s = top.ingredient
            if let pct = top.changePct {
                s += " \(pct >= 0 ? "+" : "")\(Int(pct.rounded()))%"
            }
            if ranked.count > 1 { s += " and \(ranked.count - 1) more" }
            return s
        }
    }

    enum CodingKeys: String, CodingKey {
        case ok, available, suggestions, assumption, reason
    }
}

/// POST /mobile/api/food-cost/reprice/apply → the price actually set.
struct RepriceApplyResult: Decodable {
    let ok: Bool
    let dish: String?
    let menuItemId: Int?
    let oldPrice: Double?
    let suggestedPrice: Double?
    let price: Double?
    let recKey: String?
    /// True when an outcome tracker started for this change.
    let tracked: Bool?
    let error: String?
    /// What the new price is measured on until when, or why nothing is
    /// (API_REFERENCE → Tracker-start replies). Optional: older servers.
    var tracker: RecTracker? = nil
    var trackerRefused: RecTrackerRefused? = nil

    enum CodingKeys: String, CodingKey {
        case ok, dish, price, tracked, error, tracker
        case menuItemId = "menu_item_id"
        case oldPrice = "old_price"
        case suggestedPrice = "suggested_price"
        case recKey = "rec_key"
        case trackerRefused = "tracker_refused"
    }
}
