import XCTest
@testable import CavnarAI

/// Blind re-audit of Labor / the AI schedule (10/8/26), the phone's half:
/// one tap only on the server's own verdict (#1, #13), History's times and
/// warnings (#3, #4), "—" never a green zero (#4, #5), the business date
/// (#6), a refused press keeps the week (#7), following a run (#8), every
/// day of the week in the Day picker (#9), a move keeps where it came from
/// (#10), the closed-day toggle reads the list first (#11), automation
/// switches only for a login that may (#12) — and moveShift / addShift.
@MainActor
final class LaborReauditTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
        SecureCache.purgeAll()
    }

    private func json(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func check(_ extra: String, canPublish: Bool = true) throws -> PublishCheck {
        try JSONDecoder.cavnar.decode(PublishCheck.self, from: Data("""
        {"ok": true, "schedule_id": 41, "week_start": "2026-10-12", "published_at": null, "blockers": [],
         "reach": {"total": 12, "reachable": 11, "by_app": 9, "by_text": 2, "by_email": 0, "unreachable": ["Al"]},
         "can_publish": \(canPublish) \(extra)}
        """.utf8))
    }

    nonisolated private static let week = """
    {"ok": true, "status": "done", "history_id": 91,
     "week_dates": ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"],
     "preview_rows": [
       {"date": "2026-09-29", "day": "Tuesday", "employee": "Ana", "role": "Server",
        "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6", "notes": "Patio"},
       {"date": "2026-09-30", "day": "Wednesday", "employee": "Bob", "role": "Manager",
        "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"}]}
    """

    nonisolated private static let saved = """
    {"ok": true, "saved": true, "quality": {"checked": true, "score": 80, "band": "solid", "shifts": []}}
    """

    private func weekOnScreen(_ vm: LaborViewModel) throws {
        vm.scheduleResult = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.week.utf8))
        vm.latestVersion = 3
    }

    // MARK: #1 — one tap is the server's verdict

    func testOneTapIsTheServersVerdictNeverTheBlockersAlone() throws {
        // No blockers, but the server says no (a soft flag or a note): no one tap.
        XCTAssertFalse(try check(", \"one_tap_safe\": false").allowsOneTap(scheduleId: 41))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(try check(", \"one_tap_safe\": false"), scheduleId: 41))
        // An older server that never says: never one tap.
        XCTAssertNil(try check("").oneTapSafe)
        XCTAssertFalse(try check("").allowsOneTap(scheduleId: 41))
        XCTAssertFalse(PushManager.publishCheckAllowsOneTap(try check(""), scheduleId: 41))
        // The server says yes, for this week.
        XCTAssertTrue(try check(", \"one_tap_safe\": true").allowsOneTap(scheduleId: 41))
        XCTAssertTrue(PushManager.publishCheckAllowsOneTap(try check(", \"one_tap_safe\": true"), scheduleId: 41))
        XCTAssertFalse(try check(", \"one_tap_safe\": true").allowsOneTap(scheduleId: 42))
        XCTAssertFalse(try check(", \"one_tap_safe\": true", canPublish: false).allowsOneTap(scheduleId: 41))
    }

    func testAOneTapSendSaysOneTapAndAcknowledgesNothing() throws {
        let body = try json(PublishScheduleViewModel.PublishBody(scheduleId: 41, acknowledge: true,
                                                                 keys: ["rule:manager"], oneTap: true))
        XCTAssertEqual(body["one_tap"] as? Bool, true)
        XCTAssertEqual(body["acknowledge"] as? Bool, false)
        XCTAssertEqual(body["schedule_id"] as? Int, 41)
        // A sheet's send is unchanged: the keys it showed, and no one_tap.
        let sheet = try json(PublishScheduleViewModel.PublishBody(scheduleId: 41, acknowledge: true, keys: ["k"]))
        XCTAssertEqual(sheet["acknowledge"] as? [String], ["k"])
        XCTAssertNil(sheet["one_tap"])
    }

    func testWaitingOnYousSendPostsOneTapAndOpensTheSheetWhenRefused() async {
        let bodies = Box<[[String: Any]]>([])
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/labor/publish-schedule" {
                bodies.value.append(EdgeHTTP.bodyJSON(request) ?? [:])
                return EdgeHTTP.reply(request, 409, #"{"ok": false, "one_tap_refused": true, "schedule_id": 41, "error": "Read it first."}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "schedule_id": null}"#)
        }
        let vm = LaborViewModel(client: client)
        let openSheet = await vm.sendDraft(41)
        XCTAssertTrue(openSheet, "a refused one tap opens the send sheet, never a dead error")
        XCTAssertEqual(bodies.value.first?["one_tap"] as? Bool, true)
        XCTAssertEqual(bodies.value.first?["acknowledge"] as? Bool, false)
        XCTAssertNil(vm.draftSendError)
    }

    func testTheGateResponseReadsAOneTapRefusal() throws {
        let gate = try JSONDecoder.cavnar.decode(PublishScheduleViewModel.GateResponse.self, from: Data("""
        {"ok": false, "one_tap_refused": true, "schedule_id": 41, "error": "Read it first."}
        """.utf8))
        XCTAssertEqual(gate.oneTapRefused, true)
        XCTAssertEqual(gate.scheduleId, 41)
    }

    // MARK: #13 — the confirm promises only what is known

    func testTheOneTapConfirmSaysWhoItReachesAndThatTheCheckRunsAgain() throws {
        func reach(_ total: Int, _ reachable: Int) throws -> PublishReach {
            try JSONDecoder.cavnar.decode(PublishReach.self, from: Data(#"{"total": \#(total), "reachable": \#(reachable)}"#.utf8))
        }
        let some = LaborWaitingOnYou.oneTapConfirmMessage(try reach(12, 9))
        XCTAssertTrue(some.hasPrefix("9 of the 12 on it hear about their shifts"))
        XCTAssertTrue(some.contains("the rest only see it in the staff portal"))
        XCTAssertTrue(some.contains("The publish check runs again as it sends"))
        XCTAssertFalse(some.contains("Nothing for the publish check to flag"))
        XCTAssertTrue(LaborWaitingOnYou.oneTapConfirmMessage(try reach(4, 4)).hasPrefix("All 4 on it hear"))
        XCTAssertTrue(LaborWaitingOnYou.oneTapConfirmMessage(try reach(4, 0)).contains("staff portal only"))
    }

    // MARK: #3 — when it went to staff, on the restaurant's clock

    func testASentTimeIsReadAsUTCAndShownOnTheRestaurantsClock() throws {
        let chicago = try XCTUnwrap(TimeZone(identifier: "America/Chicago"))
        XCTAssertEqual(CavnarDate.mdyTimeLocal("2026-10-09 01:30:00", in: chicago), "10/8/26 · 8:30pm")
        XCTAssertEqual(CavnarDate.mdyTimeLocal("2026-10-09T18:05:00", in: chicago), "10/9/26 · 1:05pm")
        // A bare date has no time to move.
        XCTAssertEqual(CavnarDate.mdyTimeLocal("2026-10-09", in: chicago), "10/9/26")
    }

    // MARK: #4 / #5 — "—", never a green zero

    func testNoRulesCheckIsADashNotEveryRuleKept() throws {
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data(#"{"ok": true, "history_id": 5}"#.utf8))
        let w = try XCTUnwrap(ScheduleSummaryTiles.tiles(r).first { $0.key == "warnings" })
        XCTAssertNil(w.value)
        XCTAssertEqual(w.tone, .neutral)
        XCTAssertFalse(w.sub.contains("Every rule kept"))
        let clean = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "review": {"hard": 0, "soft": 0, "lines": []}}
        """.utf8))
        let kept = try XCTUnwrap(ScheduleSummaryTiles.tiles(clean).first { $0.key == "warnings" })
        XCTAssertEqual(kept.value, "0")
        XCTAssertEqual(kept.sub, "Every rule kept")
    }

    func testOvertimeThatWasNotPricedIsADash() throws {
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "projected_cost": {"total": 9000}}
        """.utf8))
        let ot = try XCTUnwrap(ScheduleSummaryTiles.tiles(r).first { $0.key == "overtime" })
        XCTAssertNil(ot.value)
        XCTAssertFalse(ot.sub.contains("Nobody past 40h"))
        let none = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 5, "projected_cost": {"overtime_hours": 0, "overtime_premium": 0}}
        """.utf8))
        let zero = try XCTUnwrap(ScheduleSummaryTiles.tiles(none).first { $0.key == "overtime" })
        XCTAssertEqual(zero.value, "0")
        XCTAssertEqual(zero.sub, "Nobody past 40h")
    }

    func testHistoryWarningsReadThePublishCheckNotTheSavedReview() throws {
        // The review saved at generation said clean; the gate now has one.
        let r = try JSONDecoder.cavnar.decode(GeneratedSchedule.self, from: Data("""
        {"ok": true, "history_id": 41, "review": {"hard": 0, "soft": 0, "lines": []}}
        """.utf8))
        let gate = try check(", \"blocker_items\": [{\"key\": \"rule:manager\", \"text\": \"No manager on Fri 5-6pm\"}]")
        let w = try XCTUnwrap(ScheduleSummaryTiles.tiles(r, warnings: .publishCheck(gate)).first { $0.key == "warnings" })
        XCTAssertEqual(w.value, "1")
        XCTAssertEqual(w.tone, .bad)
        // Not read (yet, or it failed): "—".
        let unread = try XCTUnwrap(ScheduleSummaryTiles.tiles(r, warnings: .publishCheck(nil)).first { $0.key == "warnings" })
        XCTAssertNil(unread.value)
    }

    // MARK: #6 — the business date

    func testTheBusinessDateIsLastNightBeforeFiveOnTheRestaurantsClock() throws {
        let chicago = try XCTUnwrap(TimeZone(identifier: "America/Chicago"))
        // 2026-10-09 06:30 UTC is 1:30am in Chicago: still the 8th's service.
        let late = Date(timeIntervalSince1970: 1_791_527_400)
        XCTAssertEqual(CavnarDate.isoDay(late, in: chicago), "2026-10-09")
        XCTAssertEqual(RestaurantClock.businessDate(at: late, in: chicago), "2026-10-08")
        let evening = late.addingTimeInterval(16 * 3600)
        XCTAssertEqual(RestaurantClock.businessDate(at: evening, in: chicago), "2026-10-09")
    }

    func testCoversReadTheServersBusinessDate() throws {
        let p = try JSONDecoder.cavnar.decode(CoversPayload.self, from: Data("""
        {"ok": true, "days": [], "business_date": "2026-10-08"}
        """.utf8))
        XCTAssertEqual(p.businessDate, "2026-10-08")
        let old = try JSONDecoder.cavnar.decode(CoversPayload.self, from: Data(#"{"ok": true, "days": []}"#.utf8))
        XCTAssertNil(old.businessDate)
    }

    func testCoversTakeTheBusinessDateUntilTheOwnerPicksANight() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, #"{"ok": true, "days": [], "business_date": "2026-10-08"}"#)
        }
        let model = CoversModel(client: client)
        await model.load()
        XCTAssertEqual(model.date, "2026-10-08")
        model.date = "2026-10-05"
        await model.load()
        XCTAssertEqual(model.date, "2026-10-05", "a night the owner picked is never moved under them")
    }

    // MARK: #7 — a refused press keeps the week and its edits

    func testABusyAnswerKeepsTheWeekAndItsUnsavedEdits() async throws {
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/labor/generate-schedule" {
                return EdgeHTTP.reply(request, 409, """
                {"ok": false, "busy": true, "job_id": "auto-1", "running": {"week_start": "2026-10-12"},
                 "error": "A schedule for the week of 10/12/26 is being built."}
                """)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = LaborViewModel(client: client)
        try weekOnScreen(vm)
        vm.hasUnsavedFixes = true
        vm.overriddenRows = ["x"]
        await vm.generateSchedule()
        XCTAssertNotNil(vm.scheduleResult, "the week on screen stays when nothing started")
        XCTAssertTrue(vm.hasUnsavedFixes)
        XCTAssertEqual(vm.overriddenRows, ["x"])
        XCTAssertEqual(vm.busyRun?.jobId, "auto-1")
        XCTAssertFalse(vm.isGeneratingSchedule)
    }

    // MARK: #8 — following the run in the way

    func testFollowingABusyRunTellsTheServerThenPollsIt() async throws {
        let calls = Box<[String]>([])
        let followed = Box<[String: Any]>([:])
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            calls.value.append(path)
            if path == "/mobile/api/labor/generate-schedule/follow" {
                followed.value = EdgeHTTP.bodyJSON(request) ?? [:]
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "auto-1", "joined": true, "wait_seconds": 900}"#)
            }
            if path.hasPrefix("/mobile/api/labor/schedule-status/") {
                return EdgeHTTP.reply(request, 200, Self.week)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "versions": []}"#)
        }
        let vm = LaborViewModel(client: client)
        vm.busyRun = try JSONDecoder.cavnar.decode(GenerationBusy.self, from: Data("""
        {"busy": true, "job_id": "auto-1", "running": {"week_start": "2026-10-12"}}
        """.utf8))
        await vm.followBusyRun()
        XCTAssertEqual(followed.value["job_id"] as? String, "auto-1")
        let follow = try XCTUnwrap(calls.value.firstIndex(of: "/mobile/api/labor/generate-schedule/follow"))
        let poll = try XCTUnwrap(calls.value.firstIndex { $0.hasPrefix("/mobile/api/labor/schedule-status/auto-1") })
        XCTAssertLessThan(follow, poll)
        XCTAssertFalse(calls.value.contains("/mobile/api/labor/generate-schedule"), "following never starts a run")
        XCTAssertNotNil(vm.scheduleResult)
    }

    // MARK: #9 — every day of the week

    func testTheDayPickerOffersEveryDayOfTheWeek() throws {
        let r = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.week.utf8))
        let days = ShiftEditSheet.pickableDays(weekDates: r.weekDates, rows: r.previewRows ?? [])
        XCTAssertEqual(days.count, 7)
        XCTAssertEqual(days.first, "2026-09-28")
        XCTAssertEqual(days.last, "2026-10-04")
        // An older week with no week_dates: the days that have shifts.
        XCTAssertEqual(ShiftEditSheet.pickableDays(weekDates: nil, rows: r.previewRows ?? []),
                       ["2026-09-29", "2026-09-30"])
    }

    // MARK: #10 + moveShift

    func testMovingAShiftKeepsWhereItCameFromAndSavesTheWeek() async throws {
        let scored = Box<[[String: Any]]>([])
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/labor/schedule/score" {
                scored.value.append(EdgeHTTP.bodyJSON(request) ?? [:])
                return EdgeHTTP.reply(request, 200, Self.saved)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = LaborViewModel(client: client)
        try weekOnScreen(vm)
        let ana = try XCTUnwrap(vm.scheduleResult?.previewRows?.first)
        let refusal = await vm.moveShift(rowId: ana.id, to: "2026-10-02", start: "5:00pm", end: "11:00pm")
        XCTAssertNil(refusal)
        let moved = try XCTUnwrap(vm.scheduleResult?.previewRows?.first { $0.employee == "Ana" })
        XCTAssertEqual(moved.date, "2026-10-02")
        XCTAssertEqual(moved.day, "Friday")
        XCTAssertEqual(moved.shiftStart, "5:00pm")
        XCTAssertEqual(moved.notes, "Patio (moved from Ana Tue)")
        XCTAssertTrue(vm.overriddenRows.contains(moved.id))
        XCTAssertFalse(vm.overriddenRows.contains(ana.id))
        XCTAssertFalse(scored.value.isEmpty, "the move is re-scored and stored")
    }

    func testAMoveOntoTheSamePersonsShiftIsRefused() async throws {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, 200, Self.saved) }
        let vm = LaborViewModel(client: client)
        try weekOnScreen(vm)
        let rows = try XCTUnwrap(vm.scheduleResult?.previewRows)
        _ = await vm.addShift(date: "2026-10-02", employee: "Ana", role: "Server", start: "5:00pm", end: "10:00pm")
        let refusal = await vm.moveShift(rowId: rows[0].id, to: "2026-10-02", start: "5:00pm", end: "10:00pm")
        XCTAssertEqual(refusal, "Ana already has a shift starting at 5:00pm that day.")
    }

    func testTheMovedNoteMatchesTheWebGrid() {
        XCTAssertEqual(LaborViewModel.movedNote(nil, employee: "Ana", from: "2026-09-29"), "(moved from Ana Tue)")
        XCTAssertEqual(LaborViewModel.movedNote("Patio", employee: "Ana", from: "2026-09-29"), "Patio (moved from Ana Tue)")
    }

    // MARK: addShift

    func testAddingAShiftPutsItOnAnEmptyDayAndRefusesADuplicate() async throws {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, 200, Self.saved) }
        let vm = LaborViewModel(client: client)
        try weekOnScreen(vm)
        let added = await vm.addShift(date: "2026-10-03", employee: "Cara", role: "Host", start: "11:00am", end: "3:00pm")
        XCTAssertNil(added)
        let row = try XCTUnwrap(vm.scheduleResult?.previewRows?.first { $0.employee == "Cara" })
        XCTAssertEqual(row.day, "Saturday")
        XCTAssertEqual(row.role, "Host")
        XCTAssertTrue(vm.overriddenRows.contains(row.id))
        let again = await vm.addShift(date: "2026-10-03", employee: "Cara", role: "Host", start: "11:00am", end: "3:00pm")
        XCTAssertEqual(again, "Cara already has that shift.")
        let nobody = await vm.addShift(date: "2026-10-03", employee: "  ", role: nil, start: "11:00am", end: "3:00pm")
        XCTAssertEqual(nobody, "Pick who works it.")
        XCTAssertEqual(vm.scheduleResult?.previewRows?.count, 3)
    }

    // MARK: #11 — the closed-day toggle reads the list first

    func testTheClosedDayToggleSwitchesTheDayOnTheListAsItStandsNow() async {
        let posted = Box<[String: Any]>([:])
        let client = EdgeHTTP.client { request in
            if request.httpMethod == "POST" {
                posted.value = EdgeHTTP.bodyJSON(request) ?? [:]
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "closures": {"closed_weekdays": ["Monday", "Tuesday"]}}"#)
            }
            // Another login closed Monday since the sheet opened.
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "closures": {"closed_weekdays": ["Monday"]}}"#)
        }
        let store = TeamSetupStore(client: client)
        store.closedWeekdays = []
        await store.toggleClosedWeekday("Tuesday")
        XCTAssertEqual(posted.value["closed_weekdays"] as? [String], ["Monday", "Tuesday"],
                       "Monday, closed elsewhere, is never reopened by a stale list")
        XCTAssertEqual(store.closedWeekdays, ["Monday", "Tuesday"])
    }

    func testAToggleSomebodyAlreadyMadeWritesNothing() async {
        let posts = Box(0)
        let client = EdgeHTTP.client { request in
            if request.httpMethod == "POST" { posts.value += 1 }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "closures": {"closed_weekdays": ["Sunday"]}}"#)
        }
        let store = TeamSetupStore(client: client)
        store.closedWeekdays = []
        await store.toggleClosedWeekday("Sunday")
        XCTAssertEqual(posts.value, 0)
        XCTAssertEqual(store.closedWeekdays, ["Sunday"])
    }

    func testNoFreshListNoWrite() async {
        let posts = Box(0)
        let client = EdgeHTTP.client { request in
            if request.httpMethod == "POST" { posts.value += 1 }
            return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "down"}"#)
        }
        let store = TeamSetupStore(client: client)
        store.closedWeekdays = ["Monday"]
        await store.toggleClosedWeekday("Tuesday")
        XCTAssertEqual(posts.value, 0)
        XCTAssertNotNil(store.closedDaysError)
    }

    // MARK: #12 — the switches only for a login that may

    func testTheAutomationReadsSayWhetherThisLoginMaySwitchThem() throws {
        let draft = try JSONDecoder.cavnar.decode(ScheduleAutoDraft.self, from: Data(#"{"enabled": true, "can_edit": true}"#.utf8))
        XCTAssertTrue(draft.canEdit)
        let publish = try JSONDecoder.cavnar.decode(ScheduleAutoPublish.self, from: Data(#"{"enabled": false, "can_edit": false}"#.utf8))
        XCTAssertFalse(publish.canEdit)
        // An older server that never says: no switch.
        XCTAssertFalse(try JSONDecoder.cavnar.decode(ScheduleAutoDraft.self, from: Data(#"{"enabled": true}"#.utf8)).canEdit)
    }
}
