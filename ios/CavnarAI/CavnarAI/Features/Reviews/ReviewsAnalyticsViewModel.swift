import Foundation
import Observation

@Observable
@MainActor
final class ReviewsAnalyticsViewModel {
    var platforms: [PlatformBreakdown] = []
    var performance: ResponsePerformance?
    var heatmap: [TopicHeatmapEntry] = []
    var sentimentWeeks: [SentimentWeek] = []
    var topicWeeks: TopicWeeks?
    var insight: String?
    var isLoading = false
    var errorMessage: String?

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
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }

        async let platformsResult: DataResponse<[PlatformBreakdown]>? = try? client.send(
            "/mobile/api/reviews/platform-breakdown"
        )
        async let performanceResult: DataResponse<ResponsePerformance>? = try? client.send(
            "/mobile/api/reviews/response-performance"
        )
        async let heatmapResult: DataResponse<[TopicHeatmapEntry]>? = try? client.send(
            "/mobile/api/reviews/topic-heatmap"
        )
        async let weeksResult: WeeksResponse? = try? client.send("/mobile/api/reviews/sentiment-trend")
        async let insightResult: InsightResponse? = try? client.send("/mobile/api/reviews/insight")
        async let topicWeeksResult: DataResponse<TopicWeeks>? = try? client.send("/mobile/api/reviews/topic-weeks")

        platforms = await platformsResult?.data ?? []
        performance = await performanceResult?.data
        heatmap = await heatmapResult?.data ?? []
        sentimentWeeks = await weeksResult?.weeks ?? []
        insight = await insightResult?.insight
        topicWeeks = await topicWeeksResult?.data
    }
}
