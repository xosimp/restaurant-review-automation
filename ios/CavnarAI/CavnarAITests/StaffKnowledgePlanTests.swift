import XCTest
@testable import CavnarAI

/// House rules, staff docs, certifications and the staff brief are Labor's:
/// the server answers their routes 403 module_locked when the plan has no
/// Labor (release-safety audit #25, WS4's module map). Account hides the row
/// when the plan's modules say Labor is off, and a refusal that still arrives
/// reads as the plan's sentence — never three errors with Try again.
final class StaffKnowledgePlanTests: XCTestCase {

    private func features(_ json: String) throws -> [AccountHealth.Feature] {
        try JSONDecoder.cavnar.decode(AccountHealth.self, from: Data(json.utf8)).features
    }

    @MainActor
    func testTheRowFollowsTheLaborModule() throws {
        let off = try features(#"{"ok": true, "features": [{"key": "reviews", "label": "Reviews", "detail": "", "on": true}, {"key": "labor", "label": "Labor", "detail": "", "on": false}]}"#)
        let on = try features(#"{"ok": true, "features": [{"key": "labor", "label": "Labor", "detail": "", "on": true}]}"#)
        let unnamed = try features(#"{"ok": true, "features": [{"key": "reviews", "label": "Reviews", "detail": "", "on": true}]}"#)
        XCTAssertFalse(AccountView.showsStaffKnowledge(off), "no Labor on the plan: the row is hidden")
        XCTAssertTrue(AccountView.showsStaffKnowledge(on))
        // Not known yet (health not loaded) or not said: shown — the server
        // still refuses, and the refusal reads as the plan's sentence.
        XCTAssertTrue(AccountView.showsStaffKnowledge(nil))
        XCTAssertTrue(AccountView.showsStaffKnowledge(unnamed))
    }

    @MainActor
    func testARefusalForThePlanIsThePlanMessageNotAnError() async throws {
        MockURLProtocol.requestHandler = { request in
            EdgeHTTP.reply(request, 403, #"{"ok": false, "error": "Labor isn't on your plan — ask about adding it.", "module_locked": true, "module": "labor"}"#)
        }
        defer { MockURLProtocol.requestHandler = nil }
        let model = PeopleRulesViewModel(client: APIClient(baseURL: URL(string: "https://example.com")!,
                                                           session: MockURLProtocol.makeSession()))
        await model.load()
        XCTAssertEqual(model.planMessage, "Labor isn't on your plan — ask about adding it.")
        XCTAssertNil(model.rulesError)
        XCTAssertNil(model.docsError)
        XCTAssertNil(model.certsError)
        // Any other refusal is still the section's own error.
        let refusal = APIClient.APIError(kind: .server, message: "Something went wrong", status: 500)
        XCTAssertNil(PeopleRulesViewModel.planRefusal(refusal))
        XCTAssertNotNil(PeopleRulesViewModel.planRefusal(APIClient.APIError(kind: .moduleLocked, message: "", status: 403)))
    }
}
