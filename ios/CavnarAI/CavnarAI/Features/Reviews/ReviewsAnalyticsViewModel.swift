import Foundation
import Observation

@Observable
@MainActor
final class ReviewsAnalyticsViewModel {
    var performance: ResponsePerformance?
    var heatmap: [TopicHeatmapEntry] = []
    var sentimentWeeks: [SentimentWeek] = []
    var topicWeeks: TopicWeeks?
    var insight: String?
    /// Set when the backend flagged figures in `insight` it could not trace
    /// back to this restaurant's data — the view renders a caveat above it
    /// rather than showing the number as fact.
    var unsupportedFigures: [String] = []
    /// Names the passage used that were never in what the model was handed.
    /// A fabricated guest name is the most damaging thing this card can get
    /// wrong — the whole premise is that Cavnar has read the reviews — and
    /// the figure check cannot see it, because a name is not a figure.
    var unsupportedNames: [String] = []
    /// H2: the read gave a reason no measured signal backs — the view says
    /// so above it (CavnarCaveat.unverifiedCauses).
    var causesUnverified = false
    var unsupportedCauses: [String] = []
    /// H8: next week's rating, computed from the fitted trend (not the
    /// model's words). Nil when there is no trend to carry forward.
    var ratingForecast: ComputedForecast?
    /// The root-cause read: the step after "food quality is your
    /// most-mentioned complaint". Nil when no complaint cluster clears the
    /// evidence floor, which is a state the UI shows as nothing at all
    /// rather than as a cause invented to fill the card.
    var diagnosis: ReviewDiagnosis?
    /// The monthly revenue range implied by a rating move, or nil. Always a
    /// forecast, always shown with its inputs.
    var revenueAtRisk: RevenueAtRisk?
    /// Open complaints by severity tier — how serious, not how many.
    var severityTiers: [SeverityTier] = []
    /// Complaints no tier was assigned to (H14) — shown beside the tiers so
    /// the strip never implies it covers every complaint.
    var unclassifiedCount: Int = 0
    /// How confident the rating direction is (`high`/`medium`/`low`), or nil
    /// when there is no direction to be confident about.
    var trendConfidence: String?
    /// The rating trend itself (review_intelligence.rating_trend): its
    /// direction and the ★ change, shown beside `trendConfidence`.
    var ratingTrend: ReviewRatingTrend?
    /// What kind of claim each part of the read is (ai_guard.CLAIM_KINDS):
    /// this_week measured, watch inferred, do_today suggestion, next_week
    /// forecast, why inferred, revenue_at_risk forecast, …
    var claimKinds: [String: String] = [:]
    /// True when this passage is the last completed read rather than a fresh
    /// one, with `insightAsOf` saying when it was written.
    var insightIsStale = false
    var insightAsOf: String?
    /// The read's own recommendation lines, each with its rec_ledger key so
    /// the owner can answer it (the "Do today" line today). A line already
    /// answered never arrives — the server drops it from `insight` too.
    var insightRecs: [ReviewInsightRec] = []
    var isLoading = false
    var errorMessage: String?
    /// 30 / 90 / 180 — the same three windows as the web's analytics tab.
    /// Response performance and the topic grid take it; the 8-week
    /// sentiment river and the insight are fixed-window by design.
    var windowDays = 90

    func setWindow(_ days: Int) async {
        guard days != windowDays else { return }
        windowDays = days
        await load()
    }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Sendable, because `load()` fetches three of these concurrently with
    /// `async let` — the results cross out of the APIClient actor's isolation
    /// domain, which is an error in the Swift 6 language mode unless the type
    /// is Sendable. All stored properties are immutable value types, so the
    /// conformance is real rather than an @unchecked escape hatch.
    private struct DataResponse<T: Decodable & Sendable>: Decodable, Sendable {
        let ok: Bool
        let data: T?
        let error: String?
    }

    private struct WeeksResponse: Decodable {
        let ok: Bool
        let weeks: [SentimentWeek]
    }

