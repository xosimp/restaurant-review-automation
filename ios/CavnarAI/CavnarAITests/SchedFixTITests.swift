import XCTest
@testable import CavnarAI

/// The iPhone catching up with the 10/11/26 blind re-audit's fix round
/// (workstream TI): the Summary tiles read the pre-publish review (TRUST-22),
/// progress ticks only reported stages (TRUST-21), the labor tile says why
/// it is missing (TRUST-29), the quality gate's skipped verdict (J), a drop
/// that needs a cover (L), how firm a staff shift is (TRUST-32), the owner's
/// words confirmed before they are held (A1) and the manager rules (R).
@MainActor
final class SchedFixTITests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: TRUST-22 / TRUST-29 — the Summary reads the review

    func testTheSummaryTilesReadTheReviewsGradesAndCounts() throws {
        let draft = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "quality": {"checked": true, "score": 64, "band": "fair"},
         "review": {"hard": 9, "soft": 14, "lines": []},
         "labor_view": {"basis": "all_in", "pct": 31.0}}
        """.utf8))
        let review = try decode(PublishReview.self, """
        {"ok": true, "verdict": {"tone": "amber", "sentence": "Ready after 2 decisions"},
         "quality": {"score": 64, "grade": "typical", "lead": "typical for you"},
         "numbers": {"labor_pct": {"value": 31.0, "label": "All-in labor", "basis": "all_in", "grade": "no_target"},
                     "overtime": {"hours": 0, "premium": 0, "priced": true, "text": "No overtime"}},
         "counts": {"must_fix": 0, "decide_holding": 2, "to_fix_or_decide": 2, "suggestions": 1}}
        """)
        let tiles = Dictionary(uniqueKeysWithValues: ScheduleSummaryTiles.tiles(draft, warnings: .prepublish(review))
            .map { ($0.key, $0) })
        // Typical for this restaurant: no colour, never amber for a 64.
        XCTAssertEqual(tiles["quality"]?.tone, .neutral)
        XCTAssertTrue(tiles["quality"]?.sub.hasPrefix("typical for you") == true)
        // The track record below 3 weeks a side is a dash (TRUST-31).
        XCTAssertTrue(tiles["quality"]?.sub.contains("Your high- against low-scoring weeks: \u{2014}") == true)
        // No target: neutral, never green.
        XCTAssertEqual(tiles["labor"]?.tone, .neutral)
        XCTAssertEqual(tiles["labor"]?.label, "All-in labor")
        XCTAssertEqual(tiles["overtime"]?.sub, "No overtime")
        // Decisions, not 23 raw breaches.
        XCTAssertEqual(tiles["warnings"]?.label, "To fix or decide")
        XCTAssertEqual(tiles["warnings"]?.value, "2")
        XCTAssertEqual(tiles["warnings"]?.tone, .warn)
        XCTAssertTrue(tiles["warnings"]?.sub.hasPrefix("Decisions only") == true)
        // Before the review comes back: a dash, never the raw count.
        let waiting = ScheduleSummaryTiles.tiles(draft, warnings: .prepublish(nil)).first { $0.key == "warnings" }
        XCTAssertNil(waiting?.value)
    }

    func testAMissingLaborPercentSaysWhyNotGenerateAgain() throws {
        let draft = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(#"{"ok": true, "history_id": 5}"#.utf8))
        let review = try decode(PublishReview.self, """
        {"ok": true, "verdict": {"tone": "green", "sentence": "Ready"},
         "numbers": {"labor_missing": {"reason": "no_sales", "text": "No sales are connected yet, so there's no forecast."}}}
        """)
        let labor = ScheduleSummaryTiles.tiles(draft, warnings: .prepublish(review)).first { $0.key == "labor" }
        XCTAssertNil(labor?.value)
        XCTAssertEqual(labor?.sub, "No sales are connected yet, so there's no forecast.")
        let none = ScheduleSummaryTiles.tiles(draft).first { $0.key == "labor" }
        XCTAssertFalse(none?.sub.lowercased().contains("generate") == true)
    }

    func testAnOverYourLimitBreachIsADecisionNotAHardRule() throws {
        let v = try decode([RuleViolation].self, """
        [{"kind": "long_run", "hard": true, "policy": true, "label": "Too many days in a row"},
         {"kind": "minor_school_hours", "hard": true, "label": "A minor in school hours"}]
        """)
        XCTAssertFalse(v[0].isHard)
        XCTAssertTrue(v[0].isOverLimit)
        XCTAssertTrue(v[1].isHard)
        let draft = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "review": {"hard": 0, "soft": 0, "over_limit": 2, "lines": []}}
        """.utf8))
        let w = ScheduleSummaryTiles.tiles(draft).first { $0.key == "warnings" }
        XCTAssertEqual(w?.tone, .warn)
        XCTAssertTrue(w?.sub.contains("over your limit") == true)
    }

    // MARK: TRUST-21 — only reported stages

    func testProgressIsTheJobsReportedStages() throws {
        let p = try decode(GenerationProgress.self, """
        {"days_total": 7, "days_drafted": 3, "stage": "rules",
         "stages": [{"key": "inputs", "label": "Reading", "done": true, "current": false},
                    {"key": "draft", "label": "Drafting the week", "done": true, "current": false},
                    {"key": "rules", "label": "Checking every rule", "done": false, "current": true},
                    {"key": "save", "label": "Saving", "done": false, "current": false}]}
        """)
        XCTAssertEqual(p.currentIndex, 2)
        XCTAssertEqual(ScheduleProgressSteps.window(count: p.stages.count, around: 2), [0, 1, 2, 3])
        let old = try decode(GenerationProgress.self, #"{"days_total": 7, "days_drafted": 0}"#)
        XCTAssertNil(old.currentIndex, "nothing reported: the pulse, never a tick")
    }

    // MARK: J — the quality gate's verdict when it was owed and skipped

    func testASkippedGateIsSaid() throws {
        let g = try decode(ScheduleGate.self, #"{"ran": false, "why": "time", "reason": "Ran out of time."}"#)
        XCTAssertEqual(g.line, "Ran out of time.")
        let quiet = try decode(ScheduleGate.self, #"{"ran": false, "reason": "x"}"#)
        XCTAssertNil(quiet.line)
    }

    // MARK: TRUST-32 — how firm a staff shift is, on its one line

    func testATentativeDayIsSaidOnItsLine() throws {
        let day = try decode(StaffWeekDay.self, """
        {"date": "2026-10-16", "weekday": "Friday", "is_today": false, "off": false,
         "shift": {"start": "4pm", "end": "10pm", "role": "Server", "commitment": "tentative",
                   "commitment_text": "Tentative — may change with the game"}}
        """)
        XCTAssertTrue(StaffTodayPlan.dayLine(day).hasSuffix("Tentative"))
        XCTAssertTrue(StaffTodayPlan.accessibilityLabel(day).contains("Tentative"))
        let firm = try decode(StaffWeekDay.self, """
        {"date": "2026-10-16", "weekday": "Friday", "is_today": false, "off": false,
         "shift": {"start": "4pm", "end": "10pm", "role": "Server", "commitment": "firm"}}
        """)
        XCTAssertFalse(StaffTodayPlan.dayLine(firm).contains("Firm"))
    }

    // MARK: A1 — the owner's words, confirmed before they are held

    func testInstructionReadingsAreEachRuleAndEachWaiver() throws {
        let r = try decode(LaborViewModel.InstructionReadResponse.self, """
        {"ok": true, "sentences": [
          {"text": "Maria closes Friday", "state": "rules",
           "rules": [{"rule": {"kind": "close", "person": "Maria Lopez"}, "reads_as": "I read: Maria Lopez closes, Fri",
                      "confirmed": false}]},
          {"text": "no bar tables Tuesday", "state": "rules", "rules": [],
           "waiver": {"words": "the bar-tables bartender off Tue", "confirmed": false}}]}
        """)
        let items = LaborViewModel.readings(r.sentences)
        XCTAssertEqual(items.map(\.readsAs), ["I read: Maria Lopez closes, Fri", "I read: the bar-tables bartender off Tue"])
        XCTAssertEqual(items[1].rule["kind"], .string("waiver"))
        XCTAssertEqual(items[1].rule["quote"], .string("no bar tables Tuesday"))
    }

    func testANoteRowSaysItIsNotEnforcedAndCarriesItsReadings() throws {
        let s = try decode(NoteSentence.self, """
        {"text": "Always 2 bartenders Friday night", "kind": "unchecked", "enforced": false,
         "typed": [{"rule": {"kind": "floor"}, "words": "2 Bartenders Fri dinner", "reads_as": "I read: at least 2 Bartenders, Fri dinner"}]}
        """)
        XCTAssertTrue(s.notEnforced)
        XCTAssertNil(s.enforced)
        XCTAssertEqual(s.typed.first?.line, "I read: at least 2 Bartenders, Fri dinner")
        XCTAssertEqual(ScheduleNotesSheet.rowWords(s).1, "Not enforced yet.")
        let set = try decode(NoteSentence.self, #"{"text": "x", "kind": "unchecked", "enforced": false, "dismissed": true}"#)
        XCTAssertEqual(ScheduleNotesSheet.rowWords(set).1, "Not enforced.")
    }

    func testAsksCardPostsOnlyTheReadingsKeptTicked() throws {
        var p = try decode(AskProposal.self, """
        {"action": "generate_schedule", "summary": "Draft next week", "route": {"mobile": "/mobile/api/labor/generate-schedule", "method": "POST"},
         "body": {"week_start": "2026-10-14", "confirm_rules": [{"kind": "close"}, {"kind": "start"}]},
         "fields_shown": [{"key": "week_start", "label": "Week", "value": "10/14/26"},
                          {"key": "confirm_rules", "label": "Held as rules for this week", "value": "I read: a; I read: b"}],
         "rules_read": [{"reads_as": "I read: a", "quote": "a"}, {"reads_as": "I read: b", "quote": "b"}]}
        """)
        XCTAssertEqual(p.rulesRead?.count, 2)
        p.declinedRules = [0]
        guard case .array(let kept)? = p.postedBody["confirm_rules"] else { return XCTFail("no rules posted") }
        XCTAssertEqual(kept.count, 1)
        p.declinedRules = [0, 1]
        XCTAssertNil(p.postedBody["confirm_rules"], "every reading unticked: none held")
    }

    // MARK: R — the manager rules, stated

    func testTheManagerRulesAreStatedFromTheRulesPayload() {
        let s = ManagerRulesSummary(rules: ["closing_manager_stays_minutes": .number(0)],
                                    defaults: ["min_shift_hours": .number(3)],
                                    structure: .list([.object(["name": .string("Mia"), "text": .string("Mia is the only manager open every day.")])]),
                                    learned: .object(["closing_stay": .object(["minutes": .number(45), "dates": .number(12)])]),
                                    second: .object(["from": .number(9), "basis": .string("history")]))
        XCTAssertEqual(s.shortestLine, "No shift shorter than 3h (the default)")
        XCTAssertEqual(s.closingLine, "The closing manager leaves with the last person")
        XCTAssertTrue(s.openingLine.contains("60 min"), "EJ's 60 when unset")
        XCTAssertEqual(s.secondManagerLine, "A second manager once 9 or more are on \u{2014} your own weeks run two managers from there")
        XCTAssertEqual(s.structure, ["Mia is the only manager open every day."])
        XCTAssertTrue(s.learnedLine?.contains("45 min") == true)
    }

    // MARK: K — measurement honesty on the phone

    func testTheScoreSaysWhatWasMeasuredAndThePlanIsScheduledAgainstScheduled() throws {
        let q = try decode(QualityMeasured.self, """
        {"share": 0.26, "withdrawn_labels": ["Coverage curve", "Operational scores"], "band_capped_from": "excellent"}
        """)
        XCTAssertEqual(q.line, "Not measured this week: Coverage curve, Operational scores.")
        let r = try decode(PublishReview.self, """
        {"ok": true, "verdict": {"tone": "green", "sentence": "Ready"},
         "quality": {"score": 81, "measured_text": "Rated on 26% measured data", "outcome_text": "Your 78+ weeks ran with 0.4 issues a week"},
         "numbers": {"savings": null, "vs_last_plan": {"last_pct": 29.0, "value": null, "withheld": "Not shown: 2 shifts are short"},
                     "forecast_sales": {"value": 52000, "reconcile": "Sized on $52,000; Home says $50,100"}}}
        """)
        XCTAssertNil(r.numbers.savings)
        XCTAssertEqual(r.numbers.vsLastPlan?.withheld, "Not shown: 2 shifts are short")
        XCTAssertEqual(r.numbers.forecastSales?.reconcile, "Sized on $52,000; Home says $50,100")
        XCTAssertEqual(r.quality.measuredText, "Rated on 26% measured data")
        XCTAssertEqual(r.quality.outcomeLine, "Your 78+ weeks ran with 0.4 issues a week")
    }
}
