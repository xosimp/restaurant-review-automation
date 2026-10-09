import XCTest
@testable import CavnarAI

/// The Daily Report's iPhone readability round (10/8/26): the lead's first
/// sentence as its headline, "Do this today", the slim KPI tiles, the one-
/// line trust rows, the week's four default columns and the list's verdict
/// line — every rule here is read from plain values, not a rendered view.
final class DSRReadabilityTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    func testTheLeadSplitsAtItsFirstSentenceNeverInsideAFigure() {
        let s = DSRText.split("Net ran $6,975, 8.1% over a usual Tuesday. Labor ran 31.5% vs. a 28% target. Fix the close.")
        XCTAssertEqual(s.first, "Net ran $6,975, 8.1% over a usual Tuesday.")
        XCTAssertEqual(s.rest, "Labor ran 31.5% vs. a 28% target. Fix the close.")
        XCTAssertNil(DSRText.split("One sentence only.").rest)
        XCTAssertEqual(DSRText.split("Sales rose. $420 came from the patio.").rest, "$420 came from the patio.")
    }

    func testListsReadAsAnOwnerSaysThem() {
        XCTAssertEqual(DSRText.list(["Food"]), "Food")
        XCTAssertEqual(DSRText.list(["Food", "Labor"]), "Food and Labor")
        XCTAssertEqual(DSRText.list(["Marketing", "Intel", "Food"]), "Marketing, Intel and Food")
    }

    func testDoThisTodayIsOnlySaidForToday() throws {
        let today = try decode(DSRAction.self, #"{"text": "Call the produce rep", "urgency": "before_service", "why": "Romaine ran out at 7pm. It cost two comps."}"#)
        XCTAssertEqual(DSRDoThisToday.kicker(today), "Do this today")
        XCTAssertEqual(today.whyFirstSentence, "Romaine ran out at 7pm.")
        let week = try decode(DSRAction.self, #"{"text": "Cut a closer Tuesday", "urgency": "this_week"}"#)
        XCTAssertEqual(DSRDoThisToday.kicker(week), "Do this first")
    }

    func testFourKeyNumbersShowFirst() throws {
        let kpis = try (0..<7).map { i in
            try decode(DSRKPI.self, #"{"key": "k\#(i)", "label": "K\#(i)", "value_text": "1"}"#)
        }
        let split = DailyReportView.kpiSplit(top: kpis, all: kpis, showingAll: false)
        XCTAssertEqual(split.shown.count, 4)
        XCTAssertEqual(split.hidden, 3)
    }

    func testTheSalesLineLeadsWithItsComparisons() {
        let b = DSRBlock(json: .object(["status": .string("ready"), "metrics": .object([
            "net": .number(6975), "vs_yesterday_pct": .number(8.1), "vs_budget_net_pct": .number(-2),
            "vs_last_week_pct": .number(4),
        ])]))
        XCTAssertEqual(DSRHeadline.line(for: "sales", b), "+8.1% vs yesterday · \u{2212}2% vs budget · net $6,975")
    }

    func testYesterdaysCallsAreOneLine() throws {
        let y = try decode(DSRYesterday.self, """
            {"items": [{"key": "a", "text": "x", "outcome": "correct"},
                       {"key": "b", "text": "y", "outcome": "incorrect"},
                       {"key": "c", "text": "z", "outcome": "correct"},
                       {"key": "d", "text": "w", "outcome": null},
                       {"key": "e", "text": "v", "outcome": "correct"}]}
            """)
        XCTAssertEqual(DSRYesterdayLine.summary(y), "Yesterday\u{2019}s calls: 3 of 4 right")
        let none = try decode(DSRYesterday.self, #"{"items": [{"key": "a", "text": "x"}]}"#)
        XCTAssertEqual(DSRYesterdayLine.summary(none), "Yesterday\u{2019}s calls: not graded yet")
    }

    func testTheCheckIsSaidInAnOwnersWords() throws {
        let v = try decode(DSRVerification.self, #"{"checked": 8, "kept": 7, "dropped": [{}], "estimated": 2, "measured": 5}"#)
        XCTAssertEqual(v.ownerLines, [
            "7 of 8 lines kept.",
            "5 rest on measured figures; 2 on estimates, and say so.",
            "1 left out because its figures didn\u{2019}t match the night.",
        ])
        for line in v.ownerLines {
            XCTAssertFalse(line.contains("traced"), line)
            XCTAssertFalse(line.contains("pass the check"), line)
        }
        XCTAssertTrue(try decode(DSRVerification.self, #"{"checked": 0, "kept": 0}"#).ownerLines.isEmpty)
    }

    func testTheWeekShowsFourColumnsByDefaultAndKeepsEveryRowWhole() throws {
        let url = Bundle(for: Self.self).url(forResource: "dsr_week", withExtension: "json")!
        let all = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        let owner = try JSONDecoder.cavnar.decode(DSRWeekResponse.self,
                                                  from: JSONSerialization.data(withJSONObject: all["owner"]!)).week
        let compact = DSRWeekTable(grid: owner).compact()
        XCTAssertEqual(compact.columns.map(\.title), ["Net", "vs last yr", "vs budget", "Labor %"])
        XCTAssertTrue(compact.rows.allSatisfy { $0.cells.count == compact.columns.count })
        XCTAssertEqual(compact.rows.count, DSRWeekTable(grid: owner).rows.count)
    }

    func testAListRowCarriesTheVerdictNetAndChange() throws {
        let night = try decode(DSRSummary.self, """
            {"business_date": "2026-09-22", "status": "final", "verdict": "Good day", "tone": "good",
             "net": 6975, "vs_yesterday_pct": 8.1}
            """)
        XCTAssertEqual(DailyReportListView.summaryLine(night), "Good day · $6,975 · +8.1% vs yesterday")
        let bare = try decode(DSRSummary.self, #"{"business_date": "2026-09-22", "status": "final"}"#)
        XCTAssertNil(DailyReportListView.summaryLine(bare))
    }

    func testNoDeveloperWordsInTheReportsOwnCopy() throws {
        let moved = try decode(DSRAction.self, """
            {"text": "x", "effort": "Medium", "effort_source": "model",
             "urgency_adjusted": {"from": "before_service", "to": "this_week", "why": "nothing it cites moved 10%"}}
            """)
        XCTAssertEqual(moved.effortLabel, "Effort: medium (estimate)")
        XCTAssertEqual(moved.urgencyAdjustedLine, "Moved to This week \u{2014} the numbers don\u{2019}t show it\u{2019}s urgent")
        XCTAssertEqual(DSRPhase.provisional.label, "Missing data")
    }
}
