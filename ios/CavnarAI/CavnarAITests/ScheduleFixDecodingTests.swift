import XCTest
@testable import CavnarAI

/// The schedule fix round (10/3/26) added the manager plan, the review's
/// structured parts, origin flags, the one-tap why and the publish check's
/// notes. Every key is lenient: an odd value is empty, never a week that
/// fails to decode (B2 found `chunked: 1` failing every finished generation).
final class ScheduleFixDecodingTests: XCTestCase {
    private static let json = """
    {"ok": true, "status": "done", "history_id": 7, "chunked": 1, "calls": 2, "partial": true,
     "week_dates": ["2026-10-05", "2026-10-11"],
     "hours_scheduled": 410, "hours_hourly": 300, "hours_salaried": 110, "hours_budget": 320,
     "unwritten_dates": [{"date": "2026-10-10", "day": "Saturday", "why": "the model ran out of time"}],
     "unstaffable_dates": [{"date": "2026-10-11", "day": "Sunday", "reasons": {"approved time off": 3}}],
     "starting_point": {"no_history": true, "floors": true, "borrowed": false},
     "manager_plan": {"planned": true, "failed": false,
                      "shifts": [{"date": "2026-10-05", "employee": "Erik", "shift_start": "10:00am",
                                  "shift_end": "6:00pm", "hours": 8, "reason": "Erik usually works Mondays"}],
                      "windows": {"2026-10-05": {"from": "11:00am", "to": "11:00pm", "source": "hours"}},
                      "uncovered": [{"date": "2026-10-07", "day": "Wednesday", "from": "11:00am", "to": "3:00pm",
                                     "minutes": 240, "why": "Erik: on approved time off"}],
                      "skipped": [{"employee": "Jim", "date": "2026-10-06", "why": "their note: no Tuesdays"}],
                      "unknown_pattern": ["Anthony", "Andrew"], "question": "Which days and hours do Anthony and Andrew work?"},
     "manager_coverage": {"extended": 1, "added": 0,
                          "left": [{"date": "2026-10-07", "from": "11:00am", "to": "3:00pm",
                                    "reasons": [{"employee": "Max", "why": "on approved time off"}],
                                    "could_act": ["Kay"]}],
                          "shortfall": {"text": "4h of the week has no manager on (10/7/26)", "dates": ["2026-10-07"]}},
     "min_hours": {"moved": 0, "added": 1, "left": [{"employee": "Cook", "short_by": 1, "min": 40, "reason": "no legal shift"}]},
     "requirements": [{"date": "2026-10-09", "day": "Friday", "daypart": "late", "window": [1320, 1560],
                       "roles": [{"role": "Bartender", "required": 2, "floor": 0, "typical": 1}],
                       "reasons": ["+30% — Homecoming"]}],
     "review": {"hard": 1, "soft": 0, "lines": ["A starting point: EJ's has no shift history"],
                "hard_rows": [], "hard_days": [{"date": "2026-10-07", "day": "Wednesday", "kind": "no_manager",
                                                "detail": "No manager on 11:00am–3:00pm"}],
                "fixes": [{"index": null, "row_id": "r4", "from": "Ana", "to": "Bo", "reason": "rest",
                           "row": {"date": "2026-10-06", "employee": "Ana", "shift_start": "4:00pm"}}],
                "unfixed": [{"index": null, "employee": "Cy", "reason": "nobody legal"}],
                "stage_failures": [{"stage": "manager", "blocks_publish": true, "line": "⚠ The manager check didn't run"}],
                "unmet": [{"kind": "floor", "date": "2026-10-09", "day": "Friday", "daypart": "night",
                           "what": "2 bartenders", "why": "your floor"}, {"kind": "budget", "what": "Over budget"}],
                "setup": [{"kind": "leader_rules_inactive", "text": "Nobody is rated", "can_adopt": true}],
                "unmatched_names": [{"source": "time_off", "name": "Gabe", "detail": "Gabe: time off — matches nobody",
                                     "suggestion": "Gabriel Huerta"}],
                "budget_conflict": {"held": {"requirement": 6, "floor": 2}, "examples": []},
                "cap_floor_conflicts": [{"date": "2026-10-10", "day": "Saturday", "at": "7:00pm", "on": 8, "cap": 6}]},
     "preview_rows": [{"date": "2026-10-05", "day": "Monday", "employee": "Erik", "role": "Manager",
                       "shift_start": "10:00am", "shift_end": "6:00pm", "scheduled_hours": 8,
                       "_pinned": "manager_plan", "_pin_reason": "Erik usually works Mondays", "_rid": "r1",
                       "dst_hours": 1.0, "origin": "cavnar:apply_fixes", "origin_sig": "abc"}]}
    """

