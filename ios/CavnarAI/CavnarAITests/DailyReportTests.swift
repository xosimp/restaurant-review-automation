import XCTest
@testable import CavnarAI

/// The Daily Sales Report (dsr/) as the app reads it. The report fixtures are
/// the server's own payloads (Fixtures/dsr_preview.json — access.render for
/// the owner and the manager, the provisional version 1, and a checklist),
/// so a shape change on the server fails here, not on Erik's phone.
final class DailyReportDecodingTests: XCTestCase {

    nonisolated(unsafe) private static let fixture: [String: Any] = {
        let url = Bundle(for: DailyReportDecodingTests.self).url(forResource: "dsr_preview", withExtension: "json")!
        return try! JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
    }()

    static func payload(_ key: String) -> Data {
        try! JSONSerialization.data(withJSONObject: fixture[key]!)
    }

    private func report(_ key: String) throws -> DSRReport {
        try JSONDecoder.cavnar.decode(DSRReport.self, from: Self.payload(key))
    }

    func testTheOwnerReportDecodesEveryBlockAndTheWholeNarrative() throws {
        let r = try report("owner")
        XCTAssertEqual(r.view, "owner")
        XCTAssertTrue(r.isOwnerView)
        XCTAssertEqual(r.businessDate, "2026-09-22")
        XCTAssertEqual(r.displayDate, "9/22/26")
        XCTAssertEqual(r.phase, .final)
        XCTAssertEqual(r.fiscal?.label, "Period 9 \u{00B7} Week 4")
        // Reading order since 9/25/26: weather (intel) right after reviews.
        XCTAssertEqual(r.orderedBlocks.map(\.name), ["sales", "labor", "food", "reviews", "intel", "marketing", "closeout"])

        let sales = try XCTUnwrap(r.facts.blocks["sales"])
        XCTAssertTrue(sales.isReady)
        XCTAssertEqual(sales.metric("net"), 6975)
        XCTAssertEqual(sales.metric("budget_net"), 7300)
        XCTAssertTrue(sales.has("comps"))
        XCTAssertEqual(sales.hourly.count, 12)
        XCTAssertEqual(sales.hourly.first?.label, "11a")
        XCTAssertEqual(sales.categories.first?.name, "Food")
        XCTAssertEqual(sales.topItems.first?.name, "EJ's Burger")

        let n = try XCTUnwrap(r.narrative)
        XCTAssertNotNil(n.executiveSummary)
        XCTAssertEqual(n.wentWell.count, 4)
        XCTAssertEqual(n.needsAttention.count, 4)
        XCTAssertEqual(n.actionsTomorrow.count, 3)
        XCTAssertEqual(n.actionsTomorrow.first?.urgencyLabel, "This week")
        XCTAssertEqual(n.actionsTomorrow.first?.effortLabel, "low effort")
        XCTAssertEqual(n.callouts.map(\.label),
                       ["Highest priority", "Biggest risk", "Biggest win", "Staffing",
                        "Biggest opportunity \u{00B7} not captured",
                        "Largest dollar gap \u{00B7} an opportunity, not savings", "Guest experience"])
        // The old largest_money_saving slot is read as an opportunity (NS3 R13)
        // and no call-out label claims money was saved (NS1 #17).
        XCTAssertFalse(n.callouts.contains { $0.label.lowercased().contains("saving") && !$0.label.contains("not savings") })
        XCTAssertFalse(n.callouts.contains { $0.label == "Money on the table" })
        XCTAssertEqual(n.verification?.kept, 20)

        XCTAssertEqual(r.versions.map(\.version), [1, 2])
        XCTAssertEqual(r.versions.first?.phase, .provisional)
        XCTAssertEqual(r.checklist?.stages.count, 5)
    }

    func testTheManagerReportCarriesOnlyWhatAManagerMayRead() throws {
        let r = try report("manager")
        XCTAssertEqual(r.view, "manager")
        XCTAssertFalse(r.isOwnerView)
        // Food wasn't granted: the block is gone and named as withheld.
        XCTAssertNil(r.facts.blocks["food"])
        XCTAssertEqual(r.facts.withheld, ["food"])
        XCTAssertFalse(r.orderedBlocks.map(\.name).contains("food"))
        // Budget and loss lines are removed from the payload, not nulled.
        let sales = try XCTUnwrap(r.facts.blocks["sales"])
        XCTAssertFalse(sales.has("budget_net"))
        XCTAssertFalse(sales.has("comps"))
        XCTAssertFalse(sales.has("voids"))
        XCTAssertTrue(sales.has("discounts"))
        // …and so is every narrative line that cited them.
        let n = try XCTUnwrap(r.narrative)
        XCTAssertNil(n.executiveSummary)
        XCTAssertEqual(n.needsAttention.count, 2)
        XCTAssertNil(n.biggestFinancialOpportunity)
        XCTAssertEqual(n.actionsTomorrow.count, 2)
        let headline = try XCTUnwrap(DSRHeadline.line(for: "sales", sales))
        XCTAssertFalse(headline.contains("budget"))
    }

