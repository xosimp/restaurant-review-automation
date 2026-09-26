import XCTest
@testable import CavnarAI

/// Labor web-vs-iOS parity (9/25/26): the staffing board and Where the
/// money went decode from the server's own objects, the send sheet counts
/// every channel, shift edits compute hours as the web editor does, and an
/// approval over the draft offers to redo just those days.
@MainActor
final class LaborWebParityTests: XCTestCase {
    func testLaborStatsDecodesTheStaffingBoardAndMoneyWent() throws {
        let json = """
        {"ok": true, "is_live": true, "overall_labor_pct": 31.2, "target": 28, "on_track": false,
         "potential_savings": 0, "overtime_risk": [], "role_summary": [], "date_range": {"start": "2026-09-01", "end": "2026-09-14"},
         "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
         "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0},
         "labor_upcoming": [], "blended_rate": 18.5,
         "money_went": [{"kind": "overtime", "dollars": 120, "employee": "Olive", "hours": 52, "week": "9/7/26",
                         "label": "overtime premium", "hours_text": "52"}],
         "staffing_board": {
           "overstaffed": [{"kind": "overstaffed", "title": "Monday", "date": "9/8/26", "dollars": 212.4,
                            "dollars_text": "$212", "label": "above target", "pct_text": "41", "sales_text": "$1,500",
                            "trim_text": "11.5", "pts_text": "13", "severity": "high",
                            "consistency": {"hits": 2, "of": 2, "pct": 100}, "say": "Monday ran 41% labor.",
                            "ask": "Why did Monday run over?"}],
           "lean": [],
           "overtime": [{"kind": "overtime", "title": "Olive", "role": "Server", "week": "9/7/26", "hours": 52,
                         "extra": 12, "dollars": 120, "dollars_text": "$120", "label": "overtime premium",
                         "hours_text": "52", "extra_text": "12", "severity": "high",
                         "mate": {"name": "Pat", "hours_text": "28"}, "say": "Olive worked 52h.", "ask": "How do I keep Olive under 40?"}],
           "summary": {"at_stake_text": "$332", "at_stake": 332, "over_text": "$212", "ot_text": "$120",
                       "biggest": {"title": "Monday", "dollars_text": "$212", "label": "above target"},
                       "quick": {"title": "Olive", "kind": "overtime", "why": "move 12h to Pat"}}}}
        """
        let stats = try JSONDecoder.cavnar.decode(LaborStats.self, from: Data(json.utf8))
        let board = try XCTUnwrap(stats.staffingBoard)
        XCTAssertEqual(board.overstaffed.first?.consistency?.pct, 100)
        XCTAssertEqual(board.overtime.first?.mate?.name, "Pat")
        XCTAssertEqual(board.summary?.quick?.why, "move 12h to Pat")
        XCTAssertEqual(stats.moneyWent?.first?.line, "Olive · overtime · 52h week of 9/7/26")
        XCTAssertEqual(stats.blendedRate, 18.5)
    }

    func testAnOlderServerWithoutTheBoardStillDecodes() throws {
        let json = """
        {"ok": true, "is_live": false, "overall_labor_pct": 0, "target": 28, "on_track": false,
         "potential_savings": 0, "overtime_risk": [], "role_summary": [], "date_range": {},
         "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
         "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0}, "labor_upcoming": [],
         "staffing_board": null}
        """
        let stats = try JSONDecoder.cavnar.decode(LaborStats.self, from: Data(json.utf8))
        XCTAssertNil(stats.staffingBoard)
        XCTAssertNil(stats.moneyWent)
    }

    func testAContactIsReachableByTheAppNotOnlyByEmail() throws {
        let json = """
        [{"employee_name": "Ana", "email": "", "phone": "", "channel": "app"},
         {"employee_name": "Bo", "email": "", "phone": "", "channel": null},
         {"employee_name": "Cy", "email": "cy@x.test", "phone": ""}]
        """
        let contacts = try JSONDecoder.cavnar.decode([StaffContact].self, from: Data(json.utf8))
        XCTAssertTrue(contacts[0].isReachable)
        XCTAssertEqual(contacts[0].reachLine, "In the app")
        XCTAssertFalse(contacts[1].isReachable)
        // An older server sends no channel: an email address still counts.
        XCTAssertTrue(contacts[2].isReachable)
    }

    func testShiftHoursMatchTheWebEditor() {
        XCTAssertEqual(LaborViewModel.shiftHours("4:00pm", "10:00pm"), "6")
        XCTAssertEqual(LaborViewModel.shiftHours("4:30pm", "10:00pm"), "5.5")
        XCTAssertEqual(LaborViewModel.shiftHours("6:00pm", "1:00am"), "7")
        XCTAssertEqual(LaborViewModel.shiftTimeText(minutes: 16 * 60 + 5), "4:05pm")
        XCTAssertEqual(LaborViewModel.shiftMinutes("16:00"), 960)
        XCTAssertNil(LaborViewModel.shiftHours("soon", "later"))
    }

    func testApprovedTimeOffOverTheDraftOffersJustThoseDays() throws {
        let json = """
        {"ok": true, "history_id": 5, "preview_rows": [
          {"date": "2026-09-28", "day": "Monday", "employee": "Sofia", "shift_start": "4:00pm", "shift_end": "10:00pm"},
          {"date": "2026-09-29", "day": "Tuesday", "employee": "sofia", "shift_start": "4:00pm", "shift_end": "10:00pm"},
          {"date": "2026-10-02", "day": "Friday", "employee": "Sofia", "shift_start": "4:00pm", "shift_end": "10:00pm"},
          {"date": "2026-09-29", "day": "Tuesday", "employee": "Pat", "shift_start": "4:00pm", "shift_end": "10:00pm"}]}
        """
        let schedule = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(json.utf8))
        let request = try JSONDecoder.cavnar.decode(TimeOffRequest.self, from: Data("""
        {"id": 1, "employee_name": "Sofia", "start_date": "2026-09-28", "end_date": "2026-09-30", "status": "approved"}
        """.utf8))
        let offer = try XCTUnwrap(LaborViewModel.redoOffer(for: request, in: schedule))
        XCTAssertEqual(offer.dates, ["2026-09-28", "2026-09-29"])
        let pending = try JSONDecoder.cavnar.decode(TimeOffRequest.self, from: Data("""
        {"id": 2, "employee_name": "Sofia", "start_date": "2026-09-28", "end_date": "2026-09-30", "status": "pending"}
        """.utf8))
        XCTAssertNil(LaborViewModel.redoOffer(for: pending, in: schedule))
    }
}
