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
    // already played. Persisted to UserDefaults — this view model gets
    // recreated fresh every time a user leaves Labor entirely (e.g.
    // switches to another module) and comes back — an in-memory-only flag
    // survived an Overview/Analytics tab switch (this instance itself
    // doesn't change for that), but not a genuinely fresh navigation into
    // Labor, which is exactly the "reloads and counts up again" report this
    // fixes. Only ever reset by actually clearing UserDefaults (there's no
    // reason to replay this for a restaurant the device has already seen).
    var hasPlayedBarIntro = false

    // Same reasoning, same lifetime as hasPlayedBarIntro above — guards the
    // savings tiles' count-up-from-zero reveal.
    var hasPlayedTilesIntro = false

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
        hasPlayedBarIntro = UserDefaults.standard.bool(forKey: Self.barIntroPlayedKey(restaurantId))
        hasPlayedTilesIntro = UserDefaults.standard.bool(forKey: Self.tilesIntroPlayedKey(restaurantId))
        guard let data = SecureCache.read(key: Self.insightCacheKey(restaurantId)),
              let cached = try? JSONDecoder.cavnar.decode(AIInsight.self, from: data) else { return }
        insight = cached
    }

    private static func insightCacheKey(_ restaurantId: Int) -> String { "labor.cachedInsight.\(restaurantId)" }
    private static func barIntroPlayedKey(_ restaurantId: Int) -> String { "labor.hasPlayedBarIntro.\(restaurantId)" }
    private static func tilesIntroPlayedKey(_ restaurantId: Int) -> String { "labor.hasPlayedTilesIntro.\(restaurantId)" }

    private func cacheInsight(_ insight: AIInsight) {
        guard let restaurantId, let data = try? JSONEncoder.cavnar.encode(insight) else { return }
        SecureCache.write(data, key: Self.insightCacheKey(restaurantId))
    }

    func markBarIntroPlayed() {
        hasPlayedBarIntro = true
        guard let restaurantId else { return }
        UserDefaults.standard.set(true, forKey: Self.barIntroPlayedKey(restaurantId))
    }

    func markTilesIntroPlayed() {
        hasPlayedTilesIntro = true
        guard let restaurantId else { return }
        UserDefaults.standard.set(true, forKey: Self.tilesIntroPlayedKey(restaurantId))
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
