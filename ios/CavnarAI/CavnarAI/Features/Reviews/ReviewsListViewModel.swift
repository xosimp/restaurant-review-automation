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
    var isLoadingMore = false
    var errorMessage: String?
    var filter: ReviewInboxFilter = .all
    var searchText = ""
    /// The header figures — rating, response rate, urgent, awaiting. The
    /// app modelled all of this in ReviewStats and then never called
    /// /mobile/api/review-stats from anywhere, so the phone's Reviews tab
    /// showed no reputation summary at all while the web showed four pills.
    var stats: ReviewStats?
    /// Paging state. load() used to ask for every review the restaurant had
    /// ever received (filter=all, no limit) and filter client-side.
    private(set) var total = 0
    private(set) var hasMore = false
    private var nextOffset = 0

    /// Filtering is client-side over the full inbox (load() fetches
    /// everything with filter=all), so a chip tap is instant and the pull-to-
    /// refresh still refreshes one list.
    var filteredReviews: [Review] {
        var out = reviews
        // These must mean the same thing here, in models.get_reviews_data
        // and in the web inbox. They didn't: "To approve" was drafted-only
        // on the phone, "not approved and not posted and not urgent" in the
        // web's JS, and response_status='drafted' on the server — three
        // different sets behind one label. Server definition wins.
        switch filter {
        case .all: break
        case .urgent: out = out.filter(\.isUrgent)
        case .toApprove: out = out.filter(\.isInQueue)
        case .negative: out = out.filter { $0.sentiment == "negative" }
        case .positive: out = out.filter { $0.sentiment == "positive" }
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
        case .toApprove: return reviews.filter(\.isInQueue).count
        case .negative: return reviews.filter { $0.sentiment == "negative" }.count
        case .positive: return reviews.filter { $0.sentiment == "positive" }.count
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
        let total: Int?
        let offset: Int?
        let hasMore: Bool?

        enum CodingKeys: String, CodingKey {
            case ok, reviews, total, offset
            case hasMore = "has_more"
        }
    }

    /// One page. Matches models.REVIEWS_PAGE_SIZE.
    private static let pageSize = 50

    /// category filters to reviews tagged with that topic-heatmap category
    /// (see TopicHeatmapEntry.category); platform filters to one review
    /// platform (e.g. "google"/"yelp"). Both nil/omitted keeps the normal
    /// unfiltered inbox behavior.
    func load(category: String? = nil, platform: String? = nil) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            var query = ["filter": "all", "limit": "\(Self.pageSize)", "offset": "0"]
            if let category { query["category"] = category }
            if let platform { query["platform"] = platform }
            let response: ReviewsResponse = try await client.send("/mobile/api/reviews", query: query)
            reviews = response.reviews
            total = response.total ?? response.reviews.count
            nextOffset = response.offset ?? response.reviews.count
            hasMore = response.hasMore ?? false
            loadCategory = category
            loadPlatform = platform
            await loadStats()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is APIClient.SessionExpiredError {
            // Handled globally by SessionStore.
        } catch {
            errorMessage = "Couldn't load reviews."
        }
    }

    private var loadCategory: String?
    private var loadPlatform: String?

    /// The next page, appended. Called when the last row appears.
    func loadMore() async {
        guard hasMore, !isLoadingMore, !isLoading else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        var query = ["filter": "all", "limit": "\(Self.pageSize)", "offset": "\(nextOffset)"]
        if let loadCategory { query["category"] = loadCategory }
        if let loadPlatform { query["platform"] = loadPlatform }
        do {
            let response: ReviewsResponse = try await client.send(
                "/mobile/api/reviews", query: query, hapticOnError: false
            )
            let known = Set(reviews.map(\.id))
            reviews.append(contentsOf: response.reviews.filter { !known.contains($0.id) })
            total = response.total ?? total
            nextOffset = response.offset ?? (nextOffset + response.reviews.count)
            hasMore = response.hasMore ?? false
        } catch {
            // A failed page is not a failed screen — the rows already on
            // screen stay, and the next scroll retries.
            hasMore = true
        }
    }

    /// The header figures. Separate from the list so a paging request
    /// doesn't re-fetch them.
    func loadStats() async {
        stats = try? await client.send("/mobile/api/review-stats", hapticOnError: false)
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
