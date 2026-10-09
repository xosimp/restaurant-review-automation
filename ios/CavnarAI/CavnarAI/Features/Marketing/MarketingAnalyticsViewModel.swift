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
    /// One definition of the best post (AUX-13): named only once this many
    /// posts are measured; `measuredPosts` so far. Absent on an older server.
    var topPostFloor: Int? = nil
    var measuredPosts: Int? = nil

    var metricsSync: ServerStatusLine? { metricsSyncField?.value }

    enum CodingKeys: String, CodingKey {
        case ok, published
        case hasData = "has_data"
        case totalReach = "total_reach"
        case totalEngagement = "total_engagement"
        case topPost = "top_post"
        case metricsSyncField = "metrics_sync"
        case topPostFloor = "top_post_floor"
        case measuredPosts = "measured_posts"
    }

    /// "Fall menu · instagram" — the topic cut at 60, as the web cuts it.
    var topPostTitle: String? {
        guard let top = topPost else { return nil }
        var topic = (top.topic ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        if topic.count > 60 { topic = String(topic.prefix(60)).trimmingCharacters(in: .whitespaces) + "\u{2026}" }
        if let platform = top.platform, !platform.isEmpty { return topic.isEmpty ? platform : "\(topic) \u{00B7} \(platform)" }
        return topic.isEmpty ? nil : topic
    }

    /// "1,204 reach · 38 likes · 4 comments · 2 shares", what is above zero.
    var topPostMetrics: String? {
        guard let top = topPost else { return nil }
        var parts: [String] = []
        if top.reach > 0 { parts.append("\(top.reach.formatted()) reach") }
        if top.likes > 0 { parts.append("\(top.likes) likes") }
        if top.comments > 0 { parts.append("\(top.comments) comments") }
        if top.shares > 0 { parts.append("\(top.shares) shares") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    /// Without a top post: when one will be named.
    var topPostWaitLine: String? {
        guard topPost == nil, let floor = topPostFloor else { return nil }
        return "A top post is named once \(floor) posts are measured \u{2014} \(measuredPosts ?? 0) so far."
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
    /// GET /mobile/api/marketing/diagnosis (strategy_routes, both prefixes):
    /// why one guest text did better than another — the web shows it under
    /// the brief as its most likely cause; the phone never read it.
    var diagnosis: LaborDiagnosis?

    private struct DiagnosisResponse: Decodable {
        let ok: Bool
        let diagnosis: LaborDiagnosis?
    }
    var isLoadingInsight = false
    /// The server's sentence when the brief it was writing couldn't be
    /// written (InsightRefresh.follow, re-audit 10/8/26 #3).
    var insightError: String?
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
        // Asked stale-while-refresh (parity #37): a brief the server has not
        // written for these figures answers at once — pending, or the last
        // brief with its age — and is followed below.
        async let insightResult: (value: AIInsight, body: Data, refresh: APIClient.InsightRefreshState?)? =
            try? client.sendInsight(Self.insightPath)
        async let topicsResult: (value: RecentTopicsResponse, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/recent-topics")
        let days = windowDays
        async let windowResult: (value: MarketingWindow, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/performance-window", query: ["days": String(days)])
        async let attributionResult: (value: MarketingAttribution, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/marketing/attribution")
        async let diagnosisResult: DiagnosisResponse? = try? client.send(
            "/mobile/api/marketing/diagnosis", hapticOnError: false)

        let (p, t, w, a) = await (performanceResult, topicsResult, windowResult, attributionResult)
        // A placeholder (pending) is never shown as the brief, nor cached.
        let firstInsight = await insightResult
        let i = firstInsight?.refresh?.isPending == true ? nil : firstInsight
        // A part that failed keeps what is on screen (cached or earlier)
        // rather than blanking a card that had numbers a moment ago.
        performance = p?.value ?? performance
        insight = i?.value ?? insight
        if let t { recentTopics = t.value.topics }
        window = w?.value ?? window
        attribution = a?.value ?? attribution
        diagnosis = await diagnosisResult?.diagnosis ?? diagnosis
        isLoadingInsight = firstInsight?.refresh?.isPending == true
        if let p {
            cachedAt = nil
            let body = CacheEnvelope.make([("performance", p.body), ("insight", i?.body), ("topics", t?.body),
                                           ("window", w?.body), ("attribution", a?.body)],
                                          numbers: [("window_days", days)])
            cache.save(body, generation: generation)
        }
        isLoading = false
        if let state = firstInsight?.refresh, state.isWaiting {
            await InsightRefresh.follow(Self.insightPath, from: state, client: client,
                                        failed: { self.insightError = $0 }) {
                (fresh: AIInsight) in insight = fresh; insightError = nil
            }
        }
        isLoadingInsight = false
    }

    static let insightPath = "/mobile/api/marketing/insight"

    private struct RefreshResponse: Decodable {
        let ok: Bool
        let refreshed: Int?
    }

    /// Pull fresh numbers from Meta, then reread. The web tab polls this every
    /// 60 seconds while it is open; the app never asked at all, so the metrics
    /// an owner saw the evening they posted were whatever the nightly job had
    /// last written — which is to say, zero.
    /// Why the period the owner picked couldn't load, or nil.
    var windowError: String?

    /// A period that fails to load keeps the figures and the period on
    /// screen and says so (re-audit 10/8/26 L17) — a failure used to blank
    /// the stats with no word why.
    func setWindow(_ days: Int) async {
        let previous = windowDays
        windowDays = days
        windowError = nil
        do {
            let fresh: MarketingWindow = try await client.send("/mobile/api/marketing/performance-window",
                                                               query: ["days": String(days)], hapticOnError: false)
            window = fresh
        } catch let error as APIClient.APIError {
            windowDays = previous
            windowError = error.message
        } catch {
            windowDays = previous
            windowError = "Couldn\u{2019}t load the last \(days) days."
        }
    }

    func refresh() async {
        isRefreshingMetrics = true
        defer { isRefreshingMetrics = false }
        _ = try? await client.send("/mobile/api/marketing/refresh-metrics",
                                   method: .post) as RefreshResponse
        await load()
    }
}
