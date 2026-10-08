import XCTest
@testable import CavnarAI

/// Parity audit #24 — the request bodies tests/test_ios_request_body_parity.py
/// found short of what their routes read. Each now carries the key the web
/// sends and the server reads, with the JSON name the route reads it by.
@MainActor
final class RequestBodyParityTests: XCTestCase {

    private func json<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    /// POST /ask-cavnar/action logs the proposal's body with the answer
    /// (client_api.log_ask_action), as the web's body does.
    func testAskActionOutcomeCarriesTheProposalBody() throws {
        let body = AskCavnarViewModel.ActionOutcomeBody(
            action: "email_guest", outcome: "confirmed", summary: "Email Fresh Co",
            body: ["to": .string("a@b.test"), "count": .int(2)],
            conversation_id: 7, proposal_id: 9, reason: nil)
        let j = try json(body)
        let sent = try XCTUnwrap(j["body"] as? [String: Any])
        XCTAssertEqual(sent["to"] as? String, "a@b.test")
        XCTAssertEqual(sent["count"] as? Int, 2)
        XCTAssertEqual(j["proposal_id"] as? Int, 9)
        XCTAssertEqual(j["conversation_id"] as? Int, 7)
    }

    /// POST /account/report-bug files ios_version and screen in the report's
    /// meta (mobile_api.mobile_report_bug).
    func testBugReportCarriesIOSVersionAndScreen() throws {
        let j = try json(AccountViewModel.BugReportBody(
            message: "It froze", build: "v1 · build abc", device: "iPhone · iOS 26.1",
            appVersion: "1.0", iosVersion: "26.1", screen: "ios:account"))
        XCTAssertEqual(Set(j.keys), ["message", "build", "device", "app_version", "ios_version", "screen"])
        XCTAssertEqual(j["ios_version"] as? String, "26.1")
        XCTAssertEqual(j["screen"] as? String, "ios:account")
    }

    /// POST /labor/schedule/violations names the week (`history_id`), as
    /// the web's does.
    func testScheduleViolationsNamesTheWeek() throws {
        let j = try json(LaborViewModel.ViolationsBody(rows: [], baselineRows: [], historyId: 41))
        XCTAssertEqual(j["history_id"] as? Int, 41)
        XCTAssertNotNil(j["rows"])
        XCTAssertNotNil(j["baseline_rows"])
    }

    /// POST /labor/schedule/optimize carries the day targets the week on
    /// screen was built against (`daily_target_hours`), as the web's does.
    func testScheduleOptimizeCarriesTheDayTargets() throws {
        let j = try json(LaborViewModel.OptimizeBody(rows: [], historyId: 41,
                                                    dailyTargetHours: ["2026-10-12": 64.5]))
        let targets = try XCTUnwrap(j["daily_target_hours"] as? [String: Double])
        XCTAssertEqual(targets["2026-10-12"], 64.5)
        XCTAssertEqual(j["history_id"] as? Int, 41)
    }
}
