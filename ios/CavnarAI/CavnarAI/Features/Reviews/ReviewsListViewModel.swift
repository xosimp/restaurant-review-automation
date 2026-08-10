import Foundation
import Observation

@Observable
@MainActor
final class ReviewsListViewModel {
    var reviews: [Review] = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct ReviewsResponse: Decodable {
        let ok: Bool
        let reviews: [Review]
    }

    /// category filters to reviews tagged with that topic-heatmap category
    /// (see TopicHeatmapEntry.category); platform filters to one review
    /// platform (e.g. "google"/"yelp"). Both nil/omitted keeps the normal
    /// unfiltered inbox behavior.
    func load(category: String? = nil, platform: String? = nil) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            var query = ["filter": "all"]
            if let category { query["category"] = category }
            if let platform { query["platform"] = platform }
            let response: ReviewsResponse = try await client.send("/mobile/api/reviews", query: query)
            reviews = response.reviews
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is APIClient.SessionExpiredError {
            // Handled globally by SessionStore.
        } catch {
            errorMessage = "Couldn't load reviews."
        }
    }

    /// Called after a detail screen completes an approve/skip so the list
    /// reflects the new status in place — load() fetches every review
    /// (filter=all), not just an actionable queue, so a completed review
    /// should stay visible with its updated status pill, not disappear.
    func markCompleted(reviewID: Int, status: String) {
        guard let index = reviews.firstIndex(where: { $0.id == reviewID }) else { return }
        reviews[index] = reviews[index].withStatus(status)
    }
}
