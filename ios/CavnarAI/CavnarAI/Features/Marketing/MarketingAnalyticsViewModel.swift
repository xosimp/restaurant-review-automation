import Foundation
import Observation

struct MarketingTopPost: Decodable {
    let topic: String?
    let platform: String?
    let reach: Int
    let likes: Int
    let comments: Int
    let shares: Int
}

struct MarketingPerformance: Decodable {
    let ok: Bool
    let published: Int
    let hasData: Bool
    let totalReach: Int
    let totalEngagement: Int
    let topPost: MarketingTopPost?

    enum CodingKeys: String, CodingKey {
        case ok, published
        case hasData = "has_data"
        case totalReach = "total_reach"
        case totalEngagement = "total_engagement"
        case topPost = "top_post"
    }
}

@Observable
@MainActor
final class MarketingAnalyticsViewModel {
    var performance: MarketingPerformance?
    var insight: AIInsight?
    var isLoadingInsight = false
    var isLoading = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        isLoadingInsight = true
        defer { isLoading = false }

        async let performanceResult: MarketingPerformance? = try? client.send("/mobile/api/marketing/performance")
        async let insightResult: AIInsight? = try? client.send("/mobile/api/marketing/insight")

        performance = await performanceResult
        insight = await insightResult
        isLoadingInsight = false
    }
}
