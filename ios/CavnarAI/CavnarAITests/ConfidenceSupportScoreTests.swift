import XCTest
@testable import CavnarAI

/// Confidence round 2, group P (the owner's "support score" decision,
/// 9/24/26): the % is how well supported the advice is — not the chance it
/// works — and Historical accuracy is the lift against doing nothing. The
/// fixture is the shape confidence_engine.assemble sends (version 2).
final class ConfidenceSupportScoreTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    private static let k1 = """
    {"pct": 70, "band": "medium", "label": "70% confidence",
     "reason": "its record here doesn't yet show it beats doing nothing (93% likely; 95% needed to read higher)",
     "score": 0.7, "caution": null, "version": 2,
     "meaning": "How well supported this is — not the chance it works",
     "thresholds": {"high": 75, "medium": 50},
     "caps": {"no_track_record": 70, "record_unproven": 70, "record_against": 49, "stale": 49,
              "stale_below": 50, "freshness_unmeasured": 49, "beats_at": 95, "against_below": 50, "max": 99},
     "caps_applied": ["record_unproven"], "sample": false,
     "dimensions": {
       "evidence": {"pct": 100, "basis": "8 reviews", "n": 8, "n_full": 8, "kind": "reviews", "corroborating": 0},
       "accuracy": {"pct": 93, "basis": "improved 2 of 5 times vs about 5% by chance", "n": 5, "improved": 2,
                    "source": "own", "low": 12, "high": 76, "p_beats": 0.9307,
                    "beats_label": "93% likely to beat doing nothing",
                    "lift": {"improved": 2, "n": 5, "rate": 0.4, "untaken_improved": 0, "untaken_n": 0,
                             "do_nothing_rate": 0.05, "source": "stated"},
                    "prior": {"source": "do_nothing", "centre": 0.05, "weight": 5}},
       "freshness": {"pct": 100, "basis": "Reviews fetched 9/24/26", "as_of": "9/24/26",
                     "as_of_iso": "2026-09-24", "stalest": "reviews", "errors": []}}}
    """

    func testTheMeaningAndTheCapsDecode() throws {
        let c = try decode(TrustConfidence.self, Self.k1)
        XCTAssertEqual(c.meaning, "How well supported this is — not the chance it works")
        XCTAssertEqual(c.capsApplied, ["record_unproven"])
        XCTAssertEqual(c.caps?.recordUnproven, 70)
        XCTAssertEqual(c.caps?.freshnessUnmeasured, 49)
        XCTAssertEqual(c.dimensions?.accuracy?.beatsLabel, "93% likely to beat doing nothing")
    }

    func testTheWhySheetLeadsWithTheMeaningAndTheAccuracyRowShowsTheLift() throws {
        let d = ConfidenceDisplay(try decode(TrustConfidence.self, Self.k1))
        XCTAssertEqual(d.meaning, "How well supported this is — not the chance it works")
        let acc = d.rows[1]
        XCTAssertEqual(acc.basis, "improved 2 of 5 times vs about 5% by chance")
        XCTAssertEqual(acc.detail, "93% likely to beat doing nothing \u{00B7} improved-rate range 12\u{2013}76%")
        XCTAssertNil(acc.note)
        XCTAssertTrue(d.footer.contains("Until its record here shows it beats doing nothing, it stays at 70% or below."))
    }

    func testAnOlderPayloadStillReadsWithTheEnginesMeaning() throws {
        let old = try decode(TrustConfidence.self, """
            {"pct": 60, "dimensions": {"evidence": {"pct": 60}}, "version": 1}
            """)
        XCTAssertNil(old.meaning)
        XCTAssertEqual(ConfidenceDisplay(old).meaning, ConfidenceDisplay.engineMeaning)
        // A bare legacy band is not a measurement: no meaning line.
        XCTAssertNil(ConfidenceDisplay(TrustConfidence(legacyBand: "high")).meaning)
    }

    func testUndatedDataIsExplainedInTheFooter() throws {
        var c = try decode(TrustConfidence.self, Self.k1)
        c.capsApplied = ["freshness_unmeasured"]
        XCTAssertTrue(ConfidenceDisplay(c).footer.contains("Nothing dates the data under it, which holds it at 49% or below."))
    }
}