    func testTheFixRoundPayloadDecodes() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.json.utf8))
        XCTAssertEqual(s.chunked, true)
        XCTAssertEqual(s.calls, 2)
        XCTAssertEqual(s.partial, true)
        XCTAssertEqual(s.unwritten.first?.date, "2026-10-10")
        XCTAssertEqual(s.unstaffable.first?.reasons, ["approved time off (3)"])
        XCTAssertEqual(s.startingPoint?.noHistory, true)
        XCTAssertEqual(s.plan?.windowLine(for: "2026-10-05"), "Manager on 11:00am\u{2013}11:00pm")
        XCTAssertEqual(s.plan?.unknownPattern, ["Anthony", "Andrew"])
        XCTAssertEqual(s.coverage?.left.first?.couldAct, ["Kay"])
        XCTAssertEqual(s.minHoursLeft.first?.line, "Cook \u{2014} 1h under the 40h minimum you set: no legal shift")
        XCTAssertEqual(s.requirements?.items.first?.partLabel, "Late night 10:00pm\u{2013}2:00am")
        XCTAssertEqual(s.hoursSplit?.line(), "300h hourly of 320h budget \u{00B7} 110h salaried")
        let row = try XCTUnwrap(s.previewRows?.first)
        XCTAssertTrue(row.isManagerPlan)
        XCTAssertEqual(row.rowId, "r1")
        XCTAssertEqual(row.dstHours, 1.0)
        XCTAssertEqual(row.originSig, "abc")
        let review = try XCTUnwrap(s.review)
        XCTAssertNil(review.fixes?.first?.index)
        XCTAssertEqual(review.fixes?.first?.rowId, "r4")
        XCTAssertNil(review.unfixed?.first?.index)
        XCTAssertEqual(review.hardDays?.items.first?.kind, "no_manager")
        XCTAssertEqual(review.stageFailures?.items.first?.blocksPublish, true)
        XCTAssertEqual(review.stageFailures?.items.first?.text, "The manager check didn't run")
        XCTAssertEqual(review.setup?.items.first?.canAdopt, true)
        XCTAssertEqual(review.unmatchedNames?.items.first?.suggestion, "Gabriel Huerta")
        XCTAssertEqual(review.budgetConflict?.heldLine,
                       "6 at the shift's requirement \u{00B7} 2 at your floor")
        let groups = ScheduleReviewExtras.unmetGroups(review.unmet?.items ?? [])
        XCTAssertEqual(groups.map(\.title), ["Fri 10/9/26", "This week"])
        XCTAssertEqual(review.unmet?.items.first?.style, .hard)
    }

    /// Origin flags go back to the server on Save (L-5).
    func testARowSendsItsOriginBack() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.json.utf8))
        let data = try JSONEncoder().encode(try XCTUnwrap(s.previewRows?.first))
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertTrue(text.contains("\"origin_sig\":\"abc\""))
        XCTAssertTrue(text.contains("\"_pinned\":\"manager_plan\""))
    }

    func testAnOddShapeNeverFailsTheWeek() throws {
        let odd = """
        {"ok": true, "manager_plan": "nope", "unwritten_dates": 3, "requirements": {"x": 1},
         "review": {"hard_days": "x", "unmet": [1, {"kind": "ask", "what": "+1 server"}], "budget_conflict": []}}
        """
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(odd.utf8))
        XCTAssertEqual(s.plan?.planned, false)
        XCTAssertTrue(s.unwritten.isEmpty)
        XCTAssertEqual(s.review?.unmet?.items.count, 1)
    }

    func testThePublishCheckReadsNotesHoursAndLikelyToChange() throws {
        let json = """
        {"ok": true, "schedule_id": 7, "notes": [{"key": "quality_weak", "text": "Shift Quality 58/100"}],
         "hours": {"hourly": 300, "salaried": 110, "total": 410},
         "likely_to_change": {"ready": true, "note": "flags like these were right 7 of 9 times",
                              "rows": [{"employee": "Ana", "likelihood": 0.7, "text": "Ana Tue 4:00pm"}]}}
        """
        let c = try JSONDecoder().decode(PublishCheck.self, from: Data(json.utf8))
        XCTAssertEqual(c.notes.first?.text, "Shift Quality 58/100")
        XCTAssertEqual(c.hours?.line(budget: 320), "300h hourly of 320h budget \u{00B7} 110h salaried")
        XCTAssertEqual(c.likelyToChange?.ready, true)
        XCTAssertEqual(c.likelyToChange?.rows.first?.likelihood, 0.7)
    }

    func testTheMemoryItemSaysItsEvidenceInOwnerWords() throws {
        let json = """
        {"items": [{"key": "pattern:pair|a|b", "class_label": "Teams", "text": "Ana and Bo work together",
                    "status": "active", "status_label": "Applied", "confidence_pct": "72%",
                    "hits": 2, "opportunities": 3, "bound_by": "the requirements table (labor.apply_learned_headcount)",
                    "can_keep": true, "can_let_go": true, "can_be_rule": true}],
         "consolidated_at": "10/3/26", "can_answer": true, "can_make_rules": true}
        """
        let v = try JSONDecoder().decode(ScheduleMemoryView.self, from: Data(json.utf8))
        let item = try XCTUnwrap(v.items.first)
        XCTAssertEqual(item.evidence, "2 of 3")
        XCTAssertEqual(item.boundWords, "Already in the requirements")
        XCTAssertEqual(item.confidencePct, "72%")
    }

    func testTrimSummaryCountsCutsAndRemovals() throws {
        let json = """
        [{"employee": "Ana", "kind": "cut", "to": "9:00pm", "hours": 1},
         {"employee": "Bo", "hours": 6}]
        """
        let shifts = try JSONDecoder().decode([TrimmedShift].self, from: Data(json.utf8))
        XCTAssertEqual(ScheduleWeekNotes.trimSummary(shifts, hours: 7), "Trimmed 7h \u{2014} 1 ended early, 1 removed")
    }
}
