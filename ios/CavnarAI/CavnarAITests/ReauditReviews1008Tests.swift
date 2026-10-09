import XCTest
@testable import CavnarAI

/// Blind re-audit of the Reviews / Intel round (10/8/26): the request
/// shapes and decoding behind #1, #2 (push #3), #5, #7, #9 and #12.
final class ReauditReviews1008Tests: XCTestCase {

    private func object(_ value: some Encodable) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any] ?? [:]
    }

    // MARK: #1 approve_skipped only from "Approve after all"

    func testApproveSkippedIsSentOnlyWhenSet() throws {
        let plain = try object(ReviewDetailViewModel.ApproveBody(confirmFlagged: false, expectedDraft: "Thanks"))
        XCTAssertNil(plain["approve_skipped"], "a plain approve never carries it")
        XCTAssertEqual(plain["expected_draft"] as? String, "Thanks")
        let afterAll = try object(ReviewDetailViewModel.ApproveBody(confirmFlagged: true, expectedDraft: "Thanks",
                                                                    approveSkipped: true))
        XCTAssertEqual(afterAll["approve_skipped"] as? Bool, true)
        XCTAssertEqual(afterAll["confirm_flagged"] as? Bool, true)
    }

    // MARK: #2 / push #3 the lock-screen approve carries the fingerprint

    func testAClippedPushApproveSendsTheHashNotTheText() throws {
        let action = try XCTUnwrap(PushManager.backgroundAction(
            for: PushManager.approvePostAction,
            cavnar: ["review_id": 4, "draft": "Thanks…", "draft_complete": false, "draft_hash": "ABC123"]))
        let body = try object(try XCTUnwrap(action.body))
        XCTAssertNil(body["expected_draft"])
        XCTAssertEqual(body["expected_draft_hash"] as? String, "abc123")
    }

    func testADroppedDraftStillBindsByHash() throws {
        let action = try XCTUnwrap(PushManager.backgroundAction(
            for: PushManager.approvePostAction, cavnar: ["review_id": 4, "draft_hash": "def456"]))
        XCTAssertEqual(try object(try XCTUnwrap(action.body))["expected_draft_hash"] as? String, "def456")
    }

    func testAWholePushApproveSendsBoth() throws {
        let action = try XCTUnwrap(PushManager.backgroundAction(
            for: PushManager.approvePostAction,
            cavnar: ["review_id": 4, "draft": "Thanks!", "draft_complete": true, "draft_hash": "ff00"]))
        let body = try object(try XCTUnwrap(action.body))
        XCTAssertEqual(body["expected_draft"] as? String, "Thanks!")
        XCTAssertEqual(body["expected_draft_hash"] as? String, "ff00")
    }

    // MARK: #5 / #9 the bulk confirm and its result

    func testTheBulkAnswerSaysWhatChanged() throws {
        let r = try JSONDecoder().decode(BulkPublishResult.self, from: Data(
            #"{"ok": true, "approved": 1, "posted": 0, "failed": 0, "remaining": 0, "changed": 2}"#.utf8))
        XCTAssertEqual(r.changed, 2)
        let older = try JSONDecoder().decode(BulkPublishResult.self, from: Data(
            #"{"ok": true, "approved": 1, "posted": 0, "failed": 0}"#.utf8))
        XCTAssertNil(older.changed)
    }

    private func review(_ id: Int, platform: String) throws -> Review {
        try JSONDecoder.cavnar.decode(Review.self, from: Data("""
            {"id": \(id), "platform": "\(platform)", "author": "Ann", "rating": 4, "text": "Fine",
             "review_date": "2026-10-01T18:00:00", "urgency": "normal", "draft_response": "Thanks!",
             "response_status": "drafted", "categories": []}
            """.utf8))
    }

    /// Re-audit 10/8/26 M1: the confirm names where each reply goes, and
    /// only says "post" for a Google reply with Google connected.
    @MainActor
    func testTheConfirmSaysWhereEachReplyGoes() throws {
        let g = try review(1, platform: "google"), g2 = try review(2, platform: "google")
        let y = try review(3, platform: "yelp")
        XCTAssertEqual(BulkApproveConfirmSheet.title([g, g2], googleConnected: true), "Approve and post 2 replies?")
        XCTAssertEqual(BulkApproveConfirmSheet.title([g, y], googleConnected: true), "Approve 2 replies?")
        XCTAssertEqual(BulkApproveConfirmSheet.title([g], googleConnected: false), "Approve this reply?")
        XCTAssertEqual(BulkApproveConfirmSheet.title([g], googleConnected: nil), "Approve this reply?")
        XCTAssertEqual(BulkApproveConfirmSheet.title([g], googleConnected: true), "Approve and post this reply to Google?")
        XCTAssertEqual(BulkApproveConfirmSheet.buttonLabel([y], googleConnected: true), "Approve")
        let line = BulkApproveConfirmSheet.destinations([g, g2, y], googleConnected: true)
        XCTAssertEqual(line, "2 post to Google under your name \u{00B7} 1 approved for you to post on Yelp")
        XCTAssertTrue(BulkApproveConfirmSheet.destinations([y], googleConnected: nil).contains("post on Yelp"))
    }

    /// H1: the swipe goes through approve-all pinned to its one reply and
    /// its words; the answer reads as the row's outcome.
    @MainActor
    func testTheSwipeIsOneBoundBulkApprove() throws {
        let body = try XCTUnwrap(ReviewsListViewModel.bulkApproveBodies([try review(7, platform: "google")]).first)
        XCTAssertEqual(body.reviewIds, [7])
        XCTAssertEqual(body.reviewHashes?["7"], ReviewsListViewModel.draftHash("Thanks!"))
        func result(_ json: String) throws -> BulkPublishResult {
            try JSONDecoder().decode(BulkPublishResult.self, from: Data(json.utf8))
        }
        XCTAssertEqual(ReviewsListViewModel.quickOutcome(try result(#"{"approved": 1, "posted": 1, "failed": 0}"#)), .posted)
        XCTAssertEqual(ReviewsListViewModel.quickOutcome(try result(#"{"approved": 1, "posted": 0, "failed": 0}"#)), .approved)
        XCTAssertEqual(ReviewsListViewModel.quickOutcome(try result(#"{"approved": 0, "posted": 0, "failed": 0, "changed": 1}"#)), .changed)
        if case .held = ReviewsListViewModel.quickOutcome(try result(#"{"approved": 0, "posted": 0, "failed": 0}"#)) {} else {
            XCTFail("held by the public-reply check reads as held")
        }
        // A Yelp reply approved from the list is a next step, not a failure (M2).
        XCTAssertTrue(ReviewsListView.approvedNote(try review(3, platform: "yelp")).hasPrefix("Approved \u{00B7} post it on Yelp"))
    }

    /// H2: only a post Google refused holds the queue.
    @MainActor
    func testTheQueueMovesOnUnlessGoogleRefusedThePost() throws {
        func outcome(_ json: String) throws -> ReviewPostOutcome {
            try JSONDecoder().decode(ReviewPostOutcome.self, from: Data(json.utf8))
        }
        XCTAssertTrue(try outcome(#"{"ok": true, "post_status": "not_google"}"#).advancesQueue)
        XCTAssertTrue(try outcome(#"{"ok": true, "post_status": "not_connected"}"#).advancesQueue)
        XCTAssertTrue(try outcome(#"{"ok": true, "post_status": "posted"}"#).advancesQueue)
        XCTAssertFalse(try outcome(#"{"ok": true, "post_status": "failed", "post_error": "no"}"#).advancesQueue)
        XCTAssertEqual(ReviewDetailView.doneLabel(status: "approved", review: try review(3, platform: "yelp")),
                       "Approved \u{00B7} post it on Yelp")
    }

    /// L15 / M10: cross-checks in owner words; one answer, the cause and the
    /// action said once when a diagnosis card renders.
    @MainActor
    func testTheReadIsOneAnswer() {
        XCTAssertEqual(ReviewsAnalyticsSection.metricWords("avg_ticket_time_min").0, "Average ticket time")
        XCTAssertEqual(ReviewsAnalyticsSection.metricWords("avg_ticket_time_min").1, " min")
        XCTAssertEqual(ReviewsAnalyticsSection.metricWords("labor_pct").1, "%")
        let lines = ["📊 This week: 12 reviews.", "⚠️ Watch cold food.", "✅ Do today: call two guests.",
                     "🔍 Why: the pass is slow.", "🔮 Next week: steady."]
        let withDiagnosis = ReviewsAnalyticsSection.answerParts(lines: lines, trendSentence: "Rating improving",
                                                                hasDiagnosis: true)
        XCTAssertEqual(withDiagnosis.headline, "Rating improving")
        XCTAssertNil(withDiagnosis.action)
        XCTAssertNil(withDiagnosis.why)
        XCTAssertFalse(withDiagnosis.rest.contains { $0.hasPrefix("🔍") || $0.hasPrefix("✅") })
        let alone = ReviewsAnalyticsSection.answerParts(lines: lines, trendSentence: nil, hasDiagnosis: false)
        XCTAssertEqual(alone.headline, "This week: 12 reviews.")
        XCTAssertEqual(alone.summary, "Watch cold food.")
        XCTAssertEqual(alone.action, "✅ Do today: call two guests.")
        XCTAssertEqual(alone.why, "Why: the pass is slow.")
    }

    // MARK: #7 Read now only for logins the route accepts

    func testCanSyncDecodesAndDefaultsOff() throws {
        let yes = try JSONDecoder().decode(WebsiteConnection.self, from: Data(
            #"{"ok": true, "configured": true, "can_edit": false, "can_sync": true}"#.utf8))
        XCTAssertTrue(yes.canSync)
        let older = try JSONDecoder().decode(WebsiteConnection.self, from: Data(#"{"ok": true}"#.utf8))
        XCTAssertFalse(older.canSync)
        XCTAssertEqual(WebsiteAnalyticsViewModel.summaryPath, "/mobile/api/intel/website")
    }

    // MARK: #12 VoiceOver says the range and band

    func testVisibilityIsSpokenAsItsRangeAndBand() throws {
        let line = VisibilityOrbitChart.spokenSummary(score: 46, runs: [], low: 30, high: 62,
                                                      band: "comes up sometimes")
        XCTAssertTrue(line.hasPrefix("Somewhere between 30 and 62 out of 100. Comes up sometimes."), line)
        XCTAssertFalse(line.contains("46 out of 100"))
        XCTAssertEqual(VisibilityOrbitChart.spokenSummary(score: nil, runs: [], low: 30, high: 62), "Not measured.")
    }
}
