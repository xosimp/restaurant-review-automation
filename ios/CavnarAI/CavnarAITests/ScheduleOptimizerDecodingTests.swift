import XCTest
@testable import CavnarAI

/// The Shift Quality optimizer, the quality gate, rating ↔ roster matching,
/// the experienced flag and what `labor/intel` learns. Every new key is
/// optional so a payload cached before them still decodes.
final class ScheduleOptimizerDecodingTests: XCTestCase {
    private static let generatedJSON = """
    {"ok": true, "status": "done", "history_id": 12,
     "optimizer": {"ran": true, "applied": true, "before_score": 71, "after_score": 84, "improvement": 13,
                   "changes": [{"kind": "add", "reason": "Added Cara to Saturday dinner: coverage was one bartender short.", "gain": 6.5},
                               {"kind": "swap", "reason": "Swapped Ana and Bob on Friday: puts a closer on the night.", "gain": 3}],
                   "unresolved": [{"date": "2026-10-03", "day": "Saturday", "daypart": "night", "dimension": "leadership",
                                   "text": "Saturday dinner has no bartender authorised to close.", "fixable_by_draft": false}],
                   "evaluations": 212, "seconds": 3.4, "stopped": "no improving move",
                   "verdict": "Cavnar made 2 changes to the draft, raising Shift Quality from 71 to 84."},
     "gate": {"ran": true, "kept": "regenerated", "focus": ["coverage"], "reason": "The weakest days were regenerated with what was wrong with them."},
     "overtime_forecast": [{"employee": "Ana", "text": "Ana reaches 44h"}],
     "standby_days": [{"date": "2026-10-03", "day": "Saturday", "chance_of_a_no_show": 0.31, "people": 9}],
     "preview_rows": [{"date": "2026-10-03", "day": "Saturday", "employee": "Cara", "role": "Bartender",
                       "shift_start": "4:00pm", "shift_end": "11:00pm", "scheduled_hours": "7",
                       "notes": "Cavnar: added for coverage"}],
     "quality": {"checked": true, "score": 84, "band": "good",
                 "confidence": {"score": 41, "level": "low", "summary": "Low confidence",
                                "reasons": ["9 of 12 scheduled staff have no Operational Score."]},
                 "optimizer": {"ran": true, "before_score": 70, "after_score": 80, "changes": []},
                 "shifts": [{"date": "2026-10-03", "day": "Saturday", "daypart": "night", "scored": true,
                             "score": 62, "capped_by": "coverage",
                             "profile": {"key": "sat_dinner", "label": "Saturday dinner", "demand": "peak"},
                             "weaknesses": ["Bartender is 1 short of 2.", "Two trainees together."],
                             "dimensions": [{"key": "coverage", "label": "Coverage", "score": 62,
                                             "weaknesses": ["Bartender is 1 short of 2."]}]}]}}
    """

