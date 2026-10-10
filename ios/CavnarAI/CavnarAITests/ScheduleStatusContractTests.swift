import XCTest
@testable import CavnarAI

/// The schedule payloads exactly as the server sends them (schedule audit
/// 10/10/26 TRUST-1). The server sends `narrative` as a list; the phone
/// declared a String, so every finished generation polled on the phone
/// failed to decode — and the hand-written fixture that should have caught
/// it held a string. These fixtures are CAPTURED, not written:
/// tests/test_ios_schedule_payload_contract.py runs a real generation (only
/// the model's answer stubbed), polls /mobile/api/labor/schedule-status
/// while it runs and when it is done, and reopens it through
/// /mobile/api/labor/schedule-history/<id>; that test fails when the
/// server's payload changes shape, and re-captures these files.
final class ScheduleStatusContractTests: XCTestCase {

    private static func data(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: ScheduleStatusContractTests.self).url(forResource: name,
                                                                                    withExtension: "json"))
        return try Data(contentsOf: url)
    }

    private static func decode(_ name: String) throws -> GeneratedSchedule {
        try JSONDecoder().decode(GeneratedSchedule.self, from: data(name))
    }

    private static func object(_ name: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data(name)) as? [String: Any])
    }

    func testAFinishedGenerationDecodesWhole() throws {
        let s = try Self.decode("schedule_status_done")
        let raw = try Self.object("schedule_status_done")
        XCTAssertTrue(s.ok)
        XCTAssertEqual(s.status, "done")
        XCTAssertEqual(s.undecodedKeys, [], "a field of the real payload could not be read")
        // The bullets, as sent, and the answer card's reading of them.
        XCTAssertEqual(s.narrative, raw["narrative"] as? [String])
        XCTAssertEqual(LaborView.noteCard(s.narrative)?.head, s.narrative?.first)
        XCTAssertEqual(s.previewRows?.count, (raw["preview_rows"] as? [Any])?.count)
        XCTAssertNotNil(s.historyId)
        XCTAssertNotNil(s.version)
        XCTAssertNotNil(s.review?.lines)
        XCTAssertNotNil(s.ruleViolations)
        XCTAssertEqual(s.quality?.checked, true)
        XCTAssertNotNil(s.quality?.shifts)
        XCTAssertNotNil(s.managerPlan)
        XCTAssertNotNil(s.laborView)
        XCTAssertEqual(s.optimizer?.ran, true)
        XCTAssertEqual(s.whatIf?.ran, true)
        XCTAssertFalse(s.trimmedShifts.isEmpty)
        XCTAssertFalse((s.holidayLift ?? [:]).isEmpty)
        XCTAssertFalse((s.staffConstraints ?? [:]).isEmpty)
        XCTAssertFalse((s.pendingTimeOff ?? [:]).isEmpty)
        // The Halloween close, across the clock change.
        XCTAssertTrue((s.previewRows ?? []).contains { ($0.dstHours ?? 0) != 0 })
        // What the poll does on "done" — the scorecard opens on a stored week.
        XCTAssertNotNil(s.historyId, "showingDraftSummary needs the history id")
    }

    func testAPendingPollDecodesItsClockAndProgress() throws {
        let s = try Self.decode("schedule_status_pending")
        XCTAssertEqual(s.status, "pending")
        XCTAssertEqual(s.undecodedKeys, [])
        XCTAssertEqual(s.progress?.line, "3 of 7 days drafted")
        XCTAssertGreaterThan(s.secondsLeft ?? 0, 0)
    }

    func testAReopenedWeekDecodesWhole() throws {
        let s = try Self.decode("schedule_history_detail")
        let raw = try Self.object("schedule_history_detail")
        XCTAssertTrue(s.ok)
        XCTAssertEqual(s.undecodedKeys, [])
        XCTAssertEqual(s.previewRows?.count, (raw["preview_rows"] as? [Any])?.count)
        XCTAssertNotNil(s.version)
        XCTAssertNotNil(s.review)
        XCTAssertEqual(s.quality?.checked, true)
        XCTAssertNotNil(s.storedWhatIf)
    }

    /// Key by key, so a field that stops decoding is named rather than
    /// hidden behind the first one.
    func testEveryKeyOfEveryRealPayloadDecodesOnItsOwn() throws {
        for name in ["schedule_status_done", "schedule_status_pending", "schedule_history_detail"] {
            let raw = try Self.object(name)
            for (key, value) in raw where key != "ok" {
                let one = try JSONSerialization.data(withJSONObject: ["ok": true, key: value])
                let s = try JSONDecoder().decode(GeneratedSchedule.self, from: one)
                XCTAssertEqual(s.undecodedKeys, [], "\(name): \(key)")
            }
        }
    }

    /// The finished week is what the on-device cache keeps.
    func testTheRealPayloadSurvivesTheCache() throws {
        let s = try Self.decode("schedule_status_done")
        let again = try JSONDecoder().decode(GeneratedSchedule.self, from: JSONEncoder().encode(s))
        XCTAssertEqual(again.narrative, s.narrative)
        XCTAssertEqual(again.review, s.review)
        XCTAssertEqual(again.previewRows?.map(\.signature), s.previewRows?.map(\.signature))
        XCTAssertEqual(again.undecodedKeys, [])
    }
}
