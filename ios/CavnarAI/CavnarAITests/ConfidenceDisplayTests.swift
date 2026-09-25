import XCTest
@testable import CavnarAI

/// FIXLIST J1 / K1: the one confidence object every recommendation carries,
/// and what the phone says about it. Fixtures are shaped exactly like the
/// contract; every older shape beside them (a bare band string, Home's
/// {score, band, label, reason} object, nothing at all) must still decode.
final class ConfidenceDisplayTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    /// The K1 object with all three dimensions.
    static let k1 = """
    {"pct": 72, "band": "medium", "label": "72% confidence",
     "reason": "12 reviews in 90 days · 26 of 28 days with sales", "score": 0.72, "caution": null,
     "dimensions": {
       "evidence":  {"pct": 80, "basis": "12 reviews in 90 days · 26 of 28 days with sales", "n": 12},
       "accuracy":  {"pct": 75, "basis": "3 of 4 measured changes like this improved here",
                     "n": 4, "improved": 3, "source": "own", "low": 41, "high": 94},
       "freshness": {"pct": 94, "basis": "POS synced 9/23/26 · reviews fetched today",
                     "as_of": "9/23/26", "as_of_iso": "2026-09-23", "stalest": "pos"}},
     "version": 1}
    """

    /// K1 with no track record here: accuracy withheld, overall capped.
    static let k1NoAccuracy = """
    {"pct": 64, "band": "medium", "label": "64% confidence", "reason": "No track record here yet",
     "score": 0.64, "caution": "Without a track record here this stays at 70% or below.",
     "dimensions": {
       "evidence":  {"pct": 80, "basis": "12 reviews in 90 days", "n": 12},
       "accuracy":  {"pct": null, "basis": "Not enough history yet — 2 measured, needs 5",
                     "n": 2, "improved": 1, "source": "none"},
       "freshness": {"pct": 94, "basis": "POS synced 9/23/26", "as_of": "9/23/26", "as_of_iso": "2026-09-23"}},
     "version": 1}
    """

    // MARK: - The K1 object

    func testK1ObjectDecodesAndReadsAsPercentages() throws {
        let c = try decode(TrustConfidence.self, Self.k1)
        XCTAssertEqual(c.pct, 72)
        XCTAssertEqual(c.version, 1)
        XCTAssertEqual(c.dimensions?.accuracy?.improved, 3)
        let d = ConfidenceDisplay(c)
        XCTAssertEqual(d.lineLabel, "72% confidence")
        XCTAssertEqual(d.tone, .neutral)
        XCTAssertEqual(d.reason, "12 reviews in 90 days · 26 of 28 days with sales")
        XCTAssertTrue(d.showsWhy)
        XCTAssertEqual(d.meterFraction ?? -1, 0.72, accuracy: 0.0001)
        XCTAssertEqual(d.rows.map(\.title), ["Evidence strength", "Historical accuracy", "Data freshness"])
        XCTAssertEqual(d.rows.map(\.value), ["80%", "75%", "94%"])
        XCTAssertEqual(d.rows.map(\.tone), [.good, .good, .good])
        XCTAssertEqual(d.rows[0].basis, "12 reviews in 90 days · 26 of 28 days with sales")
        XCTAssertEqual(d.rows[1].basis, "3 of 4 measured changes like this improved here")
        // Group P: the lift against doing nothing, not a rate pulled toward 50%.
        XCTAssertEqual(d.rows[1].detail, "75% likely to beat doing nothing · improved-rate range 41–94%")
        XCTAssertEqual(d.rows[2].detail, "as of 9/23/26")
        XCTAssertEqual(d.footer, "The overall figure combines all three — the weakest pulls it down most.")
        XCTAssertEqual(d.accessibilityLabel,
                       "72 percent confidence. 12 reviews in 90 days · 26 of 28 days with sales")
    }

    func testToneBoundariesAt75And50() {
        XCTAssertEqual(ConfidenceDisplay.tone(pct: 75), .good)
        XCTAssertEqual(ConfidenceDisplay.tone(pct: 74), .neutral)
        XCTAssertEqual(ConfidenceDisplay.tone(pct: 50), .neutral)
        XCTAssertEqual(ConfidenceDisplay.tone(pct: 49), .warn)
        XCTAssertEqual(ConfidenceDisplay.tone(pct: nil), .warn)
        XCTAssertEqual(ConfidenceDisplay(TrustConfidence(pct: 75, version: 1)).tone, .good)
        XCTAssertEqual(ConfidenceDisplay(TrustConfidence(pct: 74, version: 1)).tone, .neutral)
        XCTAssertEqual(ConfidenceDisplay(TrustConfidence(pct: 50, version: 1)).tone, .neutral)
        XCTAssertEqual(ConfidenceDisplay(TrustConfidence(pct: 49, version: 1)).tone, .warn)
    }

    /// The colour map: green / ink2 / amber. Never red, never ember.
    func testToneColoursAreNeverRedOrEmber() {
        let colours = [ConfidenceDisplay.Tone.good, .neutral, .warn].map(\.color)
        XCTAssertEqual(colours, [.cavnarGreen, .cavnarInk2, .cavnarAmber])
        for c in colours {
            XCTAssertNotEqual(c, .cavnarRed)
            XCTAssertNotEqual(c, .cavnarEmber)
            XCTAssertNotEqual(c, .cavnarEmber2)
        }
    }

    func testAccuracyWithheldShowsADashAndTheServersBasis() throws {
        let d = ConfidenceDisplay(try decode(TrustConfidence.self, Self.k1NoAccuracy))
        XCTAssertEqual(d.rows[1].value, "—")
        XCTAssertEqual(d.rows[1].tone, .warn)
        XCTAssertNil(d.rows[1].meterFraction)
        XCTAssertEqual(d.rows[1].basis, "Not enough history yet — 2 measured, needs 5")
        XCTAssertNil(d.rows[1].detail)
        XCTAssertEqual(d.caution, "Without a track record here this stays at 70% or below.")
        XCTAssertEqual(d.footer, "The overall figure combines all three — the weakest pulls it down most."
                       + " Without a track record here it stays at 70% or below.")
    }

    func testAccuracyWithheldWithNoBasisBuildsTheSentence() {
        let c = TrustConfidence(pct: 60, dimensions: .init(
            evidence: .init(pct: 70), accuracy: .init(pct: nil, n: 2, source: "none"), freshness: .init(pct: 90)),
            version: 1)
        let row = ConfidenceDisplay(c).rows[1]
        XCTAssertEqual(row.value, "—")
        XCTAssertEqual(row.basis, "Not enough history yet (2 measured, needs 5)")
        let none = ConfidenceDisplay.accuracyRow(.init())
        XCTAssertEqual(none.basis, "Not enough history yet (0 measured, needs 5)")
    }

    func testCohortAccuracyNamesItsCohort() {
        // Benchmarking #40: the prefix is the cohort the server named, not a
        // generic "From other restaurants on Cavnar" (which this pinned).
        let row = ConfidenceDisplay.accuracyRow(.init(pct: 80, basis: "Mexican on Cavnar: 12 of 15 improved",
                                                      n: 15, improved: 12, source: "cohort", low: 60, high: 91,
                                                      cohortLabel: "Mexican restaurants on Cavnar"))
        XCTAssertEqual(row.detail, "From Mexican restaurants on Cavnar · 80% likely to beat doing nothing · improved-rate range 60–91%")
        let unnamed = ConfidenceDisplay.accuracyRow(.init(pct: 80, basis: "Mexican on Cavnar: 12 of 15 improved",
                                                          n: 15, improved: 12, source: "cohort", low: 60, high: 91))
        XCTAssertEqual(unnamed.detail, "80% likely to beat doing nothing · improved-rate range 60–91%")
    }

    func testMissingDimensionReadsNotMeasured() throws {
        let c = try decode(TrustConfidence.self, """
            {"pct": 40, "label": "40% confidence", "dimensions": {"evidence": {"pct": 40, "basis": "3 reviews"}}, "version": 1}
            """)
        let d = ConfidenceDisplay(c)
        XCTAssertEqual(d.rows.count, 3)
        XCTAssertEqual(d.rows[1].value, "—")
        XCTAssertEqual(d.rows[2].value, "—")
        XCTAssertEqual(d.rows[2].basis, "Not measured")
        XCTAssertEqual(d.tone, .warn)
    }

    func testOverallNotMeasurable() throws {
        let c = try decode(TrustConfidence.self, """
            {"pct": null, "band": "low", "label": "Confidence not yet measurable", "reason": "Sample data",
             "score": 0.0, "caution": null, "dimensions": {"evidence": {"pct": 0, "basis": "Sample data, never scored"},
             "accuracy": {"pct": null, "n": 0, "source": "none"}, "freshness": {"pct": null}}, "version": 1}
            """)
        let d = ConfidenceDisplay(c)
        XCTAssertNil(d.pct)
        XCTAssertEqual(d.lineLabel, "Confidence not yet measurable")
        XCTAssertEqual(d.tone, .warn)
        XCTAssertNil(d.meterFraction)
        XCTAssertTrue(d.isRenderable)
        // K1 with a null pct and no label still says it plainly.
        let bare = ConfidenceDisplay(TrustConfidence(pct: nil, version: 1))
        XCTAssertEqual(bare.lineLabel, "Confidence not yet measurable")
        XCTAssertEqual(bare.tone, .warn)
    }

    /// An owner never reads an ISO date: as_of_iso alone becomes M/D/YY.
    func testFreshnessDateIsNeverISO() {
        XCTAssertEqual(ConfidenceDisplay.freshnessRow(.init(pct: 90, asOfISO: "2026-09-23")).detail, "as of 9/23/26")
        XCTAssertEqual(ConfidenceDisplay.freshnessRow(.init(pct: 90, asOf: "2026-09-23")).detail, "as of 9/23/26")
        XCTAssertEqual(ConfidenceDisplay.freshnessRow(.init(pct: 90, asOf: "9/23/26", asOfISO: "2026-09-23")).detail,
                       "as of 9/23/26")
        XCTAssertNil(ConfidenceDisplay.freshnessRow(.init(pct: 90)).detail)
        XCTAssertNil(ConfidenceDisplay.mdyDate(asOf: "2026-13", asOfISO: nil))
    }

    // MARK: - Older shapes

    func testLegacyBandStrings() throws {
        let medium = try decode(TrustConfidence.self, #""medium""#)
        XCTAssertEqual(medium.band, "medium")
        XCTAssertFalse(medium.isMeasuredShape)
        let dm = ConfidenceDisplay(medium)
        XCTAssertEqual(dm.lineLabel, "Medium confidence")
        XCTAssertEqual(dm.tone, .neutral)
        XCTAssertFalse(dm.showsWhy)
        XCTAssertNil(dm.meterFraction)

        let moderate = ConfidenceDisplay(try decode(TrustConfidence.self, #""moderate""#))
        XCTAssertEqual(moderate.lineLabel, "Medium confidence")
        XCTAssertEqual(moderate.tone, .neutral)

        let low = ConfidenceDisplay(try decode(TrustConfidence.self, #""LOW""#))
        XCTAssertEqual(low.lineLabel, "Low confidence")
        XCTAssertEqual(low.tone, .warn)

        let high = ConfidenceDisplay(try decode(TrustConfidence.self, #""high""#))
        XCTAssertEqual(high.lineLabel, "High confidence")
        XCTAssertEqual(high.tone, .good)

        // "unknown" is not a band: nothing to draw.
        XCTAssertFalse(ConfidenceDisplay(try decode(TrustConfidence.self, #""unknown""#)).isRenderable)
    }

    /// Home's card object from before K1 — score/band/label/reason, no pct,
    /// no version — renders exactly as it did.
    func testLegacyHomeObject() throws {
        let c = try decode(TrustConfidence.self, """
            {"score": 0.55, "band": "medium", "label": "Medium confidence", "reason": "6 reviews in 90 days"}
            """)
        XCTAssertEqual(c.score, 0.55)
        XCTAssertNil(c.pct)
        let d = ConfidenceDisplay(c)
        XCTAssertEqual(d.lineLabel, "Medium confidence")
        XCTAssertEqual(d.reason, "6 reviews in 90 days")
        XCTAssertEqual(d.tone, .neutral)
        XCTAssertFalse(d.showsWhy)
        // A constant score is never drawn as a measured meter.
        XCTAssertNil(d.meterFraction)
        // The low band's caution from a server that predates the label.
        let lowOld = ConfidenceDisplay(try decode(TrustConfidence.self, """
            {"score": 0.3, "band": "low", "caution": "Only 2 reviews behind this."}
            """))
        XCTAssertEqual(lowOld.lineLabel, "Low confidence")
        XCTAssertEqual(lowOld.caution, "Only 2 reviews behind this.")
        XCTAssertEqual(lowOld.tone, .warn)
    }

    func testRoundTripsThroughTheCache() throws {
        for json in [Self.k1, #""moderate""#, #"{"score": 0.55, "band": "medium", "label": "Medium confidence"}"#] {
            let c = try decode(TrustConfidence.self, json)
            let again = try JSONDecoder.cavnar.decode(TrustConfidence.self, from: try JSONEncoder.cavnar.encode(c))
            XCTAssertEqual(again, c)
            XCTAssertEqual(ConfidenceDisplay(again), ConfidenceDisplay(c))
        }
    }

    /// Garbage never throws: an odd field is nil, the rest still reads.
    func testGarbageDegradesWithoutThrowing() throws {
        let c = try decode(TrustConfidence.self, """
            {"pct": "abc", "band": 7, "label": ["x"], "reason": "r", "score": "high",
             "dimensions": [1, 2], "version": "one"}
            """)
        XCTAssertNil(c.pct)
        XCTAssertNil(c.band)
        XCTAssertNil(c.label)
        XCTAssertNil(c.score)
        XCTAssertNil(c.dimensions)
        XCTAssertNil(c.version)
        XCTAssertEqual(c.reason, "r")
        XCTAssertFalse(ConfidenceDisplay(c).isRenderable)

        let dims = try decode(TrustConfidence.self, """
            {"pct": 72.6, "dimensions": {"evidence": "strong", "accuracy": {"pct": 140, "n": "four"},
             "freshness": {"pct": -5}}, "version": 1}
            """)
        XCTAssertEqual(dims.pct, 73)
        XCTAssertNil(dims.dimensions?.evidence)
        XCTAssertEqual(dims.dimensions?.accuracy?.pct, 100)
        XCTAssertNil(dims.dimensions?.accuracy?.n)
        XCTAssertEqual(dims.dimensions?.freshness?.pct, 0)

        // A number or an array where the object belongs: empty, not a throw.
        XCTAssertFalse(ConfidenceDisplay(try decode(TrustConfidence.self, "0.7")).isRenderable)
        XCTAssertFalse(ConfidenceDisplay(try decode(TrustConfidence.self, "[1]")).isRenderable)
    }

    // MARK: - Every surface decodes all three shapes

    private static let shapes: [(String, String)] = [
        ("string", #""confidence": "medium","#),
        ("k1", "\"confidence\": \(ConfidenceDisplayTests.k1),"),
        ("absent", ""),
        ("garbage", #""confidence": {"pct": "abc", "dimensions": []},"#),
    ]

    @MainActor
    func testHomeRecommendationDecodesEveryShapeAndDropsStrength() throws {
        for (name, field) in Self.shapes {
            let rec = try decode(HomeRecommendation.self, """
                {"key": "k", "title": "Do the thing", \(field) "strength": "strong", "model_written": true}
                """)
            XCTAssertEqual(rec.key, "k", name)
            XCTAssertEqual(rec.modelWritten, true, name)
            XCTAssertEqual(rec.confidence == nil, name == "absent", name)
        }
        // The old evidence-strength pill is gone from the meta line.
        let rec = try decode(HomeRecommendation.self, """
            {"key": "k", "title": "t", "timeframe": "This week", "impact": "Medium", "strength": "strong"}
            """)
        XCTAssertEqual(HomeRecommendations.chips(rec), ["This week", "Medium"])
        // A new server omits strength entirely.
        XCTAssertNil(try decode(HomeRecommendation.self, #"{"key": "k", "title": "t"}"#).strength)
        // An odd model_written is nil, never a failed Home.
        XCTAssertNil(try decode(HomeRecommendation.self, #"{"key": "k", "title": "t", "model_written": "yes"}"#).modelWritten)
    }

    func testNeedsAttentionItemCarriesEvidenceAndConfidence() throws {
        for (name, field) in Self.shapes {
            let item = try decode(NeedsAttentionItem.self, """
                {"type": "labor_overtime", "module": "labor", "title": "Labor over target",
                 "detail": "33% against 30%", \(field) "evidence": "8 of 28 days carry sales", "rec_key": "attn:labor"}
                """)
            XCTAssertEqual(item.evidence, "8 of 28 days carry sales", name)
            XCTAssertEqual(item.confidence == nil, name == "absent", name)
        }
        let old = try decode(NeedsAttentionItem.self, """
            {"type": "t", "module": "reviews", "title": "x", "detail": "y", "evidence": ""}
            """)
        XCTAssertNil(old.evidence)
        XCTAssertNil(old.confidence)
    }

    /// The deck grows to fit the evidence and confidence lines, the same
    /// height for every card so the ghosts line up.
    @MainActor
    func testActionDeckHeightMakesRoomForTheNewLines() throws {
        let plain = try decode(NeedsAttentionItem.self, #"{"type": "a", "module": "m", "title": "t", "detail": "d"}"#)
        let rich = try decode(NeedsAttentionItem.self, """
            {"type": "b", "module": "m", "title": "t", "detail": "d", "evidence": "e", "confidence": "low"}
            """)
        let base = HomeActionDeck.cardHeight(for: [plain])
        let grown = HomeActionDeck.cardHeight(for: [plain, rich])
        XCTAssertEqual(base, 150)
        XCTAssertGreaterThan(grown, base)
    }

    func testOneThingHeroCarriesConfidenceClaimKindAndAMoneyRange() throws {
        typealias FixFirst = HomeFollowThroughViewModel.CrossModule.FixFirst
        for (name, field) in Self.shapes {
            let ff = try decode(FixFirst.self, """
                {"key": "one", "what": "Brief the servers", \(field) "claim_kind": "inferred",
                 "money": {"low": 1200, "high": 2400, "label": "Biggest dollar opportunity · rating movement"}}
                """)
            XCTAssertEqual(ff.claimKind, "inferred", name)
            XCTAssertEqual(ff.confidence == nil, name == "absent", name)
            // The range, never one figure pulled out of it.
            XCTAssertEqual(ff.moneyRange, "$1,200–$2,400/month", name)
            XCTAssertEqual(ff.money?.label, "Biggest dollar opportunity · rating movement", name)
        }
        // A measured monthly figure wins; the range is only the fallback.
        let measured = try decode(FixFirst.self, """
            {"what": "x", "dollars_monthly": 800, "money": {"low": 1200, "high": 2400}}
            """)
        XCTAssertNil(measured.moneyRange)
        let odd = try decode(FixFirst.self, #"{"what": "x", "money": "lots"}"#)
        XCTAssertNil(odd.money)
        XCTAssertNil(odd.moneyRange)
    }

    func testDiagnosesDecodeEveryShape() throws {
        for (name, field) in Self.shapes {
            let review = try decode(ReviewDiagnosis.self, """
                {"category": "service", "mention_count": 3, "window_days": 30, "cause": "c",
                 \(field) "evidence_review_ids": [1], "operational_evidence": []}
                """)
            XCTAssertEqual(review.confidence == nil, name == "absent", name)

            let food = try decode(FoodCostCFO.self, """
                {"ok": true, "diagnosis": {"cause": "Chicken up 14%.", \(field) "rec_key": "diag_food:protein"},
                 "drivers": {"available": true, "drivers": [{"kind": "price", "label": "Chicken", "dollars_monthly": 420,
                   \(field) "difficulty": "low", "evidence": "3 invoices", "if_ignored": "keeps costing"}]}}
                """)
            XCTAssertEqual(food.diagnosis?.recKey, "diag_food:protein", name)
            XCTAssertEqual(food.drivers?.drivers.count, 1, name)
            XCTAssertEqual(food.drivers?.drivers.first?.confidence == nil, name == "absent", name)

            let labor = try decode(LaborDiagnosis.self, """
                {"available": true, "cause": "Overtime", \(field) "operational_evidence": []}
                """)
            XCTAssertEqual(labor.confidence == nil, name == "absent", name)
        }
        let review = try decode(ReviewDiagnosis.self, """
            {"category": "service", "mention_count": 3, "window_days": 30, "cause": "c", "confidence": "moderate",
             "evidence_review_ids": [], "operational_evidence": []}
            """)
        XCTAssertEqual(review.confidenceBand, "medium")
        let k1Review = try decode(ReviewDiagnosis.self, """
            {"category": "service", "mention_count": 3, "window_days": 30, "cause": "c",
             "confidence": \(Self.k1), "evidence_review_ids": [], "operational_evidence": []}
            """)
        XCTAssertEqual(k1Review.confidence?.pct, 72)
        XCTAssertEqual(k1Review.confidenceBand, "medium")
    }

    func testAskAnswerConfidenceEveryShape() throws {
        for (name, field) in Self.shapes {
            let e = try decode(APIClient.SSEEvent.self, """
                {"type": "answer", "answer": "a", \(field) "modules_consulted": ["labor"], "unverified_figures": []}
                """)
            XCTAssertEqual(e.evidence.modules, ["labor"], name)
            let expectsLine = name == "string" || name == "k1"
            XCTAssertEqual(e.evidence.confidenceLabel != nil, expectsLine, name)
        }
        let k1 = try decode(APIClient.SSEEvent.self, """
            {"type": "answer", "answer": "a", "confidence": \(Self.k1)}
            """)
        XCTAssertEqual(k1.evidence.confidenceLabel, "72% confidence")
        XCTAssertFalse(k1.evidence.isEmpty)
        // K5's nested meta block reads too.
        let nested = try decode(APIClient.SSEEvent.self, """
            {"type": "answer", "answer": "a", "meta": {"confidence": \(Self.k1)}}
            """)
        XCTAssertEqual(nested.evidence.confidence?.pct, 72)
        // Low is amber, never red (CA4 F9).
        let low = try decode(APIClient.SSEEvent.self, #"{"type": "answer", "answer": "a", "confidence": "low"}"#)
        XCTAssertEqual(low.evidence.confidenceLabel, "Low confidence")
        XCTAssertEqual(ConfidenceDisplay(try XCTUnwrap(low.evidence.confidence)).tone.color, .cavnarAmber)
        XCTAssertFalse(low.evidence.isEmpty)
        // A legacy "high" with no modules draws nothing, as before; "unknown" never does.
        XCTAssertTrue(try decode(APIClient.SSEEvent.self, #"{"type": "answer", "confidence": "high"}"#).evidence.isEmpty)
        let unknown = try decode(APIClient.SSEEvent.self, #"{"type": "answer", "confidence": "unknown"}"#)
        XCTAssertNil(unknown.evidence.confidence)
        XCTAssertTrue(unknown.evidence.isEmpty)
        // A meta that isn't an object doesn't cost the answer.
        XCTAssertEqual(try decode(APIClient.SSEEvent.self, #"{"type": "answer", "answer": "a", "meta": 3}"#).answer, "a")
    }

    func testDSRActionConfidenceAndTheVerificationFooter() throws {
        for (name, field) in Self.shapes {
            let a = try decode(DSRAction.self, """
                {"text": "Add a server Friday", \(field) "urgency": "this_week", "rec_key": "dsr_action:staff:labor"}
                """)
            XCTAssertEqual(a.confidence == nil, name == "absent", name)
            XCTAssertEqual(a.answerModule, "labor", name)
        }
        let n = try decode(DSRNarrative.self, """
            {"actions_tomorrow": [], "verification": {"rule": "r", "checked": 8, "kept": 7,
             "dropped": [{"field": "went_well[0]", "text": "x", "why": "no fact"}]}}
            """)
        XCTAssertEqual(n.verification?.dropped, 1)
        XCTAssertEqual(n.verification?.footer,
                       "Every figure above traced to a measured fact · 7 of 8 lines kept · 1 dropped because it didn’t pass the check against the night’s facts")
        let withEstimates = try decode(DSRVerification.self, #"{"checked": 5, "kept": 5, "dropped": [], "estimated": 2}"#)
        XCTAssertEqual(withEstimates.footer,
                       "Every figure above traced to a measured fact · 5 of 5 lines kept · 2 estimates, labelled as such")
        let estimatesList = try decode(DSRVerification.self, #"{"checked": 3, "kept": 3, "estimates": ["est_food_cost_pct"]}"#)
        XCTAssertEqual(estimatesList.estimated, 1)
        let older = try decode(DSRVerification.self, #"{"checked": 4, "kept": 4}"#)
        XCTAssertEqual(older.footer, "Every figure above traced to a measured fact · 4 of 4 lines kept")
        XCTAssertNil(try decode(DSRVerification.self, #"{"checked": 0, "kept": 0}"#).footer)
    }

    // MARK: - Claim kinds

    func testClaimKindLabels() {
        XCTAssertEqual(ClaimKind.label(kind: "measured"), "Measured")
        XCTAssertEqual(ClaimKind.label(kind: "computed"), "Computed")
        XCTAssertEqual(ClaimKind.label(kind: "forecast"), "Forecast")
        XCTAssertEqual(ClaimKind.label(kind: "inferred"), "Inferred")
        XCTAssertEqual(ClaimKind.label(kind: "inferred", modelWritten: true), "AI-written")
        XCTAssertEqual(ClaimKind.label(kind: nil, modelWritten: true), "AI-written")
        XCTAssertNil(ClaimKind.label(kind: "unavailable"))
        XCTAssertNil(ClaimKind.label(kind: nil))
    }

    func testClaimKindMapSkipsOddValues() throws {
        let m = try decode(ClaimKindMap.self, #"{"this_week": "measured", "why": null, "n": 3}"#)
        XCTAssertEqual(m.kinds, ["this_week": "measured"])
        XCTAssertEqual(try decode(ClaimKindMap.self, "[1]").kinds, [:])
    }

    @MainActor
    func testReviewsReadDecodesTrendAndClaimKinds() async {
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/reviews/insight":
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "insight": "📊 This week: 12 reviews.",
                 "claim_kinds": {"this_week": "measured", "why": "inferred", "rating_trend": "computed"},
                 "confidence": "moderate",
                 "trend": {"direction": "improving", "confidence": "moderate", "change": 0.2, "first": 4.1,
                           "latest": 4.3, "weeks_above_floor": 9, "reason": "r", "anomalies": [],
                           "trend_strength_pct": 62.4}}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "data": null, "weeks": []}"#)
            }
        }
        let vm = ReviewsAnalyticsViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.trendConfidence, "medium")
        XCTAssertEqual(vm.claimKinds["why"], "inferred")
        XCTAssertEqual(vm.ratingTrend?.sentence, "Rating improving (+0.2★ over 9 weeks)")
        // The trend reads its measured strength, never a band word (B4 L2).
        XCTAssertEqual(vm.ratingTrend?.trendStrengthPct, 62)
        XCTAssertEqual(ReviewsAnalyticsSection.trendStrengthLabel(vm.ratingTrend?.trendStrengthPct),
                       "Trend strength 62%")
        XCTAssertNil(ReviewsAnalyticsSection.trendStrengthLabel(nil))
        XCTAssertEqual(ReviewsAnalyticsSection.claimKey(forInsightLine: "📊 This week: 12 reviews."), "this_week")
        XCTAssertEqual(ReviewsAnalyticsSection.claimKey(forInsightLine: "✅ Do today: call"), "do_today")
    }

    func testAReviewsTrendThatIsNotAnObjectDoesNotCostTheRead() throws {
        XCTAssertNil(try decode(ReviewRatingTrend.self, #""up""#).direction)
    }

    // MARK: - Forecast and demand accuracy (K8)

    func testFoodForecastAccuracySentence() throws {
        let k8 = try decode(FoodCostCFO.self, """
            {"ok": true, "brief": {"forecast_accuracy": {"reading": "roughly right", "mean_error_pct": 22.4,
              "n_weeks": 6, "withheld": false}}}
            """)
        XCTAssertEqual(k8.brief?.forecastAccuracy?.sentence,
                       "Past forecasts here have been roughly right (22% mean error over 6 weeks)")
        // Today's server carries it inside trust, as available/scored.
        let today = try decode(FoodCostCFO.self, """
            {"ok": true, "brief": {"trust": {"recipe_coverage_pct": 82, "inferred_waste_pct": 30,
              "forecast_accuracy": {"available": true, "scored": 4, "mean_error_pct": 12.0, "reading": "close"}}}}
            """)
        XCTAssertEqual(today.brief?.forecastAccuracy?.sentence,
                       "Past forecasts here have been close (12% mean error over 4 weeks)")
        XCTAssertEqual(today.brief?.trust?.recipeCoveragePct, 82)
        let withheld = try decode(ForecastAccuracy.self, #"{"reading": "often wide", "withheld": true}"#)
        XCTAssertNil(withheld.sentence)
        let unavailable = try decode(ForecastAccuracy.self, #"{"available": false, "scored": 1, "reason": "r"}"#)
        XCTAssertNil(unavailable.sentence)
    }

    func testDemandAccuracySentence() throws {
        let d = try decode(DemandAccuracy.self, """
            {"mean_error_pct": 11.6, "bias_pct": -3.0, "inside_range_pct": 64.2, "n_nights": 21, "n_ranged": 14}
            """)
        // The inside-range share is of the nights that had a range (B6#10),
        // and a negative actual-vs-forecast means nights came in BELOW it (B6#2).
        XCTAssertEqual(d.sentence, "Demand forecasts here: inside the range on 64% of the 14 nights that had one"
                       + " · 21 nights measured · 12% mean error · nights came in 3% below the forecast on average")
        XCTAssertEqual(try decode(DemandAccuracy.self, #"{"mean_error_pct": 9, "n_nights": 14}"#).sentence,
                       "Demand forecasts here: 14 nights measured · 9% mean error")
        // 1 of 1 ranged night, 10 scored: never "100% of 10 nights" (B1 H7).
        let thin = try decode(DemandAccuracy.self, #"{"inside_range_pct": 100, "n_nights": 10, "n_ranged": 1, "mean_error_pct": 25, "actual_vs_forecast_pct": 8}"#)
        XCTAssertEqual(thin.sentence, "Demand forecasts here: inside the range on 100% of the 1 night that had one"
                       + " · 10 nights measured · 25% mean error · nights came in 8% above the forecast on average")
        // An older server with no n_ranged: no inside-range claim at all.
        XCTAssertNil(try decode(DemandAccuracy.self, #"{"inside_range_pct": 64, "n_nights": 21}"#).sentence)
        XCTAssertNil(try decode(DemandAccuracy.self, #"{"n_nights": 3}"#).sentence)
        XCTAssertNil(try decode(DemandAccuracy.self, #""soon""#).sentence)
    }

    func testScheduleCalibrationNotesAreLenient() throws {
        let edit = try decode(LikelyEdit.self, """
            {"kind": "predicted", "likelihood": 0.6, "calibration_note": "Right 7 of 10 times in the backtest"}
            """)
        XCTAssertEqual(edit.calibrationText, "Right 7 of 10 times in the backtest")
        let viaNote = try decode(StandbyDay.self, """
            {"date": "2026-09-26", "chance_of_a_no_show": 0.3, "note": "Assumes no-shows are independent"}
            """)
        XCTAssertEqual(viaNote.calibrationText, "Assumes no-shows are independent")
        let odd = try decode(StandbyDay.self, #"{"date": "2026-09-26", "calibration_note": 4}"#)
        XCTAssertNil(odd.calibrationText)
    }

    // MARK: - Smaller fixes

    func testRecipeLineMarksHighMediumLowDistinctly() throws {
        func line(_ c: String?) throws -> RecipeDraftLine {
            let conf = c.map { "\"\($0)\"" } ?? "null"
            return try decode(RecipeDraftLine.self, #"{"name": "Chicken", "qty": 6, "unit": "oz", "confidence": \#(conf)}"#)
        }
        XCTAssertNil(try line("high").mark)
        XCTAssertEqual(try line("medium").mark, .check)
        XCTAssertEqual(try line("low").mark, .doubt)
        XCTAssertNil(try line(nil).mark)
    }

    @MainActor
    func testOrbitNeverDrawsAMissingScoreAsZero() {
        XCTAssertEqual(VisibilityOrbitChart.centerText(nil), "—")
        XCTAssertEqual(VisibilityOrbitChart.centerText(0), "0")
        XCTAssertEqual(VisibilityOrbitChart.centerText(57), "57")
    }

    @MainActor
    func testPresenceToneFollowsTheServerWhenSent() throws {
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: ServerTone("good"), score: 10), .cavnarGreen)
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: ServerTone("bad"), score: 95), .cavnarRed)
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: nil, score: 75), .cavnarGreen)
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: ServerTone(nil), score: 50), .cavnarAmber)
        XCTAssertNil(try decode(ServerTone.self, "3").value)
        XCTAssertEqual(try decode(ServerTone.self, #""Warning""#).value, "warn")
    }

    @MainActor
    func testAvoidedFiguresStateTheirRates() throws {
        let av = try decode(HomeFollowThroughViewModel.ValueSummary.Avoided.self, """
            {"items": [{"key": "replies", "label": "412 review replies written", "dollars": 1442.0, "hours": 34.3,
                        "rate": "$3.50 each", "basis": "what a reply service charges"},
                       {"key": "schedules", "label": "9 weeks of schedule built", "rate": "90 min a week",
                        "basis": "building a week's schedule by hand"},
                       {"key": "bare", "label": "no rate"}],
             "dollars": 1442.0, "hours": 47.8}
            """)
        XCTAssertEqual(HomeFollowThrough.avoidedRateLines(av), [
            "412 review replies written: $3.50 each — what a reply service charges",
            "9 weeks of schedule built: 90 min a week — building a week's schedule by hand",
        ])
    }
}
