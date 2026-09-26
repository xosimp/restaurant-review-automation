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
    /// Dishes whose price an ingredient rise has eaten into, and the price
    /// that restores their food cost %. Best-effort like the CFO read.
    var reprice: RepriceSuggestions?
    /// Dish → the price the server confirmed it set. Replaces the card's
    /// buttons with "Set to $X.XX"; the next load leaves the dish out.
    var repriceApplied: [String: Double] = [:]
    /// Dish → "Measuring food cost % until 10/21/26", or why nothing is.
    var repriceTracking: [String: String] = [:]
    /// Dishes the owner said "Not for us" to (the row confirms in place).
    var repriceDismissed: Set<String> = []
    var repriceBusy: Set<String> = []
    var repriceErrors: [String: String] = [:]
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

    /// Whether the Analytics tab has asked for its first load yet.
    private(set) var hasRequestedFirstLoad = false
    /// When the analytics last loaded — the foreground-refresh clock. Nil
    /// until the Analytics tab has been shown, so a foreground return never
    /// loads (and records as shown) a tab the owner hasn't opened.
    private(set) var lastLoadedAt: Date?

    /// The Analytics tab's first appearance — the only automatic load.
    /// Opening Food Cost on the Tracker tab fetches nothing here, because
    /// every analytics read records its recommendations as shown; later
    /// visits to the tab in the same session keep what is on screen, as
    /// before (the load used to run once, on opening Food Cost).
    func loadOnFirstShow() async {
        guard !hasRequestedFirstLoad else { return }
        hasRequestedFirstLoad = true
        await load()
    }

    /// The last good analytics + trend, as one envelope (CacheEnvelope).
    private struct CachedAnalytics: Decodable {
        let analytics: FoodCostAnalytics
        let trend: FoodCostTrend?
    }
    @ObservationIgnored private let cache = ResponseCache<CachedAnalytics>("foodcost.analytics")
    /// When the cached copy on screen was stored; nil once a live load lands.
    private(set) var cachedAt: Date?
    var stalenessNotice: String? { CacheFreshness.notice(savedAt: cachedAt) }

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
        //
        // The analytics and the trend are painted from the device cache
        // first (ResponseCache) — the CFO read and the reprice suggestions
        // are not: each is a recommendation the owner may act on, and an
        // old one is not something to act on.
        let generation = SessionScope.generation
        if analytics == nil, let hit = await cache.load() {
            analytics = hit.value.analytics
            trend = hit.value.trend?.weeks ?? []
            trendTarget = hit.value.trend?.target
            cachedAt = hit.savedAt
            isLoading = false
        }
        async let trendResult: (value: FoodCostTrend, body: Data)? = try? client.sendKeepingBody(
            "/mobile/api/food-cost/trend")
        async let cfoResult: FoodCostCFO? = try? client.send("/mobile/api/food-cost/cfo")
        async let repriceResult: RepriceSuggestions? = try? client.send(
            "/mobile/api/food-cost/reprice", hapticOnError: false)
        var freshBody: Data?
        do {
            let fresh: (value: FoodCostAnalytics, body: Data) = try await client.sendKeepingBody(
                "/mobile/api/food-cost/analytics")
            analytics = fresh.value
            freshBody = fresh.body
            cachedAt = nil
            lastLoadedAt = Date()
        } catch is CancellationError {
            // View went away mid-fetch; not a failure.
        } catch let error as APIClient.APIError {
            // A login that may count but not read the margins: the same
            // answer the counts-only tile carries. Recording it switches
            // Food Cost to the counts-only screen (ModuleDestinationView)
            // rather than leaving a refused analytics tab.
            if error.kind == .moduleForbidden { ModuleAccess.shared.markCountsOnly("inventory") }
            if analytics == nil { errorMessage = error.message }
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            if analytics == nil { errorMessage = "Couldn't load food cost right now." }
        }
        let trendPayload = await trendResult
        // A cached trend stays beside cached figures; beside live ones a
        // failed trend is the chart's own "not enough data", as before.
        if trendPayload != nil || cachedAt == nil {
            trend = trendPayload?.value.weeks ?? []
            trendTarget = trendPayload?.value.target
        }
        if let freshBody {
            cache.save(CacheEnvelope.make([("analytics", freshBody), ("trend", trendPayload?.body)]),
                       generation: generation)
        }
        let cfoPayload = await cfoResult
        cfo = (cfoPayload?.ok == true) ? cfoPayload : nil
        let repricePayload = await repriceResult
        reprice = (repricePayload?.ok == true) ? repricePayload : nil
        // A fresh list no longer carries what was answered; drop the local
        // marks for dishes that are gone so a later suggestion for the same
        // dish starts clean.
        let live = Set(repriceSuggestions.map(\.dish))
        repriceApplied = repriceApplied.filter { live.contains($0.key) }
        repriceTracking = repriceTracking.filter { live.contains($0.key) }
        repriceDismissed = repriceDismissed.intersection(live)
        repriceErrors = [:]
    }

    /// The suggestions to show — empty when unavailable or none.
    var repriceSuggestions: [RepriceSuggestions.Suggestion] {
        guard let reprice, reprice.available != false else { return [] }
        return reprice.suggestions ?? []
    }

    private struct RepriceApplyBody: Encodable {
        let dish: String
        let price: Double
        let menuItemId: Int?
        enum CodingKeys: String, CodingKey {
            case dish, price
            case menuItemId = "menu_item_id"
        }
    }

    /// One tap: set the dish to its suggested price. Never retried on a
    /// guess — it changes a menu price.
    func applyReprice(_ s: RepriceSuggestions.Suggestion) async {
        guard let price = s.suggestedPrice, !repriceBusy.contains(s.dish) else { return }
        repriceBusy.insert(s.dish)
        repriceErrors[s.dish] = nil
        defer { repriceBusy.remove(s.dish) }
        do {
            let r: RepriceApplyResult = try await client.send(
                "/mobile/api/food-cost/reprice/apply", method: .post,
                body: RepriceApplyBody(dish: s.dish, price: price, menuItemId: s.menuItemId),
                retryTransient: false)
            if r.ok {
                Haptic.success()
                repriceApplied[s.dish] = r.price ?? price
                repriceTracking[s.dish] = RecTrackerNote.line(tracker: r.tracker, refused: r.trackerRefused)
            } else {
                repriceErrors[s.dish] = r.error ?? "Couldn\u{2019}t set that price."
            }
        } catch is CancellationError {
            // The screen went away mid-send.
        } catch let error as APIClient.APIError {
            // A price change whose answer was lost may have landed; say so
            // rather than inviting a second tap (DESIGN_SYSTEM §10).
            repriceErrors[s.dish] = (error.status == nil && error.mayHaveReachedServer)
                ? "Couldn\u{2019}t confirm the price changed \u{2014} check Menu margins before trying again."
                : error.message
        } catch {
            repriceErrors[s.dish] = "Couldn\u{2019}t set that price."
        }
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