    private struct InsightResponse: Decodable {
        let ok: Bool
        let insight: String?
        /// False when the backend could not trace every figure in the passage
        /// back to the data it handed the model. Optional: an older server
        /// doesn't send it, and absent means "not flagged", never "suspect".
        let figuresVerified: Bool?
        let unsupportedFigures: [String]?
        let namesVerified: Bool?
        let unsupportedNames: [String]?
        let diagnosis: ReviewDiagnosis?
        let revenueAtRisk: RevenueAtRisk?
        let severity: SeverityBreakdown?
        /// The rating trend's own confidence — a band today; read through
        /// TrustConfidence so a K1 object here decodes too.
        let confidence: TrustConfidence?
        let stale: Bool?
        let asOf: String?
        /// Optional: an older server sends no recs, and absent means "no
        /// answer controls", never an error.
        let recs: [ReviewInsightRec]?
        let claimKinds: ClaimKindMap?
        let trend: ReviewRatingTrend?
        /// H2: false when the read states a cause no stored diagnosis backs
        /// (the sentences are in `unsupportedCauses`); H8: next week's rating
        /// forecast, computed in Python rather than written by the model.
        /// Optional: absent means "not flagged" / no forecast.
        var causesVerified: Bool? = nil
        var unsupportedCauses: [String]? = nil
        var forecast: ComputedForecast? = nil

        enum CodingKeys: String, CodingKey {
            case ok, insight, diagnosis, confidence, stale, severity, recs, trend, forecast
            case causesVerified = "causes_verified"
            case unsupportedCauses = "unsupported_causes"
            case figuresVerified = "figures_verified"
            case unsupportedFigures = "unsupported_figures"
            case namesVerified = "names_verified"
            case unsupportedNames = "unsupported_names"
            case revenueAtRisk = "revenue_at_risk"
            case asOf = "as_of"
            case claimKinds = "claim_kinds"
        }
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        let window = ["days": "\(windowDays)"]
        async let performanceResult: DataResponse<ResponsePerformance>? = try? client.send(
            "/mobile/api/reviews/response-performance", query: window
        )
        async let heatmapResult: DataResponse<[TopicHeatmapEntry]>? = try? client.send(
            "/mobile/api/reviews/topic-heatmap", query: window
        )
        async let weeksResult: WeeksResponse? = try? client.send("/mobile/api/reviews/sentiment-trend")
        async let insightResult: InsightResponse? = try? client.send("/mobile/api/reviews/insight")
        async let topicWeeksResult: DataResponse<TopicWeeks>? = try? client.send("/mobile/api/reviews/topic-weeks")

        performance = await performanceResult?.data
        heatmap = await heatmapResult?.data ?? []
        sentimentWeeks = await weeksResult?.weeks ?? []
        let insightPayload = await insightResult
        insight = insightPayload?.insight
        unsupportedFigures = (insightPayload?.figuresVerified == false)
            ? (insightPayload?.unsupportedFigures ?? [])
            : []
        unsupportedNames = (insightPayload?.namesVerified == false)
            ? (insightPayload?.unsupportedNames ?? [])
            : []
        causesUnverified = insightPayload?.causesVerified == false
        unsupportedCauses = causesUnverified ? (insightPayload?.unsupportedCauses ?? []) : []
        ratingForecast = insightPayload?.forecast
        diagnosis = insightPayload?.diagnosis
        revenueAtRisk = (insightPayload?.revenueAtRisk?.available == true)
            ? insightPayload?.revenueAtRisk : nil
        // Only tiers with something still open. A tier with nothing in it is
        // not a reassurance worth a chip, it is noise between the ones that
        // matter.
        severityTiers = (insightPayload?.severity?.tiers ?? []).filter { $0.open > 0 }
        unclassifiedCount = max(0, insightPayload?.severity?.unclassified ?? 0)
        let trendBand = insightPayload?.confidence
        trendConfidence = (trendBand?.band != nil || trendBand?.pct != nil) ? trendBand?.effectiveBand : nil
        ratingTrend = insightPayload?.trend
        claimKinds = insightPayload?.claimKinds?.kinds ?? [:]
        insightIsStale = insightPayload?.stale ?? false
        insightAsOf = insightPayload?.asOf
        insightRecs = insightPayload?.recs ?? []
        topicWeeks = await topicWeeksResult?.data
    }
}

