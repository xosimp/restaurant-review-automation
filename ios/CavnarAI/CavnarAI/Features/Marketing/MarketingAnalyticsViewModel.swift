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

/// One generated piece, and what it did. The web Analytics tab has always
/// shown these; the app showed three all-time totals and nothing per piece,
/// so there was no way to tell WHICH post earned the reach.
struct MarketingRecentTopic: Decodable, Identifiable {
    let topic: String
    let posted: Bool
    let platform: String?
    let metrics: Metrics

    struct Metrics: Decodable {
        var reach: Int?
        var impressions: Int?
        var likes: Int?
        var comments: Int?
    }

    var id: String { topic }

    var reach: Int { metrics.reach ?? metrics.impressions ?? 0 }

    /// "1,204 seen · 38 likes · 4 comments", omitting whatever is zero.
    var metricsLine: String? {
        var parts: [String] = []
        if reach > 0 { parts.append("\(reach) seen") }
        if let likes = metrics.likes, likes > 0 { parts.append("\(likes) likes") }
        if let comments = metrics.comments, comments > 0 { parts.append("\(comments) comments") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    var platformLabel: String? {
        guard let platform, !platform.isEmpty else { return nil }
        return platform
            .replacingOccurrences(of: "instagram", with: "Instagram")
            .replacingOccurrences(of: "facebook", with: "Facebook")
            .replacingOccurrences(of: "google", with: "Google")
    }
}

@Observable
@MainActor
final class MarketingAnalyticsViewModel {
    var performance: MarketingPerformance?
    var recentTopics: [MarketingRecentTopic] = []
    var insight: AIInsight?
    var isLoadingInsight = false
    var isLoading = false
    var isRefreshingMetrics = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct RecentTopicsResponse: Decodable {
        let ok: Bool
        let topics: [MarketingRecentTopic]
    }

    func load() async {
        isLoading = true
        isLoadingInsight = true
        defer { isLoading = false }

        async let performanceResult: MarketingPerformance? = try? client.send("/mobile/api/marketing/performance")
        async let insightResult: AIInsight? = try? client.send("/mobile/api/marketing/insight")
        async let topicsResult: RecentTopicsResponse? = try? client.send("/mobile/api/marketing/recent-topics")

        performance = await performanceResult
        insight = await insightResult
        recentTopics = await topicsResult?.topics ?? []
        isLoadingInsight = false
    }

    private struct RefreshResponse: Decodable {
        let ok: Bool
        let refreshed: Int?
    }

    /// Pull fresh numbers from Meta, then reread. The web tab polls this every
    /// 60 seconds while it is open; the app never asked at all, so the metrics
    /// an owner saw the evening they posted were whatever the nightly job had
    /// last written — which is to say, zero.
    func refresh() async {
        isRefreshingMetrics = true
        defer { isRefreshingMetrics = false }
        _ = try? await client.send("/mobile/api/marketing/refresh-metrics",
                                   method: .post) as RefreshResponse
        await load()
    }
}
