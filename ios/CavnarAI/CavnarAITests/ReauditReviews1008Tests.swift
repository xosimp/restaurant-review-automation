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

    func testTheBulkConfirmTitleCountsReplies() {
        XCTAssertEqual(BulkApproveConfirmSheet.title(1), "Approve and post 1 reply?")
        XCTAssertEqual(BulkApproveConfirmSheet.title(3), "Approve and post 3 replies?")
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
