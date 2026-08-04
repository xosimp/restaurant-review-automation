import Foundation
import Observation

@Observable
@MainActor
final class ReviewsAnalyticsViewModel {
    var performance: ResponsePerformance?
    var heatmap: [TopicHeatmapEntry] = []
    var sentimentWeeks: [SentimentWeek] = []
    var insight: String?
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct DataResponse<T: Decodable>: Decodable {
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

        async let performanceResult: DataResponse<ResponsePerformance>? = try? client.send(
            "/mobile/api/reviews/response-performance"
        )
        async let heatmapResult: DataResponse<[TopicHeatmapEntry]>? = try? client.send(
            "/mobile/api/reviews/topic-heatmap"
        )
        async let weeksResult: WeeksResponse? = try? client.send("/mobile/api/reviews/sentiment-trend")
        async let insightResult: InsightResponse? = try? client.send("/mobile/api/reviews/insight")

        performance = await performanceResult?.data
        heatmap = await heatmapResult?.data ?? []
        sentimentWeeks = await weeksResult?.weeks ?? []
        insight = await insightResult?.insight
    }
}
