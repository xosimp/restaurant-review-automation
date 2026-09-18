import Foundation
import Observation

@Observable
@MainActor
final class FoodCostAnalyticsViewModel {
    var analytics: FoodCostAnalytics?
    var trend: [FoodCostTrendWeek] = []
    var trendTarget: FoodCostTrendTarget?
    /// The CFO read — ranked cost drivers, the stored root cause, and the
    /// month-end prime-cost projection. Best-effort like the trend: a failure
    /// here removes one card, it does not empty the tab.
    var cfo: FoodCostCFO?
    var isLoading = false
    /// Why the last load failed, when it did. `try?` used to swallow the
    /// error and assign nil over previously good data, so a server failure
    /// rendered as an empty ScrollView with no message and no way to retry —
    /// the only recovery was leaving the module. MenuMarginsViewModel already
    /// does this correctly; this is the same shape.
    var errorMessage: String?
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
        isLoading = analytics == nil
        errorMessage = nil
        defer { isLoading = false }
        // Two independent endpoints, loaded together — a trend-fetch
        // failure shouldn't block the rest of the tab from showing (the
        // chart just renders its own "not enough data" state), matching
        // how Labor's own trend fetch is similarly best-effort.
        //
        // Analytics is NOT best-effort: its failure is the difference between
        // a tab and a blank page, so it keeps whatever was already on screen
        // and reports why rather than assigning nil over it.
        async let trendResult: FoodCostTrend? = try? client.send("/mobile/api/food-cost/trend")
        async let cfoResult: FoodCostCFO? = try? client.send("/mobile/api/food-cost/cfo")
        do {
            let fresh: FoodCostAnalytics = try await client.send("/mobile/api/food-cost/analytics")
            analytics = fresh
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch let error as APIClient.APIError {
            if analytics == nil { errorMessage = error.message }
        } catch {
            if analytics == nil { errorMessage = "Couldn't load food cost right now." }
        }
        let trendPayload = await trendResult
        trend = trendPayload?.weeks ?? []
        trendTarget = trendPayload?.target
        let cfoPayload = await cfoResult
        cfo = (cfoPayload?.ok == true) ? cfoPayload : nil
    }

    /// The drivers, in the order the server ranked them. Never re-sorted
    /// here: the ranking is dollars, then confidence, then ease, and it is
    /// computed server-side precisely so two clients cannot disagree about
    /// which opportunity is the biggest.
    var drivers: [FoodCostCFO.Driver] { cfo?.drivers?.drivers ?? [] }

    /// Renders only when there is something measured to render. An owner
    /// whose numbers do not yet support a cause should see the position and
    /// stop, never a cause produced to fill a card.
    var hasCFORead: Bool {
        !drivers.isEmpty || (cfo?.diagnosis?.cause?.isEmpty == false)
    }
}
