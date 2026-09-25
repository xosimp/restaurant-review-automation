import XCTest
@testable import CavnarAI

/// The Owner DSR's "did we win today?" (dsr/scorecard.py) decodes from the
/// report payload; a manager's payload (no scorecard) decodes to nil.
final class DSRScorecardTests: XCTestCase {
    private let owner = """
    {"business_date": "2026-09-19", "view": "owner", "facts": {"blocks": {}},
     "scorecard": {"overall": 92, "verdict": {"label": "Excellent day", "tone": "good"},
       "components": [
         {"key": "sales", "label": "Sales", "measured": true, "value": "+$1,428 vs budget", "detail": "$9,200 net · +8.5%", "tone": "good"},
         {"key": "labor", "label": "Labor", "measured": true, "value": "0.8 pts below your target", "tone": "good"},
         {"key": "food", "label": "Food cost", "measured": true, "value": "On target", "tone": "good"},
         {"key": "guests", "label": "Guest experience", "measured": false, "value": null, "why": "No new reviews"}],
       "wins": [{"text": "Liquor sales +18.0% vs a usual Saturday", "key": "cat:Liquor", "source": "measured"}],
       "risks": [{"text": "Miller Lite keg running low (1.5 days left)", "key": "stock:Miller Lite keg"},
                 {"text": "Sunday reservations are light.", "key": null, "source": "narrative"}],
       "basis": "A weighted score of what was measured tonight"}}
    """

    func testTheOwnerScorecardDecodes() throws {
        let r = try JSONDecoder().decode(DSRReport.self, from: Data(owner.utf8))
        let card = try XCTUnwrap(r.scorecard)
        XCTAssertEqual(card.overall, 92)
        XCTAssertEqual(card.verdict?.label, "Excellent day")
        XCTAssertEqual(card.components.map(\.key), ["sales", "labor", "food", "guests"])
        XCTAssertFalse(card.components[3].measured)
        XCTAssertEqual(card.components[3].why, "No new reviews")
        XCTAssertEqual(card.wins.first?.text, "Liquor sales +18.0% vs a usual Saturday")
        XCTAssertEqual(card.risks.count, 2)
        XCTAssertNil(card.risks[1].key)
        XCTAssertEqual(DSRScorecard.color("good"), .cavnarGreen)
    }

    func testAManagerReportHasNoScorecard() throws {
        let json = #"{"business_date": "2026-09-19", "view": "manager", "facts": {"blocks": {}}, "scorecard": null}"#
        let r = try JSONDecoder().decode(DSRReport.self, from: Data(json.utf8))
        XCTAssertNil(r.scorecard)
    }
}
