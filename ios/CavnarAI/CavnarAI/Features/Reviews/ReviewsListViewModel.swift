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

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: ReviewsResponse = try await client.send("/mobile/api/reviews", query: ["filter": "all"])
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
    /// reflects it without a full reload.
    func removeFromQueue(reviewID: Int) {
        reviews.removeAll { $0.id == reviewID }
    }

    func replaceReview(_ updated: Review) {
        guard let index = reviews.firstIndex(where: { $0.id == updated.id }) else { return }
        reviews[index] = updated
    }
}
