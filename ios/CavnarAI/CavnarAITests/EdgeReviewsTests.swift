import XCTest
@testable import CavnarAI

/// What these protect: the public reply that goes out under the
/// restaurant's name. Approve must publish what is on screen, never the
/// pre-edit draft; a request that may already have posted must not be
/// replayed blindly; a double tap must not post twice; and the inbox must
/// not claim "no urgent reviews" because it only looked at the first page.
/// XCTExpectFailure marks confirmed CLIENT-6 / 30 / 49 / 54 / 55 / 56
/// defects; each flips when fixed.
@MainActor
final class EdgeReviewsTests: XCTestCase {

    private func review(_ id: Int = 1, draft: String? = "Thanks for the feedback!", status: String = "drafted",
                        urgency: String = "normal") -> Review {
        let json = """
        {"id": \(id), "platform": "google", "author": "Ann", "rating": 2,
         "text": "Slow service.", "review_date": "2026-07-20", "sentiment": "negative",
         "urgency": "\(urgency)", "draft_response": \(draft.map { "\"\($0)\"" } ?? "null"),
         "response_status": "\(status)", "categories": []}
        """
        return try! JSONDecoder.cavnar.decode(Review.self, from: Data(json.utf8))
    }

    nonisolated private static func reviewsJSON(_ ids: ClosedRange<Int>, urgency: String = "normal") -> String {
        ids.map { id in
            """
            {"id": \(id), "platform": "google", "author": "A\(id)", "rating": 4, "text": "fine",
             "response_status": "posted", "urgency": "\(urgency)", "sentiment": "positive", "categories": []}
            """
        }.joined(separator: ",")
    }

    override func setUp() async throws {
        await PendingWriteQueue.shared.clear()
    }

    override func tearDown() async throws {
        await PendingWriteQueue.shared.clear()
        MockURLProtocol.requestHandler = nil
    }

    // MARK: CLIENT-6 — approve after a failed or queued save

    func testApproveDoesNotPublishWhenSavingTheEditFailed() async {
        let sent = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            sent.value.append(EdgeHTTP.line(request))
            if request.url?.path.hasSuffix("/save-draft") == true {
                // save-draft answers 200 and signals failure in the body.
                return EdgeHTTP.reply(request, 200, #"{"ok": false, "error": "Couldn't save your edit."}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "auto_posted": true}"#)
        }
        let vm = ReviewDetailViewModel(review: review(draft: "Old AI draft"), client: client)
        vm.editedDraft = "The owner's corrected reply"

        await vm.approve()

        XCTAssertTrue(sent.value.contains("POST /mobile/api/reviews/1/save-draft"))
        XCTExpectFailure("CLIENT-6: approve goes out after a failed save, so Google gets the pre-edit draft", strict: true) {
            XCTAssertFalse(sent.value.contains("POST /mobile/api/reviews/1/approve"),
                           "the server posts its stored (old) draft; approve must not run until the edit is saved")
        }
    }

    func testApproveDoesNotPublishWhenTheSaveReturned500() async {
        let sent = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            sent.value.append(EdgeHTTP.line(request))
            if request.url?.path.hasSuffix("/save-draft") == true {
                return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "auto_posted": true}"#)
        }
        let vm = ReviewDetailViewModel(review: review(draft: "Old AI draft"), client: client)
        vm.editedDraft = "Edited"
        await vm.approve()
        XCTExpectFailure("CLIENT-6: a 500 on the flush save does not stop the live approve", strict: true) {
            XCTAssertFalse(sent.value.contains("POST /mobile/api/reviews/1/approve"))
        }
    }

