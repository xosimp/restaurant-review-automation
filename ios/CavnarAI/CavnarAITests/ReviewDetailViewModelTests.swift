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

    // MARK: - An approve whose Google post failed

    @MainActor
    func testApproveSurfacesAPostFailureInsteadOfClaimingSuccess() async {
        // The post runs synchronously server-side, so auto_posted:false WITH
        // a post_error is a finished failure, not work still in flight. The
        // app decoded neither field and showed a plain "Approved" banner
        // with no sign the reply never reached Google and no way to retry.
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "auto_posted": false, "post_error": "API error 403: insufficient scope"}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)

        await viewModel.approve()

        XCTAssertEqual(viewModel.postFailure, "API error 403: insufficient scope")
        XCTAssertEqual(viewModel.currentStatus, "approved")
        XCTAssertFalse(viewModel.didComplete, "a failed post must keep the owner on the screen with the retry")
    }

    @MainActor
    func testApproveWithNoPostErrorStillCompletes() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "auto_posted": true}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)

        await viewModel.approve()

        XCTAssertNil(viewModel.postFailure)
        XCTAssertEqual(viewModel.currentStatus, "posted")
        XCTAssertTrue(viewModel.didComplete)
    }

    @MainActor
    func testRetryPostClearsTheFailureOnceGoogleAcceptsIt() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "auto_posted": true}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(), client: client)
        viewModel.postFailure = "API error 403: insufficient scope"

        let ok = await viewModel.retryPost()

        XCTAssertTrue(ok)
        XCTAssertNil(viewModel.postFailure)
        XCTAssertEqual(viewModel.currentStatus, "posted")
    }

    // MARK: - Drafting is never spent without the owner asking

    @MainActor
    func testOpeningAnUndraftedReviewDoesNotSpendAModelCall() async {
        // ensureDraftIfNeeded() used to fire regenerateDraft() — a Sonnet
        // call billed to the restaurant — merely because a review with no
        // draft was opened. Browsing the inbox spent one call per tap.
        var requests = 0
        let client = makeClient { request in
            requests += 1
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true, "draft": "should not have been asked for"}
            """.utf8))
        }
        let viewModel = ReviewDetailViewModel(review: makeReview(draft: nil), client: client)

        XCTAssertTrue(viewModel.needsDraft, "an undrafted review offers the button")
        XCTAssertEqual(requests, 0, "no request fires just from constructing the screen")
    }

    // MARK: - Retract is only offered where it can work

    func testRetractIsNotOfferedWithoutARealGoogleReplyBehindIt() throws {
        // review_name is deliberately not decoded here, so the app cannot
        // derive this — the server sends can_retract instead. Without it
        // the detail screen offered "Retract from Google" on every posted
        // review, including Yelp ones, and every one 400d.
        func decode(_ json: String) throws -> Review {
            try JSONDecoder.cavnar.decode(Review.self, from: Data(json.utf8))
        }
        let yelp = try decode("""
        {"id": 1, "platform": "yelp", "author": "A", "rating": 5, "text": "x",
         "response_status": "posted", "urgency": "normal", "categories": [], "can_retract": false}
        """)
        XCTAssertFalse(yelp.canRetract)

        let google = try decode("""
        {"id": 2, "platform": "google", "author": "A", "rating": 5, "text": "x",
         "response_status": "posted", "urgency": "normal", "categories": [], "can_retract": true}
        """)
        XCTAssertTrue(google.canRetract)

        // Absent (an older server) is treated as "cannot", never "can".
        let legacy = try decode("""
        {"id": 3, "platform": "google", "author": "A", "rating": 5, "text": "x",
         "response_status": "posted", "urgency": "normal", "categories": []}
        """)
        XCTAssertFalse(legacy.canRetract)
    }

    func testApprovingAGoogleReplyMakesItRetractableWithoutARefresh() throws {
        let drafted = try JSONDecoder.cavnar.decode(Review.self, from: Data("""
        {"id": 4, "platform": "google", "author": "A", "rating": 5, "text": "x",
         "response_status": "drafted", "urgency": "normal", "categories": []}
        """.utf8))
        XCTAssertFalse(drafted.canRetract)
        // Our own auto-post is exactly what gives it a review_name.
        XCTAssertTrue(drafted.withStatus("posted").canRetract)
        // A Yelp reply marked posted by hand has no Google reply behind it.
        let yelp = try JSONDecoder.cavnar.decode(Review.self, from: Data("""
        {"id": 5, "platform": "yelp", "author": "A", "rating": 5, "text": "x",
         "response_status": "approved", "urgency": "normal", "categories": []}
        """.utf8))
        XCTAssertFalse(yelp.withStatus("posted").canRetract)
    }
}
