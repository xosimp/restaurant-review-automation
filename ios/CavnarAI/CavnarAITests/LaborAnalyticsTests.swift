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

    func testDecodesScheduleRowAndGeneratedScheduleWithPreviewRows() throws {
        let json = """
        {"ok": true, "status": "done", "summary": ["Balanced coverage"],
         "week_dates": ["2026-08-10"], "week_days": ["Monday"],
         "hours_scheduled": 120.5, "labor_target": 30,
         "schedule_csv": "date,day,employee\\n2026-08-10,Monday,Jamie",
         "hours_budget": 118.0, "labor_budget_dollars": 3068.0,
         "staff_constraints": {"Jamie": "No Sundays"},
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
        XCTAssertEqual(schedule.hoursBudget, 118.0)
        XCTAssertEqual(schedule.laborBudgetDollars, 3068.0)
        XCTAssertEqual(schedule.staffConstraints?["Jamie"], "No Sundays")
    }

    func testDecodesOvertimeEntryWithTotalHoursAndOtAllowed() throws {
        let json = """
        {"employee": "Carlos B.", "hours": 44.2, "week": "Jun 1", "status": "overtime",
         "total_hours": 88.4, "ot_allowed": true}
        """
        let entry = try JSONDecoder.cavnar.decode(LaborOvertimeEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.totalHours, 88.4)
        XCTAssertEqual(entry.otAllowed, true)
    }

    func testDecodesLaborStatsWithAnalyticsAndOverviewFields() throws {
        let json = """
        {"ok": true, "is_live": true, "overall_labor_pct": 31.2, "target": 30.0,
         "on_track": false, "potential_savings": 120.5,
         "overtime_risk": [], "role_summary": [],
         "date_range": {"start": "2026-06-01", "end": "2026-06-14"},
         "overstaffed_days": [{"date": "6/6/26", "day": "Saturday", "labor_pct": 38.0, "labor_cost": 900.0, "sales": 2200.0}],
         "understaffed_days": [{"date": "6/4/26", "day": "Thursday", "labor_pct": 22.0, "sales": 5600.0}],
         "dow_summary": {"Monday": 24.5, "Saturday": 38.0},
         "savings_breakdown": {"labor_monthly": 522, "labor_annual": 6264, "labor_overtime": 210,
                                "labor_vs_industry_monthly": 0, "labor_vs_industry_annual": 0},
         "labor_upcoming": [{"name": "Labor Day", "date_str": "September 1st", "days_away": 5}]}
        """
        let stats = try JSONDecoder.cavnar.decode(LaborStats.self, from: Data(json.utf8))
        XCTAssertEqual(stats.dateRange.start, "2026-06-01")
        XCTAssertEqual(stats.overstaffedDays.first?.day, "Saturday")
        XCTAssertEqual(stats.understaffedDays.first?.laborPct, 22.0)
        XCTAssertEqual(stats.dowSummary["Saturday"], 38.0)
        XCTAssertEqual(stats.savingsBreakdown.laborMonthly, 522)
        XCTAssertEqual(stats.laborUpcoming.first?.name, "Labor Day")
    }

    func testDecodesStaffAvailabilityEntry() throws {
        let json = """
        {"employee_name": "Jake M.", "available_days": ["Monday", "Tuesday"],
         "unavailable_days": ["Saturday"], "notes": "Student, no mornings"}
        """
        let entry = try JSONDecoder.cavnar.decode(StaffAvailabilityEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.employeeName, "Jake M.")
        XCTAssertEqual(entry.availableDays, ["Monday", "Tuesday"])
        XCTAssertEqual(entry.unavailableDays, ["Saturday"])
    }

    /// Regression test for the "generated schedule doesn't survive a
    /// relaunch" report — proves GeneratedSchedule (including its nested
    /// ScheduleRow array and staff_constraints dict) survives a real
    /// encode → UserDefaults → decode round trip losslessly, the same
    /// mechanism LaborViewModel's cacheSchedule()/configureCaching() use.
    /// If this ever fails, the bug is in the Codable/serialization layer;
    /// if it keeps passing while the symptom persists, the bug is in when
    /// those methods get called, not how they encode/decode.
    func testGeneratedScheduleSurvivesUserDefaultsRoundTrip() throws {
        let json = """
        {"ok": true, "status": "done", "summary": ["Balanced coverage", "Trimmed Sunday close"],
         "week_dates": ["2026-08-17", "2026-08-18", "2026-08-23"], "week_days": ["Monday", "Tuesday", "Sunday"],
         "hours_scheduled": 120.5, "labor_target": 30,
         "schedule_csv": "date,day,employee\\n2026-08-17,Monday,Jamie",
         "hours_budget": 118.0, "labor_budget_dollars": 3068.0,
         "staff_constraints": {"Jamie": "No Sundays"},
         "preview_rows": [
           {"date": "2026-08-17", "day": "Monday", "employee": "Jamie",
            "role": "Server", "shift_start": "9:00", "shift_end": "17:00",
            "scheduled_hours": "8", "notes": ""}
         ]}
        """
        let original = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(json.utf8))

        let suiteName = "LaborAnalyticsTests.scheduleRoundTrip"
        let defaults = UserDefaults(suiteName: suiteName)!
        defaults.removePersistentDomain(forName: suiteName)
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let encoded = try JSONEncoder.cavnar.encode(original)
        defaults.set(encoded, forKey: "schedule")

        let readBack = try XCTUnwrap(defaults.data(forKey: "schedule"))
        let decoded = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: readBack)

        XCTAssertEqual(decoded.hoursScheduled, 120.5)
        XCTAssertEqual(decoded.weekDates, original.weekDates)
        XCTAssertEqual(decoded.summary, original.summary)
        XCTAssertEqual(decoded.previewRows?.first?.employee, "Jamie")
        XCTAssertEqual(decoded.staffConstraints?["Jamie"], "No Sundays")
        XCTAssertEqual(decoded.hoursBudget, 118.0)
    }
}
