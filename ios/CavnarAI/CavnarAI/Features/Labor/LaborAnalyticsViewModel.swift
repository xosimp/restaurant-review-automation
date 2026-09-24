import Foundation
import Observation

struct LaborDay: Decodable, Identifiable {
    let date: String
    let dayOfWeek: String?
    let laborPct: Double
    let laborCost: Double?
    let sales: Double?
    let totalHours: Double?
    var id: String { date }
    enum CodingKeys: String, CodingKey {
        case date, sales
        case dayOfWeek = "day_of_week"
        case laborPct = "labor_pct"
        case laborCost = "labor_cost"
        case totalHours = "total_hours"
    }
}

struct LaborTrendWeek: Decodable, Identifiable {
    let label: String
    let pct: Double
    let labor: Double
    let sales: Double
    let start: String
    let end: String

    var id: String { label }
}

/// Why labor ran over target — labor.diagnose, served beside the Labor read
/// on GET /mobile/api/labor/insight. The action is `whatWouldConfirm`, and
/// it carries its own rec_ledger key (`diag_labor:<driver>`) so the owner
/// can answer it like any other recommendation (rec-ROI #25).
struct LaborDiagnosis: Decodable, Equatable {
    struct Evidence: Decodable, Equatable {
        let module: String?
        let metric: String?
        let value: String?
    }
    let available: Bool?
    let cause: String?
    let alternativeCause: String?
    let whatWouldConfirm: String?
    let summary: String?
    /// K6 — the K1 object (a percentage with "Why?"); an older server's
    /// bare band ("medium") still decodes.
    let confidence: TrustConfidence?
    let operationalEvidence: [Evidence]
    let recKey: String?
    let answered: Bool?
    let answerable: Bool?

    enum CodingKeys: String, CodingKey {
        case available, cause, summary, confidence, answered, answerable
        case alternativeCause = "alternative_cause"
        case whatWouldConfirm = "what_would_confirm"
        case operationalEvidence = "operational_evidence"
        case recKey = "rec_key"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = try? c.decodeIfPresent(Bool.self, forKey: .available)
        cause = try? c.decodeIfPresent(String.self, forKey: .cause)
        alternativeCause = try? c.decodeIfPresent(String.self, forKey: .alternativeCause)
        whatWouldConfirm = try? c.decodeIfPresent(String.self, forKey: .whatWouldConfirm)
        summary = try? c.decodeIfPresent(String.self, forKey: .summary)
        confidence = try? c.decodeIfPresent(TrustConfidence.self, forKey: .confidence)
        operationalEvidence = (try? c.decodeIfPresent([Evidence].self, forKey: .operationalEvidence)) ?? []
        recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
        answered = try? c.decodeIfPresent(Bool.self, forKey: .answered)
        answerable = try? c.decodeIfPresent(Bool.self, forKey: .answerable)
    }

    /// Worth a card: a cause was found (nothing over target has none).
    var hasCause: Bool { available != false && (cause?.isEmpty == false) }

    /// Done / Not for us / Track under its action — keyed, answerable, not
    /// already answered.
    var showsAnswers: Bool {
        (recKey?.isEmpty == false) && answerable != false && answered != true
    }
}

/// The Labor read's response: the structured insight (AIInsight's fields)
/// and, beside it, the diagnosis.
struct LaborInsightPayload: Decodable {
    let insight: AIInsight
    let diagnosis: LaborDiagnosis?

    private enum CodingKeys: String, CodingKey { case diagnosis }

    init(from decoder: Decoder) throws {
        insight = try AIInsight(from: decoder)
        let c = try decoder.container(keyedBy: CodingKeys.self)
        diagnosis = try? c.decodeIfPresent(LaborDiagnosis.self, forKey: .diagnosis)
    }
}

@Observable
@MainActor
final class LaborAnalyticsViewModel {
    var trend: [LaborTrendWeek] = []
    var daily: [LaborDay] = []
    var insight: AIInsight?
    var diagnosis: LaborDiagnosis?
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

    private struct DailyResponse: Decodable { let ok: Bool; let days: [LaborDay] }

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
        async let insightResult: LaborInsightPayload? = try? client.send("/mobile/api/labor/insight")
        async let dailyResult: DailyResponse? = try? client.send("/mobile/api/labor/daily")

        trend = await trendResult?.weeks ?? []
        daily = await dailyResult?.days ?? []
        if let fresh = await insightResult {
            insight = fresh.insight
            diagnosis = fresh.diagnosis
            cacheInsight(fresh.insight)
        }
        isLoadingInsight = false
    }
}
