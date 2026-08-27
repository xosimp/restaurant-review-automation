import Foundation
import Observation

@Observable
@MainActor
final class FoodCostAnalyticsViewModel {
    var analytics: FoodCostAnalytics?
    var trend: [FoodCostTrendWeek] = []
    var isLoading = false
    // Drives the shared hero-forecast-ribbon pill (see
    // FoodCostQuickEntryView's .cavnarHeroForecastRibbon call) — matches
    // LaborViewModel.forecastExpanded exactly.
    var forecastExpanded = false

    // Whether the hero card's count-up-from-zero number reveal has already
    // played. Persisted to UserDefaults for the same reason Labor's own
    // hasPlayedTilesIntro is (LaborAnalyticsViewModel) — this view model
    // gets recreated fresh every time a user leaves Food Cost entirely and
    // comes back, so an in-memory-only flag wouldn't survive a genuine
    // fresh navigation into the module, which is exactly when this should
    // only ever count up once, not replay on every return visit.
    var hasPlayedHeroIntro = false

    private let client: APIClient
    private var restaurantId: Int?

    init(client: APIClient = .shared) {
        self.client = client
    }

    func configureCaching(restaurantId: Int) {
        self.restaurantId = restaurantId
        hasPlayedHeroIntro = UserDefaults.standard.bool(forKey: Self.heroIntroPlayedKey(restaurantId))
    }

    private static func heroIntroPlayedKey(_ restaurantId: Int) -> String { "foodcost.hasPlayedHeroIntro.\(restaurantId)" }

    func markHeroIntroPlayed() {
        hasPlayedHeroIntro = true
        guard let restaurantId else { return }
        UserDefaults.standard.set(true, forKey: Self.heroIntroPlayedKey(restaurantId))
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        // Two independent endpoints, loaded together — a trend-fetch
        // failure shouldn't block the rest of the tab from showing (the
        // chart just renders its own "not enough data" state), matching
        // how Labor's own trend fetch is similarly best-effort.
        async let analyticsResult: FoodCostAnalytics? = try? client.send("/mobile/api/food-cost/analytics")
        async let trendResult: FoodCostTrend? = try? client.send("/mobile/api/food-cost/trend")
        analytics = await analyticsResult
        trend = await trendResult?.weeks ?? []
    }
}
