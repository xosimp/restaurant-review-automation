import Foundation
import Observation

struct LaborTrendWeek: Decodable, Identifiable {
    let label: String
    let pct: Double
    let labor: Double
    let sales: Double

    var id: String { label }
}

struct LaborGap: Decodable {
    let ok: Bool
    let overTarget: Bool
    let monthlyGap: Double
    let currentPct: Double
    let targetPct: Double

    enum CodingKeys: String, CodingKey {
        case ok
        case overTarget = "over_target"
        case monthlyGap = "monthly_gap"
        case currentPct = "current_pct"
        case targetPct = "target_pct"
    }
}

@Observable
@MainActor
final class LaborAnalyticsViewModel {
    var trend: [LaborTrendWeek] = []
    var gap: LaborGap?
    var insight: String?
    var isLoading = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct TrendResponse: Decodable {
        let ok: Bool
        let weeks: [LaborTrendWeek]
    }

    private struct InsightResponse: Decodable {
        let ok: Bool
        let insight: String?
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }

        async let trendResult: TrendResponse? = try? client.send("/mobile/api/labor/trend")
        async let gapResult: LaborGap? = try? client.send("/mobile/api/labor/gap")
        async let insightResult: InsightResponse? = try? client.send("/mobile/api/labor/insight")

        trend = await trendResult?.weeks ?? []
        gap = await gapResult
        insight = await insightResult?.insight
    }
}
