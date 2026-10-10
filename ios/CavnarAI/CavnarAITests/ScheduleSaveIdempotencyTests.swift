import XCTest
@testable import CavnarAI

/// A retried schedule save lands once (schedule audit 10/10/26 BUGSLIFE-22).
/// The save is sent with one silent retry for a request that died in
/// transit; when the server had already saved, the retry saved again (a
/// second identical version, the learning capture twice) or refused the
/// owner's own save as a conflict. Each save now carries a key made from
/// what it saves, and the server answers a repeat of that key from the
/// first save (tests/test_sf10_i2a_save_idempotency.py).
@MainActor
final class ScheduleSaveIdempotencyTests: XCTestCase {

    private func row(_ who: String, _ date: String = "2026-10-12") throws -> ScheduleRow {
        let json = """
        {"date": "\(date)", "day": "Monday", "employee": "\(who)", "role": "Server",
         "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": ""}
        """
        return try JSONDecoder().decode(ScheduleRow.self, from: Data(json.utf8))
    }

    private func body(_ who: String = "Ana", version: Int? = 3, history: Int? = 9,
                      targets: [String: Double] = ["2026-10-12": 40, "2026-10-13": 30]) throws -> LaborViewModel.ScoreBody {
        LaborViewModel.ScoreBody(rows: [try row(who)], dailyTargetHours: targets, save: true, historyId: history,
                                 version: version)
    }

    func testOneSaveIsOneKeyAndEveryOtherSaveAnother() throws {
        let key = try XCTUnwrap(LaborViewModel.saveKey(try body()))
        // The same save asked again — however its dictionary is ordered.
        XCTAssertEqual(LaborViewModel.saveKey(try body(targets: ["2026-10-13": 30, "2026-10-12": 40])), key)
        // A different edit, a newer base, another week: another save.
        XCTAssertNotEqual(LaborViewModel.saveKey(try body("Bob")), key)
        XCTAssertNotEqual(LaborViewModel.saveKey(try body(version: 4)), key)
        XCTAssertNotEqual(LaborViewModel.saveKey(try body(history: 10)), key)
        XCTAssertTrue(key.hasPrefix("/mobile/api/labor/schedule/score:"))
        XCTAssertLessThanOrEqual(key.count, 128, "the server reads 128 characters of a key")
    }

    func testTheKeyRidesTheBodyOfASaveOnly() throws {
        var save = try body()
        save.idempotencyKey = LaborViewModel.saveKey(save)
        let sent = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(save)) as? [String: Any])
        XCTAssertEqual(sent["idempotency_key"] as? String, save.idempotencyKey)
        let rescore = LaborViewModel.ScoreBody(rows: [try row("Ana")], dailyTargetHours: [:], save: false,
                                               historyId: 9, version: nil)
        let plain = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(rescore)) as? [String: Any])
        XCTAssertNil(plain["idempotency_key"])
    }

    func testASaveStillBeingMadeIsToldApartFromAConflict() {
        let busy = Data(#"{"ok": false, "in_progress": true, "error": "That save is still being made"}"#.utf8)
        XCTAssertTrue(LaborViewModel.saveInProgress(APIClient.APIError(message: "x", status: 409, body: busy)))
        let conflict = Data(#"{"ok": false, "conflict": true, "latest_version": 4}"#.utf8)
        XCTAssertFalse(LaborViewModel.saveInProgress(APIClient.APIError(message: "x", status: 409, body: conflict)))
        XCTAssertFalse(LaborViewModel.saveInProgress(APIClient.APIError(message: "x", status: 500, body: busy)))
    }
}
