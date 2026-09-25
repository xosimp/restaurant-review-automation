import XCTest
import SwiftUI
@testable import CavnarAI

/// Confidence re-audit round 2, group U (the phone): each test names the
/// blind-report item it holds. Fixtures are shaped the way the server sends
/// them (home_brief, schedule_engine, rec_trust, dsr/narrative).
final class ConfidenceRound2Tests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    private static let k1 = """
    {"pct": 62, "band": "medium", "label": "62% confidence", "reason": "r", "score": 0.62,
     "caution": "The data under this is out of date (POS synced 9/12/26).",
     "dimensions": {
       "evidence": {"pct": 80, "basis": "the state on file", "n": 12, "kind": "reviews"},
       "accuracy": {"pct": 75, "basis": "5 of 5 measured changes like this improved here", "n": 5,
                    "improved": 5, "source": "own", "low": 60, "high": 100},
       "freshness": {"pct": 40, "basis": "POS synced 9/12/26", "as_of": "9/12/26"}},
     "version": 1}
    """

    // MARK: - The shared confidence (B4 L1, M7; B6 low: 75/50 from the payload)

    func testSampleDataIsNotYetMeasurableNeverZero() throws {
        let c = try decode(TrustConfidence.self, """
            {"pct": 0, "band": "low", "label": "0% confidence", "reason": "Sample data — not scored",
             "dimensions": {"evidence": {"pct": 0, "basis": "Sample data — not scored", "n": 0},
                            "accuracy": {"pct": null, "n": 0, "source": "none"},
                            "freshness": {"pct": 90, "basis": "fresh"}}, "version": 1}
            """)
        let d = ConfidenceDisplay(c)
        XCTAssertNil(d.pct)
        XCTAssertEqual(d.lineLabel, "Confidence not yet measurable")
        XCTAssertEqual(d.tone, .warn)
        XCTAssertFalse(d.lineLabel.contains("0%"))
        XCTAssertEqual(d.rows.first?.value, "\u{2014}")
        // A real zero on a counted sample is still a measurement.
        let real = try decode(TrustConfidence.self, """
            {"pct": 0, "band": "low", "dimensions": {"evidence": {"pct": 0, "basis": "12 days", "n": 12}}, "version": 1}
            """)
        XCTAssertEqual(ConfidenceDisplay(real).pct, 0)
    }

    func testWhySaysTheSampleAndThePullTowardEven() throws {
        let d = ConfidenceDisplay(try decode(TrustConfidence.self, Self.k1))
        XCTAssertEqual(d.rows[0].detail, "Sample: 12")
        // Group P: accuracy is the lift against doing nothing — the "pulled
        // toward 50%" note described the shrink-to-even it replaced.
        XCTAssertNil(d.rows[1].note)
        XCTAssertTrue(d.rows[1].detail?.contains("likely to beat doing nothing") ?? false)
        XCTAssertTrue(d.footer.contains("Data under 50% fresh holds it at 49% or below"))
        var capped = try decode(TrustConfidence.self, Self.k1)
        capped.caps = TrustConfidence.Caps(noTrackRecord: 65, stale: 45, staleBelow: 55)
        XCTAssertTrue(ConfidenceDisplay(capped).footer.contains("Data under 55% fresh holds it at 45% or below"))
    }

    func testTheServerBandDecidesTheTone() throws {
        // The engine moved its cut-off: 62 is "high" by the payload's band.
        var c = try decode(TrustConfidence.self, Self.k1)
        c.band = "high"
        XCTAssertEqual(ConfidenceDisplay(c).tone, .good)
        // A dimension row reads the payload's thresholds when sent.
        c.thresholds = TrustConfidence.Thresholds(high: 70, medium: 40)
        XCTAssertEqual(ConfidenceDisplay(c).rows[0].tone, .good)
        XCTAssertEqual(ConfidenceDisplay(c).rows[2].tone, .neutral)   // freshness 40 ≥ 40
        // With none, the engine's (held to confidence_engine by a python test).
        XCTAssertEqual(TrustConfidence.Thresholds.engine, TrustConfidence.Thresholds(high: 75, medium: 50))
    }

    func testOnlyAMeasuredObjectDraws() throws {
        let band = TrustConfidence(legacyBand: "high")
        let k1 = try decode(TrustConfidence.self, Self.k1)
        XCTAssertNil(TrustConfidence.measured(nil, band))
        XCTAssertEqual(TrustConfidence.measured(nil, k1)?.pct, 62)
        XCTAssertEqual(TrustConfidence.measured(k1, band)?.pct, 62)
        // B1 L1: a diagnosis whose confidence is only the model's band
        // draws no line.
        let dg = try decode(FoodCostCFO.Diagnosis.self, #"{"cause": "c", "confidence": "high"}"#)
        XCTAssertNil(dg.trust)
    }

    // MARK: - Freshness chips (B6#9)

    func testFreshnessChipPrintsTheLabelNotTheSourceKey() throws {
        let e = try decode(HomeFreshnessEntry.self,
                           #"{"module": "labor", "source": "pos", "state": "current", "key": "labor", "label": "Labor"}"#)
        XCTAssertEqual(e.name, "Labor")
        XCTAssertEqual(e.source, "pos")
    }

    // MARK: - Shift Quality (B4 H3, H4, L2, L6)

    private static let quality = """
    {"checked": true, "score": 71, "band": "good",
     "confidence": {"score": 45, "level": "low", "reasons": ["5 of 9 scheduled staff have no Operational Score."],
                    "summary": "s"},
     "recommendations": ["Fill the gap on Friday night"],
     "recommendation_items": [{"text": "Fill the gap on Friday night", "kind": "coverage",
                               "key": "sq:coverage:fri", "rec_key": "sq:coverage:fri",
                               "confidence": \(k1)}]}
    """

    func testShiftQualityItemsDecodeTheirConfidence() throws {
        let q = try decode(ScheduleQuality.self, Self.quality)
        let item = try XCTUnwrap(q.item(for: "Fill the gap on Friday night"))
        XCTAssertEqual(item.confidence?.pct, 62)
        XCTAssertEqual(item.ledgerKey, "sq:coverage:fri")
        XCTAssertEqual(q.kind(of: "Fill the gap on Friday night"), "coverage")
        // An odd confidence never fails the schedule.
        let odd = try decode(RecommendationItem.self, #"{"text": "t", "kind": "hours", "key": "k", "confidence": [1]}"#)
        XCTAssertNil(odd.confidence?.pct)
        XCTAssertEqual(odd.text, "t")
    }

    func testShiftQualityPillIsAPercentageNeverABandWord() throws {
        let q = try decode(ScheduleQuality.self, Self.quality)
        let label = try XCTUnwrap(q.confidence?.completenessLabel)
        XCTAssertEqual(label, "Read completeness 45%")
        XCTAssertFalse(label.lowercased().contains("low confidence"))
        XCTAssertTrue(q.isProvisional)
        XCTAssertNil(q.confidenceDetail)
        // With the panel's own K1 object the pill is that figure.
        let withDetail = try decode(ScheduleQuality.self,
                                    String(Self.quality.dropLast()) + #", "confidence_detail": \#(Self.k1)}"#)
        XCTAssertEqual(withDetail.confidenceDetail?.pct, 62)
    }

    // MARK: - Older reads (B6#12)

    func testAStaleReadSaysItIsOlderAndKeepsSayingItFromTheCache() throws {
        let fresh = try decode(AIInsight.self, #"{"insight_intro": "i", "insight_recommendations": []}"#)
        XCTAssertNil(fresh.olderReadNote)
        let stale = try decode(AIInsight.self, """
            {"insight_intro": "i", "insight_recommendations": [], "stale": true, "as_of": "9/20/26",
             "stale_note": "From a read on 9/20/26 — the latest one couldn't be written."}
            """)
        XCTAssertEqual(stale.olderReadNote, "From a read on 9/20/26 — the latest one couldn't be written.")
        let bare = try decode(AIInsight.self, #"{"insight_intro": "i", "insight_recommendations": [], "stale": true, "as_of": "2026-09-20"}"#)
        XCTAssertEqual(bare.olderReadNote,
                       "This is the last read Cavnar AI completed, from 9/20/26. A new one couldn\u{2019}t be written just now.")
        // The cached copy keeps the flag.
        let again = try JSONDecoder.cavnar.decode(AIInsight.self, from: try JSONEncoder.cavnar.encode(stale))
        XCTAssertNotNil(again.olderReadNote)
    }

    // MARK: - What each dollar figure covers (B4 H7)

    func testDollarFiguresSayWhatTheyCover() throws {
        let rec = try decode(HomeRecommendation.self, """
            {"key": "trim_day:Tuesday", "title": "Trim Tuesday", "dollars_monthly": 520,
             "dollars_basis": "one Tuesday's overstaffing, per month."}
            """)
        XCTAssertEqual(rec.dollarsBasis, "covers one Tuesday's overstaffing, per month")
        XCTAssertTrue(HomeRecommendations.chips(rec).contains("covers one Tuesday's overstaffing, per month"))
        let att = try decode(NeedsAttentionItem.self, """
            {"type": "labor_over", "module": "labor", "title": "t", "detail": "d", "evidence": "$1,200 labor",
             "dollars_basis": "a week of the whole schedule's gap to your target"}
            """)
        XCTAssertEqual(att.evidenceLine, "$1,200 labor \u{00B7} covers a week of the whole schedule's gap to your target")
        XCTAssertNil(RecDollarCalibration.basis("  "))
    }

    // MARK: - The daily report (B6 sub-audit; B4 L7)

    func testDSRFooterSaysWhyEachLineWasLeftOut() throws {
        let v = try decode(DSRVerification.self, """
            {"checked": 9, "kept": 6, "dropped": [
              {"field": "went_well[0]", "text": "x", "why": "no fact"},
              {"field": "actions_tomorrow[1]", "text": "y", "why": "the owner said not for us to this"},
              {"field": "actions_tomorrow[2]", "text": "z", "why": "the same action as one above it"}]}
            """)
        XCTAssertEqual(v.footer, "Every figure above traced to a measured fact \u{00B7} 6 of 9 lines kept"
                       + " \u{00B7} 1 dropped because it didn\u{2019}t pass the check against the night\u{2019}s facts"
                       + " \u{00B7} 2 left out because you already answered them or they repeated a line above")
    }

    func testUrgencyChipIsAStatusColourNeverEmber() {
        XCTAssertEqual(DailyReportView.urgencyTint("before_service"), .cavnarAmber)
        XCTAssertNil(DailyReportView.urgencyTint("this_week"))
    }
}
