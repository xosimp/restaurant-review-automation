import XCTest
@testable import CavnarAI

/// The iPhone catching up with the 10/10/26 scheduler fix round (I2b): the
/// pre-publish review read from its one payload (TRUST-27, RM-6), each
/// must-fix sent past on its own with a reason (and a manager gap with who
/// runs the floor), the Crew stage (TRUST-12), shift locks, the re-score's
/// quality delta (SCALE-29), the closing-stay one tap (TRUST-21), the staff
/// app's short-turnaround consent (MARKET-3) and the rules' new keys.
@MainActor
final class SchedFixI2bTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    private func encode<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: The pre-publish review

    private let reviewJSON = """
    {"ok": true, "schedule_id": 41, "week_start": "2026-10-14", "week_end": "2026-10-20", "version": 7,
     "verdict": {"tone": "red", "sentence": "Not ready: Tue 10/13/26 has no manager from 9:15 to 10:30pm.",
                 "subline": "Week of 10/14/26 – 10/20/26 · 84 shifts"},
     "must_fix": [
       {"key": "rule:no_manager:2026-10-13:21:15", "kind": "no_manager", "tier": "must_fix", "rank": 1,
        "text": "Tue 10/13/26, 9:15–10:30pm: no manager on the floor.", "day": "Tuesday", "date": "2026-10-13",
        "fixes": [{"label": "Extend Gabriel to 10:30pm", "action": "edit_rows",
                   "payload": {"change": [{"match": {"employee": "Gabriel", "date": "2026-10-13", "shift_start": "3:00pm"},
                                           "set": {"shift_end": "10:30pm"}}]}},
                  {"label": "Someone else runs the floor", "action": "override", "payload": {"key": "rule:no_manager:2026-10-13:21:15"}}],
        "override_allowed": true,
        "override": {"needs": "covered_by", "candidates": ["Erik", "Ana"], "prompt": "Who runs the floor then?"},
        "overridden_before": {"weeks": 2, "of": 5, "last": "2026-10-01"}},
       {"key": "rule:minor_late:maya:2026-10-16:4:00pm", "tier": "must_fix", "text": "Maya (16) is on until 11pm Fri.",
        "fixes": [], "override_allowed": true, "override": {"needs": "reason", "candidates": [], "prompt": null}}
     ],
     "decide": [
       {"key": "hours_over", "kind": "budget", "holds": true, "text": "The week is 14h over your hourly budget.",
        "options": [{"label": "Keep it", "scope": "week", "action": "acknowledge", "payload": {"key": "hours_over"}}]},
       {"key": "crew:Server|Monday|night", "holds": false, "text": "Monday dinner has 3 servers; you usually run 4.",
        "options": [{"label": "3 is fine every Monday", "scope": "every", "action": "post",
                     "payload": {"path": "labor/crew-matrix", "body": {"role": "Server", "day": "Monday", "part": "night", "answer": "lean"}}}]}
     ],
     "fyi": [{"key": "meal", "kind": "meal_break", "text": "3 shifts can't fit the meal break.", "count": 3}],
     "numbers": {"labor_pct": {"value": 27.8, "low": 26.4, "high": 29.6, "basis": "all_in", "label": "All-in labor",
                               "target": 30, "grade": "under", "track_record": "Our sales forecasts here were within 8% on 22 of the last 26 weeks."},
                 "cost": {"hourly": 18420, "salaried": 3100, "total": 18420, "text": "$18,420 hourly"},
                 "overtime_hours": 0, "overtime": {"hours": 0, "premium": 0, "priced": true, "text": "Nobody past their ceiling."},
                 "forecast_sales": {"value": 66200, "basis": "same weeks last year"},
                 "savings": {"value": 1320, "basis": "against your last 14 days", "projected": true}},
     "quality": {"score": 82, "band": "good", "grade": "typical", "lead": "capped by Tue dinner coverage"},
     "changed": {"vs_last_week": ["+6 shifts, +31h"], "vs_usual": ["2 shifts leaner"], "vs_last_draft": [], "cavnar_changes": []},
     "fragile": [{"key": "standby:sat", "text": "Sat dinner: about a 22% chance someone doesn't show.",
                  "action": {"label": "Ask Lee", "action": "post", "payload": {"path": "labor/standby/ask", "body": {"employee": "Lee"}}}}],
     "why": {"assignments": [{"text": "Fri closer: Ana over Ben.", "employee": "Ana"}],
             "left_off": [{"name": "Ben", "reason": "at his 30h cap", "kind": "hours_cap"}]},
     "sure": {"kept_share": 0.91, "kept_trend": {"direction": "rising", "older": 0.88, "newer": 0.94},
              "lines": ["Your last 6 drafts went out with 91% of shifts unchanged."]},
     "send": {"reach": {"app": 15, "text": 4, "email": 2, "portal_only": 2, "total": 23, "reachable": 21},
              "label": "Send to 21 staff"}}
    """

    func testTheReviewDecodesWhole() throws {
        let r = try decode(PublishReview.self, reviewJSON)
        XCTAssertTrue(r.isUsable)
        XCTAssertEqual(r.verdict?.tone, "red")
        XCTAssertEqual(r.version, 7)
        XCTAssertEqual(r.mustFix.count, 2)
        let gap = r.mustFix[0]
        XCTAssertTrue(gap.hard, "a must-fix is red, never amber (TRUST-27)")
        XCTAssertTrue(gap.needsCoveredBy)
        XCTAssertEqual(gap.candidates, ["Erik", "Ana"])
        XCTAssertEqual(gap.reasonPrompt, "Who runs the floor then?")
        XCTAssertEqual(gap.overriddenBefore, "You sent past this in 2 of 5 weeks.")
        XCTAssertEqual(gap.fixes.first?.action, "edit_rows")
        XCTAssertFalse(r.mustFix[1].needsCoveredBy)
        XCTAssertEqual(r.holding.map(\.key), ["hours_over"])
        XCTAssertEqual(r.decide[1].options.first?.buttonLabel, "3 is fine every Monday")
        XCTAssertEqual(r.numbers.laborPct?.grade, "under")
        XCTAssertEqual(r.numbers.laborPct?.label, "All-in labor")
        XCTAssertEqual(r.numbers.cost?.value, 18420)
        XCTAssertEqual(r.numbers.cost?.salaried, 3100)
        XCTAssertEqual(r.numbers.overtimeText, "Nobody past their ceiling.")
        XCTAssertEqual(r.quality.score, 82)
        XCTAssertEqual(r.fyi.first?.text, "3 shifts can't fit the meal break.")
        XCTAssertEqual(r.changed.groups.map(\.title), ["Against last week", "Against your usual crew"])
        XCTAssertEqual(r.fragile.first?.action?.label, "Ask Lee")
        XCTAssertEqual(r.why.leftOff.first?.reason, "at his 30h cap")
        XCTAssertEqual(r.sure.lines, ["Your last 6 drafts went out with 91% of shifts unchanged."])
        XCTAssertEqual(r.send.label, "Send to 21 staff")
        XCTAssertEqual(r.send.line, "15 in the app, 4 by text, 2 by email \u{00B7} 2 only in the staff portal")
    }

    func testOneOddPartIsThatPartMissingNeverTheReview() throws {
        let r = try decode(PublishReview.self, """
        {"ok": true, "verdict": {"tone": "green", "sentence": "Ready to send."},
         "must_fix": [{"key": "x"}, 5, {"key": "k", "text": "A thing"}], "numbers": "odd", "sure": [1],
         "decide": [{"text": "Pick", "options": [{"nothing": 1}]}]}
        """)
        XCTAssertTrue(r.isUsable)
        XCTAssertEqual(r.mustFix.map(\.key), ["k"])
        XCTAssertTrue(r.numbers.isEmpty)
        XCTAssertTrue(r.decide[0].options.isEmpty)
    }

    func testEachMustFixIsOverriddenOnItsOwnWithWhatItNeeds() throws {
        let r = try decode(PublishReview.self, reviewJSON)
        var chosen: [String: PublishOverrideChoice] = [:]
        XCTAssertEqual(PublishGate.open(r.mustFix, overrides: chosen).count, 2)
        // A manager gap needs a name; a reason alone is not enough.
        chosen[r.mustFix[0].key] = PublishOverrideChoice(reason: "I'll cover", coveredBy: nil)
        XCTAssertEqual(PublishGate.open(r.mustFix, overrides: chosen).count, 2)
        chosen[r.mustFix[0].key] = PublishOverrideChoice(reason: "", coveredBy: "Erik")
        XCTAssertEqual(PublishGate.open(r.mustFix, overrides: chosen).count, 1)
        // A hard item needs words.
        chosen[r.mustFix[1].key] = PublishOverrideChoice(reason: "  ")
        XCTAssertEqual(PublishGate.open(r.mustFix, overrides: chosen).count, 1)
        chosen[r.mustFix[1].key] = PublishOverrideChoice(reason: "Her mother signed the waiver")
        XCTAssertTrue(PublishGate.open(r.mustFix, overrides: chosen).isEmpty)
        let sent = PublishGate.overrides(r.mustFix, chosen)
        XCTAssertEqual(sent.map(\.key), r.mustFix.map(\.key))
        XCTAssertEqual(sent[0].coveredBy, "Erik")
        XCTAssertEqual(sent[0].reason, "Erik runs the floor", "the gate wants a reason on every must-fix")
        XCTAssertNil(sent[1].coveredBy)
        // An override for something since fixed is never sent.
        XCTAssertTrue(PublishGate.overrides([], chosen).isEmpty)
        XCTAssertEqual(PublishGate.sendLabel(open: 2, overriding: 0, reachable: 21, serverLabel: nil), "Fix 2 things first")
        XCTAssertEqual(PublishGate.sendLabel(open: 0, overriding: 0, reachable: 21, serverLabel: nil, undecided: 1),
                       "Decide 1 thing first")
        XCTAssertEqual(PublishGate.sendLabel(open: 0, overriding: 1, reachable: 21, serverLabel: "Send to 21 staff"),
                       "Send to 21 staff (overriding 1 rule)")
    }

    func testSendCarriesTheOverridesByKeyWithTheirReasons() throws {
        let body = PublishScheduleViewModel.PublishBody(
            scheduleId: 41, acknowledge: true, keys: ["rule:no_manager:x", "hours_over"], version: 7,
            overrides: [PublishOverride(key: "rule:no_manager:x", reason: "Erik runs the floor", coveredBy: "Erik")])
        let j = try encode(body)
        XCTAssertEqual(j["acknowledge"] as? [String], ["rule:no_manager:x", "hours_over"])
        let overrides = try XCTUnwrap(j["overrides"] as? [[String: Any]])
        XCTAssertEqual(overrides.first?["key"] as? String, "rule:no_manager:x")
        XCTAssertEqual(overrides.first?["reason"] as? String, "Erik runs the floor")
        XCTAssertEqual(overrides.first?["covered_by"] as? String, "Erik")
        // A one-tap send never overrides anything.
        var oneTap = body
        oneTap.oneTap = true
        XCTAssertNil(try encode(oneTap)["overrides"])
        // None chosen: no key at all.
        XCTAssertNil(try encode(PublishScheduleViewModel.PublishBody(scheduleId: 41, acknowledge: false))["overrides"])
    }

    func testTheGatesTierNotItsDemotableFlagMakesAMustFix() throws {
        // Re-audit 10/11/26 TRUST-24: a must-fix is the TIER; a decision
        // (the budget, the notice rule) is amber and answered in one tap,
        // whatever the old `hard` / new `demotable` flag says.
        let gate = try decode(PublishScheduleViewModel.GateResponse.self, """
        {"needs_ack": true, "blocker_items": [{"key": "rule:no_manager:a", "text": "Gap"},
                                               {"key": "hours_over", "text": "Over budget", "demotable": true}],
         "must_fix_keys": ["rule:no_manager:a"]}
        """)
        XCTAssertEqual(gate.items.map(\.key), ["rule:no_manager:a"])
        XCTAssertTrue(gate.items[0].hard)
        XCTAssertEqual(gate.decisions.map(\.key), ["hours_over"])
        XCTAssertTrue(gate.decisions[0].holds)
        XCTAssertEqual(gate.decisions[0].options.first?.action, "acknowledge")
        let check = try decode(PublishCheck.self, """
        {"ok": true, "blocker_items": [{"key": "notice_short", "text": "Short notice", "tier": "decide",
                                        "demotable": false, "overridden_before": {"weeks": 3, "of": 3}},
                                       {"key": "rule:minor_late:a:x", "text": "Minor late", "tier": "must_fix",
                                        "law": true, "fixable": true}]}
        """)
        XCTAssertFalse(check.blockerItems[0].isMustFix, "a notice decision is never a red must-fix")
        XCTAssertEqual(check.blockerItems[0].decision.overriddenBefore, "You sent past this in 3 of 3 weeks.")
        let law = check.blockerItems[1].mustFix
        XCTAssertTrue(law.hard && law.law)
        XCTAssertFalse(law.overrideAllowed, "the law an edit can fix is never sent past")
    }

    func testTheReviewCarriesTsFieldsAndTheLawsOverride() throws {
        let r = try decode(PublishReview.self, """
        {"ok": true, "verdict": {"tone": "green", "sentence": "Ready to send", "worth_a_look": [{"key": "overtime", "text": "3h of overtime"}]},
         "published_at": null, "can_publish": true,
         "must_fix": [{"key": "notice:law", "tier": "must_fix", "text": "Posted 5 days ahead; the law asks 14", "law": true,
                       "cls": "law", "override_allowed": true,
                       "override": {"needs": "legal_ack", "legal": {"text": "I understand the law asks 14 days."},
                                    "min_reason": {"chars": 8, "words": 2}}}],
         "must_fix_groups": [{"kind": "rule:long_run", "count": 3, "keys": ["a", "b", "c"], "text": "3 people — too many days in a row",
                              "fixes": [{"label": "Fix all 3 (shows each change)", "action": "apply_fixes", "payload": {}}]}],
         "decide": [{"key": "left_off:ana", "kind": "left_off", "holds": false, "text": "Ana has no shifts",
                     "options": [{"label": "Leave them off this week", "action": "dismiss", "scope": "week",
                                  "payload": {"key": "left_off:ana", "scope": "week", "until": "2026-10-20"}}]}],
         "counts": {"must_fix": 1, "decide": 1, "decide_holding": 0, "suggestions": 1, "to_fix_or_decide": 1},
         "always_overridden": [{"kind": "rule:long_run", "text": "You've sent past it in 9 of 9 weeks — change the rule?",
                                "actions": [{"label": "Allow 7 days in a row", "action": "post", "scope": "every",
                                             "payload": {"path": "labor/rules", "body": {"rules": {"max_consecutive_days": 7}}}}]}],
         "numbers": {"labor_pct": null, "overtime": {"hours": 0, "premium": 0, "priced": true, "text": "No overtime"},
                     "labor_missing": {"reason": "no_sales", "text": "No sales are connected yet.",
                                       "action": {"label": "Enter a weekly sales estimate", "action": "open",
                                                  "payload": {"stage": "setup", "panel": "targets"}}}},
         "why": {"assignments": [{"text": "Cara could take it and is rated higher — swap?", "basis": "engine",
                                  "basis_label": "Cavnar AI's reason", "favours_pick": false,
                                  "swap": {"label": "Swap to Cara", "action": "edit_rows", "payload": {}}}],
                 "engine": {"text": "Recorded with the draft."}, "draft": {"text": "Drafted by the solver."}},
         "sure": {"confidence": {"pct": null, "basis": ["1 of the 2 published weeks it needs have been measured"],
                                 "meaning": "How well supported"}, "lines": []},
         "hours": {"hourly": 312, "salaried": 110}}
        """)
        let m = r.mustFix[0]
        XCTAssertTrue(m.needsLegalAck && m.law)
        XCTAssertFalse(PublishGate.complete(m, PublishOverrideChoice(reason: "Store opening week rush")),
                       "the law needs its acknowledgement")
        XCTAssertFalse(PublishGate.complete(m, PublishOverrideChoice(reason: "x", legalAck: true)),
                       "a one-letter reason is no reason (TRUST-4)")
        let ok = PublishOverrideChoice(reason: "Store opening week rush", legalAck: true)
        XCTAssertTrue(PublishGate.complete(m, ok))
        let sent = try encode(PublishGate.overrides(r.mustFix, [m.key: ok])[0])
        XCTAssertEqual(sent["legal_ack"] as? Bool, true)
        XCTAssertEqual(r.mustFixGroups.first?.count, 3)
        XCTAssertEqual(r.suggestions.map(\.key), ["left_off:ana"])
        XCTAssertTrue(r.holding.isEmpty)
        XCTAssertEqual(r.counts.holding, 1)
        XCTAssertEqual(r.alwaysOverridden.first?.actions.first?.action, "post")
        XCTAssertEqual(r.numbers.laborMissing?.reason, "no_sales")
        XCTAssertEqual(ReviewOpenTarget.from(r.numbers.laborMissing?.action?.payload ?? [:]), .targets)
        XCTAssertEqual(r.numbers.overtimeHours, 0)
        XCTAssertEqual(r.numbers.overtimePriced, true)
        XCTAssertEqual(r.why.assignments.first?.basisLabel, "Cavnar AI's reason")
        XCTAssertEqual(r.why.assignments.first?.swap?.label, "Swap to Cara")
        XCTAssertEqual(r.why.draftText, "Drafted by the solver.")
        XCTAssertEqual(r.sure.confidence?.figure, "\u{2014}", "below the floor, a dash, never a guess")
        XCTAssertEqual(r.verdict?.worthALook.first?.text, "3h of overtime")
        XCTAssertEqual(r.hours?.hourly, 312)
    }

    func testOpenPayloadsGoWhereTheySay() {
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("schedule"), "redo": .list([.string("2026-10-14")])]),
                       .redo(["2026-10-14"]))
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("setup"), "panel": .string("managers")]), .managers)
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("team"), "employee": .string("Bob")]), .person("Bob"))
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("schedule"), "employee": .string("Ana")]), .addShift("Ana"))
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("setup"), "panel": .string("rules")]),
                       .web(path: "labor/rules"))
        XCTAssertEqual(ReviewOpenTarget.from(["stage": .string("schedule"), "date": .string("2026-10-14"),
                                              "shift_id": .string("abc")]),
                       .shift(date: "2026-10-14", shiftId: "abc", employee: nil, start: nil))
    }

    func testAManagerGapIsCoveredOnlyByAnOfferedName() throws {
        let r = try decode(PublishReview.self, reviewJSON)
        let gap = r.mustFix[0]
        XCTAssertFalse(PublishGate.complete(gap, PublishOverrideChoice(reason: "", coveredBy: "Mickey Mouse")),
                       "a name the gate did not offer is refused (TRUST-5)")
        XCTAssertTrue(PublishGate.complete(gap, PublishOverrideChoice(reason: "", coveredBy: "erik")))
    }

    func testThePublishResultSaysWhoIsStillBeingSent() throws {
        let r = try decode(PublishResult.self, """
        {"ok": true, "sent": [], "unreachable": [], "failed": [], "sending": ["Ana", "Ben"], "portal_only": false,
         "status": [{"employee_name": "Ana"}], "delivery": {"sending": 2, "failed": 0, "unreachable": 0},
         "staff_changes_undone": ["Ben's swap with Cara was undone"]}
        """)
        XCTAssertEqual(r.sending, ["Ana", "Ben"])
        XCTAssertEqual(r.stillSending(nil), 2)
        XCTAssertEqual(r.stillSending(PublishDeliveryCounts(sending: 0, failed: 1, unreachable: 0)), 0)
        XCTAssertEqual(r.staffChangesUndone.count, 1)
        XCTAssertEqual(PublishScheduleSheet.deliveryLine(r, now: nil), "2 still sending")
    }

    // MARK: Fixes that edit the week

    private func rows() throws -> [ScheduleRow] {
        try decode([ScheduleRow].self, """
        [{"date": "2026-10-13", "day": "Tuesday", "employee": "Gabriel", "role": "Manager", "shift_start": "3:00pm",
          "shift_end": "9:15pm", "scheduled_hours": "6.25", "shift_id": "aaaabbbbccccddddeeeeffff00001111"},
         {"date": "2026-10-13", "day": "Tuesday", "employee": "Ana", "role": "Server", "shift_start": "4pm",
          "shift_end": "10:00pm", "scheduled_hours": "6"}]
        """)
    }

    func testAnActionFindsItsRowByIdOrByPersonDateAndStart() throws {
        let rs = try rows()
        XCTAssertEqual(LaborViewModel.rowFor(["shift_id": .string("AAAABBBBCCCCDDDDEEEEFFFF00001111")], in: rs)?.employee,
                       "Gabriel")
        XCTAssertEqual(LaborViewModel.rowFor(["employee": .string("ana"), "date": .string("2026-10-13"),
                                              "shift_start": .string("4:00pm")], in: rs)?.employee, "Ana")
        XCTAssertNil(LaborViewModel.rowFor(["employee": .string("Ana"), "date": .string("2026-10-14")], in: rs))
        XCTAssertTrue(LaborViewModel.rowActions.contains("edit_rows"))
        XCTAssertTrue(LaborViewModel.rowActions.contains("extend_shift"))
    }

    func testAClosingStayIsOneTapThatEndsTheShiftAtTheStay() throws {
        let review = try decode(ScheduleReview.self, """
        {"hard": 0, "soft": 0, "assumed_stays": [
          {"date": "2026-10-09", "day": "Friday", "employee": "Max Doe", "index": 3, "shift_start": "4:00pm",
           "scheduled_end": "11:00pm", "until": "11:45pm", "minutes": 45, "hourly": true,
           "text": "Fri 10/9/26 close: Max Doe is assumed to stay until 11:45pm.",
           "action": {"label": "Schedule Max to 11:45pm", "action": "extend_shift",
                      "payload": {"employee": "Max Doe", "date": "2026-10-09", "shift_start": "4:00pm",
                                  "shift_end": "11:00pm", "new_end": "11:45pm"}}}]}
        """)
        let stay = try XCTUnwrap(review.assumedStays?.items.first)
        let action = try XCTUnwrap(stay.action)
        XCTAssertEqual(action.label, "Schedule Max to 11:45pm")
        XCTAssertEqual(action.action, "extend_shift")
        XCTAssertEqual(LaborViewModel.payloadText(action.payload, "new_end"), "11:45pm")
        XCTAssertEqual(LaborViewModel.payloadText(action.payload, "shift_start"), "4:00pm")
        // Kept in the on-device cache as it came.
        let again = try JSONDecoder().decode(ScheduleReview.self, from: JSONEncoder().encode(review))
        XCTAssertEqual(again.assumedStays?.items.first?.action?.label, "Schedule Max to 11:45pm")
    }

    func testTheFullTimeSetupLineCarriesItsOneTap() throws {
        let item = try decode(ReviewSetupItem.self, """
        {"kind": "full_time_unconfirmed", "text": "Ana is marked full-time…",
         "action": {"label": "Make 32h the full-time minimum", "endpoint": "/api/labor/rules",
                    "rules": {"full_time_min_confirmed": true}}}
        """)
        XCTAssertEqual(item.actionLabel, "Make 32h the full-time minimum")
    }

    // MARK: Locks

    func testALockIsSentAsTheRowsLockAndTheEchoIsNot() throws {
        var row = try rows()[0]
        XCTAssertNil(try encode(row)["lock"], "an untouched row says nothing about its lock")
        row.lock = true
        row.locked = true
        let j = try encode(row)
        XCTAssertEqual(j["lock"] as? Bool, true)
        XCTAssertEqual(j["shift_id"] as? String, "aaaabbbbccccddddeeeeffff00001111")
        // Read back from the server, `lock` is never set — only `locked`.
        let echoed = try decode(ScheduleRow.self, #"{"date": "2026-10-13", "employee": "A", "locked": "1", "lock": true}"#)
        XCTAssertEqual(echoed.locked, true)
        XCTAssertNil(echoed.lock)
    }

    // MARK: The quality delta (SCALE-29)

    private func shift(_ date: String, _ part: String, score: Int) -> String {
        """
        {"date": "\(date)", "day": "Mon", "daypart": "\(part)", "scored": true, "score": \(score),
         "profile": {"key": "k", "label": "L", "demand": "normal"}}
        """
    }

    func testADeltaIsMergedOntoTheHeldAnswerInTheServersOrder() throws {
        let held = try decode(ScheduleQuality.self, """
        {"checked": true, "score": 70, "shifts": [\(shift("2026-10-12", "morning", score: 60)),
                                                   \(shift("2026-10-12", "night", score: 70)),
                                                   \(shift("2026-10-12", "night", score: 75))]}
        """)
        XCTAssertEqual(LaborViewModel.qualityShiftKeys(held.shifts ?? []),
                       ["2026-10-12|morning", "2026-10-12|night", "2026-10-12|night#1"])
        let delta = try decode(LaborViewModel.QualityDelta.self, """
        {"base": "e1", "head": {"checked": true, "score": 74},
         "order": ["2026-10-12|night", "2026-10-12|morning", "2026-10-12|night#1"],
         "changed": {"2026-10-12|morning": \(shift("2026-10-12", "morning", score: 88))}}
        """)
        let merged = try XCTUnwrap(LaborViewModel.mergeQuality(held: held, delta: delta))
        XCTAssertEqual(merged.score, 74)
        XCTAssertEqual(merged.shifts?.map { $0.score ?? 0 }, [70, 88, 75])
        // A reading neither changed nor held: the whole answer is asked for.
        let missing = try decode(LaborViewModel.QualityDelta.self, """
        {"base": "e1", "head": {"checked": true}, "order": ["2026-10-13|night"], "changed": {}}
        """)
        XCTAssertNil(LaborViewModel.mergeQuality(held: held, delta: missing))
    }

    func testTheRescoreAsksForADeltaAfterItsSaveKey() throws {
        var body = LaborViewModel.ScoreBody(rows: try rows(), dailyTargetHours: [:], save: true, historyId: 9, version: 3)
        let key = LaborViewModel.saveKey(body)
        body.delta = true
        body.qualityEtag = "e1"
        let j = try encode(body)
        XCTAssertEqual(j["delta"] as? Bool, true)
        XCTAssertEqual(j["quality_etag"] as? String, "e1")
        XCTAssertNotNil(key)
    }

    // MARK: Crew (TRUST-12)

    func testCrewShowsTheDifferencesInTheRestaurantsOwnWeekOrder() throws {
        let m = try decode(CrewMatrix.self, """
        {"ok": true, "week_start": "2026-10-14", "history_id": 41, "has_usual": true, "differences": 3, "open": 1,
         "unit": "concurrent", "days": ["Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday"],
         "roles": [{"role": "Server", "usual_week": 30, "draft_week": 28, "cells": [
            {"day": "Monday", "part": "night", "usual": 4, "draft": 3, "status": "under", "can_add": true, "demand": "busy"},
            {"day": "Wednesday", "part": "morning", "usual": 2, "draft": 2, "status": "match"},
            {"day": "Wednesday", "part": "night", "usual": 3, "draft": 4, "status": "over"},
            {"day": "Friday", "part": "night", "usual": 4, "draft": 3, "status": "under", "answer": "hold",
             "answer_state": "in_force", "kept": 4, "review": "1/9/27"}]},
          {"role": "Cook", "cells": [{"day": "Tuesday", "part": "morning", "usual": 2, "draft": 1, "floor": 2,
                                      "status": "below_rule"}]}]}
        """)
        XCTAssertEqual(m.items().map { "\($0.cell.dayShort) \($0.cell.partWord) \($0.role)" },
                       ["Wed dinner Server", "Fri dinner Server", "Mon dinner Server", "Tue lunch Cook"])
        XCTAssertEqual(m.items(all: true).count, 5)
        XCTAssertEqual(m.leaner.count, 3)
        XCTAssertEqual(m.headline, "3 shifts leaner than usual \u{00B7} 1 to answer")
        XCTAssertTrue(m.items()[2].cell.isOpen)
        XCTAssertFalse(m.items()[1].cell.isOpen, "a kept answer in force is answered")
        let body = try encode(CrewAnswerRequest(role: "Server", day: "Monday", part: "night", answer: "lean",
                                                historyId: 41, rows: try rows()))
        XCTAssertEqual(body["answer"] as? String, "lean")
        XCTAssertEqual(body["history_id"] as? Int, 41)
        XCTAssertEqual((body["rows"] as? [Any])?.count, 2)
    }

    // MARK: Rules

    func testTheRulesSendTheBoundaryAndTheFullTimeLine() throws {
        var p = ScheduleSetupViewModel.RulesPatch(rules: ["full_time_min_confirmed": .bool(true)])
        p.daypartBoundary = .some("3:30pm")
        var j = try encode(p)
        XCTAssertEqual(j["daypart_boundary"] as? String, "3:30pm")
        XCTAssertEqual((j["rules"] as? [String: Any])?["full_time_min_confirmed"] as? Bool, true)
        p.daypartBoundary = .some(nil)
        j = try encode(p)
        XCTAssertTrue(j.keys.contains("daypart_boundary"), "back to 3:00pm is a null, not an absence")
        XCTAssertFalse(try encode(ScheduleSetupViewModel.RulesPatch()).keys.contains("daypart_boundary"))
        XCTAssertEqual(ScheduleRulesSheet.boundaryChoices.first?.1, "3:00pm (default)")
        XCTAssertTrue(ScheduleRulesSheet.boundaryChoices.contains { $0.0 == "12:00pm" })
        XCTAssertTrue(ScheduleRulesSheet.boundaryChoices.contains { $0.0 == "6:00pm" })
        XCTAssertFalse(ScheduleRulesSheet.boundaryChoices.contains { $0.0 == "3:00pm" })
    }

    // MARK: Stop

    func testAStoppedRunNamesTheDraftItPutBack() throws {
        let r = try decode(GeneratedSchedule.self, """
        {"ok": false, "stopped": true, "restored_history_id": 40, "error": "Stopped — your draft from before is back."}
        """)
        XCTAssertEqual(r.stopped, true)
        XCTAssertEqual(r.restoredHistoryId, 40)
        XCTAssertTrue(r.undecodedKeys.isEmpty)
    }

    // MARK: The staff app

    func testAShortTurnaroundAskIsReadAndTheTapIsSentAgainWithConsent() throws {
        let ask = try decode(StaffNeedsConsent.self, """
        {"ok": false, "needs_consent": {"kind": "rest_waiver", "date": "2026-10-17",
          "text": "I agree to work with 8 hours between shifts.", "premium": "Paid 1.25x."},
         "error": "this needs your written consent"}
        """)
        XCTAssertEqual(ask.text, "I agree to work with 8 hours between shifts.")
        XCTAssertEqual(try encode(StaffConsentedBody(accept: true)) as NSDictionary,
                       ["accept": true, "consent_rest": true] as NSDictionary)
        XCTAssertEqual(try encode(StaffConsentedBody()) as NSDictionary, ["consent_rest": true] as NSDictionary)
    }

    func testAgreementsAndOtherLocationsDecode() throws {
        let c = try decode(StaffCompliance.self, """
        {"ok": true, "consents": {"pending": [{"id": 3, "kind": "shift_change", "text": "I agree to work 5–11pm on 10/17/26.",
                                               "status": "pending", "subject_date": "2026-10-17"}],
                                  "recent": [], "standing_rest": null},
         "rest": {"hours": 10, "text": "You may agree in writing to a shorter turnaround."},
         "estimate": {"id": 9, "text": "About 24–32 hours a week.", "acknowledged_at": null},
         "requests": [{"id": 4, "body": "Fewer Sundays", "status": "open", "due_at": "2026-10-14 12:00:00", "overdue": false}]}
        """)
        XCTAssertEqual(c.pending.first?.id, 3)
        XCTAssertEqual(c.restText, "You may agree in writing to a shorter turnaround.")
        XCTAssertEqual(c.estimate?.id, 9)
        XCTAssertEqual(c.requests.first?.line, "Waiting for a written answer, due 10/14/26.")
        XCTAssertFalse(c.isEmpty)
        let t = try decode(StaffTravel.self, """
        {"ok": true, "travels": true, "group": true, "offers": [{"id": 2, "place": "Oak St", "date": "2026-10-17",
          "date_label": "10/17/26", "shift_start": "4:00pm", "shift_end": "10:00pm", "role": "Server"}]}
        """)
        XCTAssertEqual(t.offers.first?.line, "Oak St \u{00B7} 10/17/26 \u{00B7} 4:00pm\u{2013}10:00pm \u{00B7} Server")
    }

    // MARK: On the floor (J2)

    func testTheLiveReadAndItsMovesDecode() throws {
        let r = try decode(LiveLaborResponse.self, """
        {"ok": true, "can_act": true,
         "read": {"available": true, "as_of": "10/10/26 7:15pm", "sentence": "Labor is 31% of sales so far.",
                  "sales": {"so_far": 3200, "projected": 6100}, "labor": {"to_date": 990, "rest": 400, "basis": "punches"},
                  "pct": {"hourly_so_far": 31, "all_in_projected": 33.5, "target": 30, "over_by": 3.5}, "moves": []},
         "moves_today": [{"id": 5, "kind": "cut", "employee": "Ben", "text": "Let Ben go at 8:30pm.", "status": "pushed"},
                         {"id": 6, "kind": "extend", "employee": "Ana", "text": "Ana stays.", "status": "offered", "how": "auto"}]}
        """)
        XCTAssertEqual(r.read.allInProjected, 33.5)
        XCTAssertEqual(r.read.overBy, 3.5)
        XCTAssertEqual(r.movesToday.map(\.isOpen), [true, false])
        XCTAssertEqual(r.movesToday.first?.actLabel, "Let them go")
        let preview = try decode(PosPushPreview.self, """
        {"ok": true, "supported": true, "history_id": 41,
         "shifts": [{"employee": "Ana", "start": "2026-10-17T16:00:00", "role": "Server", "status": "would_send"}]}
        """)
        XCTAssertEqual(preview.shifts.first?.line, "10/17/26 4pm \u{00B7} Server \u{00B7} will go")
        let set = try encode(FloorStore.SettingsBody(posPush: true, previewed: true))
        XCTAssertEqual(set as NSDictionary, ["pos_push": true, "previewed": true] as NSDictionary)
    }

    // MARK: J3 — push to talk, the Bench, how firm a shift is

    func testTheReadBackSaysTheAnswerInPlainWords() {
        XCTAssertEqual(AskReadBack.plain("**Ana** can close — [see the week](https://x)", proposals: 1),
                       "Ana can close — see the week Tap the card to send it.")
        XCTAssertNil(AskReadBack.plain("  **  ", proposals: 0))
        let long = String(repeating: "word ", count: 200)
        XCTAssertLessThanOrEqual(AskReadBack.plain(long, proposals: 0)?.count ?? 0, 602)
    }

    func testTheBenchPartsDecodeAndItsActionsCarryTheirKeys() throws {
        let s = try decode(BenchStrength.self, """
        {"ok": true, "available": true, "pools": [{"family": "Bar", "daypart": "night", "status": "critical", "why": "Only Ana closes"},
                                                   {"family": "Kitchen", "daypart": "morning", "status": "ok"}],
         "cross_train": [{"text": "Train Ben on bar next"}], "hiring": [{"text": "Hire a cook by 11/14/26"}], "basis": "Last 8 weeks"}
        """)
        XCTAssertEqual(s.pools.map(\.family), ["Bar"])
        let sb = try decode(BenchStandby.self, """
        {"ok": true, "history_id": 41, "policy": {"pay_hours": 2, "flat_pay": null, "stated": false},
         "suggestions": [{"date": "2026-10-17", "day": "Saturday", "chance_of_a_no_show": 0.22,
                          "standby": {"employee": "Lee", "role": "Server", "shift_start": "4:00pm", "shift_end": "10:00pm"},
                          "price": {"standby_pay": 36, "expected_short_cost": 410}, "slots": []}],
         "slots": [{"id": 3, "status": "accepted", "employee_name": "Lee", "date": "2026-10-10", "shift_start": "4:00pm",
                    "shift_end": "10:00pm", "called_in": false}]}
        """)
        XCTAssertEqual(sb.suggestions.first?.standby?.employee, "Lee")
        XCTAssertEqual(sb.suggestions.first?.taken, false)
        XCTAssertEqual(sb.policyLine, "Standby pay: 2 hours at their rate (the default)")
        let r = try decode(BenchRetention.self, """
        {"ok": true, "people": [{"name": "Ana", "level": "high", "risk": 0.4, "reasons": ["hours cut 3 weeks"], "protect": false},
                                {"name": "Bo", "level": "none"}]}
        """)
        XCTAssertEqual(r.people.map(\.name), ["Ana"])
        let body = try encode(BenchActionBody(action: "standby_offer", date: "2026-10-17", role: "Server", employee: "Lee",
                                              shiftStart: "4:00pm", historyId: 41, chance: 0.22))
        XCTAssertEqual(body["action"] as? String, "standby_offer")
        XCTAssertEqual(body["shift_start"] as? String, "4:00pm")
        XCTAssertEqual(body["history_id"] as? Int, 41)
        XCTAssertNil(body["protect"])
    }

    func testAStaffShiftSaysHowFirmItIs() throws {
        let shift = try decode(StaffShift.self, """
        {"date": "Sat 10/17", "start": "4:00pm", "end": "10:00pm", "commitment": "firm",
         "commitment_text": "Firm \u{2014} needed even on a slow night. It changes only in an emergency."}
        """)
        let pill = try XCTUnwrap(shift.commitmentPill)
        XCTAssertEqual(pill.word, "Firm")
        XCTAssertEqual(pill.meaning, "needed even on a slow night. It changes only in an emergency.")
        XCTAssertNil(try decode(StaffShift.self, #"{"commitment": "maybe"}"#).commitmentPill)
        // Kept in the device cache as it came.
        let again = try JSONDecoder().decode(StaffShift.self, from: JSONEncoder().encode(shift))
        XCTAssertEqual(again.commitment, "firm")
    }
}
