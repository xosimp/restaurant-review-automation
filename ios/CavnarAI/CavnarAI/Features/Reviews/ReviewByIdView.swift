import Foundation
import Observation
import SwiftUI

/// Finds one review by id so a diagnosis's "Reviews this rests on" can open
/// the review it cites — the phone's version of the web's `jumpToReview`.
///
/// Reads GET /mobile/api/reviews/<id>. The route's own 404 ("that review
/// isn't in this inbox") is the answer. Only an older backend without the
/// route at all (the app's catch-all 404) falls back to the paged inbox the
/// Reviews tab reads: first scoped to the diagnosis's topic, then the whole
/// inbox a few pages deep, saying the review is further back rather than
/// claiming it doesn't exist. Offline or a server error is said as such.
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

    private struct OneResponse: Decodable {
        let ok: Bool
        let review: Review?
    }

    func load() async {
        guard review == nil else { return }
        isLoading = true
        notFound = false
        notFoundMessage = nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            let one: OneResponse = try await client.send("/mobile/api/reviews/\(reviewID)", hapticOnError: false)
            if let hit = one.review {
                review = hit
                return
            }
        } catch let error as APIClient.APIError where error.status == 404 {
            // The route answered "not in this inbox": that is the answer —
            // paging 800 reviews to look again cost four requests and
            // ~200 KB for nothing (parity perf). Only an older server with
            // no such route at all falls back to the inbox pages.
            guard Self.isMissingRoute(error) else {
                notFound = true
                notFoundMessage = error.message
                return
            }
        } catch is CancellationError {
            return
        } catch is APIClient.SessionExpiredError {
            return
        } catch let error as APIClient.APIError {
            // Offline, a timeout, a 5xx: the inbox pages would fail the same
            // way. Say so, with Try again.
            errorMessage = error.message
            return
        } catch {
            errorMessage = "Couldn\u{2019}t load that review."
            return
        }
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

    /// The server's sentence for a review it looked for and did not find,
    /// shown instead of "further back".
    var notFoundMessage: String?

    private struct RouteMissing: Decodable {
        let unknownRoute: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case error
            case unknownRoute = "unknown_route"
        }
    }

    /// The app's catch-all 404 — the route does not exist on this server
    /// (hosted_dashboard's handler: `unknown_route`, and on a server older
    /// than that flag, its sentence) — as against the route's own "that
    /// review isn't in this inbox".
    nonisolated static func isMissingRoute(_ error: APIClient.APIError) -> Bool {
        guard error.status == 404 else { return false }
        guard let body = error.body,
              let decoded = try? JSONDecoder.cavnar.decode(RouteMissing.self, from: body) else {
            // Not our JSON at all (a proxy's HTML page): no route here.
            return true
        }
        if decoded.unknownRoute == true { return true }
        return decoded.error == "That endpoint doesn't exist. Please update the app."
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
                                 ? (viewModel.notFoundMessage
                                    ?? "Review #\(viewModel.reviewID) is further back \u{2014} search for it in the inbox.")
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
