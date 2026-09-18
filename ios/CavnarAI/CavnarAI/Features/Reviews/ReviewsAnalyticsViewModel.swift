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
    /// How confident the rating direction is (`high`/`medium`/`low`), or nil
    /// when there is no direction to be confident about.
    var trendConfidence: String?
    /// True when this passage is the last completed read rather than a fresh
    /// one, with `insightAsOf` saying when it was written.
    var insightIsStale = false
    var insightAsOf: String?
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
        let confidence: String?
        let stale: Bool?
        let asOf: String?

        enum CodingKeys: String, CodingKey {
            case ok, insight, diagnosis, confidence, stale, severity
            case figuresVerified = "figures_verified"
            case unsupportedFigures = "unsupported_figures"
            case namesVerified = "names_verified"
            case unsupportedNames = "unsupported_names"
            case revenueAtRisk = "revenue_at_risk"
            case asOf = "as_of"
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
        diagnosis = insightPayload?.diagnosis
        revenueAtRisk = (insightPayload?.revenueAtRisk?.available == true)
            ? insightPayload?.revenueAtRisk : nil
        // Only tiers with something still open. A tier with nothing in it is
        // not a reassurance worth a chip, it is noise between the ones that
        // matter.
        severityTiers = (insightPayload?.severity?.tiers ?? []).filter { $0.open > 0 }
        trendConfidence = insightPayload?.confidence
        insightIsStale = insightPayload?.stale ?? false
        insightAsOf = insightPayload?.asOf
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
    let confidence: String?
    let ageHours: Double?
    let stale: Bool?

    struct OperationalEvidence: Decodable, Sendable, Equatable {
        let module: String
        let metric: String
        let value: String
    }

    enum CodingKeys: String, CodingKey {
        case category, cause, confidence, stale
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

    /// How serious to treat this read. Mirrors the web's three-band chip.
    var confidenceBand: String { (confidence ?? "low").lowercased() }
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
