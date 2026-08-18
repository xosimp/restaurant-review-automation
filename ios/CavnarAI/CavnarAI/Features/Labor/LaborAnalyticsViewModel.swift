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
    // already played. Persisted to UserDefaults, same as
    // hasPlayedInsightTypewriter below and for the same reason: this view
    // model gets recreated fresh every time a user leaves Labor entirely
    // (e.g. switches to another module) and comes back — an in-memory-only
    // flag survived an Overview/Analytics tab switch (this instance itself
    // doesn't change for that), but not a genuinely fresh navigation into
    // Labor, which is exactly the "reloads and counts up again" report this
    // fixes. Only ever reset by actually clearing UserDefaults (there's no
    // reason to replay this for a restaurant the device has already seen).
    var hasPlayedBarIntro = false

    // Same reasoning, same lifetime as hasPlayedBarIntro above — guards the
    // savings tiles' count-up-from-zero reveal.
    var hasPlayedTilesIntro = false

    // Whether the AI Consultant's intro has already typewritten out once.
    // Persisted to UserDefaults (not just in-memory, unlike the two flags
    // above) — this view model itself gets recreated fresh every time a
    // user leaves Labor and taps back into it (a new NavigationLink push),
    // so an in-memory flag alone would still replay the reveal on every
    // revisit, only fixing it within a single continuous view on the
    // Overview tab. Set true immediately whenever configureCaching finds a
    // previously-cached insight (that content was already shown once
    // before, however long ago), and again as soon as a fresh network
    // insight actually starts typing out for the first time.
    var hasPlayedInsightTypewriter = false

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
        hasPlayedInsightTypewriter = UserDefaults.standard.bool(forKey: Self.typewriterPlayedKey(restaurantId))
        hasPlayedBarIntro = UserDefaults.standard.bool(forKey: Self.barIntroPlayedKey(restaurantId))
        hasPlayedTilesIntro = UserDefaults.standard.bool(forKey: Self.tilesIntroPlayedKey(restaurantId))
        guard let data = UserDefaults.standard.data(forKey: Self.insightCacheKey(restaurantId)),
              let cached = try? JSONDecoder.cavnar.decode(AIInsight.self, from: data) else { return }
        insight = cached
        // A cached insight is, by definition, one this device has already
        // shown before — never replay the typewriter for it even if this
        // is technically the first time *this* view model instance has
        // seen it.
        hasPlayedInsightTypewriter = true
    }

    private static func insightCacheKey(_ restaurantId: Int) -> String { "labor.cachedInsight.\(restaurantId)" }
    private static func typewriterPlayedKey(_ restaurantId: Int) -> String { "labor.hasTypedInsight.\(restaurantId)" }
    private static func barIntroPlayedKey(_ restaurantId: Int) -> String { "labor.hasPlayedBarIntro.\(restaurantId)" }
    private static func tilesIntroPlayedKey(_ restaurantId: Int) -> String { "labor.hasPlayedTilesIntro.\(restaurantId)" }

    private func cacheInsight(_ insight: AIInsight) {
        guard let restaurantId, let data = try? JSONEncoder.cavnar.encode(insight) else { return }
        UserDefaults.standard.set(data, forKey: Self.insightCacheKey(restaurantId))
    }

    /// Called once the typewriter reveal actually starts for a genuinely
    /// new insight — persists so it never replays again for this
    /// restaurant, on this device, across any future app session.
    func markInsightTypewriterPlayed() {
        hasPlayedInsightTypewriter = true
        guard let restaurantId else { return }
        UserDefaults.standard.set(true, forKey: Self.typewriterPlayedKey(restaurantId))
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