// MARK: - The consultant layer's payload

/// One stored root-cause diagnosis. Everything here was produced by
/// `review_intelligence.diagnose`, which is required to cite review ids that
/// exist and to offer an alternative explanation — so `evidenceReviewIds`
/// always points at reviews the owner can actually open, and `cause` is never
/// the only explanation on offer.
struct ReviewDiagnosis: Decodable, Sendable, Equatable {
    let category: String
    let mentionCount: Int
    let windowDays: Int
    let cause: String
    let alternativeCause: String?
    let whatWouldConfirm: String?
    let recommendedAction: String?
    let expectedOutcome: String?
    let evidenceReviewIds: [Int]
    let operationalEvidence: [OperationalEvidence]
    /// K6: the K1 object (a percentage with "Why?"); an older server's bare
    /// band ("medium") still decodes.
    let confidence: TrustConfidence?
    /// The K1 object when `confidence` stays the band word for older builds
    /// (confidence audit, group E). Views read `trust`.
    var confidenceDetail: TrustConfidence? = nil
    var trust: TrustConfidence? { TrustConfidence.measured(confidenceDetail, confidence) }
    let ageHours: Double?
    let stale: Bool?
    /// The recommended action's rec_ledger key — present only when there is
    /// an action to answer. This and the three below are optional so an
    /// older server or a cached payload still decodes.
    let recKey: String?
    /// True when the owner already answered the action: the card keeps its
    /// evidence but drops the action and its controls.
    let answered: Bool?
    /// When the read was written, already M/D/YY.
    let asOf: String?
    /// The server's own sentence for a read that hasn't been refreshed.
    let staleNote: String?
    /// Figures in the cause the server could not trace to the data (M-17).
    let unsupportedFigures: [String]?

    struct OperationalEvidence: Decodable, Sendable, Equatable {
        let module: String
        let metric: String
        let value: String
    }

    enum CodingKeys: String, CodingKey {
        case category, cause, confidence, stale, answered
        case confidenceDetail = "confidence_detail"
        case recKey = "rec_key"
        case asOf = "as_of"
        case staleNote = "stale_note"
        case unsupportedFigures = "unsupported_figures"
        case mentionCount = "mention_count"
        case windowDays = "window_days"
        case alternativeCause = "alternative_cause"
        case whatWouldConfirm = "what_would_confirm"
        case recommendedAction = "recommended_action"
        case expectedOutcome = "expected_outcome"
        case evidenceReviewIds = "evidence_review_ids"
        case operationalEvidence = "operational_evidence"
        case ageHours = "age_hours"
    }

    /// How serious to treat this read, as a band — the K1 band, else the
    /// legacy one ("moderate" reads as medium), else low.
    var confidenceBand: String { confidence?.effectiveBand ?? "low" }
}

/// review_intelligence.rating_trend, the part the phone shows: which way
/// the rating is moving, by how much, over how many weeks. Every field
/// lenient.
struct ReviewRatingTrend: Decodable, Sendable, Equatable {
    let direction: String?
    let change: Double?
    let weeksAboveFloor: Int?
    let reason: String?
    /// How steady the trend is, as a measured percentage (trend_strength_pct:
    /// the share of weekly moves agreeing with the slope × sample adequacy).
    /// Shown instead of a band word (B4 L2). Absent on an older server.
    var trendStrengthPct: Int? = nil

    enum CodingKeys: String, CodingKey {
        case direction, change, reason
        case weeksAboveFloor = "weeks_above_floor"
        case trendStrengthPct = "trend_strength_pct"
    }

    init(direction: String? = nil, change: Double? = nil, weeksAboveFloor: Int? = nil, reason: String? = nil,
         trendStrengthPct: Int? = nil) {
        self.direction = direction; self.change = change
        self.weeksAboveFloor = weeksAboveFloor; self.reason = reason
        self.trendStrengthPct = trendStrengthPct.map { max(0, min(100, $0)) }
    }

