import Foundation
import Observation
import SwiftUI

/// Finds one review by id so a diagnosis's "Reviews this rests on" can open
/// the review it cites — the phone's version of the web's `jumpToReview`.
///
/// There is no single-review GET on /mobile/api, so this reads the same
/// paged inbox the Reviews tab does: first scoped to the diagnosis's topic
/// (a cited review is tagged with it, so it is almost always on that first
/// page), then the whole inbox a few pages deep. Like the web, it gives up
/// after a bounded look and says the review is further back rather than
/// claiming it doesn't exist.
@Observable
@MainActor
final class ReviewByIdViewModel {
    let reviewID: Int
    let category: String?
    var review: Review?
    /// Starts true so the first frame is the orb, not the failure sentence.
    var isLoading = true
    var notFound = false
    var errorMessage: String?

    private let client: APIClient

    /// Largest page the route allows (mobile_reviews caps limit at 200).
    static let pageSize = 200
    /// Whole-inbox pages to try after the topic-scoped one.
    static let maxInboxPages = 3

    init(reviewID: Int, category: String?, client: APIClient = .shared) {
        self.reviewID = reviewID
        self.category = category
        self.client = client
    }

    private struct PageResponse: Decodable {
        let ok: Bool
        let reviews: [Review]
        let hasMore: Bool?
        enum CodingKeys: String, CodingKey {
            case ok, reviews
            case hasMore = "has_more"
        }
    }

    func load() async {
        guard review == nil else { return }
        isLoading = true
        notFound = false
        errorMessage = nil
        defer { isLoading = false }
        do {
            if let category, !category.isEmpty,
               let hit = try await page(offset: 0, category: category).reviews.first(where: { $0.id == reviewID }) {
                review = hit
                return
            }
            for n in 0..<Self.maxInboxPages {
                let p = try await page(offset: n * Self.pageSize, category: nil)
                if let hit = p.reviews.first(where: { $0.id == reviewID }) {
                    review = hit
                    return
                }
                if p.hasMore != true { break }
            }
            notFound = true
        } catch is CancellationError {
            // The screen went away mid-load — not a failure.
        } catch is APIClient.SessionExpiredError {
            // Handled globally by SessionStore.
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t load that review."
        }
    }

    private func page(offset: Int, category: String?) async throws -> PageResponse {
        var query = ["filter": "all", "limit": "\(Self.pageSize)", "offset": "\(offset)"]
        if let category { query["category"] = category }
        return try await client.send("/mobile/api/reviews", query: query)
    }
}

/// Pushed from a diagnosis's evidence chip. Resolves the id, then shows the
/// same ReviewDetailView the inbox opens.
struct ReviewByIdView: View {
    @State private var viewModel: ReviewByIdViewModel

    init(reviewID: Int, category: String?) {
        _viewModel = State(initialValue: ReviewByIdViewModel(reviewID: reviewID, category: category))
    }

    var body: some View {
        Group {
            if let review = viewModel.review {
                ReviewDetailView(viewModel: ReviewDetailViewModel(review: review), onCompleted: { _ in })
            } else {
                Group {
                    if viewModel.isLoading {
                        CavnarLoadingOrb()
                    } else {
                        VStack(spacing: 10) {
                            Text(viewModel.notFound
                                 ? "Review #\(viewModel.reviewID) is further back \u{2014} search for it in the inbox."
                                 : (viewModel.errorMessage ?? "Couldn\u{2019}t load that review."))
                                .font(.cavnarBody(15))
                                .foregroundStyle(viewModel.notFound ? Color.cavnarInk3 : Color.cavnarRed)
                                .multilineTextAlignment(.center)
                                .fixedSize(horizontal: false, vertical: true)
                            if !viewModel.notFound {
                                Button("Try again") { Task { await viewModel.load() } }
                                    .font(.cavnarBody(15, weight: 600))
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                        .padding(.horizontal, 28)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .cavnarModuleBackground()
                .navigationTitle("Review")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar { cavnarTitleToolbar("Review") }
                .cavnarEmberBackButton()
            }
        }
        .task { await viewModel.load() }
    }
}
