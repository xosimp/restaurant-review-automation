import XCTest
@testable import CavnarAI

/// iOS parity, Labor & the AI schedule (10/7/26): the measured generation
/// copy (#15), a run in the way (409), the drafted push's lock-screen send
/// (#16), open shifts (#25), roster and shift edits (#45), the build notes and
/// their rules (#46), the scorecard (#47), the day pager and person week
/// (#48), the Labor focus cases (#49), proof photos (#50), note holds (#68),
/// covers (#69), History's richer rows and the PDF. Every request body here
/// carries each key the server reads for its route.
@MainActor
final class LaborParityRoundTests: XCTestCase {
    private func json(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: #15 — honest progress

    func testTheEtaLineIsMeasuredNeverAFixedMinute() {
        let typical = GenerationTypical(seconds: 600, n: 4, basis: "yours")
        let line = GenerationCopy.etaLine(elapsed: 192, typical: typical, until: nil)
        XCTAssertEqual(line, "3:12 so far. A full week usually takes about 10 min (your last 4 drafts). "
                             + "You can leave \u{2014} the draft lands in History.")
        let all = GenerationCopy.etaLine(elapsed: 5, typical: GenerationTypical(seconds: 330, n: 12, basis: "all"), until: nil)
        XCTAssertTrue(all.contains("about 6 min (the last 12 drafts on Cavnar AI)"))
        let none = GenerationCopy.etaLine(elapsed: 30, typical: nil, until: nil)
        XCTAssertTrue(none.contains("a draft takes several minutes"))
        XCTAssertFalse(none.contains("minute\u{2026}"))
    }

    func testPastHalfAgainTheUsualItSaysSoAndWhenItStops() {
        let typical = GenerationTypical(seconds: 400, n: 3, basis: "yours")
        let slow = GenerationCopy.etaLine(elapsed: 601, typical: typical, until: Date(), clock: { _ in "6:42pm" })
        XCTAssertTrue(slow.contains("Taking longer than usual \u{2014} it is still working, and stops by 6:42pm at the latest."))
        XCTAssertFalse(GenerationCopy.etaLine(elapsed: 599, typical: typical, until: nil).contains("longer than usual"))
        // Nothing measured: long only past ten minutes.
        XCTAssertTrue(GenerationCopy.etaLine(elapsed: 601, typical: nil, until: nil).contains("longer than usual"))
    }

    func testTheStepsStretchToTheMeasuredDraft() {
        XCTAssertEqual(GenerationCopy.stepScale(typical: nil), 3)
        XCTAssertEqual(GenerationCopy.stepScale(typical: GenerationTypical(seconds: 300, n: 2, basis: "all")), 4)
        XCTAssertEqual(GenerationCopy.stepScale(typical: GenerationTypical(seconds: 30, n: 2, basis: "all")), 1)
    }

    func testABusyPressNamesTheRunInTheWayAndItsJob() throws {
        let body = """
        {"ok": false, "busy": true, "running": {"week_start": "2026-10-12", "dates": null, "history_id": null,
         "instruction": null}, "job_id": "abc-1", "wait_seconds": 900,
         "typical": {"seconds": 420, "n": 3, "basis": "yours"},
         "error": "A schedule for the week of 10/12/26 is being built — try again when it finishes. Nothing was started."}
        """
        let busy = try JSONDecoder.cavnar.decode(GenerationBusy.self, from: Data(body.utf8))
        XCTAssertEqual(busy.busy, true)
        XCTAssertEqual(busy.jobId, "abc-1")
        XCTAssertEqual(busy.waitSeconds, 900)
        XCTAssertEqual(busy.typical?.seconds, 420)
        XCTAssertEqual(busy.headline, "The week of 10/12/26 is being built")
        let redo = try JSONDecoder.cavnar.decode(GenerationBusy.self, from: Data("""
        {"busy": true, "running": {"week_start": "2026-10-12", "dates": ["2026-10-16"]}}
        """.utf8))
        XCTAssertEqual(redo.headline, "Cavnar AI is redoing Fri 10/16/26 of the week of 10/12/26")
    }

    // MARK: #16 — Send to staff from the drafted push

    func testSendIsOfferedOnlyOnAPushTheServerMarkedSafe() {
        XCTAssertEqual(PushManager.scheduleSendAllowed(["schedule_id": 41, "one_tap_safe": true]), 41)
        XCTAssertEqual(PushManager.scheduleSendAllowed(["schedule_id": "41", "one_tap_safe": true]), 41)
        XCTAssertNil(PushManager.scheduleSendAllowed(["schedule_id": 41, "one_tap_safe": false]))
        XCTAssertNil(PushManager.scheduleSendAllowed(["schedule_id": 41]))
        XCTAssertNil(PushManager.scheduleSendAllowed(["one_tap_safe": true]))
    }

    func testThePublishCheckMustStillFindNothingToReadFirst() throws {
        func check(_ extra: String) throws -> PublishCheck {
            try JSONDecoder.cavnar.decode(PublishCheck.self, from: Data("""
            {"ok": true, "schedule_id": 41, "week_start": "2026-10-12", "published_at": null, "blockers": [],
             "reach": {"total": 12, "reachable": 11, "by_app": 9, "by_text": 2, "by_email": 0, "unreachable": ["Al"]},
             "can_publish": true \(extra)}
            """.utf8))
        }
        // The server's own verdict decides (re-audit 10/8/26 #1); the phone
        // never re-derives it from the blockers.
        XCTAssertTrue(PushManager.publishCheckAllowsOneTap(try check(", \"one_tap_safe\": true"), scheduleId: 41))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(try check(", \"one_tap_safe\": true"), scheduleId: 42))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(try check(""), scheduleId: 41))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(
            try check(", \"one_tap_safe\": false, \"blocker_items\": [{\"key\": \"hard\", \"text\": \"Ana is past 40 hours\"}]"),
            scheduleId: 41))
        let cannot = try JSONDecoder.cavnar.decode(PublishCheck.self, from: Data("""
        {"ok": true, "schedule_id": 41, "blockers": [], "reach": {"total": 12, "reachable": 11}, "can_publish": false,
         "one_tap_safe": true}
        """.utf8))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(cannot, scheduleId: 41))
    }

    func testTheSendBodyNamesTheWeekAndAcknowledgesNothing() throws {
        let body = try json(PublishScheduleViewModel.PublishBody(scheduleId: 41, acknowledge: false))
        XCTAssertEqual(body["schedule_id"] as? Int, 41)
        XCTAssertEqual(body["acknowledge"] as? Bool, false)
    }

    // MARK: #5 / #49 — where a link lands

    func testLaborFocusKnowsNotesClosersAndTasks() {
        XCTAssertEqual(LaborFocus(section: "notes"), .notes)
        XCTAssertEqual(LaborFocus(section: "staff-notes"), .notes)
        XCTAssertEqual(LaborFocus(section: "closers"), .closers)
        XCTAssertEqual(LaborFocus(section: "tasks"), .tasks)
        XCTAssertEqual(LaborFocus(section: "task-sheets"), .tasks)
        XCTAssertEqual(LaborFocus(section: "schedule"), .schedule)
    }

    func testTheDraftedPushOpensItsWeek() throws {
        let nav = try XCTUnwrap(NavPath("schedule/41"))
        let route = try XCTUnwrap(ModuleRoute.from(nav))
        XCTAssertEqual(route.key, "labor")
        XCTAssertEqual(route.section, "schedule")
        XCTAssertEqual(route.itemId, "41")
    }

    // MARK: #25 — open shifts

    func testPostingAShiftSendsEveryKeyTheRouteReadsAndDropsBlanks() throws {
        let full = try json(OpenShiftPostBody(date: "2026-10-16", shiftStart: "5:00pm", shiftEnd: "10:00pm",
                                              role: "Server", employee: "Ana", offerTo: "Bo", note: "Bears game"))
        XCTAssertEqual(full["date"] as? String, "2026-10-16")
        XCTAssertEqual(full["shift_start"] as? String, "5:00pm")
        XCTAssertEqual(full["shift_end"] as? String, "10:00pm")
        XCTAssertEqual(full["role"] as? String, "Server")
        XCTAssertEqual(full["employee"] as? String, "Ana")
        XCTAssertEqual(full["offer_to"] as? String, "Bo")
        XCTAssertEqual(full["note"] as? String, "Bears game")
        let extra = try json(OpenShiftPostBody(date: "2026-10-16", shiftStart: "5:00pm", shiftEnd: "10:00pm",
                                               role: "", employee: "", offerTo: " ", note: ""))
        XCTAssertEqual(Set(extra.keys), ["date", "shift_start", "shift_end"])
    }

    func testOffersDecodeWithTheirShift() throws {
        let offers = try JSONDecoder.cavnar.decode([ShiftOffer].self, from: Data("""
        [{"id": 3, "request_id": 9, "name": "Bo", "status": "offered", "date": "2026-10-16"}]
        """.utf8))
        XCTAssertEqual(offers.first?.requestId, 9)
        XCTAssertEqual(offers.first?.name, "Bo")
    }

    // MARK: #46 — the build notes and their rules

    func testTheTargetAndNotesSaveOneFieldEach() throws {
        XCTAssertEqual(try json(LaborTargetBody(laborTargetPct: 28.5)) as NSDictionary, ["labor_target_pct": 28.5])
        XCTAssertEqual(try json(SchedNotesBody(schedNotes: "Two cooks Friday lunch.")) as NSDictionary,
                       ["sched_notes": "Two cooks Friday lunch."])
        XCTAssertEqual(ScheduleBuildSettings.pct(28), "28")
        XCTAssertEqual(ScheduleBuildSettings.pct(28.5), "28.5")
    }

    func testANoteRuleCarriesEveryKeyTheRouteReads() throws {
        let body = try json(NoteRuleAddBody(role: "Cook", min: 2, dayparts: ["morning"], days: ["Friday"], scope: "every",
                                            weekStart: "2026-10-12", sourceText: "Keep two cooks on Friday lunch."))
        XCTAssertEqual(Set(body.keys), ["role", "min", "dayparts", "days", "scope", "week_start", "source_text"])
        XCTAssertEqual(body["min"] as? Int, 2)
    }

    func testTheNotesReadBackSentenceBySentence() throws {
        let p = try JSONDecoder.cavnar.decode(NoteRulesPayload.self, from: Data("""
        {"ok": true, "week_of": "2026-10-12", "can_edit": true, "roles": ["Cook", "Server"],
         "sentences": [
           {"text": "Keep two cooks on Friday lunch.", "kind": "rule",
            "rule": {"role": "Cook", "role_choices": ["Cook"], "min": 2, "dayparts": ["morning"], "days": ["Friday"], "scope": "every"},
            "why": null},
           {"text": "Open with a bartender on game days.", "kind": "unchecked", "why": "No number of people."},
           {"text": "Fewer servers late.", "kind": "guidance", "rule_id": 7, "enforced": "at least 1 Server at dinner"}],
         "rules": [{"id": 7, "words": "at least 1 Server at dinner, every day · every week"},
                   {"id": 8, "words": "at least 2 Host at lunch", "source_text": "Two hosts at lunch"}]}
        """.utf8))
        XCTAssertEqual(p.sentences.map(\.id), [0, 1, 2])
        XCTAssertEqual(p.sentences[0].rule?.min, 2)
        XCTAssertEqual(p.sentences[2].ruleId, 7)
        XCTAssertEqual(p.earlierRules.map(\.id), [8])
    }

    func testTheAutoDraftReadsTheDayAndAnyTool() throws {
        let d = try JSONDecoder.cavnar.decode(ScheduleAutoDraft.self, from: Data("""
        {"ok": true, "enabled": true, "weekday": 2, "day": "Wednesday", "publish_day": "Thursday", "external_tool": null}
        """.utf8))
        XCTAssertTrue(d.enabled)
        XCTAssertEqual(d.day, "Wednesday")
        XCTAssertEqual(d.externalTool, "")
    }

    // MARK: #47 — the scorecard

    func testTheScorecardSaysWhatWasMeasuredAndNothingElse() throws {
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "week_dates": ["2026-10-12", "2026-10-18"],
         "preview_rows": [{"date": "2026-10-12", "day": "Monday", "employee": "Ana", "role": "Server",
                           "shift_start": "5:00pm", "shift_end": "10:00pm", "scheduled_hours": "5"}],
         "quality": {"checked": true, "score": 84, "band": "strong",
                     "dimensions": [{"key": "coverage", "label": "Coverage", "score": 92}]},
         "review": {"hard": 1, "soft": 2, "lines": []},
         "projected_cost": {"straight": 900, "overtime_premium": 45, "overtime_hours": 6, "total": 945},
         "labor_view": {"basis": "all_in", "pct": 27.4, "target_pct": 28, "recent_pct": 31.0, "recent_days": 14,
                        "savings": 612},
         "projected_revenue": 17000}
        """.utf8))
        let tiles = Dictionary(uniqueKeysWithValues: ScheduleSummaryTiles.tiles(r).map { ($0.key, $0) })
        XCTAssertEqual(tiles["quality"]?.value, "84")
        XCTAssertEqual(tiles["quality"]?.tone, .good)
        XCTAssertEqual(tiles["labor"]?.value, "27.4")
        XCTAssertTrue(tiles["labor"]?.sub.contains("all-in · your target is 28%") == true)
        XCTAssertEqual(tiles["coverage"]?.value, "92")
        XCTAssertEqual(tiles["overtime"]?.value, "6")
        XCTAssertEqual(tiles["overtime"]?.tone, .bad)
        XCTAssertEqual(tiles["warnings"]?.value, "3")
        XCTAssertEqual(tiles["warnings"]?.tone, .bad)
        XCTAssertEqual(tiles["savings"]?.value, "$612")
        XCTAssertTrue(tiles["savings"]?.sub.contains("on $17,000 forecast sales") == true)
        XCTAssertEqual(r.forecastSales, 17000)
    }

    func testAnUnmeasuredTileIsADashNeverZero() throws {
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "preview_rows": []}
        """.utf8))
        let tiles = Dictionary(uniqueKeysWithValues: ScheduleSummaryTiles.tiles(r).map { ($0.key, $0) })
        // The warnings too: no rules check is "—", never a green "0 · Every
        // rule kept" (re-audit 10/8/26 #4).
        for key in ["quality", "labor", "coverage", "overtime", "warnings", "savings"] {
            XCTAssertNil(tiles[key]?.value, key)
        }
    }

    func testAReopenedWeekReadsItsForecastFromEconomics() throws {
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "economics": {"projected_revenue": "15250.5", "blended_rate": 17}}
        """.utf8))
        XCTAssertEqual(r.forecastSales, 15250.5)
        let odd = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "projected_revenue": "n/a"}
        """.utf8))
        XCTAssertNil(odd.forecastSales)
    }

    // MARK: #48 — the day pager and a person's week

    func testEveryonesWeekIsMostHoursFirstWithTheirShiftsInOrder() {
        let rows = [
            ScheduleRow(date: "2026-10-13", day: "Tuesday", employee: "Ana", role: "Server", shiftStart: "5:00pm",
                        shiftEnd: "10:00pm", scheduledHours: "5", notes: nil),
            ScheduleRow(date: "2026-10-12", day: "Monday", employee: "ana", role: "Bartender", shiftStart: "4:00pm",
                        shiftEnd: "11:00pm", scheduledHours: nil, notes: nil),
            ScheduleRow(date: "2026-10-12", day: "Monday", employee: "Bo", role: "Cook", shiftStart: "9:00am",
                        shiftEnd: "1:00pm", scheduledHours: "4", notes: nil),
        ]
        let people = ScheduleWeekMath.people(rows)
        XCTAssertEqual(people.map(\.name), ["Ana", "Bo"])
        XCTAssertEqual(people[0].hours, 12)
        XCTAssertEqual(people[0].days, 2)
        XCTAssertEqual(people[0].rows.first?.day, "Monday")
        XCTAssertEqual(people[0].roles, ["Bartender", "Server"])
    }

    func testThePDFRunsADayToAPageAndSplitsALongDay() {
        var rows: [ScheduleRow] = []
        for i in 0..<30 {
            rows.append(ScheduleRow(date: "2026-10-12", day: "Monday", employee: "P\(i)", role: "Server",
                                    shiftStart: "5:00pm", shiftEnd: "10:00pm", scheduledHours: "5", notes: nil))
        }
        rows.append(ScheduleRow(date: "2026-10-13", day: "Tuesday", employee: "Q", role: "Cook",
                                shiftStart: "9:00am", shiftEnd: "3:00pm", scheduledHours: "6", notes: nil))
        let pages = SchedulePDF.pages(rows)
        XCTAssertEqual(pages.map(\.day), ["Monday", "Monday", "Tuesday"])
        XCTAssertEqual(pages.map(\.continued), [false, true, false])
        XCTAssertEqual(pages[0].rows.count, SchedulePDF.rowsPerPage)
    }

    // MARK: #45 / #69 / #68 — roster, covers, note holds

    func testCoversSaveTheWebsOneNightRow() throws {
        let body = try json(CoversSaveBody(rows: [.init(date: "2026-10-07", covers: 84)]))
        let rows = try XCTUnwrap(body["rows"] as? [[String: Any]])
        XCTAssertEqual(rows.first?["date"] as? String, "2026-10-07")
        XCTAssertEqual(rows.first?["covers"] as? Int, 84)
        let p = try JSONDecoder.cavnar.decode(CoversPayload.self, from: Data("""
        {"ok": true, "days": [{"date": "2026-10-06", "covers": 80, "source": "pos"},
                              {"date": "2026-10-05", "covers": 91, "source": "manual"}], "pos_offers": []}
        """.utf8))
        XCTAssertEqual(p.averageLine, "2 days on file \u{00B7} about 86 a day")
    }

    func testANoteHoldPostsTheReadingAndANotePartReadsItsHold() throws {
        let body = try json(StaffNoteHoldBody(employeeName: "Ana", partText: "no Tuesdays", days: ["Tuesday"],
                                              dayparts: [], start: nil, end: "2026-12-01"))
        XCTAssertEqual(Set(body.keys), ["employee_name", "part_text", "days", "end"])
        let part = try JSONDecoder.cavnar.decode(StaffNotePart.self, from: Data("""
        {"index": 0, "text": "no Tuesdays", "ended": false, "stale": false,
         "reading": {"kind": "hold", "why": null, "reason": null,
                     "hold": {"days": ["Tuesday"], "dayparts": null, "start": null, "end": null, "words": "no Tuesdays"}},
         "held": {"id": 4, "words": "off Tuesdays"}}
        """.utf8))
        XCTAssertEqual(part.reading?.kind, "hold")
        XCTAssertEqual(part.reading?.hold?.days, ["Tuesday"])
        XCTAssertEqual(part.held?.id, 4)
        let plain = try JSONDecoder.cavnar.decode(StaffNotePart.self, from: Data("""
        {"index": 1, "text": "prefers mornings", "reading": "odd"}
        """.utf8))
        XCTAssertNil(plain.reading)
    }

    // MARK: History — the rows say what each week is

    func testHistoryRowsCarrySentDraftReplacedAndQuality() throws {
        let rows = try JSONDecoder.cavnar.decode([ScheduleHistoryEntry].self, from: Data("""
        [{"id": 1, "generated_at": "2026-10-05 10:00:00", "week_start": "2026-10-12", "week_end": "2026-10-18",
          "hours_scheduled": 412.5, "hours_budget": 430, "labor_target": 28, "published_at": "2026-10-09 18:45:00",
          "quality_score": 84.0, "quality_band": "strong", "summary_line": "Strong week · 412h"},
         {"id": 2, "generated_at": "2026-10-04 10:00:00", "week_start": "2026-10-12", "week_end": "2026-10-18",
          "hours_scheduled": null, "hours_budget": null, "labor_target": null, "superseded_by": 1, "quality_score": null},
         {"id": 3, "generated_at": "2026-10-06 10:00:00", "week_start": "2026-10-19", "week_end": "2026-10-25"}]
        """.utf8))
        XCTAssertEqual(rows.map(\.state), ["Sent", "Replaced", "Draft"])
        XCTAssertEqual(rows[0].qualityScore?.value, 84)
        XCTAssertEqual(rows[0].summaryLine, "Strong week · 412h")
        XCTAssertNil(rows[1].qualityScore?.value)
    }
}
