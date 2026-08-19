import XCTest
@testable import CavnarAI

final class LaborAnalyticsTests: XCTestCase {
    func testDecodesLaborTrendWeek() throws {
        let json = """
        {"label": "8/1", "pct": 29.5, "labor": 4200, "sales": 14200, "start": "2026-08-01", "end": "2026-08-07"}
        """
        let week = try JSONDecoder.cavnar.decode(LaborTrendWeek.self, from: Data(json.utf8))
        XCTAssertEqual(week.label, "8/1")
        XCTAssertEqual(week.pct, 29.5)
        XCTAssertEqual(week.start, "2026-08-01")
        XCTAssertEqual(week.end, "2026-08-07")
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

    /// The previous round-trip test only proved Codable/UserDefaults work
    /// in isolation — it didn't prove the actual LaborViewModel methods,
    /// called in the actual sequence the app uses, work end to end. This
    /// exercises exactly that sequence: configureCaching (app open) →
    /// cacheSchedule (a schedule finishes generating) → a brand new
    /// LaborViewModel instance (simulating whatever tears the real one
    /// down) → configureCaching again (app reopens) → assert the schedule
    /// comes back. Reported still missing after the encode/decode fix — this
    /// test does pass, and stays passing, because the actual remaining gap
    /// was never in this round trip: LaborView only ever renders
    /// scheduleResultSection nested inside `if let stats = viewModel.stats`,
    /// and stats had no cache of its own (see the next test below). A
    /// correctly-restored scheduleResult was still invisible behind a fresh,
    /// uncached network fetch for stats on every view-model recreation.
    @MainActor
    func testScheduleSurvivesAcrossFreshViewModelInstancesLikeARealRelaunch() throws {
        let restaurantId = 987_654_321  // unlikely to collide with real data
        let key = "labor.cachedSchedule.\(restaurantId)"
        UserDefaults.standard.removeObject(forKey: key)
        defer { UserDefaults.standard.removeObject(forKey: key) }

        let farFuture = Calendar.current.date(byAdding: .day, value: 10, to: Date())!
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        let json = """
        {"ok": true, "status": "done", "hours_scheduled": 500.0,
         "week_dates": ["\(formatter.string(from: farFuture))"]}
        """
        let schedule = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(json.utf8))

        let firstLaunch = LaborViewModel()
        firstLaunch.configureCaching(restaurantId: restaurantId)
        firstLaunch.cacheSchedule(schedule)

        let secondLaunch = LaborViewModel()
        secondLaunch.configureCaching(restaurantId: restaurantId)

        XCTAssertNotNil(secondLaunch.scheduleResult, "a fresh LaborViewModel should restore the cached schedule on configureCaching")
        XCTAssertEqual(secondLaunch.scheduleResult?.hoursScheduled, 500.0)
    }

