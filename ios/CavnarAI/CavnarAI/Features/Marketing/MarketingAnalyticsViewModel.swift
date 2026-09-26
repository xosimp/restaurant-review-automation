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
    /// "Metrics synced 9/21/26", tone warn when the nightly Meta pull is
    /// stale or failing (DH4-8). Lenient; absent on an older server.
    var metricsSyncField: LenientStatusLine? = nil

    var metricsSync: ServerStatusLine? { metricsSyncField?.value }

    enum CodingKeys: String, CodingKey {
        case ok, published
        case hasData = "has_data"
        case totalReach = "total_reach"
        case totalEngagement = "total_engagement"
        case topPost = "top_post"
        case metricsSyncField = "metrics_sync"
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

    /// Windowed and compared, which the all-time totals never were.
    var window: MarketingWindow?
    var windowDays = 30
    /// What a post did to the till. Correlational, and said so on screen.
    var attribution: MarketingAttribution?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct RecentTopicsResponse: Decodable {
        let ok: Bool
        let topics: [MarketingRecentTopic]
    }

    /// The five answers, as one cached envelope: each part is the server's
    /// own body under its name (ResponseCache), `window_days` says which
    /// window the window part measured.
    private struct CachedAnalytics: Decodable {
        let windowDays: Int?
        let performance: MarketingPerformance?
        let insight: AIInsight?
        let topics: RecentTopicsResponse?
        let window: MarketingWindow?
        let attribution: MarketingAttribution?
        enum CodingKeys: String, CodingKey {
            case performance, insight, topics, window, attribution
            case windowDays = "window_days"
        }
    }
    @ObservationIgnored private let cache = ResponseCache<CachedAnalytics>("marketing.analytics")
    /// When the cached copy on screen was stored; nil once a live load lands.
    private(set) var cachedAt: Date?
    var stalenessNotice: String? { CacheFreshness.notice(savedAt: cachedAt) }

    func load() async {
        let generation = SessionScope.generation
        if performance == nil, let hit = await cache.load() {
            performance = hit.value.performance
            insight = hit.value.insight
            recentTopics = hit.value.topics?.topics ?? []
            if hit.value.windowDays == windowDays { window = hit.value.window }
            attribution = hit.value.attribution
            cachedAt = hit.savedAt
        }
        isLoading = true
        isLoadingInsight = true
        defer { isLoading = false }

        async let performanceResult: (value: MarketingPerformance, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/performance")
        async let insightResult: (value: AIInsight, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/insight")
        async let topicsResult: (value: RecentTopicsResponse, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/recent-topics")
        let days = windowDays
        async let windowResult: (value: MarketingWindow, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/performance-window", query: ["days": String(days)])
        async let attributionResult: (value: MarketingAttribution, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/attribution")

        let (p, i, t, w, a) = await (performanceResult, insightResult, topicsResult, windowResult, attributionResult)
        // A part that failed keeps what is on screen (cached or earlier)
        // rather than blanking a card that had numbers a moment ago.
        performance = p?.value ?? performance
        insight = i?.value ?? insight
        if let t { recentTopics = t.value.topics }
        window = w?.value ?? window
        attribution = a?.value ?? attribution
        isLoadingInsight = false
        guard let p else { return }
        cachedAt = nil
        let body = CacheEnvelope.make([("performance", p.body), ("insight", i?.body), ("topics", t?.body),
                                       ("window", w?.body), ("attribution", a?.body)],
                                      numbers: [("window_days", days)])
        cache.save(body, generation: generation)
    }

    private struct RefreshResponse: Decodable {
        let ok: Bool
        let refreshed: Int?
    }

    /// Pull fresh numbers from Meta, then reread. The web tab polls this every
    /// 60 seconds while it is open; the app never asked at all, so the metrics
    /// an owner saw the evening they posted were whatever the nightly job had
    /// last written — which is to say, zero.
    func setWindow(_ days: Int) async {
        windowDays = days
        window = try? await client.send("/mobile/api/marketing/performance-window",
                                        query: ["days": String(days)])
    }

    func refresh() async {
        isRefreshingMetrics = true
        defer { isRefreshingMetrics = false }
        _ = try? await client.send("/mobile/api/marketing/refresh-metrics",
                                   method: .post) as RefreshResponse
        await load()
    }
}
