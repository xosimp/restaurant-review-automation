import XCTest
@testable import CavnarAI

/// The iOS fix round after the 9/25/26 blind re-audit (F3-1 … F3-18, D3-8,
/// D3-9, D3-13). Each test pins the rule the finding broke. alert_type is
/// left empty where routing is the point: a non-empty type records the open
/// through APIClient.shared, which is a real request.
@MainActor
final class IOSFixRoundTests: XCTestCase {

    // MARK: F3-1 — a second "go to" the same tab switches again

    func testThePendingTabIsHandedOutOnceSoTheNextRouteIsAChange() {
        let router = DeepLinkRouter()
        router.open(NavPath("review/1")!)
        XCTAssertEqual(router.consumePendingTab(), .modules)
        XCTAssertNil(router.pendingTab, "consumed — onChange must see the next .modules as a change")
        router.open(NavPath("review/2")!)
        XCTAssertEqual(router.pendingTab, .modules)
    }

    // MARK: F3-2 — an Ask proposal is not a queued send

    func testAProposalOpensAsAProposalNotAQueuedSend() {
        let router = DeepLinkRouter()
        router.open(NavPath("proposal/57")!)
        XCTAssertEqual(router.pendingProposalId?.id, 57)
        XCTAssertNil(router.pendingActionId)

        let tagged = DeepLinkRouter()
        tagged.open(NavPath("action/57?kind=proposal")!)
        XCTAssertEqual(tagged.pendingProposalId?.id, 57)
        XCTAssertNil(tagged.pendingActionId)

        let send = DeepLinkRouter()
        send.open(NavPath("action/57")!)
        XCTAssertEqual(send.pendingActionId?.id, 57)
        XCTAssertNil(send.pendingProposalId)
    }

    func testAWaitingAskRowOpensItsProposalWhateverHeadTheServerGaveIt() throws {
        let item = try JSONDecoder().decode(CommandWaitingItem.self, from: Data("""
            {"key": "ask:57", "kind": "proposal", "title": "Send the order", "nav": "action/57"}
            """.utf8))
        XCTAssertEqual(item.destination?.raw, "proposal/57")
        let request = try JSONDecoder().decode(CommandWaitingItem.self, from: Data("""
            {"key": "time_off:12", "kind": "time_off", "title": "Dana", "nav": "request/time_off-12"}
            """.utf8))
        XCTAssertEqual(request.destination?.raw, "request/time_off-12")
    }

    // MARK: F3-3 — "Approve & post" says when Google didn't take it

    private func outcome(_ json: String) throws -> ReviewPostOutcome {
        try JSONDecoder().decode(ReviewPostOutcome.self, from: Data(json.utf8))
    }

