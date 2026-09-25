import XCTest
@testable import CavnarAI

/// How you compare (benchmark_views.card / location_compare): the card
/// decodes the server's words, the strength is a percentage with its four
/// Why? rows, the below-the-minimum state survives, and an odd payload is
/// empty rather than a failed screen (Benchmarking audit 9/24/26, #23 / #18
/// / #19).
final class BenchmarkCardTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    func testTheSelfCardBelowTheMinimumDecodes() throws {
        let card = try decode(BenchmarkCard.self, """
            {"ok": true, "scope": "module", "module": "labor",
             "who": {"kind": "self", "text": "vs your own previous 13 weeks", "n": 13, "as_of": "9/20/26", "inferred": false},
             "strength": null,
             "below_minimum": {"text": "Not enough restaurants like yours yet — here's how you compare to your own last 13 weeks.",
                               "why_not": "no current band of at least 8 other pizza restaurants on Cavnar with this measured yet"},
             "rows": [{"metric": "labor_pct_28d", "label": "Labor %", "kind": "self", "value_text": "35%",
                       "standing": "worse than your normal", "tone": "warn", "behind": true,
                       "against": "your own previous 13 weeks", "middle_text": "30.3%", "as_of": "9/20/26",
                       "action": {"label": "Ask what to change", "ask": "My labor % is worse than your normal (35%). What should I change first?"}}],
             "unmeasured": 3}
            """)
        XCTAssertTrue(card.hasContent)
        XCTAssertEqual(card.who?.kind, "self")
        XCTAssertEqual(card.who?.subline, "13 weeks measured \u{00B7} through 9/20/26")
        XCTAssertNil(card.strength)
        XCTAssertTrue(card.belowMinimum?.text?.hasPrefix("Not enough restaurants like yours yet") == true)
        let row = try XCTUnwrap(card.rows.first)
        XCTAssertEqual(row.headline, "Labor % 35% \u{2014} worse than your normal")
        XCTAssertEqual(row.detail, "vs your own previous 13 weeks (30.3%) \u{00B7} as of 9/20/26")
        XCTAssertTrue(row.behind)
        XCTAssertNotNil(row.action?.ask)
    }

    func testThePeerStrengthIsAPercentageWithItsFourRows() throws {
        let card = try decode(BenchmarkCard.self, """
            {"ok": true, "who": {"kind": "peers", "text": "Compared to 11 other Pizza restaurants on Cavnar", "n": 11, "as_of": "9/20/26"},
             "strength": {"pct": 68, "label": "68% comparison strength", "reason": "11 other restaurants — 20 makes a full comparison",
                          "meaning": "How well supported this comparison is — not how well you are doing",
                          "footer": "The overall figure combines all four — the weakest pulls it down most.",
                          "rows": [{"key": "size", "title": "Peer count", "pct": 55, "basis": "11 other restaurants measured this"},
                                   {"key": "freshness", "title": "Band freshness", "pct": 100, "basis": "as of 9/20/26"},
                                   {"key": "own", "title": "Your own figure", "pct": 100},
                                   {"key": "similarity", "title": "Type match", "pct": "odd"}]},
             "rows": []}
            """)
        let s = try XCTUnwrap(card.strength)
        XCTAssertEqual(s.lineLabel, "68% comparison strength")
        XCTAssertEqual(s.displayRows.map(\.title), ["Peer count", "Band freshness", "Your own figure", "Type match"])
        XCTAssertEqual(s.displayRows.map(\.value), ["55%", "100%", "100%", "\u{2014}"])
        XCTAssertEqual(card.who?.subline, "as of 9/20/26")
    }

    func testAnOddPayloadIsEmptyNotAFailure() throws {
        let card = try decode(BenchmarkCard.self, #"{"ok": false, "error": "This login can't see that module.", "rows": "nope"}"#)
        XCTAssertFalse(card.hasContent)
        XCTAssertTrue(card.rows.isEmpty)
        let strip = BenchmarkCard(who: BenchmarkWho(kind: "self", text: "vs your own previous 13 weeks"),
                                  strength: BenchmarkStrength(pct: 72))
        XCTAssertEqual(HomeBenchmarkStrip.kicker(strip), "HOW YOU COMPARE \u{00B7} VS YOUR OWN PREVIOUS 13 WEEKS \u{00B7} 72% STRENGTH")
    }

    func testLocationsDecodeWithTheirOwnNormalAndTheirGap() throws {
        let lc = try decode(LocationComparison.self, """
            {"ok": true, "metrics": [{"metric": "labor_pct_28d", "label": "Labor %", "locations": [
              {"id": 3, "name": "East", "value_text": "38%", "vs_own": "about your normal",
               "vs_group": "behind your other locations", "tone": "warn", "called": true, "group_median_text": "30.4%"},
              {"id": 4, "name": "New", "value_text": "31%", "vs_own": null,
               "vs_group": "not called — needs 6 weeks of history to tell a gap from noise", "tone": "neutral", "called": false}]}],
             "why_not": null}
            """)
        let east = try XCTUnwrap(lc.metrics.first?.locations.first)
        XCTAssertTrue(east.called)
        XCTAssertEqual(east.detail, "vs its own normal: about its normal \u{00B7} others\u{2019} middle 30.4%")
        XCTAssertEqual(lc.metrics.first?.locations.last?.detail, "its own normal: not enough history yet")
    }
}