    func testABlockThatIsNotReadyKeepsItsReasonAndNoFigures() throws {
        let r = try report("v1_owner")
        XCTAssertEqual(r.phase, .provisional)
        XCTAssertTrue(r.provisional)
        XCTAssertNil(r.narrative, "a null narrative must not fail the report")
        let sales = try XCTUnwrap(r.facts.blocks["sales"])
        XCTAssertFalse(sales.isReady)
        XCTAssertEqual(sales.status, "awaiting")
        XCTAssertEqual(sales.reason, "The POS closed the day but its tickets haven't arrived yet")
        XCTAssertTrue(sales.metrics.isEmpty)
        XCTAssertNil(DSRHeadline.line(for: "sales", sales))
        XCTAssertEqual(r.facts.missing, ["The POS closed the day but its tickets haven't arrived yet"])
        XCTAssertEqual(DSRBlockStatus.describe(sales.status).0, "Waiting")
    }

    func testANullMetricIsPresentButUnmeasured() throws {
        let r = try report("owner")
        let labor = try XCTUnwrap(r.facts.blocks["labor"])
        XCTAssertTrue(labor.has("hours_after_6pm"))
        XCTAssertNil(labor.metric("hours_after_6pm"))
        XCTAssertEqual(DSRFormat.count(labor.metric("no_shows")), "\u{2014}")
        let sales = try XCTUnwrap(r.facts.blocks["sales"])
        XCTAssertNil(sales.metric("forecast_net"))
        XCTAssertEqual(DSRFormat.signedPct(sales.metric("vs_forecast_pct")), "\u{2014}")
        // A zero that WAS measured is a zero, not a dash.
        XCTAssertEqual(DSRFormat.money(sales.metric("refunds")), "$0")
    }

    func testMetricsThatAreAllNullMakeNoHeadline() throws {
        let json = """
        {"view": "owner", "business_date": "2026-09-22", "status": "final", "provisional": false,
         "facts": {"blocks": {"labor": {"status": "ready", "source": "rpower", "reason": null,
           "metrics": {"pct": null, "target_pct": null, "vs_target_pts": null}, "detail": null}},
           "missing": [], "withheld": []},
         "narrative": {}, "checklist": null, "versions": []}
        """
        let r = try JSONDecoder.cavnar.decode(DSRReport.self, from: Data(json.utf8))
        let labor = try XCTUnwrap(r.facts.blocks["labor"])
        XCTAssertNil(DSRHeadline.line(for: "labor", labor))
        XCTAssertEqual(DSRFormat.pct(labor.metric("pct")), "\u{2014}")
        XCTAssertTrue(r.narrative?.isEmpty ?? true)
    }

    func testTheCloseoutIsVerbatimInTheServersOrder() throws {
        let closeout = try XCTUnwrap(try report("owner").facts.blocks["closeout"])
        let fields = closeout.closeoutFields
        XCTAssertEqual(fields.first?.label, "What went well")
        XCTAssertEqual(fields.first?.text, "Fish fry sold out the second batch by 8. Patio full 6\u{2013}8:30.")
        // Unwritten fields (maintenance, general notes) are left out.
        XCTAssertFalse(fields.map(\.key).contains("maintenance"))
        XCTAssertEqual(fields.last?.key, "influence")
        XCTAssertEqual(closeout.closeoutByline, "Filed by Jim \u{00B7} 9/23/26 \u{00B7} 8:01pm")
    }

    func testTheChecklistDecodesWithLocalTimes() throws {
        let c = try JSONDecoder.cavnar.decode(DSRChecklist.self, from: Self.payload("checklist_v2"))
        XCTAssertEqual(c.phase, .final)
        XCTAssertEqual(c.statusLabel, "Ready")
        XCTAssertEqual(c.stages.map(\.key), ["scheduled", "awaiting_close", "collecting", "writing", "final"])
        XCTAssertEqual(DSRFormat.localTime(c.stages.first?.atLocal), "8:01pm")
        XCTAssertNil(DSRFormat.localTime(c.stages[1].atLocal))
        XCTAssertEqual(c.blocks.count, 7)
        XCTAssertEqual(c.closedBy, "pos")
    }

    func testStatusForANightNotStartedIsAnAnswerNotAnError() throws {
        let json = """
        {"ok": true, "view": "manager", "exists": false, "business_date": "2026-09-23",
         "label": "9/23/26", "status": null}
        """
        let s = try JSONDecoder.cavnar.decode(DSRStatusResponse.self, from: Data(json.utf8))
        XCTAssertFalse(s.exists)
        XCTAssertNil(s.checklist)
        XCTAssertEqual(s.view, "manager")
    }

