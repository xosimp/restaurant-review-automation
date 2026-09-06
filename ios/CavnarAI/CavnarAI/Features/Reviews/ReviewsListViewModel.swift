import Foundation
import Observation

/// The web inbox's filter chips, on the phone.
enum ReviewInboxFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case urgent = "Urgent"
    case toApprove = "To approve"
    case negative = "Negative"
    case positive = "Positive"
    var id: String { rawValue }
}

@Observable
@MainActor
final class ReviewsListViewModel {
    var reviews: [Review] = []
    var isLoading = false
    var errorMessage: String?
    var filter: ReviewInboxFilter = .all
    var searchText = ""

    /// Filtering is client-side over the full inbox (load() fetches
    /// everything with filter=all), so a chip tap is instant and the pull-to-
    /// refresh still refreshes one list.
    var filteredReviews: [Review] {
        var out = reviews
        switch filter {
        case .all: break
        case .urgent: out = out.filter(\.isUrgent)
        case .toApprove: out = out.filter(\.isAwaitingApproval)
        case .negative: out = out.filter { ($0.rating ?? 3) <= 2 || $0.sentiment == "negative" }
        case .positive: out = out.filter { ($0.rating ?? 3) >= 4 || $0.sentiment == "positive" }
        }
        let q = searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if !q.isEmpty {
            out = out.filter {
                ($0.text ?? "").lowercased().contains(q)
                    || ($0.author ?? "").lowercased().contains(q)
                    || ($0.draftResponse ?? "").lowercased().contains(q)
            }
        }
        return out
    }

    func count(for filter: ReviewInboxFilter) -> Int {
        switch filter {
        case .all: return reviews.count
        case .urgent: return reviews.filter(\.isUrgent).count
        case .toApprove: return reviews.filter(\.isAwaitingApproval).count
        case .negative: return reviews.filter { ($0.rating ?? 3) <= 2 || $0.sentiment == "negative" }.count
        case .positive: return reviews.filter { ($0.rating ?? 3) >= 4 || $0.sentiment == "positive" }.count
        }
    }

    func remove(reviewID: Int) {
        reviews.removeAll { $0.id == reviewID }
    }

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
        if status == "deleted" { remove(reviewID: reviewID); return }
        guard let index = reviews.firstIndex(where: { $0.id == reviewID }) else { return }
        reviews[index] = reviews[index].withStatus(status)
    }
}
