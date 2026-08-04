import XCTest
@testable import CavnarAI

final class LaborAnalyticsTests: XCTestCase {
    func testDecodesLaborTrendWeek() throws {
        let json = """
        {"label": "8/1", "pct": 29.5, "labor": 4200, "sales": 14200}
        """
        let week = try JSONDecoder.cavnar.decode(LaborTrendWeek.self, from: Data(json.utf8))
        XCTAssertEqual(week.label, "8/1")
        XCTAssertEqual(week.pct, 29.5)
    }

    func testDecodesLaborGap() throws {
        let json = """
        {"ok": true, "over_target": true, "monthly_gap": 850.0, "current_pct": 32.1, "target_pct": 30.0}
        """
        let gap = try JSONDecoder.cavnar.decode(LaborGap.self, from: Data(json.utf8))
        XCTAssertTrue(gap.overTarget)
        XCTAssertEqual(gap.monthlyGap, 850.0)
    }

    func testDecodesScheduleRowAndGeneratedScheduleWithPreviewRows() throws {
        let json = """
        {"ok": true, "status": "done", "summary": ["Balanced coverage"],
         "week_dates": ["2026-08-10"], "week_days": ["Monday"],
         "hours_scheduled": 120.5, "labor_target": 30,
         "schedule_csv": "date,day,employee\\n2026-08-10,Monday,Jamie",
         "preview_rows": [
           {"date": "2026-08-10", "day": "Monday", "employee": "Jamie",
            "role": "Server", "shift_start": "9:00", "shift_end": "17:00",
            "scheduled_hours": "8", "notes": ""}
         ]}
        """
        let schedule = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(json.utf8))
        XCTAssertEqual(schedule.previewRows?.count, 1)
        XCTAssertEqual(schedule.previewRows?.first?.employee, "Jamie")
        XCTAssertEqual(schedule.scheduleCsv?.contains("Jamie"), true)
    }
}
