import XCTest
@testable import CavnarAI

final class ReviewDetailViewModelTests: XCTestCase {
    private func makeReview(id: Int = 1, draft: String? = "Thanks for the feedback!") -> Review {
        let json = """
        {"id": \(id), "platform": "google", "author": "Ann", "rating": 2,
         "text": "Slow service.", "review_date": "2026-07-20", "sentiment": "negative",
         "urgency": "normal", "draft_response": \(draft.map { "\"\($0)\"" } ?? "null"),
         "response_status": "drafted", "categories": []}
        """
        return try! JSONDecoder.cavnar.decode(Review.self, from: Data(json.utf8))
    }

    private func makeClient(handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) -> APIClient {
        MockURLProtocol.requestHandler = handler
        return APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    @MainActor
    func testApproveSetsDidCompleteOnSuccess() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "auto_posted": false}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)

        await viewModel.approve()

        XCTAssertTrue(viewModel.didComplete)
        XCTAssertNil(viewModel.errorMessage)
    }

    @MainActor
    func testApproveSurfacesErrorAndDoesNotCompleteOnFailure() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 500, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": false, "error": "Server error"}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)

        await viewModel.approve()

        XCTAssertFalse(viewModel.didComplete)
        XCTAssertEqual(viewModel.errorMessage, "Server error")
    }

    @MainActor
    func testSkipSetsDidCompleteOnSuccess() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)

        await viewModel.skip()

        XCTAssertTrue(viewModel.didComplete)
    }

    @MainActor
    func testRegenerateDraftReplacesEditedDraftOnSuccess() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "draft": "A brand new AI draft."}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(draft: "Old draft"), client: client)
        XCTAssertEqual(viewModel.editedDraft, "Old draft")

        await viewModel.regenerateDraft()

        XCTAssertEqual(viewModel.editedDraft, "A brand new AI draft.")
    }

    @MainActor
    func testRegenerateDraftLeavesEditedDraftUnchangedOnLogicalFailure() async {
        // regenerate-draft always answers HTTP 200 and signals failure via
        // the body's ok/error fields (mirrors client_api.py) — this must not
        // be treated as a network-layer error, just surfaced as a message.
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": false, "error": "Too many regenerations — please wait a moment and try again."}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(draft: "Old draft"), client: client)

        await viewModel.regenerateDraft()

        XCTAssertEqual(viewModel.editedDraft, "Old draft")
        XCTAssertEqual(viewModel.errorMessage, "Too many regenerations — please wait a moment and try again.")
    }
}
