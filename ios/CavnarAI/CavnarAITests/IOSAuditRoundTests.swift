import XCTest
@testable import CavnarAI

/// The iOS audit round of 9/25/26: a review list that one integer emptied,
/// an offline queue that held a refused write for a day, a drain that
/// removed the wrong write, and a cold launch that ran as nobody.
@MainActor
final class IOSAuditRoundTests: XCTestCase {

    override func tearDown() async throws {
        await PendingWriteQueue.shared.clear()
        await PendingWriteQueue.shared.setActiveRestaurant(nil)
        MockURLProtocol.requestHandler = nil
    }

    // MARK: 1 — a SQLite 0/1 flag never fails the review list

    /// The raw server shape before the fix: models.get_reviews_data sent
    /// `draft_needs_review` straight from the column, as a number.
    private static let rawRow = """
    {"id": 41, "platform": "google", "author": "Ann", "rating": 2, "text": "Slow.",
     "review_date": "2026-09-20", "sentiment": "negative", "urgency": "high",
     "draft_response": "Sorry, Ann.", "response_status": "drafted", "categories": ["service"],
     "draft_needs_review": 0, "draft_review_reason": null, "draft_edited": 1,
     "processed": 1, "can_retract": 0}
    """

    func testAReviewWhoseFlagsArriveAsNumbersStillDecodes() throws {
        let review = try JSONDecoder().decode(Review.self, from: Data(Self.rawRow.utf8))
        XCTAssertEqual(review.draftNeedsReview, false)
        XCTAssertFalse(review.draftIsFlagged)
        XCTAssertEqual(review.processed, true)
        XCTAssertFalse(review.canRetract)
    }

    func testOneNumericFlagNoLongerEmptiesTheWholeList() throws {
        let flagged = Self.rawRow.replacingOccurrences(of: "\"draft_needs_review\": 0", with: "\"draft_needs_review\": 1")
            .replacingOccurrences(of: "\"id\": 41", with: "\"id\": 42")
        let list = try JSONDecoder().decode([Review].self, from: Data("[\(Self.rawRow), \(flagged)]".utf8))
        XCTAssertEqual(list.map(\.id), [41, 42])
        XCTAssertEqual(list.map(\.draftIsFlagged), [false, true])
    }