    func testTheListDecodes() throws {
        let json = """
        {"ok": true, "view": "owner", "reports": [
          {"business_date": "2026-09-22", "label": "9/22/26", "version": 2, "status": "final",
           "provisional": false, "missing": [], "finalized_at": "2026-09-24 01:01:45"},
          {"business_date": "2026-09-21", "label": "9/21/26", "version": 1, "status": "collecting",
           "provisional": false, "missing": ["Labor hasn't synced yet"], "finalized_at": null}]}
        """
        let list = try JSONDecoder.cavnar.decode(DSRListResponse.self, from: Data(json.utf8))
        XCTAssertEqual(list.reports.map(\.phase), [.final, .running])
        XCTAssertEqual(list.reports[1].missing.count, 1)
    }
}

// MARK: - Formatting

final class DailyReportFormatTests: XCTestCase {
    func testDatesReadMDYWithNoLeadingZeros() {
        XCTAssertEqual(CavnarDate.mdy("2026-09-02"), "9/2/26")
        XCTAssertEqual(DSRFormat.weekday("2026-09-22"), "Tuesday")
        XCTAssertEqual(DSRFormat.isoAdding(days: 7, to: "2026-09-16"), "2026-09-23")
        XCTAssertEqual(DSRFormat.isoAdding(days: -7, to: "2026-03-04"), "2026-02-25")
    }

    func testNullIsAlwaysADash() {
        let dash = "\u{2014}"
        XCTAssertEqual(DSRFormat.money(nil), dash)
        XCTAssertEqual(DSRFormat.signedMoney(nil), dash)
        XCTAssertEqual(DSRFormat.pct(nil), dash)
        XCTAssertEqual(DSRFormat.signedPct(nil), dash)
        XCTAssertEqual(DSRFormat.signedPoints(nil), dash)
        XCTAssertEqual(DSRFormat.count(nil), dash)
        XCTAssertEqual(DSRFormat.rating(nil), dash)
        XCTAssertEqual(DSRFormat.degrees(nil), dash)
    }

    func testFiguresReadAsTheReportWritesThem() {
        XCTAssertEqual(DSRFormat.money(6975), "$6,975")
        XCTAssertEqual(DSRFormat.money(176.4), "$176.40")
        XCTAssertEqual(DSRFormat.money(-325), "\u{2212}$325")
        XCTAssertEqual(DSRFormat.signedMoney(525), "+$525")
        XCTAssertEqual(DSRFormat.pct(27.4), "27.4%")
        XCTAssertEqual(DSRFormat.pct(26.0), "26%")
        XCTAssertEqual(DSRFormat.signedPct(8.1), "+8.1%")
        XCTAssertEqual(DSRFormat.signedPct(-1.9), "\u{2212}1.9%")
        XCTAssertEqual(DSRFormat.signedPoints(1.4), "+1.4 pts")
        XCTAssertEqual(DSRFormat.count(124.5), "124.5")
        XCTAssertEqual(DSRFormat.count(1840), "1,840")
        XCTAssertEqual(DSRFormat.rating(4), "4.0\u{2605}")
    }

    func testHoursAndLocalTimes() {
        XCTAssertEqual(DSRFormat.hourLabel("11"), "11a")
        XCTAssertEqual(DSRFormat.hourLabel("12"), "12p")
        XCTAssertEqual(DSRFormat.hourLabel("18"), "6p")
        XCTAssertEqual(DSRFormat.hourLabel("0"), "12a")
        XCTAssertEqual(DSRFormat.localTime("2026-09-23T20:01"), "8:01pm")
        XCTAssertEqual(DSRFormat.localTime("2026-09-23T00:05"), "12:05am")
        XCTAssertNil(DSRFormat.localTime(nil))
    }

    func testOnlyARealISODateIsAccepted() {
        XCTAssertTrue(DSRFormat.isISODate("2026-09-22"))
        XCTAssertFalse(DSRFormat.isISODate("2026-9-22"))
        XCTAssertFalse(DSRFormat.isISODate("2026-13-01"))
        XCTAssertFalse(DSRFormat.isISODate("../../dsr1"))
        XCTAssertFalse(DSRFormat.isISODate(nil))
    }

    func testPhases() {
        XCTAssertEqual(DSRPhase(status: "awaiting_close"), .running)
        XCTAssertEqual(DSRPhase(status: "writing"), .running)
        XCTAssertEqual(DSRPhase(status: "final"), .final)
        XCTAssertEqual(DSRPhase(status: "final", provisional: true), .provisional)
        XCTAssertEqual(DSRPhase(status: "failed"), .failed)
        XCTAssertEqual(DSRPhase(status: nil), .notStarted)
        XCTAssertFalse(DSRPhase.running.isTerminal)
    }
}

