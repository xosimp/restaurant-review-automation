import XCTest
@testable import CavnarAI

/// The sections both nightly reports share (9/25/26): KPIs with direction,
/// the manager's shift, AI insights, tomorrow with its confidence, and
/// yesterday's predictions graded — all decoded from the report payload.
final class DSRSectionsTests: XCTestCase {
    private let manager = """
    {"business_date": "2026-09-19", "view": "manager", "facts": {"blocks": {}}, "scorecard": null,
     "kpis": [{"key": "labor_pct", "label": "Labor", "value": 27.5, "value_text": "27.5%",
               "change": {"delta": -1.4, "text": "↓ 1.4 pts vs last Saturday", "tone": "good"},
               "streak": {"text": "Best Saturday in 5 weeks", "tone": "good"}, "spark": [28.9, 28.1, 27.5],
               "target": {"value": 28, "value_text": "28%", "label": "Target"},
               "peers": {"available": false, "label": "Restaurants like yours", "why_not": "too few"},
               "estimate": false}],
     "operations": [{"key": "complaints", "label": "Guest complaints", "value_text": "2", "detail": "negative reviews"}],
     "shift": {"rows": [{"key": "scheduled", "label": "Employees scheduled", "value_text": "18", "tone": null},
                        {"key": "no_shows", "label": "Call-offs", "value_text": "1", "tone": "warn"}], "note": null},
     "insights": [{"kind": "biggest_risk", "label": "Biggest risk", "text": "Chicken runs out Sunday."}],
     "tomorrow": {"date": "2026-09-20", "weekday": "Sunday", "scheduled": 15,
                  "items": [{"kind": "weather", "tone": "warn", "text": "Rain forecast (70% chance)"}],
                  "forecast": {"text": "$6,200–$7,900", "basis": "the median of the last 8 Sundays"},
                  "confidence": {"pct": 70, "based_on": ["tonight's sales"], "missing": ["tomorrow's schedule"], "track": "3 graded"}},
     "yesterday": {"items": [{"key": "sales_range", "text": "Sales between $8,000 and $9,500", "outcome": "correct",
                              "actual_text": "Net sales $9,400"}],
                   "accuracy": {"pct": null, "correct": 1, "graded": 1, "window_days": 30, "min_graded": 5}}}
    """

    func testTheSharedSectionsDecode() throws {
        let r = try JSONDecoder().decode(DSRReport.self, from: Data(manager.utf8))
        XCTAssertFalse(r.isOwnerView)
        XCTAssertNil(r.scorecard)
        let labor = try XCTUnwrap(r.kpis.first)
        XCTAssertEqual(labor.change?.text, "↓ 1.4 pts vs last Saturday")
        XCTAssertEqual(labor.streak?.text, "Best Saturday in 5 weeks")
        XCTAssertEqual(labor.target?.valueText, "28%")
        XCTAssertEqual(labor.peers?.available, false)
        XCTAssertEqual(labor.spark.count, 3)
        XCTAssertEqual(r.operations.first?.detail, "negative reviews")
        XCTAssertEqual(r.shift?.rows.map(\.label), ["Employees scheduled", "Call-offs"])
        XCTAssertEqual(r.insights.first?.label, "Biggest risk")
        XCTAssertEqual(r.tomorrow?.confidence?.pct, 70)
        XCTAssertEqual(r.tomorrow?.items.first?.tone, "warn")
        XCTAssertEqual(r.yesterday?.items.first?.outcome, "correct")
        XCTAssertNil(r.yesterday?.accuracy?.pct)
    }

    func testAnOlderPayloadStillDecodes() throws {
        let json = #"{"business_date": "2026-09-19", "view": "owner", "facts": {"blocks": {}}}"#
        let r = try JSONDecoder().decode(DSRReport.self, from: Data(json.utf8))
        XCTAssertTrue(r.kpis.isEmpty && r.operations.isEmpty && r.insights.isEmpty)
        XCTAssertNil(r.tomorrow)
        XCTAssertNil(r.yesterday)
    }
}