    func testDecodesTheOptimizerAndTheGateOnAGeneratedWeek() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.generatedJSON.utf8))
        let o = try XCTUnwrap(s.optimizer)
        XCTAssertTrue(o.ran)
        XCTAssertEqual(o.beforeScore, 71)
        XCTAssertEqual(o.afterScore, 84)
        XCTAssertEqual(o.changes?.count, 2)
        XCTAssertEqual(o.changes?.first?.kind, "add")
        XCTAssertEqual(o.changes?.first?.gain, 6.5)
        XCTAssertEqual(o.unresolved?.first?.dimension, "leadership")
        XCTAssertEqual(o.headline, "Cavnar improved this draft from 71 to 84 — 2 changes")
        XCTAssertTrue(o.hasContent)
        XCTAssertEqual(s.gate?.ran, true)
        XCTAssertEqual(s.gate?.kept, "regenerated")
        XCTAssertEqual(s.previewRows?.first?.notes, "Cavnar: added for coverage")
        // The live optimizer wins over the one stored with the quality.
        XCTAssertEqual(s.optimizerSummary?.beforeScore, 71)
        XCTAssertEqual(s.quality?.optimizer?.beforeScore, 70)
    }

    func testLowConfidenceIsProvisionalAndAsksForRatings() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.generatedJSON.utf8))
        let q = try XCTUnwrap(s.quality)
        XCTAssertTrue(q.isProvisional)
        XCTAssertTrue(q.needsRatings)
        XCTAssertEqual(q.shifts?.first?.cappedBy, "coverage")
        XCTAssertEqual(q.shifts?.first?.dimensions?.first?.weaknesses, ["Bartender is 1 short of 2."])
    }

    func testAnOptimizerThatChangedNothingHasNoHeadline() throws {
        let json = #"{"ran": true, "applied": false, "before_score": 90, "after_score": 90, "changes": [], "unresolved": [], "verdict": "The draft already met the quality target."}"#
        let o = try JSONDecoder().decode(ScheduleOptimizer.self, from: Data(json.utf8))
        XCTAssertNil(o.headline)
        XCTAssertFalse(o.hasContent)
        XCTAssertEqual(o.verdict, "The draft already met the quality target.")
    }

    func testAPayloadFromBeforeTheOptimizerStillDecodes() throws {
        let json = #"{"ok": true, "status": "done", "preview_rows": [], "quality": {"checked": true, "score": 80}}"#
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(json.utf8))
        XCTAssertNil(s.optimizer)
        XCTAssertNil(s.gate)
        XCTAssertNil(s.optimizerSummary)
        XCTAssertFalse(s.quality?.isProvisional ?? true)
    }

    func testTheScoreDeltaIsTheDifferenceOrNothing() {
        XCTAssertEqual(LaborViewModel.delta(from: 71, to: 74), 3)
        XCTAssertEqual(LaborViewModel.delta(from: 80, to: 78), -2)
        XCTAssertNil(LaborViewModel.delta(from: nil, to: 78))
        XCTAssertEqual(ScoreDeltaChip.signed(3), "+3")
        XCTAssertEqual(ScoreDeltaChip.signed(-2), "−2")
        XCTAssertEqual(ScoreDeltaChip.signed(0), "±0")
    }

    func testDecodesUnmatchedRatings() throws {
        let json = #"[{"rated": "Kim Tran", "score": 4, "suggestion": "Kim T.", "candidates": ["Kim T."]}, {"rated": "Jo", "score": 3.0, "suggestion": null, "candidates": []}]"#
        let list = try JSONDecoder().decode([UnmatchedRating].self, from: Data(json.utf8))
        XCTAssertEqual(list.count, 2)
        XCTAssertEqual(list[0].suggestion, "Kim T.")
        XCTAssertEqual(list[0].score, 4)
        XCTAssertNil(list[1].suggestion)
        XCTAssertEqual(list[1].id, "Jo")
    }

    func testTheExperiencedFlagRoundTrips() throws {
        let settings = try JSONDecoder().decode(RosterSettings.self, from: Data(#"{"experienced": true, "active": true}"#.utf8))
        XCTAssertEqual(settings.experienced, true)
        let patch = ScheduleSetupViewModel.StaffSettingsPatch(employeeName: "Ana", experienced: true)
        let body = try JSONSerialization.jsonObject(with: JSONEncoder().encode(patch)) as? [String: Any]
        XCTAssertEqual(body?["experienced"] as? Bool, true)
        XCTAssertEqual(body?["employee_name"] as? String, "Ana")
        XCTAssertNil(body?["active"], "an unset field is absent, not null")
    }

    func testDecodesWhatIntelLearns() throws {
        let json = """
        {"ok": true,
         "draft_acceptance": {"available": true,
                              "weeks": [{"history_id": 3, "week_start": "2026-09-21", "changes": 1, "unchanged_share": 0.97},
                                        {"history_id": 2, "week_start": "2026-09-14", "changes": 6, "unchanged_share": 0.81}],
                              "trend": {"direction": "rising", "older": 0.81, "newer": 0.97, "delta": 0.16},
                              "mean_unchanged_share": 0.89, "mean_changes": 3.5},
         "weight_calibration": {"ready": true, "weeks": 6, "shifts": 60, "applied": false,
                                "dimensions": {"coverage": {"default": 20, "suggested": 22.0, "nudge_pct": 10,
                                                            "evidence": 0.3, "reading": "tracked better outcomes"}},
                                "suggested_weights": {"coverage": 22.0},
                                "note": "Suggestions only — the engine keeps its current weights until someone changes them."},
         "attendance_by_weekday": {},
         "auto_publish_offer": {"eligible": true, "score": 88, "reason": "Your last 3 drafts went out with 2 or fewer changes each."}}
        """
        let intel = try JSONDecoder().decode(ScheduleIntel.self, from: Data(json.utf8))
        let acc = try XCTUnwrap(intel.draftAcceptance)
        XCTAssertEqual(acc.chartWeeks.map(\.weekStart), ["2026-09-14", "2026-09-21"], "oldest first")
        XCTAssertEqual(acc.trendLine, "You're keeping more of each draft: 81% then, 97% lately.")
        XCTAssertEqual(acc.meanChanges, 3.5)
        XCTAssertEqual(intel.weightCalibration?.ready, true)
        XCTAssertEqual(intel.weightCalibration?.dimensions?["coverage"]?.suggested, 22)
        XCTAssertEqual(intel.weightCalibration?.dimensions?["coverage"]?.nudgePct, 10)
        XCTAssertEqual(intel.autoPublishOffer?.eligible, true)
        XCTAssertEqual(intel.autoPublishOffer?.score, 88)
        XCTAssertFalse(intel.isEmpty, "an offer or an acceptance record is something to show")
    }

    func testCalibrationNotReadyCarriesItsReason() throws {
        let json = #"{"ok": true, "weight_calibration": {"ready": false, "weeks": 1, "shifts": 4, "reason": "1 published week and 4 shift outcomes with a stored score — calibration needs at least 4 weeks and 40 shifts."}, "auto_publish_offer": {"eligible": false, "reason": "Needs 3 published weeks of drafts to judge."}}"#
        let intel = try JSONDecoder().decode(ScheduleIntel.self, from: Data(json.utf8))
        XCTAssertEqual(intel.weightCalibration?.ready, false)
        XCTAssertTrue(intel.weightCalibration?.reason?.hasPrefix("1 published week") ?? false)
        XCTAssertEqual(intel.autoPublishOffer?.eligible, false)
    }

    /// A row whose scheduled_hours arrived as a number (one server path wrote
    /// a float) used to fail the whole response.
    func testScheduleRowReadsHoursAsTextOrNumber() throws {
        let json = #"[{"date": "2026-09-12", "employee": "Ana", "role": "Server", "scheduled_hours": 7.5},"# +
                   #" {"date": "2026-09-12", "employee": "Bob", "role": "Server", "scheduled_hours": "6.0"},"# +
                   #" {"date": "2026-09-12", "employee": "Cy", "role": "Server", "scheduled_hours": 6}]"#
        let rows = try JSONDecoder().decode([ScheduleRow].self, from: Data(json.utf8))
        XCTAssertEqual(rows.map(\.scheduledHours), ["7.5", "6.0", "6.0"])
    }
}
