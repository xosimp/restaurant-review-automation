import Foundation
import Observation

struct LaborTrendWeek: Decodable, Identifiable {
    let label: String
    let pct: Double
    let labor: Double
    let sales: Double
    let start: String
    let end: String

    var id: String { label }
}

@Observable
@MainActor
final class LaborAnalyticsViewModel {
    var trend: [LaborTrendWeek] = []
    var insight: AIInsight?
    var isLoadingInsight = false
    var isLoading = false

    // Whether the performance chart's grow-up-from-zero bar reveal has
    // already played. Lives here (not as the chart's own local @State)
    // for the same reason the dropdown expand states moved to
    // LaborViewModel — Overview/Analytics is an if/else branch in
    // LaborView, so a view-local flag would replay every single time the
    // user switches back to Analytics. This view model instance survives
    // that switch; it only resets when LaborView itself is torn down and
    // rebuilt (e.g. the Face ID lock/unlock cycle), which is exactly when
    // a fresh "first load" reveal is actually wanted.
    var hasPlayedBarIntro = false

    private let client: APIClient
    private var restaurantId: Int?

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct TrendResponse: Decodable {
        let ok: Bool
        let weeks: [LaborTrendWeek]
    }

    /// Shows the last insight this device has seen for this restaurant
    /// immediately, before the network round-trip resolves — the backend
    /// already caches the actual AI-generated text server-side, so a
    /// relaunch was never re-running a Claude call, but it was still a
    /// blank skeleton flash every time while re-fetching the same cached
    /// string. This just removes that flash.
    func configureCaching(restaurantId: Int) {
        self.restaurantId = restaurantId
        guard let data = UserDefaults.standard.data(forKey: Self.insightCacheKey(restaurantId)),
              let cached = try? JSONDecoder.cavnar.decode(AIInsight.self, from: data) else { return }
        insight = cached
    }

    private static func insightCacheKey(_ restaurantId: Int) -> String { "labor.cachedInsight.\(restaurantId)" }

    private func cacheInsight(_ insight: AIInsight) {
        guard let restaurantId, let data = try? JSONEncoder.cavnar.encode(insight) else { return }
        UserDefaults.standard.set(data, forKey: Self.insightCacheKey(restaurantId))
    }

    func load() async {
        isLoading = true
        isLoadingInsight = true
        defer { isLoading = false }

        // /labor/gap was dropped from here — its single "monthly gap" figure
        // is now fully subsumed by the richer savings_breakdown tiles
        // (LaborStats, fetched by LaborViewModel) the Analytics tab renders
        // instead; showing both was two overlapping "here's your overspend"
        // widgets side by side. The route itself is untouched — nothing
        // else currently depends on removing it too.
        async let trendResult: TrendResponse? = try? client.send("/mobile/api/labor/trend")
        async let insightResult: AIInsight? = try? client.send("/mobile/api/labor/insight")

        trend = await trendResult?.weeks ?? []
        if let fresh = await insightResult {
            insight = fresh
            cacheInsight(fresh)
        }
        isLoadingInsight = false
    }
}
