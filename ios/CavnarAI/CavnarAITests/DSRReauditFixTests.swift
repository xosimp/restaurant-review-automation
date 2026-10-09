import XCTest
@testable import CavnarAI

/// The Daily Report's blind re-audit fix round (10/8/26, D1–D27): every rule
/// read from plain values, not a rendered view.
final class DSRReauditFixTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    private let kpis = #"""
        [{"key": "net", "label": "Net", "value_text": "$6,975"},
         {"key": "labor_pct", "label": "Labor", "value_text": "31%"},
         {"key": "avg_ticket", "label": "Avg check", "value_text": "$42"},
         {"key": "guests", "label": "Guests", "value_text": "166"}]
        """#

    // D1: kpis_headline is a list of KEYS (dsr/access.py) — decoded as
    // keys and looked up in `kpis`, so Key numbers never falls back to the
    // score card's four.
    func testHeadlineKeysPickTheirKPIs() throws {
        let r = try decode(DSRReport.self, """
            {"business_date": "2026-10-06", "kpis": \(kpis), "kpis_headline": ["avg_ticket", "guests"]}
            """)
        XCTAssertEqual(r.kpisHeadline, ["avg_ticket", "guests"])
        XCTAssertEqual(r.topKPIs.map(\.key), ["avg_ticket", "guests"])
        // An empty headline is honoured — every top KPI is on the score card.
        let none = try decode(DSRReport.self, #"{"business_date": "2026-10-06", "kpis": \#(kpis), "kpis_headline": []}"#)
        XCTAssertTrue(none.topKPIs.isEmpty)
        // An older server without the field: every KPI.
        let old = try decode(DSRReport.self, #"{"business_date": "2026-10-06", "kpis": \#(kpis)}"#)
        XCTAssertEqual(old.topKPIs.count, 4)
    }

    // D5: the caveat counts the parts a card explains; a reason no card
    // carries stays in it.
    func testStillMissingCountsWhatTheCardsSay() throws {
        let r = try decode(DSRReport.self, """
            {"business_date": "2026-10-06", "status": "provisional", "provisional": true,
             "facts": {"blocks": {"sales": {"status": "awaiting", "reason": "The POS hasn't sent tickets"}},
                       "missing": ["The POS hasn't sent tickets", "Weather didn't load"]}}
            """)
        let line = try XCTUnwrap(DailyReportView.missingLine(r))
        XCTAssertTrue(line.hasPrefix("1 part of the night isn\u{2019}t in yet"))
        XCTAssertFalse(line.contains("The POS hasn't sent tickets"))
        XCTAssertTrue(line.hasSuffix("Weather didn't load"))
        let clean = try decode(DSRReport.self, #"{"business_date": "2026-10-06"}"#)
        XCTAssertNil(DailyReportView.missingLine(clean))
    }

    // D6 + D19: the score card states the net, so the Sales line leaves it
    // out; vs_last_week_pct reads "vs last <weekday>" everywhere.
    func testTheSalesLineLeavesTheNetToTheScoreCard() {
        let b = DSRBlock(json: .object(["status": .string("ready"), "metrics": .object([
            "net": .number(6975), "vs_yesterday_pct": .number(8.1), "vs_last_week_pct": .number(4),
        ])]))
        XCTAssertEqual(DSRHeadline.line(for: "sales", b, businessDate: "2026-10-06", netOnScorecard: true),
                       "+8.1% vs yesterday · +4% vs last Tuesday")
        XCTAssertEqual(DSRHeadline.line(for: "sales", b, businessDate: "2026-10-06"),
                       "+8.1% vs yesterday · +4% vs last Tuesday · net $6,975")
        XCTAssertEqual(DSRHeadline.lastWeekLabel(nil), "vs last week")
    }

    // D24: a status this build doesn't know is "Not ready", never the key.
    func testAnUnknownBlockStatusIsNotReady() {
        XCTAssertEqual(DSRBlockStatus.describe("item_missing").0, "Not ready")
        XCTAssertEqual(DSRBlockStatus.describe("awaiting").0, "Waiting")
    }

    // D2: the web link names this night; D15/D23: the week's link names it.
    func testWebLinksNameTheirNightAndWeek() {
        XCTAssertEqual(DSRNightDetail.webPath("2026-09-24"), "dsr/night/2026-09-24")
        XCTAssertEqual(DSRNightDetail.webPath(nil), "dsr")
        XCTAssertEqual(DailyReportWeekView.webPath("2026-09-21"), "dsr/week/2026-09-21")
    }

    // D25: a period's rows are weeks.
    func testThePeriodGridSaysWeek() {
        XCTAssertEqual(DSRWeekGrid.labelTitle(kind: "period"), "Week")
        XCTAssertEqual(DSRWeekGrid.labelTitle(kind: "week"), "Day")
    }

    // D27: voids are said on their own.
    func testVoidsAreTheirOwnLine() {
        let v = DSRBlock.LossGroup(total: 86, lines: 4, pctOfGross: nil, byReason: [], byApprover: [])
        XCTAssertEqual(DSRNightDetail.voidLine(v), "4 lines voided · $86")
        let none = DSRBlock.LossGroup(total: 0, lines: 0, pctOfGross: nil, byReason: [], byApprover: [])
        XCTAssertNil(DSRNightDetail.voidLine(none))
        XCTAssertNil(DSRNightDetail.voidLine(nil))
    }

    // D4: "Read more" follows the measured clamp, not only a character count.
    func testReadMoreFollowsTheMeasuredClamp() {
        XCTAssertTrue(DSRLeadCard.clamps(characters: 60, full: 120, clamped: 66))
        XCTAssertFalse(DSRLeadCard.clamps(characters: 200, full: 66, clamped: 66))
        XCTAssertTrue(DSRLeadCard.clamps(characters: 200, full: 0, clamped: 0))
    }

    // D11: a priority's dollars are at stake, with what they cover.
    func testPriorityDollarsAreAtStakeWithTheirBasis() throws {
        let a = try decode(DSRAction.self, #"{"text": "x", "dollars_monthly": 1500, "dollars_basis": "the whole schedule's gap to target."}"#)
        XCTAssertEqual(a.dollarsLine, "$1,500/mo at stake · covers the whole schedule's gap to target")
    }
}