    /// The actual root cause: scheduleResultSection only renders nested
    /// inside `if let stats = viewModel.stats` (LaborView.swift), so a
    /// perfectly-restored scheduleResult stayed invisible on every Face ID
    /// lock/unlock (RootView tears down and recreates the whole mainTabs
    /// subtree, including this view model, every time) until a fresh
    /// network fetch for stats completed — or, on a bad connection right
    /// after unlocking, failed and left the whole Overview tab on an
    /// error/Retry screen instead of showing anything. Both stats and
    /// scheduleResult now cache the same way; this proves they restore
    /// together, which is the actual invariant the view depends on.
    @MainActor
    func testStatsSurvivesAlongsideScheduleAcrossFreshViewModelInstances() throws {
        let restaurantId = 987_654_322  // distinct from the schedule-only test above
        let statsKey = "labor.cachedStats.\(restaurantId)"
        let scheduleKey = "labor.cachedSchedule.\(restaurantId)"
        UserDefaults.standard.removeObject(forKey: statsKey)
        UserDefaults.standard.removeObject(forKey: scheduleKey)
        defer {
            UserDefaults.standard.removeObject(forKey: statsKey)
            UserDefaults.standard.removeObject(forKey: scheduleKey)
        }

        let statsJson = """
        {"ok": true, "is_live": true, "overall_labor_pct": 31.2, "target": 30.0,
         "on_track": false, "potential_savings": 120.5,
         "overtime_risk": [], "role_summary": [],
         "date_range": {"start": "2026-06-01", "end": "2026-06-14"},
         "overstaffed_days": [], "understaffed_days": [],
         "dow_summary": {"Monday": 24.5},
         "savings_breakdown": {"labor_monthly": 522, "labor_annual": 6264, "labor_overtime": 210,
                                "labor_vs_industry_monthly": 0, "labor_vs_industry_annual": 0},
         "labor_upcoming": []}
        """
        let stats = try JSONDecoder.cavnar.decode(LaborStats.self, from: Data(statsJson.utf8))
        let farFuture = Calendar.current.date(byAdding: .day, value: 10, to: Date())!
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        let scheduleJson = """
        {"ok": true, "status": "done", "hours_scheduled": 500.0,
         "week_dates": ["\(formatter.string(from: farFuture))"]}
        """
        let schedule = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(scheduleJson.utf8))

        let firstInstance = LaborViewModel()
        firstInstance.configureCaching(restaurantId: restaurantId)
        firstInstance.cacheStats(stats)
        firstInstance.cacheSchedule(schedule)

        // Simulates RootView tearing down and recreating mainTabs (and
        // everything nested in it, including LaborViewModel) on Face ID
        // unlock — a fresh instance, not the same one that just cached.
        let afterLockUnlock = LaborViewModel()
        afterLockUnlock.configureCaching(restaurantId: restaurantId)

        XCTAssertNotNil(afterLockUnlock.stats, "stats must survive a lock/unlock or the Overview tab renders nothing at all")
        XCTAssertEqual(afterLockUnlock.stats?.overallLaborPct, 31.2)
        XCTAssertNotNil(afterLockUnlock.scheduleResult, "a fresh instance should restore the cached schedule on configureCaching")
        XCTAssertEqual(afterLockUnlock.scheduleResult?.hoursScheduled, 500.0)
    }

    /// Regression test for "Labor Analytics counts up / bars grow every
    /// time the module is backed out of and back into" — mark the tiles
    /// and bar-chart intros played (as the real views do via
    /// onIntroPlayed/onAppear once they've actually shown once), then
    /// simulate a fresh LaborAnalyticsViewModel instance the way leaving
    /// and re-entering Labor produces one, and confirm configureCaching
    /// restores both flags as already-played so the animations don't
    /// replay.
    @MainActor
    func testAnalyticsIntroFlagsSurviveAcrossFreshViewModelInstances() throws {
        let restaurantId = 987_654_323  // distinct from the other tests above
        let tilesKey = "labor.hasPlayedTilesIntro.\(restaurantId)"
        let barKey = "labor.hasPlayedBarIntro.\(restaurantId)"
        UserDefaults.standard.removeObject(forKey: tilesKey)
        UserDefaults.standard.removeObject(forKey: barKey)
        defer {
            UserDefaults.standard.removeObject(forKey: tilesKey)
            UserDefaults.standard.removeObject(forKey: barKey)
        }

        let firstInstance = LaborAnalyticsViewModel()
        firstInstance.configureCaching(restaurantId: restaurantId)
        firstInstance.markTilesIntroPlayed()
        firstInstance.markBarIntroPlayed()

        // Simulates backing out of Labor and back in — a fresh instance,
        // not the same one that just marked the intros played.
        let afterReturn = LaborAnalyticsViewModel()
        afterReturn.configureCaching(restaurantId: restaurantId)

        XCTAssertTrue(afterReturn.hasPlayedTilesIntro, "tiles count-up must not replay on a return visit")
        XCTAssertTrue(afterReturn.hasPlayedBarIntro, "bar chart grow-in must not replay on a return visit")
    }
}