// MARK: - Polling

final class DailyReportPollingTests: XCTestCase {
    private func tick(exists: Bool? = true, phase: DSRPhase? = .running, version: Int? = 1,
                      elapsed: TimeInterval = 10, waiting: Int = 0, errors: Int = 0,
                      expectingRow: Bool = false, newerThan: Int? = nil) -> DSRPollPolicy.Tick {
        DSRPollPolicy.Tick(exists: exists, phase: phase, version: version, elapsed: elapsed, waitingTicks: waiting,
                           consecutiveErrors: errors, expectingRow: expectingRow, expectNewerThan: newerThan)
    }

    func testARunningNightKeepsPolling() {
        XCTAssertEqual(DSRPollPolicy.decide(tick()), .keepPolling)
    }

    func testAFinishedNightStopsAndReloads() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .final)), .reload)
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .provisional)), .reload)
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .failed)), .reload)
    }

    func testNothingStartedAndNothingAskedForStops() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(exists: false, phase: nil)), .stop)
    }

    func testCloseDayWaitsForTheFirstRowThenGivesUp() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(exists: false, phase: nil, waiting: 3, expectingRow: true)), .keepPolling)
        XCTAssertEqual(DSRPollPolicy.decide(tick(exists: false, phase: nil, waiting: DSRPollPolicy.maxWaitingTicks,
                                                 expectingRow: true)), .gaveUp)
    }

    func testAReRunIgnoresTheOldVersionsFinalStatus() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .final, version: 2, newerThan: 2)), .keepPolling)
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .running, version: 3, newerThan: 2)), .keepPolling)
        XCTAssertEqual(DSRPollPolicy.decide(tick(phase: .final, version: 3, newerThan: 2)), .reload)
    }

    func testAFailedTickTriesAgainUntilTheErrorCap() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(exists: nil, errors: 1)), .keepPolling)
        XCTAssertEqual(DSRPollPolicy.decide(tick(exists: nil, errors: DSRPollPolicy.maxConsecutiveErrors)), .gaveUp)
    }

    func testPollingIsBoundedInTime() {
        XCTAssertEqual(DSRPollPolicy.decide(tick(elapsed: DSRPollPolicy.maxDuration)), .gaveUp)
    }

    private func close(_ json: String) throws -> DSRCloseResponse {
        try JSONDecoder.cavnar.decode(DSRCloseResponse.self, from: Data(json.utf8))
    }

    func testWhatAStartedRunWillLookLike() throws {
        // No row yet: wait for one.
        let fresh = DSRFollow.after(try close(#"{"ok": true, "business_date": "2026-09-23", "started": true, "status": "scheduled", "version": null}"#))
        XCTAssertEqual(fresh, DSRFollow(expectingRow: true, newerThan: nil))
        // A finished night re-run: a new version above 2.
        let rerun = DSRFollow.after(try close(#"{"ok": true, "business_date": "2026-09-22", "started": true, "status": "final", "version": 2}"#))
        XCTAssertEqual(rerun, DSRFollow(expectingRow: false, newerThan: 2))
        // In flight: the same version moves on.
        let inFlight = DSRFollow.after(try close(#"{"ok": true, "business_date": "2026-09-23", "started": true, "status": "awaiting_close", "version": 1}"#))
        XCTAssertEqual(inFlight, DSRFollow(expectingRow: false, newerThan: nil))
        // Already final, nothing started.
        XCTAssertNil(DSRFollow.after(try close(#"{"ok": true, "business_date": "2026-09-22", "started": false, "status": "final", "version": 2}"#)))
    }
}

// MARK: - The week

final class DailyReportWeekTests: XCTestCase {
    private func grid(owner: Bool) throws -> DSRGrid {
        let budgetDay = owner ? #""budget_gross": 7800.0, "budget_net": 7300.0, "vs_budget_net": -325.0, "vs_budget_net_pct": -4.5,"# : ""
        let budgetTotals = owner ? #""budget_gross": 7800.0, "budget_net": 7300.0, "vs_budget_net": -325.0, "vs_budget_net_pct": -4.5,"# : ""
        let withheld = owner ? "" : #", "withheld": ["budget"]"#
        let json = """
        {"ok": true, "view": "\(owner ? "owner" : "manager")", "week": {
          "kind": "week", "start": "2026-09-16", "end": "2026-09-22", "label": "Period 9 · Week 4",
          "fiscal": {"period": 9, "week": 4, "label": "Period 9 · Week 4"},
          "categories": ["Food", "Liquor"],
          "days": [
            {"date": "2026-09-16", "weekday": "Wed", "label": "Wed 9/16/26", "status": null, "provisional": false,
             "version": null, "cats": {}, "gross": null, "net": null, "transactions": null, "guests": null,
             \(owner ? #""budget_gross": null, "budget_net": 6800.0,"# : "")
             "last_year_net": 6100.0, "last_year_source": "import", "labor_cost": null, "labor_pct": null,
             "weather": null, "event": null, "influence": null,
             "vs_last_year_net": null, "vs_last_year_net_pct": null
             \(owner ? #", "vs_budget_net": null, "vs_budget_net_pct": null"# : "")},
            {"date": "2026-09-22", "weekday": "Tue", "label": "Tue 9/22/26", "status": "final", "provisional": false,
             "version": 2, "cats": {"Food": 4610.0, "Liquor": 1045.0}, "gross": 7415.0, "net": 6975.0,
             "transactions": 212.0, "guests": 288.0, \(budgetDay)
             "last_year_net": 6720.0, "last_year_source": "pos_sync", "labor_cost": 1912.0, "labor_pct": 27.4,
             "weather": "Partly Sunny · high 71° · 10% rain", "event": "Cubs home game (7:05pm)",
             "influence": "Cubs game pulled a late rush.", "vs_last_year_net": 255.0, "vs_last_year_net_pct": 3.8}
          ],
          "totals": {"gross": 7415.0, "gross_days": 1, "net": 6975.0, "net_days": 1, \(budgetTotals)
                     "last_year_net": 12820.0, "labor_cost": 1912.0, "cats": {"Food": 4610.0, "Liquor": 1045.0},
                     "days_measured": 1, "vs_last_year_net": 255.0, "vs_last_year_net_pct": 3.8, "labor_pct": 27.4},
          "period_to_date": {"gross": 31000.0, "net": 29100.0, "cats": {"Food": 19000.0, "Liquor": null},
                             "days_measured": 21, "labor_pct": 26.9, "start": "2026-08-26", "end": "2026-09-22",
                             "vs_last_year_net_pct": null, "last_year_net": null}
          \(withheld)}}
        """
        return try JSONDecoder.cavnar.decode(DSRWeekResponse.self, from: Data(json.utf8)).week
    }

    func testTheOwnersWeekHasBudgetColumns() throws {
        let g = try grid(owner: true)
        XCTAssertTrue(g.showsBudget)
        let table = DSRWeekTable(grid: g)
        // The web's three budget columns (D3-13).
        XCTAssertEqual(table.columns.map(\.title),
                       ["Food", "Liquor", "Gross", "Net", "Budget gross", "Budget net", "vs budget",
                        "Last year", "vs last yr", "Labor %", "Notes"])
        let tuesday = try XCTUnwrap(table.rows.first { $0.id == "2026-09-22" })
        XCTAssertEqual(tuesday.cells.map(\.text)[5], "$7,300")
        XCTAssertEqual(tuesday.cells.map(\.text)[6], "\u{2212}4.5%")
        XCTAssertEqual(tuesday.cells[6].tone, .bad)
        XCTAssertEqual(tuesday.cells[8].tone, .good)
        XCTAssertTrue(table.rows.allSatisfy { $0.cells.count == table.columns.count })
    }

    func testAManagersWeekHasNoBudgetAtAll() throws {
        let g = try grid(owner: false)
        XCTAssertFalse(g.showsBudget)
        let table = DSRWeekTable(grid: g)
        XCTAssertFalse(table.columns.map(\.title).contains("Budget net"))
        XCTAssertFalse(table.columns.map(\.title).contains("Budget gross"))
        XCTAssertFalse(table.columns.map(\.title).contains("vs budget"))
        XCTAssertTrue(table.rows.allSatisfy { $0.cells.count == table.columns.count })
    }

    func testADayWithNoReportIsDashesNotZeros() throws {
        let table = DSRWeekTable(grid: try grid(owner: true))
        let wednesday = try XCTUnwrap(table.rows.first { $0.id == "2026-09-16" })
        XCTAssertNil(wednesday.opens, "a night with no report has nothing to open")
        XCTAssertEqual(wednesday.cells[0].text, "\u{2014}")
        XCTAssertEqual(wednesday.cells[0].tone, .muted)
        XCTAssertEqual(wednesday.cells[3].text, "\u{2014}", "net")
        XCTAssertEqual(wednesday.cells[7].text, "$6,100", "last year is still known")
        XCTAssertFalse(wednesday.cells.map(\.text).contains("$0"))
    }

    func testTotalsSayHowManyDaysTheyCoverAndPeriodToDateFollows() throws {
        let table = DSRWeekTable(grid: try grid(owner: true))
        let totals = try XCTUnwrap(table.rows.first { $0.id == "totals" })
        XCTAssertTrue(totals.isTotal)
        XCTAssertEqual(totals.cells.last?.text, "1 of 2 days measured")
        let ptd = try XCTUnwrap(table.rows.last)
        XCTAssertEqual(ptd.id, "ptd")
        XCTAssertEqual(ptd.label, "Period to date")
        XCTAssertEqual(ptd.cells[1].text, "\u{2014}", "an unmeasured category total is a dash")
        XCTAssertEqual(ptd.cells.last?.text, "8/26/26 \u{2013} 9/22/26")
    }

    /// Fixtures/dsr_week.json is dsr.rollup.week's own output (owner, and
    /// access.redact_grid's manager copy) for tests/test_dsr_rollup.py's
    /// Simple EJ's week: two nights measured, a budget on a night not run.
    private func serverWeek(_ view: String) throws -> DSRGrid {
        let url = Bundle(for: Self.self).url(forResource: "dsr_week", withExtension: "json")!
        let all = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        let data = try JSONSerialization.data(withJSONObject: all[view]!)
        return try JSONDecoder.cavnar.decode(DSRWeekResponse.self, from: data).week
    }

    func testTheServersOwnWeekDecodesAndReads() throws {
        let g = try serverWeek("owner")
        XCTAssertEqual(g.label, "Period 9 \u{00B7} Week 4")
        XCTAssertEqual(g.days.map(\.displayLabel).first, "Wed 9/16/26")
        XCTAssertEqual(g.categories, ["Food", "Liquor", "Beer", "Wine", "Retail", "NA Beverage", "Kiosk"])
        let table = DSRWeekTable(grid: g)
        let tue = try XCTUnwrap(table.rows.first { $0.id == "2026-09-22" })
        XCTAssertNil(tue.opens)
        let budgetAt = try XCTUnwrap(table.columns.firstIndex { $0.title == "Budget net" })
        let netAt = try XCTUnwrap(table.columns.firstIndex { $0.title == "Net" })
        XCTAssertEqual(tue.cells[budgetAt].text, "$7,300", "a budget for a night not yet run still shows")
        XCTAssertEqual(tue.cells[netAt].text, "\u{2014}")
        let wed = try XCTUnwrap(table.rows.first { $0.id == "2026-09-16" })
        XCTAssertEqual(wed.opens, "2026-09-16")
        XCTAssertEqual(wed.cells.last?.text, "Sunny \u{00B7} high 74\u{00B0} \u{00B7} Cubs home game \u{00B7} Game night pulled a late rush")
        let totals = try XCTUnwrap(table.rows.first { $0.id == "totals" })
        XCTAssertEqual(totals.cells[0].text, "$7,700")
        XCTAssertEqual(totals.cells[4].text, "\u{2014}", "Retail was never measured")
        XCTAssertEqual(totals.cells.last?.text, "2 of 7 days measured")

        let mgr = try serverWeek("manager")
        XCTAssertFalse(mgr.showsBudget)
        XCTAssertFalse(DSRWeekTable(grid: mgr).columns.contains { $0.title == "Budget net" })
    }

    func testNeighbouringWeeks() {
        XCTAssertEqual(DSRFormat.isoAdding(days: 7, to: "2026-09-16"), "2026-09-23")
        XCTAssertEqual(DSRFormat.isoAdding(days: -7, to: "2026-09-16"), "2026-09-09")
    }
}

// MARK: - The push

@MainActor
final class DailyReportPushTests: XCTestCase {
    func testTheSpecifiedTopLevelPayloadIsRead() {
        let userInfo: [AnyHashable: Any] = ["aps": ["alert": "Last night"], "type": "dsr", "business_date": "2026-09-22"]
        let data = PushManager.cavnarPayload(userInfo)
        XCTAssertNotNil(data)
        XCTAssertEqual(PushManager.alertType(data!), "dsr")
        XCTAssertEqual(PushManager.businessDate(data!), "2026-09-22")
    }

    func testThePushPyNestedShapeIsRead() {
        let userInfo: [AnyHashable: Any] = ["cavnar": ["alert_type": "dsr", "business_date": "2026-09-22",
                                                       "restaurant_id": 1, "module": "reviews"]]
        let data = PushManager.cavnarPayload(userInfo)!
        XCTAssertEqual(PushManager.alertType(data), "dsr")
        XCTAssertEqual(PushManager.businessDate(data), "2026-09-22")
    }

    func testABadDateIsDropped() {
        XCTAssertNil(PushManager.businessDate(["business_date": "2026-09-22/../../x"]))
        XCTAssertNil(PushManager.cavnarPayload(["aps": [:]]))
    }

    func testTheReportIsRecognisedByTypeOrModule() {
        XCTAssertTrue(DeepLinkRouter.isDailyReport(alertType: "dsr", module: "reviews"))
        XCTAssertTrue(DeepLinkRouter.isDailyReport(alertType: "", module: "dsr"))
        XCTAssertFalse(DeepLinkRouter.isDailyReport(alertType: "morning_brief", module: "ask"))
    }

    // alertType left empty: a non-empty one records the open through
    // APIClient.shared, which is a real request (see DeepLinkRoutingTests).

    func testATapWithADateOpensThatNightOnHome() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "dsr", businessDate: "2026-09-22")
        XCTAssertEqual(router.pendingTab, .home)
        XCTAssertEqual(router.pendingDailyReport, .report(date: "2026-09-22"))
        XCTAssertNil(router.pendingModuleKey)
        XCTAssertEqual(router.consumePendingDailyReport(), .report(date: "2026-09-22"))
        XCTAssertNil(router.pendingDailyReport)
    }

    func testATapWithNoDateOpensTheListOfNights() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "dsr")
        XCTAssertEqual(router.pendingDailyReport, .list)
        let bad = DeepLinkRouter()
        bad.handleNotificationTap(alertType: "", reviewId: nil, module: "dsr", businessDate: "not-a-date")
        XCTAssertEqual(bad.pendingDailyReport, .list)
    }
}

// MARK: - The screen's view model

/// A scripted server: each request is answered by the first handler whose
/// path matches, and every request is recorded.
private final class ScriptedServer: @unchecked Sendable {
    private let lock = NSLock()
    private var statusAnswers: [String]
    private let routes: [(String, Int, String)]
    private(set) var paths: [String] = []

    init(routes: [(String, Int, String)], statusAnswers: [String] = []) {
        self.routes = routes
        self.statusAnswers = statusAnswers
    }

    func answer(_ request: URLRequest) -> (HTTPURLResponse, Data) {
        let path = request.url!.path
        lock.lock()
        paths.append(request.httpMethod == "POST" ? "POST " + path : path)
        var body: (Int, String)?
        if path.hasSuffix("/status"), !statusAnswers.isEmpty {
            body = (200, statusAnswers.count > 1 ? statusAnswers.removeFirst() : statusAnswers[0])
        }
        lock.unlock()
        if body == nil, let r = routes.first(where: { path.hasSuffix($0.0) }) { body = (r.1, r.2) }
        let (status, text) = body ?? (404, #"{"ok": false, "error": "not found"}"#)
        return (HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, Data(text.utf8))
    }

    var recorded: [String] { lock.lock(); defer { lock.unlock() }; return paths }
}

@MainActor
final class DailyReportViewModelTests: XCTestCase {
    private func client(_ server: ScriptedServer) -> APIClient {
        MockURLProtocol.requestHandler = { server.answer($0) }
        return APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    private static func text(_ key: String) -> String {
        String(decoding: DailyReportDecodingTests.payload(key), as: UTF8.self)
    }

    /// D3-9: "Try again now" on a night that couldn't finish or finished
    /// provisional, for a manager too, as the web offers it.
    func testAManagerCanTryAFailedOrProvisionalNightAgain() async throws {
        for status in ["failed", "provisional"] {
            var obj = try XCTUnwrap(JSONSerialization.jsonObject(with: DailyReportDecodingTests.payload("manager"))
                                    as? [String: Any])
            obj["status"] = status
            if var checklist = obj["checklist"] as? [String: Any] {
                checklist["status"] = status
                obj["checklist"] = checklist
            }
            let text = String(decoding: try JSONSerialization.data(withJSONObject: obj), as: UTF8.self)
            let server = ScriptedServer(routes: [("/dsr/2026-09-22", 200, text)])
            let vm = DailyReportViewModel(businessDate: "2026-09-22", client: client(server))
            await vm.load()
            XCTAssertFalse(vm.isOwner)
            XCTAssertFalse(vm.canRerun, "re-run stays the owner's")
            XCTAssertTrue(vm.canCloseDay, status)
            XCTAssertEqual(vm.closeDayLabel, "Try again now")
        }
    }

    func testAnOwnersFinishedNightCanBeReRunButNotClosed() async {
        let server = ScriptedServer(routes: [("/dsr/2026-09-22", 200, Self.text("owner"))])
        let vm = DailyReportViewModel(businessDate: "2026-09-22", client: client(server))
        await vm.load()
        XCTAssertNil(vm.errorMessage)
        XCTAssertEqual(vm.phase, .final)
        XCTAssertTrue(vm.isOwner)
        XCTAssertTrue(vm.canRerun)
        XCTAssertFalse(vm.canCloseDay)
        XCTAssertEqual(vm.pollGeneration, 0, "a finished night is not polled")
    }

    func testAManagerCannotReRun() async {
        let server = ScriptedServer(routes: [("/dsr/2026-09-22", 200, Self.text("manager"))])
        let vm = DailyReportViewModel(businessDate: "2026-09-22", client: client(server))
        await vm.load()
        XCTAssertFalse(vm.isOwner)
        XCTAssertFalse(vm.canRerun)
    }

    func testNoDateOpensTheLatestNight() async {
        let list = #"{"ok": true, "view": "owner", "reports": [{"business_date": "2026-09-22", "label": "9/22/26", "version": 2, "status": "final", "provisional": false, "missing": [], "finalized_at": null}]}"#
        let server = ScriptedServer(routes: [("/mobile/api/dsr", 200, list), ("/dsr/2026-09-22", 200, Self.text("owner"))])
        let vm = DailyReportViewModel(businessDate: nil, client: client(server))
        await vm.load()
        XCTAssertEqual(vm.businessDate, "2026-09-22")
        XCTAssertNotNil(vm.report)
    }

    func testANightWithNoRowOffersCloseDay() async {
        let status = #"{"ok": true, "view": "manager", "exists": false, "business_date": "2026-09-23", "label": "9/23/26", "status": null}"#
        let server = ScriptedServer(routes: [("/dsr/2026-09-23", 404, #"{"ok": false, "error": "There's no report for that night yet."}"#),
                                             ("/dsr/2026-09-23/status", 200, status)])
        let vm = DailyReportViewModel(businessDate: "2026-09-23", client: client(server))
        await vm.load()
        XCTAssertTrue(vm.nothingYet)
        XCTAssertNil(vm.errorMessage)
        XCTAssertEqual(vm.phase, .notStarted)
        XCTAssertTrue(vm.canCloseDay)
        XCTAssertFalse(vm.canRerun)
    }

    func testARunningNightIsFollowedUntilItFinishesThenReloaded() async throws {
        let running = #"{"ok": true, "view": "owner", "exists": true, "business_date": "2026-09-22", "version": 2, "status": "collecting", "status_label": "Collecting", "provisional": false, "stages": [], "blocks": [], "missing": []}"#
        var finishedObject = try JSONSerialization.jsonObject(with: DailyReportDecodingTests.payload("checklist_v2")) as! [String: Any]
        finishedObject["ok"] = true
        finishedObject["view"] = "owner"
        finishedObject["exists"] = true
        let finished = String(decoding: try JSONSerialization.data(withJSONObject: finishedObject), as: UTF8.self)
        let server = ScriptedServer(routes: [("/dsr/2026-09-22", 404, #"{"ok": false, "error": "no"}"#)],
                                    statusAnswers: [running, running, finished])
        let vm = DailyReportViewModel(businessDate: "2026-09-22", client: client(server))
        vm.pollInterval = .milliseconds(5)
        await vm.load()
        XCTAssertEqual(vm.phase, .running)
        XCTAssertGreaterThan(vm.pollGeneration, 0, "a running night starts polling")
        await vm.followProgress()
        XCTAssertEqual(vm.checklist?.phase, .final)
        XCTAssertFalse(vm.isPolling)
        XCTAssertFalse(vm.pollGaveUp)
        // Polled until final, then asked for the report once more (whose
        // 404 here falls back to /status).
        let statusCalls = server.recorded.filter { $0.hasSuffix("/status") }.count
        XCTAssertGreaterThanOrEqual(statusCalls, 3)
        XCTAssertEqual(Array(server.recorded.suffix(2)),
                       ["/mobile/api/dsr/2026-09-22", "/mobile/api/dsr/2026-09-22/status"])
    }

    func testARefusedCloseShowsTheServersSentence() async {
        let refusal = #"{"ok": false, "error": "Only tonight (9/23/26) or the night before can be closed here."}"#
        let server = ScriptedServer(routes: [("/dsr/close", 400, refusal)])
        let vm = DailyReportViewModel(businessDate: "2026-09-20", client: client(server))
        await vm.closeDay()
        XCTAssertEqual(vm.actionError, "Only tonight (9/23/26) or the night before can be closed here.")
        XCTAssertEqual(vm.pollGeneration, 0)
        XCTAssertFalse(vm.isSubmitting)
    }

    func testAnAcceptedReRunWaitsForTheNewVersion() async {
        let accepted = #"{"ok": true, "business_date": "2026-09-22", "label": "9/22/26", "started": true, "status": "final", "version": 2}"#
        let server = ScriptedServer(routes: [("/dsr/close", 202, accepted)])
        let vm = DailyReportViewModel(businessDate: "2026-09-22", client: client(server))
        await vm.closeDay(rerun: true)
        XCTAssertNil(vm.actionError)
        XCTAssertEqual(vm.phase, .running)
        XCTAssertTrue(vm.isStarting)
        XCTAssertEqual(vm.pollGeneration, 1)
        XCTAssertFalse(vm.canRerun)
        XCTAssertFalse(vm.canCloseDay)
    }
}