    func testApproveDoesNotOvertakeASaveThatWasQueuedOffline() async {
        let sent = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            if request.url?.path.hasSuffix("/save-draft") == true { throw URLError(.timedOut) }
            sent.value.append(EdgeHTTP.line(request))
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "auto_posted": true}"#)
        }
        let vm = ReviewDetailViewModel(review: review(draft: "Old AI draft"), client: client)
        vm.editedDraft = "Edited in the walk-in"
        await vm.approve()
        let queued = await PendingWriteQueue.shared.pendingLabels
        XCTAssertEqual(queued.first, "Save draft response", "the edit itself was queued")
        XCTExpectFailure("CLIENT-6: the approve is sent live while the edit it depends on sits in the queue", strict: true) {
            XCTAssertFalse(sent.value.contains("POST /mobile/api/reviews/1/approve"),
                           "approve must wait behind the queued save, not publish the stored draft now")
        }
    }

    func testAnUnchangedDraftApprovesWithoutASave() async {
        let sent = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            sent.value.append(EdgeHTTP.line(request))
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "auto_posted": true}"#)
        }
        let vm = ReviewDetailViewModel(review: review(draft: "Same"), client: client)
        await vm.approve()
        XCTAssertEqual(sent.value, ["POST /mobile/api/reviews/1/approve"])
        XCTAssertTrue(vm.didComplete)
    }

    func testATimedOutApproveIsNotQueuedForABlindReplay() async {
        // The Google post runs synchronously server-side, so a timeout may
        // well mean it already posted. Replaying it re-posts and re-fires
        // the response.approved webhook.
        let client = EdgeHTTP.client { _ in throw URLError(.timedOut) }
        let vm = ReviewDetailViewModel(review: review(draft: "Same"), client: client)
        await vm.approve()
        let queued = await PendingWriteQueue.shared.pendingLabels
        XCTExpectFailure("CLIENT-6: a timed-out approve is queued and replayed as a full second approve", strict: true) {
            XCTAssertFalse(queued.contains { $0.hasPrefix("Approve") })
            XCTAssertFalse(vm.didComplete, "the outcome is unknown; the owner stays on the review")
        }
    }

    func testAnOfflineApproveIsStillQueued() async {
        // The request never left the phone, so queueing it is safe.
        MockURLProtocol.requestHandler = nil
        let client = EdgeHTTP.client { _ in throw URLError(.notConnectedToInternet) }
        let vm = ReviewDetailViewModel(review: review(draft: "Same"), client: client)
        await vm.approve()
        let queued = await PendingWriteQueue.shared.pendingCount
        // -1009 on an online device is classified .timedOut (see
        // APIClientTests); either way it is retryable and queued.
        XCTAssertEqual(queued, 1)
        XCTAssertEqual(vm.currentStatus, "pending-sync")
    }

    // MARK: CLIENT-55 — retry posting double tap

    func testASecondRetryPostWhileTheFirstIsInFlightIsIgnored() async {
        let posts = Box(0)
        let client = EdgeHTTP.client { request in
            posts.value += 1
            Thread.sleep(forTimeInterval: 0.05)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "auto_posted": true}"#)
        }
        let vm = ReviewDetailViewModel(review: review(status: "approved"), client: client)
        vm.postFailure = "API error 403"
        async let first = vm.retryPost()
        async let second = vm.retryPost()
        _ = await (first, second)
        XCTExpectFailure("CLIENT-55: retryPost has no in-flight guard and the button is only dimmed, so a double tap posts twice", strict: true) {
            XCTAssertEqual(posts.value, 1)
        }
    }

    func testASecondWriteAReplyWhileDraftingIsIgnored() async {
        let calls = Box(0)
        let client = EdgeHTTP.client { request in
            calls.value += 1
            Thread.sleep(forTimeInterval: 0.05)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "draft": "A fresh draft."}"#)
        }
        let vm = ReviewDetailViewModel(review: review(draft: nil, status: "pending"), client: client)
        async let a: Void = vm.regenerateDraft()
        async let b: Void = vm.regenerateDraft()
        _ = await (a, b)
        XCTExpectFailure("CLIENT-55: \"Write a reply\" is only dimmed, so a double tap pays for two drafts", strict: true) {
            XCTAssertEqual(calls.value, 1)
        }
    }

    // MARK: CLIENT-56 — reopening after drafting

    func testReopeningAReviewAfterDraftingDoesNotOfferToDraftAgain() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, #"{"ok": true, "draft": "A fresh draft."}"#)
        }
        let list = ReviewsListViewModel(client: client)
        list.reviews = [review(draft: nil, status: "pending")]
        let first = ReviewDetailViewModel(review: list.reviews[0], client: client)
        await first.regenerateDraft()
        XCTAssertFalse(first.needsDraft)
        // Back to the list, tap the same row: the detail is built from the
        // list's copy of the review.
        let reopened = ReviewDetailViewModel(review: list.reviews[0], client: client)
        XCTExpectFailure("CLIENT-56: the list's copy never learns about the draft, so reopening offers a second paid draft", strict: true) {
            XCTAssertFalse(reopened.needsDraft)
        }
    }

    // MARK: CLIENT-30 — filters over the first page only

    func testTheUrgentFilterFindsAnUrgentReviewBeyondTheFirstPage() async {
        let client = EdgeHTTP.client { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let q = Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.value ?? "") })
            if request.url?.path == "/mobile/api/review-stats" {
                return EdgeHTTP.reply(request, 500, #"{"ok": false}"#)
            }
            if q["filter"] == "urgent" {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "reviews": [\(Self.reviewsJSON(51...51, urgency: "high"))], "total": 1, "offset": 1, "has_more": false}
                """)
            }
            if q["offset"] == "0" {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "reviews": [\(Self.reviewsJSON(1...50))], "total": 51, "offset": 50, "has_more": true}
                """)
            }
            return EdgeHTTP.reply(request, 200, """
            {"ok": true, "reviews": [\(Self.reviewsJSON(51...51, urgency: "high"))], "total": 51, "offset": 51, "has_more": false}
            """)
        }
        let vm = ReviewsListViewModel(client: client)
        vm.filter = .urgent
        await vm.load()
        XCTAssertNil(vm.errorMessage)
        XCTExpectFailure("CLIENT-30: filtering runs over the first 50 rows only; review 51 is urgent and \"No urgent reviews\" shows", strict: true) {
            XCTAssertEqual(vm.filteredReviews.map(\.id), [51])
        }
    }

    func testAFailedInboxLoadIsReportedNotShownAsAnEmptyInbox() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = ReviewsListViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.errorMessage, "Internal error", "the view model records the failure")
        XCTAssertTrue(vm.reviews.isEmpty)
        // …but the list view never renders it: its empty state is gated on
        // errorMessage == nil and nothing else shows the message.
        let view = try EdgeSource.read("Features/Reviews/ReviewsListView.swift")
        let renders = view.components(separatedBy: "errorMessage").count - 1
        XCTExpectFailure("CLIENT-30: ReviewsListView checks errorMessage but never renders it, so a failed load reads as an empty inbox", strict: true) {
            XCTAssertGreaterThan(renders, 1, "errorMessage must be shown (with Retry), not only used to hide the empty state")
        }
    }

    func testAStalePageAfterARefreshIsNotAppended() async {
        // loadMore's answer can land after a pull-to-refresh replaced the
        // list; its rows belong to the old paging and must not be appended.
        EdgeHeldURLProtocol.reset()
        defer { EdgeHeldURLProtocol.reset() }
        let refreshed = Box(false)
        EdgeHeldURLProtocol.handler = { request in
            let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            let offset = items.first { $0.name == "offset" }?.value
            if request.url?.path == "/mobile/api/review-stats" { return EdgeHTTP.reply(request, 500, "{}") }
            if offset == "50" {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "reviews": [\(Self.reviewsJSON(900...901))], "total": 52, "offset": 52, "has_more": false}
                """)
            }
            if refreshed.value {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "reviews": [\(Self.reviewsJSON(1...3))], "total": 3, "offset": 3, "has_more": false}
                """)
            }
            return EdgeHTTP.reply(request, 200, """
            {"ok": true, "reviews": [\(Self.reviewsJSON(1...50))], "total": 52, "offset": 50, "has_more": true}
            """)
        }
        EdgeHeldURLProtocol.shouldHold = { request in
            URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "offset" }?.value == "50"
        }
        let vm = ReviewsListViewModel(client: EdgeHeldURLProtocol.makeClient())
        await vm.load()
        XCTAssertEqual(vm.reviews.count, 50)
        let more = Task { await vm.loadMore() }
        await EdgeHTTP.waitUntil { EdgeHeldURLProtocol.held.value }
        // A refresh (to one platform) lands while page two is in flight.
        refreshed.value = true
        await vm.load(platform: "yelp")
        XCTAssertEqual(vm.reviews.map(\.id), [1, 2, 3], "the refresh itself landed")
        EdgeHeldURLProtocol.release.signal()
        await more.value
        XCTExpectFailure("CLIENT-30: loadMore appends a page requested before the refresh", strict: true) {
            XCTAssertEqual(vm.reviews.map(\.id), [1, 2, 3])
        }
    }

    // MARK: CLIENT-49 — a cancelled load is not an error

    func testACancelledInboxLoadSetsNoError() async {
        // What URLSession raises when the view's .task is torn down.
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        let vm = ReviewsListViewModel(client: client)
        await vm.load()
        XCTExpectFailure("CLIENT-49: ReviewsListViewModel has no CancellationError branch; \"Couldn't load reviews.\" flashes after navigating away", strict: true) {
            XCTAssertNil(vm.errorMessage)
        }
    }

    // MARK: CLIENT-54 — the sentiment river's rating line

    func testTheSentimentRiverSkipsWeeksWithNoReviews() throws {
        // The Canvas drawing has no seam a unit test can read; the rating
        // series is built at the source. A week with no reviews has
        // avg_rating 0, which the fixed 3.8-5.0 axis plots below the chart
        // and labels "0.0★" — a missing measurement drawn as zero.
        let source = try EdgeSource.read("Features/Reviews/SentimentRiverChart.swift")
        let draw = try XCTUnwrap(EdgeSource.slice(source, from: "private func draw(", length: 4200))
        let ratingLine = try XCTUnwrap(EdgeSource.slice(draw, from: "// Rating line", length: 1400))
        XCTExpectFailure("CLIENT-54: the rating line maps every week's avgRating, zero-review weeks included", strict: true) {
            XCTAssertTrue(ratingLine.contains("total > 0") || ratingLine.contains("total == 0")
                          || ratingLine.contains("avgRating > 0"),
                          "weeks with no reviews must be left out of the rating line")
        }
    }
}
