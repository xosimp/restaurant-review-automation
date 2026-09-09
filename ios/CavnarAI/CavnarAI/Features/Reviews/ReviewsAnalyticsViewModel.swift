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

        enum CodingKeys: String, CodingKey {
            case ok, insight
            case figuresVerified = "figures_verified"
            case unsupportedFigures = "unsupported_figures"
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
        topicWeeks = await topicWeeksResult?.data
    }
}