    /// Never throws — a trend that is not an object must not take the whole
    /// reviews read down with it.
    init(from decoder: Decoder) throws {
        guard let c = try? decoder.container(keyedBy: CodingKeys.self) else {
            self.init()
            return
        }
        let strength = ((try? c.decodeIfPresent(Double.self, forKey: .trendStrengthPct)) ?? nil)
            .flatMap { $0.isFinite ? Int($0.rounded()) : nil }
        self.init(direction: try? c.decodeIfPresent(String.self, forKey: .direction),
                  change: try? c.decodeIfPresent(Double.self, forKey: .change),
                  weeksAboveFloor: try? c.decodeIfPresent(Int.self, forKey: .weeksAboveFloor),
                  reason: try? c.decodeIfPresent(String.self, forKey: .reason),
                  trendStrengthPct: strength)
    }

    /// "Rating improving (+0.2★ over 9 weeks)" — nil without a direction.
    var sentence: String? {
        guard let dir = direction?.lowercased(), !dir.isEmpty else { return nil }
        let word: String
        switch dir {
        case "improving", "up": word = "improving"
        case "declining", "down": word = "declining"
        case "flat", "steady", "stable": word = "holding steady"
        default: word = dir.replacingOccurrences(of: "_", with: " ")
        }
        var s = "Rating " + word
        var bits: [String] = []
        if let change, abs(change) >= 0.05 {
            bits.append((change > 0 ? "+" : "\u{2212}") + String(format: "%.1f", abs(change)) + "\u{2605}")
        }
        if let w = weeksAboveFloor, w > 0 { bits.append("over \(w) week\(w == 1 ? "" : "s")") }
        if !bits.isEmpty { s += " (" + bits.joined(separator: " ") + ")" }
        return s
    }
}

/// One answerable line from the reviews read. `kind` is "do_today" for the
/// read's "Do today" line — the only kind the server promotes today.
struct ReviewInsightRec: Decodable, Sendable, Equatable, Identifiable {
    let key: String
    let text: String
    let kind: String?
    /// E13: the "Do today" line's OWN measured confidence (K1) — not the
    /// rating trend's band, which is what top-level `confidence` is.
    /// Absent on an older server.
    var confidenceDetail: TrustConfidence? = nil
    var id: String { key }

    enum CodingKeys: String, CodingKey {
        case key, text, kind
        case confidenceDetail = "confidence_detail"
    }

    init(key: String, text: String, kind: String?, confidenceDetail: TrustConfidence? = nil) {
        self.key = key; self.text = text; self.kind = kind; self.confidenceDetail = confidenceDetail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = try c.decode(String.self, forKey: .key)
        text = try c.decode(String.self, forKey: .text)
        kind = try? c.decodeIfPresent(String.self, forKey: .kind)
        confidenceDetail = try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidenceDetail)
    }
}

/// The monthly revenue range implied by a rating movement. `available` is
/// false — and every figure absent — whenever an input is missing, because a
/// revenue estimate with no revenue base is arithmetic on nothing.
struct RevenueAtRisk: Decodable, Sendable, Equatable {
    let available: Bool
    let reason: String?
    let direction: String?
    let ratingDelta: Double?
    let monthlyLow: Double?
    let monthlyHigh: Double?
    let salesSource: String?
    let assumption: String?

    enum CodingKeys: String, CodingKey {
        case available, reason, direction, assumption
        case ratingDelta = "rating_delta"
        case monthlyLow = "monthly_low"
        case monthlyHigh = "monthly_high"
        case salesSource = "sales_source"
    }
}

struct SeverityBreakdown: Decodable, Sendable, Equatable {
    let days: Int
    let tiers: [SeverityTier]
    let unclassified: Int?
}

struct SeverityTier: Decodable, Sendable, Equatable, Identifiable {
    let key: String
    let label: String
    let total: Int
    let open: Int
    var id: String { key }
}