    func testTheLenientFlagReadsEveryShapeAndNeverThrows() throws {
        struct Box: Codable { @LenientBool var flag: Bool? }
        func read(_ json: String) throws -> Bool? {
            let box = try JSONDecoder().decode(Box.self, from: Data(json.utf8)); return box.flag
        }
        XCTAssertEqual(try read(#"{"flag": true}"#), true)
        XCTAssertEqual(try read(#"{"flag": false}"#), false)
        XCTAssertEqual(try read(#"{"flag": 1}"#), true)
        XCTAssertEqual(try read(#"{"flag": 0}"#), false)
        XCTAssertEqual(try read(#"{"flag": "1"}"#), true)
        XCTAssertEqual(try read(#"{"flag": "false"}"#), false)
        XCTAssertNil(try read(#"{"flag": null}"#))
        XCTAssertNil(try read(#"{}"#), "a missing key is nil, not keyNotFound")
        XCTAssertNil(try read(#"{"flag": {"odd": 1}}"#))
        // What the offline cache writes back reads the same.
        let review = try JSONDecoder().decode(Review.self, from: Data(Self.rawRow.utf8))
        let again = try JSONDecoder().decode(Review.self, from: JSONEncoder().encode(review))
        XCTAssertEqual(again, review)
    }

    // MARK: 2 — a refused queued write, and what depended on it

    private final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var _paths: [String] = []
        var paths: [String] { lock.withLock { _paths } }
        func add(_ p: String) { lock.withLock { _paths.append(p) } }
    }

    private func queue(_ q: PendingWriteQueue, _ path: String, _ label: String) async {
        await q.enqueue(path: path, method: "POST", bodyJSON: Data("{}".utf8), label: label)
    }

    func testARefusedDraftSaveTakesItsApproveWithItAndTheRestStillSend() async {
        let q = PendingWriteQueue.shared
        await q.clear()
        await q.setActiveRestaurant(7)
        let sent = Recorder()
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            sent.add(path)
            if path.hasSuffix("/save-draft") {
                // _do_save_draft's refusal: HTTP 200, ok false.
                return EdgeHTTP.reply(request, 200, #"{"ok": false, "error": "Draft cannot be empty"}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        await queue(q, "/mobile/api/reviews/5/save-draft", "Save draft response")
        await queue(q, "/mobile/api/reviews/5/approve", "Approve response for Ann")
        await queue(q, QueuedWrite.recEventPath, "Mark a recommendation done")

        await q.drain(client: client)

        XCTAssertEqual(sent.paths, ["/mobile/api/reviews/5/save-draft", QueuedWrite.recEventPath],
                       "the approve must never go out behind a refused save: it would post the old stored draft")
        let left = await q.pendingCount
        XCTAssertEqual(left, 0, "nothing refused stays at the head of the queue")
        let dropped = await q.dropped
        XCTAssertEqual(dropped.map(\.label), ["Save draft response", "Approve response for Ann"])
        XCTAssertEqual(dropped.first?.reason, "Draft cannot be empty")
        XCTAssertEqual(PendingWriteQueue.droppedNote(dropped),
                       "2 changes couldn't be sent: Save draft response (and 1 more). Draft cannot be empty. Tap to dismiss.")
        await q.dismissDropped()
        let afterDismiss = await q.dropped
        XCTAssertTrue(afterDismiss.isEmpty)
    }

    func testARefusedApproveIsDroppedWithTheServersReasonAndOthersKeepDraining() async {
        let q = PendingWriteQueue.shared
        await q.clear()
        await q.setActiveRestaurant(7)
        let sent = Recorder()
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            sent.add(path)
            if path == "/mobile/api/reviews/5/approve" {
                return EdgeHTTP.reply(request, 409, """
                {"ok": false, "needs_review": true, "review_reason": "promises a refund",
                 "error": "Read this reply before you post it: it promises a refund. Open the review to post it anyway."}
                """)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        await queue(q, "/mobile/api/reviews/5/approve", "Approve response for Ann")
        await queue(q, "/mobile/api/reviews/6/approve", "Approve response for Bo")

        await q.drain(client: client)

        XCTAssertEqual(sent.paths, ["/mobile/api/reviews/5/approve", "/mobile/api/reviews/6/approve"],
                       "another review's approve does not depend on this one")
        let dropped = await q.dropped
        XCTAssertEqual(dropped.map(\.label), ["Approve response for Ann"])
        XCTAssertTrue(dropped.first?.reason.hasPrefix("Read this reply before you post it") == true)
    }

    func testAPassingFailureStillHoldsTheQueueInOrder() async {
        let q = PendingWriteQueue.shared
        await q.clear()
        await q.setActiveRestaurant(7)
        let sent = Recorder()
        let client = EdgeHTTP.client { request in
            sent.add(request.url?.path ?? "")
            return EdgeHTTP.reply(request, 503, #"{"ok": false, "error": "Service unavailable"}"#)
        }
        await queue(q, "/mobile/api/reviews/5/save-draft", "Save draft response")
        await queue(q, "/mobile/api/reviews/5/approve", "Approve")

        await q.drain(client: client)

        XCTAssertEqual(sent.paths, ["/mobile/api/reviews/5/save-draft"], "a 5xx stops the drain; nothing overtakes")
        let left = await q.pendingCount
        XCTAssertEqual(left, 2)
        let dropped = await q.dropped
        XCTAssertTrue(dropped.isEmpty)
    }

    func testWhatDependsOnWhat() {
        XCTAssertEqual(QueuedWrite.dependencyKey(path: "/mobile/api/reviews/5/save-draft"), "review:5")
        XCTAssertEqual(QueuedWrite.dependencyKey(path: "/mobile/api/reviews/5/approve"), "review:5")
        XCTAssertNotEqual(QueuedWrite.dependencyKey(path: "/mobile/api/reviews/6/approve"), "review:5")
        XCTAssertNil(QueuedWrite.dependencyKey(path: QueuedWrite.recEventPath))
        XCTAssertNil(QueuedWrite.dependencyKey(path: QueuedWrite.countSheetPath))
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 200), "a 2xx carried by an error is ok:false")
    }

    func testTheQueuedApproveCarriesTheDraftItApproved() throws {
        let body = ReviewDetailViewModel.ApproveBody(confirmFlagged: false, expectedDraft: "Thanks, Ann.")
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual(json?["expected_draft"] as? String, "Thanks, Ann.")
        XCTAssertEqual(json?["confirm_flagged"] as? Bool, false)
    }

    // MARK: 3 — removal by id, not by position

    func testASignOutDuringASendDoesNotRemoveTheNextWrite() async {
        let q = PendingWriteQueue.shared
        await q.clear()
        await q.setActiveRestaurant(7)
        let sent = Recorder()
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            sent.add(path)
            if path == "/mobile/api/reviews/1/approve" {
                // While the first write is in flight the queue is cleared
                // (sign-out) and a new write arrives. removeFirst() used to
                // crash on the empty queue, or delete the newcomer.
                let done = DispatchSemaphore(value: 0)
                Task.detached {
                    await q.clear()
                    await q.enqueue(path: "/mobile/api/reviews/2/approve", method: "POST",
                                    bodyJSON: Data("{}".utf8), label: "Newcomer")
                    done.signal()
                }
                done.wait()
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        await queue(q, "/mobile/api/reviews/1/approve", "First")

        await q.drain(client: client)

        XCTAssertEqual(sent.paths, ["/mobile/api/reviews/1/approve", "/mobile/api/reviews/2/approve"],
                       "the newcomer is sent, not silently removed in the first write's place")
        let left = await q.pendingCount
        XCTAssertEqual(left, 0)
    }

    // MARK: 5 — a cold launch resumes its own scope

    private func meClient(restaurantId: Int, userId: Int = 3, timezone: String = "America/New_York") -> APIClient {
        EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/me" {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "timezone": "\(timezone)",
                 "user": {"id": \(userId), "username": "o", "email": "o@x.com", "restaurant_id": \(restaurantId),
                          "role": "owner", "is_admin": false}}
                """)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
    }

    private func settle() async { try? await Task.sleep(nanoseconds: 250_000_000) }

    func testAStoredSessionResumesItsScopeBeforeMeAnswers() async {
        SessionScope.begin(userId: 3, restaurantId: 11)   // what the last process recorded
        let store = SessionStore(client: meClient(restaurantId: 11), storedToken: "t")
        // Synchronously, at init: not user 0 at restaurant 0.
        XCTAssertEqual(SessionScope.restaurantId, 11)
        XCTAssertEqual(SessionScope.key("home.summary"), "home.summary.u3.r11")
        let resumed = SessionScope.generation

        await settle()
        XCTAssertEqual(store.currentUser?.restaurantId, 11)
        XCTAssertEqual(SessionScope.generation, resumed,
                       "/me confirming the same scope must not discard Home's first load")
        XCTAssertTrue(RestaurantClock.isKnown, "the restaurant clock is learned from /me")
        XCTAssertEqual(RestaurantClock.timeZone.identifier, "America/New_York")
    }

    func testMeNamingAnotherScopeBeginsItAndReloads() async {
        SessionScope.begin(userId: 3, restaurantId: 11)
        let store = SessionStore(client: meClient(restaurantId: 12), storedToken: "t")
        var reloaded: [Int] = []
        store.onLocationSwitched = { reloaded.append($0) }
        let resumed = SessionScope.generation
        await settle()
        XCTAssertEqual(SessionScope.restaurantId, 12)
        XCTAssertGreaterThan(SessionScope.generation, resumed)
        XCTAssertEqual(reloaded, [12], "what loaded under the old scope was discarded, so the app reloads")
    }

    func testTheRouterUsesThePersistedLocationOnAColdLaunch() {
        SessionScope.begin(userId: 3, restaurantId: 11)
        XCTAssertEqual(DeepLinkRouter().activeRestaurantId(), 11)
    }

    func testAFailedSwitchOpensNothingAndSaysWhy() async {
        let router = DeepLinkRouter()
        router.activeRestaurantId = { 2 }
        router.switchLocation = { _ in false }
        router.handleNotificationTap(alertType: "1star", reviewId: 55, restaurantId: 5)
        for _ in 0..<20 where router.locationSwitchFailure == nil {
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        XCTAssertNil(router.pendingReviewID, "location A's review must not open inside location B")
        XCTAssertEqual(router.locationSwitchFailure, DeepLinkRouter.switchFailedMessage)
    }
}
