import XCTest
@testable import CavnarAI

/// What these protect: the published week staff actually work from. A
/// generation must survive one bad poll and a manager leaving the screen;
/// two quick edits must not read as somebody else's save; a refused roster
/// edit must roll back the right person; a sent schedule must not be
/// re-emailed to everyone by a second tap; and week labels follow the
/// M/D/YY rule. XCTExpectFailure marks confirmed CLIENT-27 / 29 / 32 / 36 /
/// 41 / 45 / 49 / 60 / 62 defects; each flips when fixed.
@MainActor
final class EdgeLaborSchedulingTests: XCTestCase {

    nonisolated private static let doneSchedule = """
    {"ok": true, "status": "done", "history_id": 91,
     "week_dates": ["2026-09-28", "2026-10-04"], "hours_scheduled": 12, "summary": [],
     "preview_rows": [
       {"date": "2026-09-29", "day": "Tuesday", "employee": "Ana", "role": "Server",
        "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"},
       {"date": "2026-09-30", "day": "Wednesday", "employee": "Bob", "role": "Server",
        "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"}]}
    """

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
        SecureCache.purgeAll()
    }

    private func query(_ request: URLRequest) -> [String: String] {
        let items = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        return Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.value ?? "") })
    }

    // MARK: CLIENT-41 — one failed poll

    func testASingleFailedPollDoesNotEndTheGeneration() async {
        let polls = Box(0)
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/labor/generate-schedule" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "job-1"}"#)
            }
            if path.hasPrefix("/mobile/api/labor/schedule-status/") {
                polls.value += 1
                if polls.value == 1 {
                    return EdgeHTTP.reply(request, 502, "<html>Bad Gateway</html>")
                }
                return EdgeHTTP.reply(request, 200, Self.doneSchedule)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "versions": []}"#)
        }
        let vm = LaborViewModel(client: client)
        await vm.generateSchedule()
        XCTAssertGreaterThanOrEqual(polls.value, 1)
        XCTAssertNil(vm.scheduleError)
        XCTAssertNotNil(vm.scheduleResult, "the second poll had the finished week")
    }

    func testAFailedPartialRedoClearsItsRegeneratingDays() async {
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/labor/generate-schedule" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "job-2"}"#)
            }
            return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = LaborViewModel(client: client)
        await vm.generateSchedule(dates: ["2026-09-29"], historyId: 91)
        XCTAssertFalse(vm.isGeneratingSchedule)
        XCTAssertNotNil(vm.scheduleError)
        XCTAssertEqual(vm.regeneratingDates, [])
    }

    // MARK: CLIENT-27 — leaving during generation

    func testReturningToLaborDuringAGenerationReattachesToTheJob() async {
        let polls = Box(0)
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/labor/generate-schedule" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "job-3"}"#)
            }
            if path.hasPrefix("/mobile/api/labor/schedule-status/") {
                polls.value += 1
                return polls.value == 1
                    ? EdgeHTTP.reply(request, 200, #"{"ok": true, "status": "pending"}"#)
                    : EdgeHTTP.reply(request, 200, Self.doneSchedule)
            }
            if path == "/mobile/api/labor" {
                return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "stats unavailable in this test"}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "versions": []}"#)
        }
        // LaborView creates a fresh LaborViewModel per push.
        let first = LaborViewModel(client: client)
        let running = Task { await first.generateSchedule() }
        await EdgeHTTP.waitUntil { polls.value >= 1 }
        running.cancel()          // the manager backs out of Labor
        await running.value

        let second = LaborViewModel(client: client)
        await second.load()
        XCTAssertTrue(second.isGeneratingSchedule || second.scheduleResult != nil)
    }

    func testLeavingMidGenerationIsNotReportedAsALostConnection() async {
        let polls = Box(0)
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            if path == "/mobile/api/labor/generate-schedule" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "job_id": "job-4"}"#)
            }
            polls.value += 1
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "status": "pending"}"#)
        }
        let vm = LaborViewModel(client: client)
        let running = Task { await vm.generateSchedule() }
        await EdgeHTTP.waitUntil { polls.value >= 1 }
        running.cancel()
        await running.value
        XCTAssertNil(vm.scheduleError)
    }

    func testACancelledStatsLoadSetsNoError() async {
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        let vm = LaborViewModel(client: client)
        await EdgeHTTP.tornDown { await vm.load() }
        XCTAssertNil(vm.errorMessage)
    }

    // MARK: CLIENT-29 / CLIENT-62 — overrides

    private func scheduleOnScreen(_ vm: LaborViewModel) throws {
        vm.scheduleResult = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.doneSchedule.utf8))
        vm.latestVersion = 3
    }

    nonisolated private static let savedScore = """
    {"ok": true, "saved": true, "quality": {"checked": true, "score": 80, "band": "solid", "shifts": []}}
    """

    func testTwoQuickOverridesDoNotConflictWithEachOther() async throws {
        let versions = Box<[Int]>([])
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/labor/schedule/score" {
                if let v = EdgeHTTP.bodyJSON(request)?["version"] as? Int { versions.value.append(v) }
                Thread.sleep(forTimeInterval: 0.05)
                return EdgeHTTP.reply(request, 200, Self.savedScore)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "versions": [{"version": 3}]}"#)
        }
        let vm = LaborViewModel(client: client)
        try scheduleOnScreen(vm)
        let rows = try XCTUnwrap(vm.scheduleResult?.previewRows)
        async let a: Void = vm.overrideEmployee(rowId: rows[0].id, to: "Cara")
        async let b: Void = vm.overrideEmployee(rowId: rows[1].id, to: "Dev")
        _ = await (a, b)
        XCTAssertEqual(versions.value.count, 2)
        XCTAssertEqual(versions.value, [3, 4], "the second save must name the version the first one wrote")
    }

    func testASavedOverrideDoesNotHoldTheScreenForSeconds() async throws {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, 200, Self.savedScore) }
        let vm = LaborViewModel(client: client)
        try scheduleOnScreen(vm)
        let started = Date()
        await vm.rescoreQuality()
        let took = Date().timeIntervalSince(started)
        XCTAssertEqual(vm.overrideState, .idle)
        XCTAssertLessThan(took, 1.0)
    }

    // MARK: CLIENT-32 — roster rollback

    private func member(_ name: String) throws -> RosterMember {
        try JSONDecoder().decode(RosterMember.self, from: Data(#"{"name": "\#(name)", "active": true}"#.utf8))
    }

    func testARefusedSettingRollsBackOnlyThatPerson() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, #"{"ok": false, "error": "Couldn't save that."}"#)
        }
        let vm = ScheduleSetupViewModel(client: client)
        vm.roster = [try member("Ana"), try member("Bob")]
        let ok = await vm.updateSettings(.init(employeeName: "Bob", active: false))
        XCTAssertFalse(ok)
        XCTAssertEqual(vm.roster.map(\.name), ["Ana", "Bob"])
        XCTAssertTrue(vm.roster[1].isActive, "Bob's optimistic change was rolled back")
        XCTAssertEqual(vm.settingsToast, "Couldn't save that.")
    }

    func testARollbackAfterARosterReloadTargetsThePersonNotTheOldIndex() async throws {
        let arrived = Box(false)
        let gate = DispatchSemaphore(value: 0)
        let client = EdgeHTTP.client { request in
            arrived.value = true
            _ = gate.wait(timeout: .now() + 5)
            return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = ScheduleSetupViewModel(client: client)
        vm.roster = [try member("Ana"), try member("Bob")]
        let save = Task { await vm.updateSettings(.init(employeeName: "Ana", active: false)) }
        await EdgeHTTP.waitUntil { arrived.value }
        // A roster reload lands mid-save and a new hire sorts first.
        vm.roster = [try member("Aaron"), try member("Ana"), try member("Bob")]
        gate.signal()
        _ = await save.value
        XCTAssertEqual(vm.roster.map(\.name), ["Aaron", "Ana", "Bob"])
    }

    // MARK: CLIENT-36 — send to staff twice

    private func publishClient(posts: Box<Int>) -> APIClient {
        EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/labor/publish-schedule":
                posts.value += 1
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "sent": [{"employee_name": "Ana", "sent_to": "ana@example.com", "shifts": 3}],
                 "unreachable": [], "failed": []}
                """)
            case "/mobile/api/labor/staff-contacts":
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "contacts": [], "reachable": 0, "week_start": "2026-09-28", "week_end": "2026-10-04"}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "status": []}"#)
            }
        }
    }

    func testSendToStaffAfterASuccessfulSendDoesNotEmailEveryoneAgain() async {
        let posts = Box(0)
        let vm = PublishScheduleViewModel(client: publishClient(posts: posts))
        await vm.publish()
        XCTAssertEqual(vm.lastResult?.ok, true)
        await vm.publish()      // the button still reads "Send to N staff"
        XCTAssertEqual(posts.value, 1, "a re-send needs its own confirmation")
    }

    func testAConcurrentSecondSendIsIgnored() async {
        // The in-flight guard that does exist.
        let posts = Box(0)
        let vm = PublishScheduleViewModel(client: publishClient(posts: posts))
        async let a: Void = vm.publish()
        async let b: Void = vm.publish()
        _ = await (a, b)
        XCTAssertEqual(posts.value, 1)
    }

    // MARK: CLIENT-45 — dates

    func testThePublishSheetsWeekLabelIsMDY() async {
        let vm = PublishScheduleViewModel(client: publishClient(posts: Box(0)))
        await vm.load()
        XCTAssertNotNil(vm.weekLabel)
        XCTAssertEqual(vm.weekLabel, "9/28/26 – 10/4/26")
    }

    func testTheSharedDateHelperWritesMDY() {
        XCTAssertEqual(CavnarDate.mdy("2026-09-21"), "9/21/26")
        XCTAssertEqual(CavnarDate.mdyRange("2026-09-28", "2026-10-04"), "9/28/26 – 10/4/26")
        XCTAssertEqual(LaborViewModel.GenerateWeek.date(
            ISO8601DateFormatter().date(from: "2026-10-07T17:00:00Z")!).label.split(separator: "/").count, 3)
    }

    // MARK: CLIENT-60 / 62 — source-level

    func testSwipingAHistoryDraftAwayAsksFirst() throws {
        let source = try EdgeSource.read("Features/ScheduleHistory/ScheduleHistoryView.swift")
        XCTAssertTrue(source.contains(".swipeActions"))
        XCTAssertTrue(!source.contains("allowsFullSwipe: true") || source.contains(".confirmationDialog"))
    }

    func testAReloadDoesNotOverwriteARuleBeingEdited() throws {
        // sync() lives in the view; there is no view-model seam for it.
        let source = try EdgeSource.read("Features/Labor/ScheduleRulesSheet.swift")
        XCTAssertTrue(source.contains("onChange(of: viewModel.rules)"))
        XCTAssertFalse(source.contains("{ _, _ in sync() }"),
                       "a reload must not replace fields the manager is editing")
    }
}