    func testAnApprovedReplyThatDidNotPostIsSaid() throws {
        XCTAssertNil(PushManager.approvePostShortfall(try outcome(#"{"ok": true, "post_status": "posted"}"#)))
        XCTAssertNil(PushManager.approvePostShortfall(try outcome(#"{"ok": true, "auto_posted": true}"#)))
        let note = try outcome(#"{"ok": true, "post_status": "not_connected", "post_note": "Connect Google to post."}"#)
        XCTAssertFalse(note.posted)
        XCTAssertEqual(note.shortfall, "Connect Google to post.")
        XCTAssertTrue(PushManager.approvePostShortfall(note)?.contains("Connect Google to post.") == true)
        XCTAssertNotNil(try outcome(#"{"ok": true, "post_status": "not_google"}"#).shortfall)
        XCTAssertNotNil(try outcome(#"{"ok": true, "auto_posted": false, "post_error": "Google said no"}"#).shortfall,
                        "an older server's answer is read too")
        let approve = PushManager.backgroundAction(for: PushManager.approvePostAction, cavnar: ["review_id": 4])
        XCTAssertEqual(approve?.postsReply, true)
    }

    // MARK: S follow-ups — schedule changes, the publish gate, the person record

    func testAChangedSentWeekTellsOnlyThePeopleWhoseShiftsMoved() {
        XCTAssertEqual(LaborViewModel.unsentNotice([]), "Saved; nobody's shifts changed.")
        XCTAssertEqual(LaborViewModel.unsentNotice(["Ana"]),
                       "Saved. Ana hasn\u{2019}t been told \u{2014} Send tells only them.")
        XCTAssertTrue(PendingSendAttributes.countdownKinds.contains("schedule_changes_send"))
        XCTAssertEqual(PendingSendAttributes.plainTitle(kind: "schedule_changes_send"), "Changes to the sent schedule")
    }

    func testThePublishNamesTheWeekAndTheBlockersItAcknowledges() throws {
        let gate = try JSONDecoder().decode(PublishScheduleViewModel.GateResponse.self, from: Data("""
            {"needs_ack": true, "blockers": ["old"], "blocker_keys": ["k1", "k2"],
             "blocker_items": [{"key": "k1", "text": "2 rows need review"}, {"key": "k2", "text": "A rule break"}],
             "schedule_id": 44}
            """.utf8))
        XCTAssertEqual(gate.shown.lines, ["2 rows need review", "A rule break"])
        XCTAssertEqual(gate.shown.keys, ["k1", "k2"])
        let body = PublishScheduleViewModel.PublishBody(scheduleId: 44, acknowledge: true, keys: ["k1", "k2"])
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual(json?["schedule_id"] as? Int, 44)
        XCTAssertEqual(json?["acknowledge"] as? [String], ["k1", "k2"])
        let plain = PublishScheduleViewModel.PublishBody(scheduleId: 44, acknowledge: false, keys: ["k1"])
        let plainJSON = try JSONSerialization.jsonObject(with: JSONEncoder().encode(plain)) as? [String: Any]
        XCTAssertEqual(plainJSON?["acknowledge"] as? Bool, false)
    }

    func testAPublishWithoutAWeekIsNeverSent() async {
        let vm = PublishScheduleViewModel(client: APIClient(baseURL: URL(string: "https://example.invalid")!))
        vm.scheduleId = nil
        await vm.publish()
        XCTAssertNotNil(vm.publishError)
        XCTAssertNil(vm.lastResult)
    }

    func testThePersonRecordSaysWhatThisLoginMayChange() throws {
        let person = try JSONDecoder().decode(PersonRecord.self, from: Data("""
            {"key": "dana-k", "name": "Dana K", "role": "Server", "can_edit": true,
             "pay_rate": {"role": "Server", "rate": 15.0, "source": "role"}, "pay_rate_amount": 16.5,
             "editable": {"role": false, "pay_rate": true}}
            """.utf8))
        XCTAssertEqual(person.payRate, 16.5)
        XCTAssertFalse(person.mayEdit("role", fallback: true))
        XCTAssertTrue(person.mayEdit("pay_rate", fallback: false))
        let vm = PersonSheetViewModel()
        vm.role = "Bartender"
        vm.payRate = "18.00"
        let changes = vm.changes(from: person)
        XCTAssertNil(changes["role"], "a field the server won't take is not sent")
        XCTAssertEqual(changes["pay_rate"], .double(18))
    }

    // MARK: DB follow-ups — the nightly report

    func testTomorrowsConfidenceWithoutAFigureKeepsTheCard() throws {
        let t = try JSONDecoder().decode(DSRTomorrow.self, from: Data("""
            {"weekday": "Saturday", "confidence": {"pct": null, "label": "\u{2014}",
             "based_on": ["Saturday history"], "missing": ["weather"], "watch": ["a Cubs game"]}}
            """.utf8))
        XCTAssertNil(t.confidence?.pct)
        XCTAssertEqual(t.confidence?.figure, "\u{2014}")
        XCTAssertEqual(t.confidence?.watch, ["a Cubs game"])
    }

    func testAnOpenPOSDayIsAskedAboutNotFailed() {
        let body = Data(#"{"ok": false, "code": "before_close", "needs_confirm": true, "error": "The POS is open."}"#.utf8)
        XCTAssertTrue(DSRCloseGate.isBeforeClose(APIClient.APIError(message: "x", status: 409, body: body)))
        XCTAssertFalse(DSRCloseGate.isBeforeClose(APIClient.APIError(message: "x", status: 400, body: body)))
    }

    func testARefusedRerunSaysWhy() throws {
        let c = try JSONDecoder().decode(DSRChecklist.self, from: Data("""
            {"status": "final", "rerun": {"reason": "Re-runs are for tonight or last night."}}
            """.utf8))
        XCTAssertEqual(c.rerun?.reason, "Re-runs are for tonight or last night.")
    }

    // MARK: F3-4 / F3-11 — every switch reaches RootView

    func testEverySwitchReportsItselfSoTheStacksReset() async {
        let store = SessionStore(client: APIClient(baseURL: URL(string: "https://example.com")!), storedToken: nil)
        var heard: [Int] = []
        store.onLocationSwitched = { heard.append($0) }
        await store.didSwitchLocation(to: 9, name: "Dallas")
        XCTAssertEqual(heard, [9])
        XCTAssertEqual(SessionScope.activeRestaurantId, 9)
    }

    // MARK: F3-5 — the person sheet tells the truth about pay and role

    func testPayRateArrivesAsTheRolesRateAndReadsAsOne() throws {
        let person = try JSONDecoder().decode(PersonRecord.self, from: Data("""
            {"key": "dana-k", "name": "Dana K", "role": "Server", "can_edit": false,
             "pay_rate": {"role": "Server", "rate": 15.0, "source": "role"}}
            """.utf8))
        XCTAssertEqual(person.payRate, 15.0)
        XCTAssertEqual(person.payRateLine, "$15.00/h \u{00B7} Server rate")
        XCTAssertEqual(person.canEdit, false)
        let blended = try JSONDecoder().decode(PersonRecord.self, from: Data("""
            {"key": "a", "name": "A", "pay_rate": {"role": null, "rate": null, "source": "blended"}}
            """.utf8))
        XCTAssertNil(blended.payRateLine, "no rate is Not set, never $0")
    }

    func testAFieldTheServerDidNotWriteIsNeverSaved() {
        XCTAssertEqual(PersonSheetViewModel.unsaved(sent: ["role", "phone"], changed: ["phone"]), ["role"])
        XCTAssertEqual(PersonSheetViewModel.unsaved(sent: ["role"], changed: ["job_title"]), [])
        XCTAssertEqual(PersonSheetViewModel.unsaved(sent: ["phone"], changed: nil), [], "an older server: trusted")
    }

    // MARK: F3-8 — the widget keeps each half on its own clock

    func testAFailedReadKeepsTheLastGoodHalfAndItsOwnTimestamp() throws {
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        let first = try XCTUnwrap(WidgetSnapshotService.merge(
            previous: nil, restaurantId: 3, restaurantName: nil,
            waiting: WidgetSnapshotService.WaitingPart(count: 5, replies: 2),
            night: WidgetSnapshotService.NightPart(date: "2026-09-24", label: "9/24/26", net: "$4,210"), now: t0))
        let later = t0.addingTimeInterval(3600)
        // The queue read failed: 5 waiting stays 5, not "Nothing waiting".
        let second = try XCTUnwrap(WidgetSnapshotService.merge(
            previous: first, restaurantId: 3, restaurantName: nil, waiting: nil,
            night: WidgetSnapshotService.NightPart(date: "2026-09-24", label: "9/24/26", net: "$4,210"), now: later))
        XCTAssertEqual(second.waitingCount, 5)
        XCTAssertEqual(second.waitingUpdatedAt, t0)
        XCTAssertEqual(second.nightUpdatedAt, later)
        XCTAssertFalse(second.waitingIsCurrent(now: t0.addingTimeInterval(7 * 3600)),
                       "a count hours old is not now's")
        XCTAssertTrue(second.nightIsCurrent(now: t0.addingTimeInterval(7 * 3600)))
        XCTAssertNil(WidgetSnapshotService.merge(previous: first, restaurantId: 3, restaurantName: nil,
                                                 waiting: nil, night: nil, now: later), "nothing new: nothing saved")
    }

    func testAnotherLocationsSnapshotIsNeverCarriedOver() throws {
        let a = try XCTUnwrap(WidgetSnapshotService.merge(
            previous: nil, restaurantId: 3, restaurantName: "Chicago", waiting: WidgetSnapshotService.WaitingPart(count: 5, replies: 0),
            night: WidgetSnapshotService.NightPart(date: "2026-09-24", label: "9/24/26", net: "$4,210"), now: Date()))
        let b = try XCTUnwrap(WidgetSnapshotService.merge(
            previous: a, restaurantId: 4, restaurantName: "Dallas", waiting: WidgetSnapshotService.WaitingPart(count: 1, replies: 0),
            night: nil, now: Date()))
        XCTAssertNil(b.netLabel, "Chicago's net must not show under Dallas")
        XCTAssertEqual(b.restaurantName, "Dallas")
        XCTAssertFalse(b.nightIsCurrent())
    }

    func testTheWidgetLinksLastNightByDate() {
        var snap = WidgetSnapshot.empty
        snap.nightDate = "2026-09-24"
        snap.updatedAt = Date()
        snap.waitingUpdatedAt = Date()
        XCTAssertEqual(snap.link(), "cavnarai://nav/dsr/night/2026-09-24")
        snap.waitingCount = 2
        XCTAssertEqual(snap.link(), "cavnarai://command")
    }

    // MARK: F3-9 — no Undo on a countdown that can't undo

    func testTheCountdownOffersUndoOnlyWhileItCanWork() {
        let now = Date()
        let ahead = PendingSendAttributes.ContentState(fireAt: now.addingTimeInterval(600), status: "pending", note: nil)
        XCTAssertTrue(ahead.offersUndo(isStale: false, now: now))
        XCTAssertFalse(ahead.offersUndo(isStale: true, now: now))
        let past = PendingSendAttributes.ContentState(fireAt: now.addingTimeInterval(-5), status: "pending", note: nil)
        XCTAssertFalse(past.offersUndo(isStale: false, now: now))
        let undoAction = PushManager.backgroundAction(for: PushManager.undoAction, cavnar: ["delayed_action_id": 7])
        XCTAssertEqual(undoAction?.cancelsActionId, 7, "a lock-screen Undo ends its countdown")
    }

    // MARK: F3-10 — notification rows

    private func rows(_ json: String) throws -> [NotificationItem] {
        try JSONDecoder().decode([NotificationItem].self, from: Data(json.utf8))
    }

    func testTwoAlertsInOneSecondAreTwoRows() throws {
        let r = try rows("""
            [{"id": 1, "type": "1star", "label": "1★", "fired_at": "2026-09-25 10:00:00", "review_id": 5},
             {"id": 2, "type": "1star", "label": "1★", "fired_at": "2026-09-25 10:00:00", "review_id": 6}]
            """)
        XCTAssertNotEqual(r[0].id, r[1].id)
    }

    func testUndoStopsOnlyTheSendTheRowIsAbout() throws {
        let r = try rows("""
            [{"id": 9, "type": "order_send_pending", "label": "Order", "fired_at": "2026-09-25 09:00:00"},
             {"id": 4, "type": "order_send_pending", "label": "Order", "fired_at": "2026-09-24 09:00:00"}]
            """)
        let pending = [PendingAction(id: 77, kind: "order_send", label: nil,
                                     executeAt: "2026-09-25T15:00:00Z", status: "pending")]
        XCTAssertEqual(NotificationsListViewModel.undoTarget(for: r[0], among: r, pending: pending, answered: false)?.id, 77)
        XCTAssertNil(NotificationsListViewModel.undoTarget(for: r[1], among: r, pending: pending, answered: false),
                     "yesterday's row must not stop today's order")
        let named = try rows("""
            [{"id": 4, "type": "order_send_pending", "label": "Order", "fired_at": "2026-09-24 09:00:00",
              "delayed_action_id": 12}]
            """)
        XCTAssertNil(NotificationsListViewModel.undoTarget(for: named[0], among: named, pending: pending, answered: false))
    }

    func testARowCarriesTheDraftItWouldPublish() throws {
        let r = try rows("""
            [{"id": 3, "type": "draft_ready", "label": "Reply ready", "fired_at": "2026-09-25 10:00:00",
              "review_id": 8, "can_approve": true, "draft": "Thanks, Sam!"}]
            """)
        XCTAssertEqual(r[0].canApprove, true)
        XCTAssertEqual(r[0].draft, "Thanks, Sam!")
    }

    // MARK: F3-12 — a link opens, it never acts

    func testALinkFillsInAQuestionAndOffersTheSwitcher() {
        let router = DeepLinkRouter()
        router.openFromLink(NavPath("ask?q=Email+my+staff")!)
        XCTAssertEqual(router.pendingAskPrompt, "Email my staff")
        XCTAssertFalse(router.pendingAskAutoSend, "a link fills the question in; the owner sends it")

        let switcher = DeepLinkRouter()
        var switched = false
        switcher.activeRestaurantId = { 2 }
        switcher.switchLocation = { _ in switched = true; return true }
        switcher.openFromLink(NavPath("location/7")!)
        XCTAssertTrue(switcher.pendingLocationPicker)
        XCTAssertFalse(switched)

        let url = URL(string: "cavnarai://nav/ask?q=hi")!
        guard case .link? = SystemEntry.destination(for: url).map(SystemEntry.fromLink) else {
            return XCTFail("a URL is a link")
        }
    }

    // MARK: F3-13 — lock-screen actions and the app's own lock

    func testAPasscodeKeepsOutwardActionsInTheApp() throws {
        let approve = try XCTUnwrap(PushManager.backgroundAction(for: PushManager.approvePostAction, cavnar: ["review_id": 4]))
        let undo = try XCTUnwrap(PushManager.backgroundAction(for: PushManager.undoAction, cavnar: ["delayed_action_id": 7]))
        XCTAssertTrue(PushManager.needsAppUnlock(approve, passcodeSet: true))
        XCTAssertFalse(PushManager.needsAppUnlock(approve, passcodeSet: false))
        XCTAssertFalse(PushManager.needsAppUnlock(undo, passcodeSet: true), "Undo is the safe direction")
    }

    // MARK: F3-14 — a swipe holds the bulk bar's recency too

    func testAnOldDraftHasNoSwipe() throws {
        func review(_ date: String) throws -> Review {
            try JSONDecoder().decode(Review.self, from: Data("""
                {"id": 1, "platform": "google", "author": "Al", "rating": 5, "text": "Great",
                 "review_date": "\(date)", "response_status": "drafted", "draft_response": "Thanks",
                 "urgency": "normal", "categories": []}
                """.utf8))
        }
        let now = ISO8601DateFormatter().date(from: "2026-09-25T12:00:00Z")!
        XCTAssertTrue(ReviewsListViewModel.canQuickApprove(try review("2026-09-20"), now: now))
        XCTAssertFalse(ReviewsListViewModel.canQuickApprove(try review("2026-06-01"), now: now))
    }

    // MARK: F3-15 — paths the server sends that iOS used to drop

    func testPastChatsAccountSectionsAndLastNightOpenWhereTheyPoint() {
        let chat = DeepLinkRouter()
        chat.open(NavPath("ask?conversation=31")!)
        XCTAssertEqual(chat.pendingAskConversation, 31)
        XCTAssertNil(chat.pendingAskPrompt)

        let account = DeepLinkRouter()
        account.open(NavPath("account/security")!)
        XCTAssertEqual(account.pendingTab, .account)
        XCTAssertEqual(account.consumePendingAccountSection(), "security")
        XCTAssertEqual(AccountLinkSection("integrations"), .connections)
        XCTAssertEqual(AccountLinkSection("people"), .team)
        XCTAssertNil(AccountLinkSection("appearance"))

        XCTAssertEqual(DeepLinkRouter.dailyReportRoute(NavPath("dsr")!), .report(date: nil),
                       "bare dsr is last night's report, as on the web")
        XCTAssertEqual(DeepLinkRouter.dailyReportRoute(NavPath("dsr/night/2026-09-24")!), .report(date: "2026-09-24"))
        XCTAssertEqual(DeepLinkRouter.dailyReportRoute(NavPath("dsr/week")!), .week(date: nil))
        XCTAssertEqual(DeepLinkRouter.dailyReportRoute(NavPath("dsr/list")!), .list)
    }

    // MARK: F3-17 — hours are read in every stored form, and kept

    func testStoredTimesInAnyFormAreRead() {
        XCTAssertEqual(HoursFormat.parse("11:00am")?.hour, 11)
        XCTAssertEqual(HoursFormat.parse("11am")?.hour, 11)
        XCTAssertEqual(HoursFormat.parse("9:30 PM")?.hour, 21)
        XCTAssertEqual(HoursFormat.parse("23:00")?.hour, 23)
        XCTAssertEqual(HoursFormat.parse("12am")?.hour, 0)
        XCTAssertNil(HoursFormat.parse("late"))
    }

    func testAnUntouchedDayIsSavedExactlyAsStored() {
        let days = ["Monday", "Tuesday"]
        let drafts = HoursDayDraft.drafts(days: days, opens: ["Tuesday": "late"],
                                          closes: ["Monday": "10pm", "Tuesday": "23:00"])
        XCTAssertEqual(drafts["Monday"]?.closed, false, "a close-only day is open, not closed")
        XCTAssertEqual(drafts["Tuesday"]?.closed, false, "an unreadable time is unknown, not closed")
        let (open, close) = HoursDayDraft.payload(days: days, drafts: drafts)
        XCTAssertNil(open["Monday"], "no invented 11:00am")
        XCTAssertEqual(open["Tuesday"], "late")
        XCTAssertEqual(close, ["Monday": "10pm", "Tuesday": "23:00"])
    }

    func testClosuresReadMDYY() {
        XCTAssertEqual(AccountHoursSheet.closureLabel("2026-09-25"), "Fri, 9/25/26")
    }

    // MARK: F3-18 — the passcode lockout can't be skipped by the clock

    func testTheLockoutRunsOnAClockSettingsCannotMove() {
        // Same boot: the wall clock wound a day forward changes nothing.
        XCTAssertEqual(AppPasscode.remaining(until: 1_000 + 120, penalty: 120, startedMono: 50,
                                             nowWall: 1_000 + 86_400, nowMono: 80), 90)
        // After a reboot: the wall deadline, never more than the penalty.
        XCTAssertEqual(AppPasscode.remaining(until: 1_000 + 120, penalty: 120, startedMono: 500,
                                             nowWall: 0, nowMono: 10), 120)
    }

    // MARK: D3-13 — the report reads as the web does

    func testUrgencyAndEffortUseTheWebsWords() throws {
        let a = try JSONDecoder().decode(DSRAction.self, from: Data("""
            {"text": "x", "urgency": "before_service", "effort": "low", "effort_source": "model"}
            """.utf8))
        XCTAssertEqual(a.urgencyLabel, "Today")
        XCTAssertEqual(a.effortLabel, "low effort (Cavnar AI\u{2019}s estimate)")
    }

    func testAWithheldBlockIsSaidAndAMissingOneIsNotCollected() throws {
        let manager = try JSONDecoder.cavnar.decode(DSRReport.self, from: DailyReportDecodingTests.payload("manager"))
        let names = manager.displayedBlocks.map(\.name)
        XCTAssertFalse(names.contains("food"), "withheld, not missing")
        XCTAssertEqual(manager.withheldLine?.hasPrefix("Not part of your view: Food."), true)
        var obj = try JSONSerialization.jsonObject(with: DailyReportDecodingTests.payload("manager")) as! [String: Any]
        var facts = obj["facts"] as! [String: Any]
        var blocks = facts["blocks"] as! [String: Any]
        blocks["intel"] = nil
        facts["blocks"] = blocks
        obj["facts"] = facts
        let missing = try JSONDecoder.cavnar.decode(DSRReport.self, from: JSONSerialization.data(withJSONObject: obj))
        let intel = missing.displayedBlocks.first { $0.name == "intel" }?.block
        XCTAssertEqual(intel?.reason, "Not collected for this night")
    }

    func testAManagersWeekHasNoGrossOrLaborColumnOfDashes() throws {
        let url = Bundle(for: Self.self).url(forResource: "dsr_week", withExtension: "json")!
        let all = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        var week = (all["manager"] as! [String: Any])
        var grid = week["week"] as! [String: Any]
        grid["withheld"] = ["budget", "gross", "labor"]
        week["week"] = grid
        let g = try JSONDecoder.cavnar.decode(DSRWeekResponse.self,
                                              from: JSONSerialization.data(withJSONObject: week)).week
        let titles = DSRWeekTable(grid: g).columns.map(\.title)
        XCTAssertFalse(titles.contains("Gross"))
        XCTAssertFalse(titles.contains("Labor %"))
        XCTAssertTrue(DSRWeekTable(grid: g).rows.allSatisfy { $0.cells.count == titles.count })
    }

    func testTomorrowIsHeadedWithItsDate() throws {
        let t = try JSONDecoder().decode(DSRTomorrow.self, from: Data("""
            {"date": "2026-09-26", "weekday": "Saturday"}
            """.utf8))
        XCTAssertEqual(t.heading, "Tomorrow \u{00B7} Saturday \u{00B7} 9/26/26")
    }
}
