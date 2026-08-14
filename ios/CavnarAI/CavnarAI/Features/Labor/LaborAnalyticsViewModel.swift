import Foundation
import Observation

struct LaborTrendWeek: Decodable, Identifiable {
    let label: String
    let pct: Double
    let labor: Double
    let sales: Double

    var id: String { label }
}

@Observable
@MainActor
final class LaborAnalyticsViewModel {
    var trend: [LaborTrendWeek] = []
    var insight: AIInsight?
    var isLoadingInsight = false
    var isLoading = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct TrendResponse: Decodable {
        let ok: Bool
        let weeks: [LaborTrendWeek]
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
        insight = await insightResult
        isLoadingInsight = false
    }
}
