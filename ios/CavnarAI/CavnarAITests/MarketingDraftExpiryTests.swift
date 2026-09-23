import XCTest
@testable import CavnarAI

/// A quiet-night post or guest text still unapproved three days after it was
/// written comes back with status "expired". It must decode, read as
/// Expired, and offer nothing that would release it — Delete only.
final class MarketingDraftExpiryTests: XCTestCase {
    private let draftsJSON = """
    {"ok": true, "drafts": [
      {"id": 1, "content_type": "instagram", "topic": "Slow Tuesday", "body": "Half-price apps tonight.",
       "status": "expired", "updated_at": "2026-09-18 21:00:00", "created_by_name": "Cavnar AI",
       "approved_by_name": null, "media_token": null},
      {"id": 2, "content_type": "instagram", "topic": "Brunch", "body": "Brunch is back.",
       "status": "draft", "created_by_name": "Will"},
      {"id": 3, "topic": "Patio", "body": "Patio's open.", "status": "approved",
       "created_by_name": "Ana", "approved_by_name": "Will"},
      {"id": 4, "body": "Something newer.", "status": "archived_by_future_server"}
    ]}
    """

    func testDraftsPayloadWithAnExpiredDraftDecodes() throws {
        struct List: Decodable { let ok: Bool; let drafts: [MarketingDraft] }
        let list = try JSONDecoder.cavnar.decode(List.self, from: Data(draftsJSON.utf8))
        XCTAssertEqual(list.drafts.count, 4)
        let expired = list.drafts[0]
        XCTAssertTrue(expired.isExpired)
        XCTAssertEqual(expired.statusLabel, "Expired")
        XCTAssertEqual(expired.expiredNote, "Its night has passed, so it can\u{2019}t be approved or sent.")
        XCTAssertEqual(list.drafts[1].statusLabel, "Draft")
        XCTAssertEqual(list.drafts[2].statusLabel, "Approved")
        // An unknown status decodes and reads as itself, not as a live draft.
        XCTAssertNotEqual(list.drafts[3].statusLabel, "Draft")
        XCTAssertFalse(list.drafts[3].isExpired)
        XCTAssertNil(list.drafts[1].expiredNote)
    }

    func testExpiredDraftExposesNoApproveOrSendActions() throws {
        struct List: Decodable { let drafts: [MarketingDraft] }
        let drafts = try JSONDecoder.cavnar.decode(List.self, from: Data(draftsJSON.utf8)).drafts
        let expired = drafts[0], live = drafts[1], approved = drafts[2]
        XCTAssertFalse(expired.canApprove)
        XCTAssertFalse(expired.canOpenInComposer, "Open leads to Post / Schedule / Send")
        XCTAssertTrue(live.canApprove)
        XCTAssertTrue(live.canOpenInComposer)
        XCTAssertFalse(approved.canApprove)
        XCTAssertTrue(approved.canOpenInComposer)
    }

    @MainActor
    func testApprovingAnExpiredDraftSendsNothing() async throws {
        let requests = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            requests.value.append("\(request.httpMethod ?? "") \(request.url?.path ?? "")")
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        struct List: Decodable { let drafts: [MarketingDraft] }
        let expired = try JSONDecoder.cavnar.decode(List.self, from: Data(draftsJSON.utf8)).drafts[0]
        let vm = MarketingComposeViewModel(client: client)
        await vm.approve(expired)
        XCTAssertTrue(requests.value.isEmpty, "no approve request for an expired draft")
        XCTAssertEqual(vm.draftError, expired.expiredNote)
    }
}
