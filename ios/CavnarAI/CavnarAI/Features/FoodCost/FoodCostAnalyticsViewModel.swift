import Foundation
import Observation

@Observable
@MainActor
final class FoodCostAnalyticsViewModel {
    var analytics: FoodCostAnalytics?
    var isLoading = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        analytics = try? await client.send("/mobile/api/food-cost/analytics")
    }
}
