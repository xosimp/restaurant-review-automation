import XCTest
@testable import CavnarAI

/// The inbox had no tests at all, which is how its filter chips came to mean
/// something different from the same-named chips on the web and from the
/// server's own filter, and how "load everything and filter on the phone"
/// survived as the loading strategy.
final class ReviewsListViewModelTests: XCTestCase {
    private func review(_ id: Int, status: String, sentiment: String? = nil,
                        rating: Int = 3, urgency: String = "normal") throws -> Review {
        let sentimentJSON = sentiment.map { "\"\($0)\"" } ?? "null"
        return try JSONDecoder.cavnar.decode(Review.self, from: Data("""
        {"id": \(id), "platform": "google", "author": "A", "rating": \(rating),
         "text": "text", "response_status": "\(status)", "urgency": "\(urgency)",
         "sentiment": \(sentimentJSON), "categories": []}
        """.utf8))
    }

    @MainActor
    func testToApproveCountsEverythingStillInTheQueueNotJustDraftedOnes() throws {
        // "To approve" meant response_status='drafted' here, which left out
        // a review whose draft hasn't been written yet — work that is very
        // much still in the queue, and which the server's own "pending"
        // filter includes. Three different definitions behind one label.
        let viewModel = ReviewsListViewModel()
        viewModel.reviews = [
            try review(1, status: "drafted"),
            try review(2, status: "pending"),
            try review(3, status: "posted"),
            try review(4, status: "skipped"),
        ]
        viewModel.filter = .toApprove

        XCTAssertEqual(viewModel.filteredReviews.map(\.id), [1, 2])
        XCTAssertEqual(viewModel.count(for: .toApprove), 2)
    }

    @MainActor
    func testNegativeAndPositiveFollowSentimentNotTheStarRating() throws {
        // The phone ORed in the star rating (<=2 / >=4) while the web and
        // the server both filter on the analyser's sentiment alone, so the
        // same chip returned different sets on the two platforms.
        let viewModel = ReviewsListViewModel()
        viewModel.reviews = [
            try review(1, status: "posted", sentiment: "negative", rating: 4),
            try review(2, status: "posted", sentiment: "positive", rating: 2),
            try review(3, status: "posted", sentiment: nil, rating: 1),
        ]

        viewModel.filter = .negative
        XCTAssertEqual(viewModel.filteredReviews.map(\.id), [1])
        viewModel.filter = .positive
        XCTAssertEqual(viewModel.filteredReviews.map(\.id), [2])
    }

    @MainActor
    func testUrgentIsUnaffected() throws {
        let viewModel = ReviewsListViewModel()
        viewModel.reviews = [
            try review(1, status: "drafted", urgency: "high"),
            try review(2, status: "drafted"),
        ]
        viewModel.filter = .urgent
        XCTAssertEqual(viewModel.filteredReviews.map(\.id), [1])
    }

    @MainActor
    func testSearchStillMatchesAuthorTextAndDraft() throws {
        let viewModel = ReviewsListViewModel()
        viewModel.reviews = [try review(1, status: "drafted")]
        viewModel.searchText = "nothing like this"
        XCTAssertTrue(viewModel.filteredReviews.isEmpty)
        viewModel.searchText = "text"
        XCTAssertEqual(viewModel.filteredReviews.count, 1)
    }

    @MainActor
    func testAnUnanalysedReviewIsMarkedAsSuchRatherThanReadingAsNeutral() throws {
        let pending = try JSONDecoder.cavnar.decode(Review.self, from: Data("""
        {"id": 9, "platform": "google", "author": "A", "rating": 2, "text": "x",
         "response_status": "pending", "urgency": "normal", "categories": [], "processed": false}
        """.utf8))
        XCTAssertFalse(pending.isAnalysed)
        // An older server that doesn't send the field is assumed analysed,
        // so nothing regresses into showing every review as pending.
        let legacy = try review(10, status: "drafted")
        XCTAssertTrue(legacy.isAnalysed)
    }
}
