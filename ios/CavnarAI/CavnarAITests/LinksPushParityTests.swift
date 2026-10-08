import XCTest
@testable import CavnarAI

/// iOS parity fix round, 10/7/26 — links, pushes and deep links
/// (#1, #22, #35, #36, #49, #54, #55, #81). Pure logic and request-body
/// shapes only; nothing here makes a request.
@MainActor
final class LinksPushParityTests: XCTestCase {

    // MARK: #1 — the dashboard's links open the app

    private func linkPath(_ s: String) -> String? {
        guard let url = URL(string: s), case .nav(let p)? = SystemEntry.destination(for: url) else { return nil }
        return p.raw
    }

    func testEveryLinkTheServerSendsOpensItsPlace() {
        // notify.alert_url / morning_brief / reporter: /?nav=<path>&loc=<id>
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?nav=labor%2Fovertime&loc=4"), "labor/overtime")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?nav=issue/12&loc=3"), "issue/12")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?nav=reviews%3Ffilter%3Dpending"), "reviews?filter=pending")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?review=88"), "review/88")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/#labor/requests"), "labor/requests")
        // An older sender's ?tab= — the web's tab ids.
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?tab=account"), "account")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?tab=competitor"), "intel")
        XCTAssertEqual(linkPath("https://dashboard.cavnar.ai/?tab=food"), "inventory")
    }

    func testAnAskLinkFillsTheQuestionIn() throws {
        let url = try XCTUnwrap(URL(string: "https://dashboard.cavnar.ai/?ask=Why%20was%20Friday%20slow%3F&rec=k&src=alert_email"))
        guard case .nav(let p)? = SystemEntry.destination(for: url) else { return XCTFail("an ask link opens Ask") }
        XCTAssertEqual(p.head, "ask")
        XCTAssertEqual(p.query["q"], "Why was Friday slow?")
        let router = DeepLinkRouter()
        router.openFromLink(p)
        XCTAssertEqual(router.pendingAskPrompt, "Why was Friday slow?")
        XCTAssertFalse(router.pendingAskAutoSend, "a link fills the question in; it never sends it")
    }

    func testPagesThatAreNotTheDashboardStayWeb() {
        for s in ["https://dashboard.cavnar.ai/login?nav=labor", "https://dashboard.cavnar.ai/s/abc",
                  "https://dashboard.cavnar.ai/reset-password/x", "https://dashboard.cavnar.ai/admin?nav=x",
                  "https://dashboard.cavnar.ai/i/token"] {
            XCTAssertNil(SystemEntry.destination(for: URL(string: s)!), s)
        }
    }

    func testALinkCarriesItsLocationAndRecommendation() throws {
        let url = try XCTUnwrap(URL(string:
            "https://dashboard.cavnar.ai/?nav=labor%2Fovertime&loc=4&rec=labor_over%3Asat&src=alert_email&rid=4"))
        let ctx = SystemEntry.linkContext(for: url)
        XCTAssertEqual(ctx, LinkContext(location: 4, rec: "labor_over:sat", src: "alert_email", rid: 4))
        // A recommendation link without loc= names its location by rid=.
        let ask = try XCTUnwrap(URL(string: "https://dashboard.cavnar.ai/?ask=hi&rec=k&src=weekly_email&rid=9"))
        XCTAssertEqual(SystemEntry.linkContext(for: ask).location, 9)
        XCTAssertTrue(SystemEntry.linkContext(for: URL(string: "cavnarai://nav/review/4")!).isEmpty)
        guard case .link(_, let carried)? = SystemEntry.destination(for: url)
            .map({ SystemEntry.fromLink($0, context: ctx) }) else { return XCTFail("a URL is a link") }
        XCTAssertEqual(carried, ctx)
    }

    func testTheLinkOpenIsRecordedWithTheWebsBody() throws {
        let body = try XCTUnwrap(DeepLinkRouter.linkOpenBody(LinkContext(location: 4, rec: "k", src: "alert_sms", rid: 4)))
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual(json?["rec"] as? String, "k")
        XCTAssertEqual(json?["src"] as? String, "alert_sms")
        XCTAssertEqual(json?["rid"] as? Int, 4)
        XCTAssertNil(DeepLinkRouter.linkOpenBody(LinkContext(location: 4)))
    }

    func testALocationTheLoginDoesNotHaveIsIgnored() async throws {
        let router = DeepLinkRouter()
        var switched: [Int] = []
        router.activeRestaurantId = { 2 }
        router.switchLocation = { id in switched.append(id); return true }
        router.mayOpenLocation = { _ in false }
        router.openFromLink(NavPath("review/9")!, context: LinkContext(location: 7))
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(switched, [], "not this login's location: opened where the session is")
        XCTAssertEqual(router.pendingReviewID, 9)
    }

    func testALocationTheLoginHasIsSwitchedToFirst() async throws {
        let router = DeepLinkRouter()
        var switched: [Int] = []
        router.activeRestaurantId = { 2 }
        router.switchLocation = { id in switched.append(id); return true }
        router.mayOpenLocation = { $0 == 7 }
        router.openFromLink(NavPath("review/9")!, context: LinkContext(location: 7))
        try await Task.sleep(nanoseconds: 100_000_000)
        XCTAssertEqual(switched, [7])
        XCTAssertEqual(router.pendingReviewID, 9)
    }

    // MARK: #22 / #49 — the route carries the section and the item

    func testTheQuietNightPushLandsOnItsCardAndDraft() throws {
        let nav = try XCTUnwrap(NavPath("marketing/opportunities?card=slow_day%3ATuesday&post_draft_id=41"))
        let route = try XCTUnwrap(ModuleRoute.from(nav))
        XCTAssertEqual(route.key, "marketing")
        XCTAssertEqual(route.section, "opportunities")
        XCTAssertEqual(route.itemId, "41")
        XCTAssertEqual(route.nav?.query["card"], "slow_day:Tuesday")
        XCTAssertNil(ModuleRoute.from(NavPath("marketing/opportunities")!)?.itemId)
    }

    func testASectionItemRidesTheRoute() {
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/inbox/12")!)?.itemId, "12")
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/overtime")!)?.section, "overtime")
        XCTAssertNil(ModuleRoute.from(NavPath("labor/overtime")!)?.itemId)
        XCTAssertEqual(ModuleRoute.from(NavPath("intel/visibility/3")!)?.itemId, "3")
    }

    // MARK: #54 — a reply typed on the lock screen

    private func json(_ action: PushManager.BackgroundAction?) throws -> [String: Any] {
        let body = try XCTUnwrap(action?.body)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
    }

    func testATypedReplyIsSavedThenApprovedAsTyped() throws {
        let action = PushManager.backgroundAction(for: PushManager.replyTextAction, cavnar: ["review_id": 5],
                                                  userText: "  Thank you, Ann!  ")
        XCTAssertEqual(action?.savesDraft, PushManager.SavedDraft(path: "/mobile/api/reviews/5/save-draft",
                                                                 text: "Thank you, Ann!"))
        XCTAssertEqual(action?.path, "/mobile/api/reviews/5/approve")
        XCTAssertEqual(try json(action)["expected_draft"] as? String, "Thank you, Ann!")
        XCTAssertEqual(action?.postsReply, true)
        XCTAssertTrue(PushManager.needsAppUnlock(try XCTUnwrap(action), passcodeSet: true))
        // Nothing typed: nothing posts.
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.replyTextAction, cavnar: ["review_id": 5],
                                                  userText: "   "))
    }

    // MARK: #36 — Approve & post sends back the reply it showed

    func testApproveSendsTheReplyTheNotificationShowed() throws {
        let whole = PushManager.backgroundAction(for: PushManager.approvePostAction,
                                                 cavnar: ["review_id": 4, "draft": "Thanks!", "draft_complete": true])
        XCTAssertEqual(try json(whole)["expected_draft"] as? String, "Thanks!")
        // A clipped draft is not the text approved: no expected_draft.
        let clipped = PushManager.backgroundAction(for: PushManager.approvePostAction,
                                                   cavnar: ["review_id": 4, "draft": "Thanks…", "draft_complete": false])
        XCTAssertNil(clipped?.body)
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.approvePostAction, cavnar: ["review_id": 4])?.body)
    }

    func testThePreviewShowsTheReplyAndSaysWhenItIsClipped() {
        let full = NotificationPreview(title: "New 5★ review", body: "Lovely night", category: "CAVNAR_REVIEW_DRAFTED",
                                       userInfo: ["cavnar": ["draft": "Thank you!", "draft_complete": true]])
        XCTAssertEqual(full.kind, .reply)
        XCTAssertEqual(full.draft, "Thank you!")
        XCTAssertEqual(full.draftNote, "This is the reply Approve & post publishes.")
        let cut = NotificationPreview(title: "t", body: "b", category: "CAVNAR_REVIEW_DRAFTED",
                                      userInfo: ["cavnar": ["draft": "Thank…", "draft_complete": false]])
        XCTAssertFalse(cut.draftComplete)
        XCTAssertTrue(cut.draftNote?.contains("open it to read the rest") == true)
        let report = NotificationPreview(title: "Your daily report is ready", body: "Simple EJ's · Mon: $4,210 net.",
                                         category: "CAVNAR_DSR",
                                         userInfo: ["cavnar": ["type": "dsr", "business_date": "2026-10-06"]])
        XCTAssertEqual(report.kind, .report)
        XCTAssertEqual(report.night, "10/6/26")
        XCTAssertNil(report.draftNote)
        XCTAssertNil(NotificationPreview.mdy("yesterday"))
    }

    // MARK: #35 — Done / Not for us

    func testDoneAndNotForUsPostTheCardsBody() throws {
        let cavnar: [String: Any] = ["rec_key": "quiet_night:2026-10-09", "answerable": true,
                                     "surface": "alert_push", "module": "marketing"]
        let done = PushManager.backgroundAction(for: PushManager.recDoneAction, cavnar: cavnar)
        XCTAssertEqual(done?.path, "/mobile/api/recs/event")
        let d = try json(done)
        XCTAssertEqual(d["key"] as? String, "quiet_night:2026-10-09")
        XCTAssertEqual(d["event"] as? String, "completed")
        XCTAssertEqual(d["surface"] as? String, "alert_push")
        XCTAssertEqual(d["module"] as? String, "marketing")
        XCTAssertNil(d["kind"])
        let pass = try json(PushManager.backgroundAction(for: PushManager.recNotForUsAction, cavnar: cavnar))
        XCTAssertEqual(pass["event"] as? String, "dismissed")
        XCTAssertEqual(pass["kind"] as? String, "not_for_us")
        // An answer is not outward: the app passcode does not hold it.
        XCTAssertFalse(PushManager.needsAppUnlock(try XCTUnwrap(done), passcodeSet: true))
        // Not answerable, or no key: the button does nothing in the background.
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.recDoneAction,
                                                  cavnar: ["rec_key": "k", "answerable": false]))
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.recDoneAction, cavnar: ["answerable": true]))
        XCTAssertEqual(PushManager.recModule(["module": "inventory"]), "food")
        XCTAssertEqual(PushManager.recModule(["module": "competitor"]), "intel")
    }

    // MARK: #55 — the other actionable pushes

    func testSendNowAcknowledgesTheBlockersThePushNamed() throws {
        let action = PushManager.backgroundAction(for: PushManager.sendNowAction,
                                                  cavnar: ["schedule_id": 88, "blocker_keys": ["no_manager:fri", "ot:dana"]])
        XCTAssertEqual(action?.path, "/mobile/api/labor/publish-schedule")
        let body = try json(action)
        XCTAssertEqual(body["schedule_id"] as? Int, 88)
        XCTAssertEqual(body["acknowledge"] as? [String], ["no_manager:fri", "ot:dana"])
        XCTAssertTrue(PushManager.needsAppUnlock(try XCTUnwrap(action), passcodeSet: true))
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.sendNowAction, cavnar: ["schedule_id": 88]))
    }

    func testThisWasntMeNamesTheLogin() throws {
        let action = PushManager.backgroundAction(for: PushManager.notMeAction, cavnar: ["login_user_id": 7])
        XCTAssertEqual(action?.path, "/mobile/api/account/not-me")
        XCTAssertEqual(try json(action)["login_user_id"] as? Int, 7)
        XCTAssertEqual(action?.successTitle, "Signed out everywhere")
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.notMeAction, cavnar: [:]))
    }

    func testTheForegroundButtonsOpenTheirOwnPlace() {
        XCTAssertEqual(PushManager.foregroundNav(for: PushManager.draftOrderAction), "inventory/order")
        XCTAssertEqual(PushManager.foregroundNav(for: PushManager.reconnectAction), "account/integrations")
        XCTAssertEqual(PushManager.foregroundNav(for: PushManager.askLastNightAction), "ask")
        XCTAssertNil(PushManager.foregroundNav(for: "CAVNAR_OPEN"))
        XCTAssertEqual(PushManager.lastNightQuestion(["type": "dsr", "business_date": "2026-10-06"]),
                       "Walk me through the daily report for 10/6/26.")
        XCTAssertEqual(PushManager.lastNightQuestion([:]), "How did last night go?")
    }
}
