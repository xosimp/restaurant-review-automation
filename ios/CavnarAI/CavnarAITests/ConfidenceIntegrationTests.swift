import XCTest
@testable import CavnarAI

/// The confidence audit's client-side integration pass (groups E–J on the
/// phone): every field the server now sends decodes present, absent and
/// null, under the name the Python actually uses, and the owner-facing
/// wording it drives is pinned here rather than by one rendered payload.
@MainActor
final class ConfidenceIntegrationTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(type, from: Data(json.utf8))
    }

    // MARK: - Group I: AI visibility chip and listing strength

    private static let aiBase = #""ok": true, "ai_score": 50, "ai_score_low": 30, "ai_score_high": 90, "presence_score": 72"#

    func testAIVisibilityBandFromTheRange() throws {
        let r = try decode(AIVisibilityResult.self, """
            {\(Self.aiBase), "ai_score_band": "uncertain",
             "ai_score_label": "somewhere between 30% and 90% — too few questions to say more",
             "ai_score_tone": "neutral", "presence_label": "Listing strength",
             "presence_band_label": "a few gaps", "presence_tone": "warn"}
            """)
        XCTAssertEqual(r.aiScoreBand, "uncertain")
        XCTAssertEqual(r.aiChipText, "Somewhere between 30% and 90% — too few questions to say more")
        XCTAssertEqual(r.aiScoreTone?.value, "neutral")
        XCTAssertEqual(r.aiScoreTone?.color, .cavnarInk2)
        // presence_label is the NAME; the band is presence_band_label.
        XCTAssertEqual(r.presenceHeading, "Listing strength")
        XCTAssertEqual(r.presenceChipText, "A few gaps")
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: r.presenceTone, score: 95), .cavnarAmber)
    }

    func testAIVisibilityOlderServerAndNulls() throws {
        let old = try decode(AIVisibilityResult.self, "{\(Self.aiBase)}")
        XCTAssertNil(old.aiChipText)
        XCTAssertNil(old.aiScoreTone)
        XCTAssertEqual(old.presenceHeading, "Listing strength")
        XCTAssertNil(old.presenceChipText)
        let nulls = try decode(AIVisibilityResult.self, """
            {\(Self.aiBase), "ai_score_band": null, "ai_score_label": null, "ai_score_tone": null,
             "presence_label": null, "presence_band_label": null, "presence_tone": 7}
            """)
        XCTAssertNil(nulls.aiChipText)
        XCTAssertNil(nulls.presenceTone?.value)
        XCTAssertEqual(AIVisibilitySection.presenceColor(server: ServerTone("neutral"), score: 10), .cavnarInk2)
    }

    // MARK: - Group I: market standing

    private static let intelBase = #""ok": true, "has_data": true, "recommendations": [], "sections": [], "competitors": [], "own_rating": 4.6, "own_rating_basis": "google_all_time", "market_rating": 4.3"#

    func testIntelStandingFromTheServer() throws {
        let s = try decode(IntelSummary.self, """
            {\(Self.intelBase), "own_vs_market": 0.3, "standing": "ahead",
             "standing_label": "You lead the block", "standing_tone": "good"}
            """)
        XCTAssertEqual(s.standingLine, "You lead the block · +0.3★ against the market")
        XCTAssertEqual(IntelView.ownRatingTone(s, own: 4.6, market: 4.3), .cavnarGreen)
        let behind = try decode(IntelSummary.self, """
            {\(Self.intelBase), "own_vs_market": -0.4, "standing": "behind",
             "standing_label": "Behind the block", "standing_tone": "bad"}
            """)
        XCTAssertEqual(behind.standingLine, "Behind the block · −0.4★ against the market")
        XCTAssertEqual(IntelView.ownRatingTone(behind, own: 4.6, market: 4.3), .cavnarRed)
    }

    func testIntelStandingNullAndAbsent() throws {
        // The server declined the comparison (not the same kind of number).
        let declined = try decode(IntelSummary.self, """
            {\(Self.intelBase), "own_vs_market": null, "standing": null, "standing_label": null,
             "standing_tone": "neutral"}
            """)
        XCTAssertNil(declined.standingLine)
        XCTAssertEqual(IntelView.ownRatingTone(declined, own: 4.6, market: 4.3), .cavnarInk)
        // An older server: the client's own ±0.3★ rule.
        let old = try decode(IntelSummary.self, "{\(Self.intelBase)}")
        XCTAssertNil(old.standingLine)
        XCTAssertEqual(IntelView.ownRatingTone(old, own: 4.6, market: 4.3), .cavnarGreen)
    }

    // MARK: - Group I: labor

    private static let laborBase = """
        "ok": true, "is_live": true, "overall_labor_pct": 31.2, "target": 30.0, "on_track": false,
        "potential_savings": 0, "overtime_risk": [], "role_summary": [],
        "date_range": {"start": "2026-09-01", "end": "2026-09-14"},
        "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}
        """

    func testLaborIndustryUpcomingAndAccuracy() throws {
        let s = try decode(LaborStats.self, """
            {\(Self.laborBase),
             "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0,
               "labor_vs_industry_monthly": 1200, "labor_vs_industry_annual": 14400,
               "labor_industry_pct": 34.5,
               "labor_industry_basis": "midpoint of the 33-36% full-service range (NRA 2024 Restaurant Operations Data Abstract)"},
             "labor_upcoming": [{"name": "Halloween", "date": "2026-10-31", "date_str": "10/31/26", "days_away": 7,
               "lift_pct": 18, "based_on": "3 Saturdays", "claim_kind": "measured",
               "label": "Last year 18% above a typical Saturday here — 3 Saturdays"}],
             "demand_accuracy": {"available": true, "n_nights": 21, "mean_error_pct": 11.6, "bias_pct": -3.0,
               "inside_range_pct": 64, "n_ranged": 14},
             "week_projection_accuracy": {"available": true, "kind": "revenue_week", "n_weeks": 5,
               "mean_error_pct": 8.2, "bias_pct": 6.0, "reading": "close", "withheld": false}}
            """)
        XCTAssertEqual(s.savingsBreakdown.industryPctText, "34.5%")
        XCTAssertTrue(s.savingsBreakdown.laborIndustryBasis?.hasPrefix("midpoint") == true)
        let e = try XCTUnwrap(s.laborUpcoming.first)
        XCTAssertEqual(e.dateStr, "10/31/26")
        XCTAssertEqual(e.label, "Last year 18% above a typical Saturday here — 3 Saturdays")
        XCTAssertEqual(e.claimKind, "measured")
        XCTAssertEqual(e.planningLine, "Check this week's schedule against it.")
        XCTAssertEqual(s.demandAccuracy?.sentence, "Demand forecasts here: inside the range on 64% of the 14 nights that had one · 21 nights measured · 12% mean error · nights came in 3% below the forecast on average")
        XCTAssertEqual(LaborView.forecastRecordLines(s), [
            "Demand forecasts here: inside the range on 64% of the 14 nights that had one · 21 nights measured · 12% mean error · nights came in 3% below the forecast on average",
            "Past weekly sales projections here have been close (8% mean error over 5 weeks); they have run 6% high on average",
        ])
        // The published mark is context (Benchmarking #2): no "points below
        // the industry benchmark" sentence is built any more.
        XCTAssertTrue(LaborAnalyticsSection.industryDefinitionNote.contains("context, not a like-for-like"))
    }

    /// Benchmarking #2 / #10: the server may drop the "vs industry" dollars
    /// (or send 0) and names the target — the decoder takes both.
    func testLaborIndustryDollarsAbsentAndTheServersTargetLabel() throws {
        let s = try decode(LaborStats.self, """
            {\(Self.laborBase),
             "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0,
               "labor_target_label": "Cavnar's starting target", "labor_target_source": "default"},
             "labor_upcoming": [], "demand_accuracy": null, "week_projection_accuracy": null}
            """)
        XCTAssertNil(s.savingsBreakdown.laborVsIndustryMonthly)
        XCTAssertNil(s.savingsBreakdown.laborVsIndustryAnnual)
        XCTAssertEqual(LaborAnalyticsSection.targetName(s), "Cavnar's starting target")
        XCTAssertEqual(LaborAnalyticsSection.targetLegend(s, target: 30), "Cavnar's starting target (30%)")
        let older = try decode(LaborStats.self, """
            {\(Self.laborBase),
             "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0,
               "labor_vs_industry_monthly": 0, "labor_vs_industry_annual": 0}, "labor_upcoming": [],
             "demand_accuracy": null, "week_projection_accuracy": null}
            """)
        XCTAssertEqual(older.savingsBreakdown.laborVsIndustryMonthly, 0)
        XCTAssertEqual(LaborAnalyticsSection.targetLegend(older, target: 30), "Your target (30%)")
    }

    func testLaborFieldsAbsentAndNull() throws {
        let s = try decode(LaborStats.self, """
            {\(Self.laborBase),
             "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0,
               "labor_vs_industry_monthly": 0, "labor_vs_industry_annual": 0},
             "labor_upcoming": [{"name": "Labor Day", "date_str": "September 1st", "days_away": 12}],
             "demand_accuracy": null, "week_projection_accuracy": null}
            """)
        XCTAssertNil(s.savingsBreakdown.laborIndustryPct)
        // No benchmark for the type (NS4 H3): nothing to name, never 34.5%.
        XCTAssertNil(s.savingsBreakdown.industryPctText)
        XCTAssertNil(s.laborUpcoming.first?.label)
        XCTAssertEqual(s.laborUpcoming.first?.planningLine, "Flag it for your next schedule build.")
        XCTAssertEqual(LaborView.forecastRecordLines(s), [])
        // Not enough nights yet: nothing is said, never a zero.
        let early = try decode(LaborStats.self, """
            {\(Self.laborBase),
             "savings_breakdown": {"labor_monthly": 0, "labor_annual": 0, "labor_overtime": 0,
               "labor_vs_industry_monthly": 0, "labor_vs_industry_annual": 0}, "labor_upcoming": [],
             "demand_accuracy": {"available": false, "n_nights": 3, "mean_error_pct": null, "bias_pct": null,
               "inside_range_pct": null, "n_ranged": 0, "reason": "only 3 nights scored"},
             "week_projection_accuracy": {"available": false, "n_weeks": 1, "reading": null, "withheld": false,
               "reason": "not enough scored forecasts yet"}}
            """)
        XCTAssertEqual(LaborView.forecastRecordLines(early), [])
    }

    // MARK: - K8: forecast accuracy (food, prime cost)

    func testPrimeCostAccuracyCountsMonthsAndWithheldSaysWhy() throws {
        let cfo = try decode(FoodCostCFO.self, """
            {"ok": true, "brief": {
               "forecast_accuracy": {"reading": "often wide", "mean_error_pct": 41.0, "n_weeks": 5, "withheld": true,
                 "bias_pct": 30.0, "reason": "past forecasts here missed by 41% on average over 5 weeks, so the next one is not shown"},
               "prime_cost_accuracy": {"reading": "close", "mean_error_pct": 3.1, "n_months": 4, "withheld": false,
                 "bias_pct": -1.0}}}
            """)
        let waste = try XCTUnwrap(cfo.brief?.forecastAccuracy)
        XCTAssertNil(waste.sentence)
        XCTAssertEqual(waste.line, "Past forecasts here missed by 41% on average over 5 weeks, so the next one is not shown")
        let prime = try XCTUnwrap(cfo.brief?.primeCostAccuracy)
        XCTAssertEqual(prime.nMonths, 4)
        XCTAssertNil(prime.nWeeks)
        XCTAssertEqual(prime.line, "Past forecasts here have been close (3% mean error over 4 months)")
        let none = try decode(FoodCostCFO.self, #"{"ok": true, "brief": {"prime_cost_accuracy": null}}"#)
        XCTAssertNil(none.brief?.primeCostAccuracy)
        let absent = try decode(FoodCostCFO.self, #"{"ok": true, "brief": {}}"#)
        XCTAssertNil(absent.brief?.primeCostAccuracy?.line)
    }

    // MARK: - Group I: inventory

    private static let analyticsBase = """
        "ok": true, "insight_recommendations": [], "waste_items": [], "overstock": [], "critical_low": [],
        "reorder_soon": [], "order_reduction": [], "price_watch": []
        """

    func testNotMeasuredWasteNeverReadsAsAResult() throws {
        let sent = try decode(FoodCostAnalytics.self, """
            {\(Self.analyticsBase), "benchmark_label": "No waste recorded", "benchmark_state": "not_measured",
             "recoverable_kind": "opportunity", "annual_recoverable_basis": "one week's count × 4.33 weeks a month × 12"}
            """)
        XCTAssertEqual(sent.wasteState, "not_measured")
        XCTAssertEqual(sent.recoverableKind, "opportunity")
        XCTAssertEqual(sent.recoverableBasisLine, "one week's count × 4.33 weeks a month × 12")
        // The mobile route doesn't pass benchmark_state: inferred from the label.
        let inferred = try decode(FoodCostAnalytics.self, """
            {\(Self.analyticsBase), "benchmark_label": "No waste recorded", "waste_rate_pct": 0}
            """)
        XCTAssertEqual(inferred.wasteState, "not_measured")
        XCTAssertEqual(try decode(FoodCostAnalytics.self, #"{\#(Self.analyticsBase), "benchmark_label": "Under target"}"#).wasteState,
                       "measured")
        let noData = try decode(FoodCostAnalytics.self, #"{\#(Self.analyticsBase), "benchmark_label": "—", "benchmark_state": null}"#)
        XCTAssertEqual(noData.wasteState, "no_data")
        XCTAssertTrue(noData.recoverableBasisLine.contains("not money saved"))
    }

    // MARK: - Group I9: likely edits, standby, reliability

    func testLikelyEditBacktestAndStandbyAssumption() throws {
        let edit = try decode(LikelyEdit.self, """
            {"kind": "predicted", "employee": "Ana", "likelihood": 0.6, "backtest_hit_rate": 0.625,
             "backtest_hits": 5, "backtest_flagged": 8, "backtest_weeks": 6, "base_rate": 0.12,
             "calibration_note": "flags like this were right 5 of 8 times on your last 6 drafts"}
            """)
        XCTAssertEqual(edit.backtestLine,
                       "Flags like this were right 5 of 8 times (63%) on your last 6 drafts · 12% of all rows get edited")
        XCTAssertEqual(edit.calibrationText, "flags like this were right 5 of 8 times on your last 6 drafts")
        let bare = try decode(LikelyEdit.self, #"{"kind": "predicted", "backtest_hits": "x", "base_rate": null}"#)
        XCTAssertNil(bare.backtestLine)

        let days = try decode([StandbyDay].self, """
            [{"date": "2026-10-03", "chance_of_a_no_show": 0.31, "base_rate": 0.042,
              "assumption": "Assumes no-shows are independent of each other."},
             {"date": "2026-10-04", "chance_of_a_no_show": 0.2}]
            """)
        XCTAssertEqual(StandbyDay.basisLine(days),
                       "Assumes no-shows are independent of each other. Overall no-show rate here: 4%.")
        XCTAssertNil(StandbyDay.basisLine(try decode([StandbyDay].self, #"[{"date": "2026-10-03"}]"#)))
    }

    func testRosterReliabilityCarriesTheEnginesLine() throws {
        let r = try decode(RosterReliability.self, """
            {"no_show_rate": 0.24, "raw_no_show_rate": 0.33, "no_shows": 2, "shifts": 6, "base_rate": 0.05,
             "no_show_threshold": 0.2, "unreliable": true, "short_rate": 0.0}
            """)
        XCTAssertTrue(r.isUnreliable)
        XCTAssertEqual(r.noShowLabel, "24% no-show (2 of 6 shifts) — past the 20% line the scheduler uses")
        let old = try decode(RosterReliability.self, #"{"no_show_rate": 0.04, "shifts": 42}"#)
        XCTAssertFalse(old.isUnreliable)
        XCTAssertEqual(old.noShowLabel, "4% no-show")
        let byLine = try decode(RosterReliability.self, #"{"no_show_rate": 0.21, "no_show_threshold": 0.2, "unreliable": null}"#)
        XCTAssertTrue(byLine.isUnreliable)
    }

    // MARK: - F8: dayparts nobody watched

    func testDaypartIssuesNullIsNotNoIssues() throws {
        let unwatched = try decode(IntelOutcome.self, """
            {"weeks": 4, "issues": null, "watched": 0,
             "issues_label": "not watched — coverage wasn't checked on these nights"}
            """)
        XCTAssertNil(unwatched.issues)
        XCTAssertEqual(unwatched.issuesText, "not watched — coverage wasn't checked on these nights")
        let clean = try decode(IntelOutcome.self, #"{"weeks": 4, "issues": 0, "issues_label": "no issues on 3 watched nights"}"#)
        XCTAssertEqual(clean.issuesText, "no issues on 3 watched nights")
        XCTAssertNil(try decode(IntelOutcome.self, #"{"weeks": 4, "issues": 0}"#).issuesText)
        XCTAssertNil(try decode(IntelOutcome.self, #"{"weeks": 4, "issues": null}"#).issuesText)
        XCTAssertEqual(try decode(IntelOutcome.self, #"{"weeks": 4, "issues": 2}"#).issuesText, "2 issues")
    }

    // MARK: - I11 / H13: the nightly report

    func testDSRActionUrgencyMoveAndCalibratedDollars() throws {
        let a = try decode(DSRAction.self, """
            {"text": "Cut a closer Tuesday", "urgency": "this_week", "dollars_monthly": 1500,
             "urgency_basis": "nothing it cites moved 10% (2 points) from what it is compared with",
             "urgency_adjusted": {"from": "before_service", "to": "this_week",
                                  "why": "nothing it cites moved 10% (2 points) from what it is compared with"},
             "dollars_adjusted": 1240, "calibration_n": 6, "calibration_note": "adjusted from 6 measured results"}
            """)
        XCTAssertEqual(a.urgencyAdjustedLine,
                       "Moved from Before service to This week — nothing it cites moved 10% (2 points) from what it is compared with")
        XCTAssertEqual(a.dollarsLine, "$1,240/mo · adjusted from 6 measured results")
        let plain = try decode(DSRAction.self, #"{"text": "x", "dollars_monthly": 1500, "dollars_adjusted": null, "urgency_adjusted": null}"#)
        XCTAssertNil(plain.urgencyAdjustedLine)
        XCTAssertEqual(plain.dollarsLine, "$1,500/mo")
        XCTAssertNil(try decode(DSRAction.self, #"{"text": "x"}"#).dollarsLine)
    }

    func testDSRVerificationSplitsMeasuredFromEstimated() throws {
        let v = try decode(DSRVerification.self, #"{"checked": 8, "kept": 7, "dropped": [{}], "estimated": 2, "measured": 5}"#)
        XCTAssertEqual(v.measured, 5)
        XCTAssertEqual(v.footer,
                       "Every figure above traced to a fact it cites · 7 of 8 lines kept · 5 measured, 2 estimated (labelled as such) · 1 dropped because it didn’t pass the check against the night’s facts")
        // No estimates: the measured wording stands.
        let clean = try decode(DSRVerification.self, #"{"checked": 4, "kept": 4, "estimated": 0, "measured": 4}"#)
        XCTAssertEqual(clean.footer, "Every figure above traced to a measured fact · 4 of 4 lines kept")
        XCTAssertNil(try decode(DSRVerification.self, #"{"checked": 4, "kept": 4, "measured": null}"#).measured)
    }

    // MARK: - F: outcomes, value, goals

    func testOutcomeRowsSayWhatTheyCanBeReadAs() throws {
        let o = try decode(RecOutcome.self, """
            {"id": 9, "status": "evaluated", "verdict": "improved", "baseline_kind": "before the trigger",
             "baseline_overlaps_trigger": 1, "false_alarm_rate": 0.1, "counts": false,
             "grade_phrase": "past normal variation once — not yet a clear result"}
            """)
        XCTAssertTrue(o.overlapsTrigger)
        XCTAssertEqual(o.standing, .neutral)
        XCTAssertEqual(o.measurementNotes, [
            "Not counted — its baseline overlaps the weeks that prompted the recommendation, so part of any move is the number settling back",
            "Past normal variation once — not yet a clear result",
            "Compared with the same number of weeks before what prompted it; a change this size shows up by chance about 10% of the time here",
        ])
        let asBool = try decode(RecOutcome.self, #"{"id": 1, "status": "evaluated", "baseline_overlaps_trigger": false, "grade_phrase": null}"#)
        XCTAssertFalse(asBool.overlapsTrigger)
        XCTAssertEqual(asBool.measurementNotes, [])
        let tracking = try decode(RecOutcome.self, #"{"id": 2, "status": "tracking", "baseline_overlaps_trigger": 1}"#)
        XCTAssertEqual(tracking.measurementNotes, [])
    }

    func testValueSectionsAndTheGradeSplitNeverSum() throws {
        let v = try decode(HomeFollowThroughViewModel.ValueSummary.self, """
            {"ok": true,
             "delivered": {"monthly": 1420, "wins": 3, "scope": "monthly_rate",
                           "consistent_monthly": 900, "associated_monthly": 520},
             "sections": [{"key": "measured", "heading": "What was measured", "figures": ["delivered"]},
                          {"key": "surfaced", "heading": "What Cavnar surfaced / still available",
                           "figures": ["avoided", "surfaced", "opportunity"]}]}
            """)
        XCTAssertEqual(v.delivered?.scope, "monthly_rate")
        XCTAssertEqual(v.heading("measured", fallback: "x"), "What was measured")
        XCTAssertEqual(v.heading("surfaced", fallback: "x"), "What Cavnar surfaced / still available")
        XCTAssertEqual(v.heading("other", fallback: "fallback"), "fallback")
        let d = try XCTUnwrap(v.delivered)
        XCTAssertEqual(RecValueFormat.gradeSplitLine(d),
                       "Of that, $900/month were clear moves or held at their re-check; $520/month came alongside other changes or crossed normal variation only once.")
        let old = try decode(HomeFollowThroughViewModel.ValueSummary.self, #"{"ok": true, "delivered": {"monthly": 100, "wins": 1}}"#)
        XCTAssertNil(old.sections)
        XCTAssertNil(RecValueFormat.gradeSplitLine(try XCTUnwrap(old.delivered)))
        XCTAssertEqual(old.heading("measured", fallback: "What was measured"), "What was measured")
    }

    func testHomeValueScopeAndGoalAtTarget() throws {
        let block = try decode(HomeValueBlock.self, #"{"monthly": 900, "scope": "monthly_rate", "cumulative_scope": "measured_days_sum"}"#)
        XCTAssertEqual(block.scope, "monthly_rate")
        XCTAssertEqual(block.cumulativeScope, "measured_days_sum")
        XCTAssertEqual(HomeValueBlock.periodCaption(scope: block.scope), "PER MONTH")
        XCTAssertEqual(HomeValueBlock.periodCaption(scope: nil), "PER MONTH")
        XCTAssertEqual(HomeValueBlock.periodCaption(scope: "all_time_sum"), "ALL TIME")

        let goal = try decode(GoalRow.self, #"{"id": 3, "label": "Labor %", "state": "at_target", "summary": null}"#)
        XCTAssertEqual(goal.tone, .cavnarInk2)
        XCTAssertEqual(goal.stateLabel, "at the target, within its normal movement — not yet a clear hit")
        XCTAssertEqual(try decode(GoalRow.self, #"{"id": 4, "state": "met"}"#).tone, .cavnarGreen)
    }

    // MARK: - F6: dollar calibration on Home and the one thing

    func testRecDollarCalibration() throws {
        let rec = try decode(HomeRecommendation.self, """
            {"key": "k", "title": "Trim Tuesday lunch", "dollars_monthly": 1500,
             "dollars_adjusted": 1240, "calibration_n": 6, "calibration_note": null}
            """)
        XCTAssertEqual(rec.statedDollars, 1240)
        XCTAssertEqual(rec.dollarsNote, "adjusted from 6 measured results")
        XCTAssertEqual(HomeRecommendations.chips(rec).first, "$1,240/mo at stake (adjusted from 6 measured results)")
        let raw = try decode(HomeRecommendation.self, #"{"key": "k", "title": "t", "dollars_monthly": 1500, "dollars_adjusted": null, "calibration_n": 2}"#)
        XCTAssertEqual(raw.statedDollars, 1500)
        XCTAssertNil(raw.dollarsNote)
        XCTAssertEqual(HomeRecommendations.chips(raw).first, "$1,500/mo at stake")
        let odd = try decode(HomeRecommendation.self, #"{"key": "k", "title": "t", "dollars_adjusted": "lots"}"#)
        XCTAssertNil(odd.statedDollars)

        let ff = try decode(HomeFollowThroughViewModel.CrossModule.FixFirst.self, """
            {"what": "Reprice the burger", "dollars_monthly": 800, "dollars_adjusted": 610,
             "calibration_n": 5, "calibration_note": "adjusted from 5 measured results"}
            """)
        XCTAssertEqual(ff.statedDollars, 610)
        XCTAssertEqual(ff.dollarsNote, "adjusted from 5 measured results")
        XCTAssertEqual(RecDollarCalibration.note(adjusted: 1, n: 1, note: nil), "adjusted from 1 measured result")
    }

    // MARK: - H: causes, forecasts, recipe drafts

    func testMarketingInsightCausesAndComputedForecast() throws {
        let i = try decode(AIInsight.self, """
            {"insight_intro": "Reach fell.", "insight_recommendations": [], "insight_forecast": "FORECAST: steady",
             "causes_verified": false, "unsupported_causes": ["Reach fell because of the algorithm."],
             "forecast": {"kind": "marketing_reach_week", "predicted": 1240.4, "computed": true}}
            """)
        XCTAssertEqual(i.causesVerified, false)
        XCTAssertEqual(i.unsupportedCauses?.count, 1)
        XCTAssertEqual(i.forecast, "FORECAST: steady")
        XCTAssertEqual(i.computedForecast?.line, "Next week’s reach per post, carried forward from last week: 1,240")
        let odd = try decode(AIInsight.self, #"{"insight_intro": "x", "insight_recommendations": [], "forecast": "soon"}"#)
        XCTAssertNil(odd.computedForecast?.line)
        XCTAssertNil(odd.causesVerified)
        XCTAssertEqual(ComputedForecast(kind: "review_rating_week", predicted: 4.27).line,
                       "Next week’s rating, computed from the trend: 4.3★")
        XCTAssertTrue(CavnarCaveat.unverifiedCauses(["It dipped because of rain."]).detail.contains("It dipped because of rain."))
    }

    func testRecipeDraftFlagsAndTheAcceptResult() throws {
        let d = try decode(RecipeDraft.self, """
            {"id": 4, "menu_item_name": "Lasagna", "note": "From a photographed recipe card",
             "is_estimate": false, "needs_yield": true,
             "unit_warnings": [{"name": "Olive oil", "note": "tbsp doesn't convert to L"}],
             "confidence_levels": ["high", "low"],
             "lines": [{"ingredient_id": 1, "name": "Pasta", "qty": 2, "unit": "lb", "confidence": "high",
                        "source": "transcribed", "per": "batch", "unit_ok": true, "card_qty": 32, "card_unit": "oz"},
                       {"ingredient_id": 2, "name": "Olive oil", "qty": 3, "unit": "tbsp", "confidence": "low",
                        "source": "transcribed", "per": "batch", "unit_ok": false, "unit_note": "tbsp doesn't convert to L"}]}
            """)
        XCTAssertTrue(d.requiresYield)
        XCTAssertEqual(d.unitWarningLines, ["Olive oil: tbsp doesn't convert to L"])
        XCTAssertEqual(d.lines.first?.cardLine, "card: 32 oz")
        XCTAssertTrue(d.lines.last?.unitUnconverted == true)
        XCTAssertEqual(d.confidenceLevels, ["high", "low"])
        // An older draft: no flags; plate lines need no yield.
        let old = try decode(RecipeDraft.self, #"{"id": 5, "lines": [{"name": "Salt", "qty": 1}], "note": null}"#)
        XCTAssertFalse(old.requiresYield)
        XCTAssertEqual(old.unitWarningLines, [])
        XCTAssertNil(old.lines.first?.cardLine)

        XCTAssertEqual(RecipeDraftsSheet.parsedYield("12"), 12)
        XCTAssertEqual(RecipeDraftsSheet.parsedYield("2,5"), 2.5)
        XCTAssertNil(RecipeDraftsSheet.parsedYield("0"))
        XCTAssertNil(RecipeDraftsSheet.parsedYield(""))

        let r = try decode(RecipeAcceptResult.self, #"{"ok": true, "written": 5, "skipped": 1, "unit_skipped": ["Olive oil"]}"#)
        XCTAssertEqual(RecipeAcceptResult.message(dish: "Lasagna", result: r),
                       "Recipe written for Lasagna — 5 lines. Left out because their units didn’t convert: Olive oil — add them by hand.")
        let plain = try decode(RecipeAcceptResult.self, #"{"ok": true, "written": 1, "unit_skipped": null}"#)
        XCTAssertEqual(RecipeAcceptResult.message(dish: "Soup", result: plain), "Recipe written for Soup — 1 line.")
    }

    // MARK: - F2: marketing verdicts

    func testMarketingGroupVerdictsAndChangeNote() throws {
        let a = try decode(MarketingAttribution.self, """
            {"ok": true, "posts": [{"topic": "Margherita", "window_sales": 1, "baseline_sales": 1, "lift_pct": 4,
               "baseline_days": 8, "menu_item_name": "Margherita", "item_lift_pct": 6, "item_verdict": "no_clear_change",
               "item_noise_band_pct": 9}],
             "verdicts": {"lifted": 2, "dropped": 1, "no_clear_change": 4}, "median_verdict": "no_clear_change",
             "by_kind": [{"group": "dish", "posts": 3, "median_lift_pct": 5, "verdict": "no_clear_change",
                          "verdicts": {"lifted": 1, "dropped": 0, "no_clear_change": 2}}]}
            """)
        XCTAssertEqual(a.verdictLine, "2 of 7 posts lifted past their weekday’s normal movement · 1 dropped — no clear pattern overall")
        XCTAssertEqual(a.byKind?.first?.liftVerdict, .noClearChange)
        XCTAssertEqual(a.posts.first?.detailLine, "Margherita units: no clear change (±9%)")
        let old = try decode(MarketingAttribution.self, """
            {"ok": true, "posts": [], "by_kind": [{"group": "dish", "posts": 3, "median_lift_pct": -2}]}
            """)
        XCTAssertNil(old.verdictLine)
        XCTAssertEqual(old.byKind?.first?.liftVerdict, .dropped)

        let w = try decode(MarketingWindow.self, """
            {"days": 30, "posts": 1, "reach": 200, "engagement": 10, "engagement_rate": 5.0,
             "previous": {"posts": 2, "reach": 300, "engagement": 20, "engagement_rate": 6.6},
             "change": {"posts": null, "reach": null, "engagement": null},
             "change_note": "Too few posts to compare periods — 3 each are needed", "by_platform": []}
            """)
        XCTAssertNil(w.change.reach)
        XCTAssertEqual(w.changeNote, "Too few posts to compare periods — 3 each are needed")
    }

    // MARK: - G: connections

    func testConnectionsSyncStateRpowerAndGoogleSource() throws {
        let c = try decode(AccountConnections.self, """
            {"google_business": {"connected": false, "source": "places_sampled",
                                 "label": "Google reviews (sampled — Places returns 5 at a time)"},
             "instagram": {"connected": false},
             "toast": {"connected": true, "last_synced": "2026-09-20 03:10:00", "error": null,
                       "sync_state": "stale", "age_days": 4.2},
             "square": {"connected": false, "last_synced": null, "error": null, "sync_state": "not_connected", "age_days": null},
             "clover": {"connected": false, "error": "401 Unauthorized", "sync_state": "error", "age_days": null},
             "rpower": {"connected": true, "last_synced": "2026-09-23 06:00:00", "sync_state": "current", "age_days": 0.4},
             "pos": {"provider": "rpower", "connected": true, "state": "current", "age_days": 0.4}}
            """)
        XCTAssertEqual(c.googleBusiness.label, "Google reviews (sampled — Places returns 5 at a time)")
        XCTAssertEqual(c.toast.syncLine?.text, "No sync since 4 days ago — figures from this POS are out of date")
        XCTAssertEqual(c.toast.syncLine?.tone, .warn)
        XCTAssertNil(c.square.syncLine)
        XCTAssertEqual(c.clover.syncLine?.text, "Sync failing: 401 Unauthorized")
        XCTAssertEqual(c.clover.syncLine?.tone, .bad)
        XCTAssertEqual(c.rpower?.syncLine?.text, "Syncing · last data today")
        XCTAssertEqual(c.pos?.provider, "rpower")
        XCTAssertTrue(c.toast.lastSyncedText?.hasPrefix("Last synced 9/") == true)

        // An older server: five rows, none of the new keys.
        let old = try decode(AccountConnections.self, """
            {"google_business": {"connected": true, "last_synced": null}, "instagram": {"connected": false},
             "toast": {"connected": true, "last_synced": "2026-01-01T00:00:00"},
             "square": {"connected": false}, "clover": {"connected": false}}
            """)
        XCTAssertNil(old.rpower)
        XCTAssertNil(old.pos)
        XCTAssertNil(old.toast.syncLine)
        XCTAssertNil(old.googleBusiness.label)
    }

    // MARK: - E13: the reviews "Do today" line's own confidence

    func testReviewDoTodayCarriesItsOwnConfidence() throws {
        let rec = try decode(ReviewInsightRec.self, """
            {"key": "insight_review:ab", "text": "Reply to the three 2-star reviews", "kind": "do_today",
             "confidence_detail": {"pct": 58, "band": "medium", "label": "58% confidence", "version": 1}}
            """)
        XCTAssertEqual(rec.confidenceDetail?.pct, 58)
        XCTAssertEqual(ConfidenceDisplay(try XCTUnwrap(rec.confidenceDetail)).lineLabel, "58% confidence")
        XCTAssertNil(try decode(ReviewInsightRec.self, #"{"key": "k", "text": "t"}"#).confidenceDetail)
        XCTAssertNil(try decode(ReviewInsightRec.self, #"{"key": "k", "text": "t", "confidence_detail": null}"#).confidenceDetail)
    }

    // MARK: - K8 on the morning brief's forecast line

    func testServerToneNeutralDecodes() throws {
        XCTAssertEqual(try decode(ServerTone.self, #""neutral""#).value, "neutral")
        XCTAssertEqual(ServerTone("bad").color, .cavnarRed)
        XCTAssertNil(ServerTone(nil).color)
    }
}
