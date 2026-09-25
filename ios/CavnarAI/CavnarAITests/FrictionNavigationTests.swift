import XCTest
import UserNotifications
@testable import CavnarAI

/// Friction audit (9/25/26), iOS navigation and notifications: a push, a
/// card or a notification row opens the thing it is about (#3), the push's
/// buttons act from the lock screen (#22), "Ask about this" sends (#15),
/// the lock asks Face ID on its own after a minute's grace (#9), and the
/// review queue moves on to the next reply (#21).
@MainActor
final class FrictionNavigationTests: XCTestCase {

    // alert_type is left empty where routing is the point: a non-empty type
    // records the open through APIClient.shared, which is a real request.

    // MARK: - #3 destinations

    func testAReviewPathOpensThatReviewInsideReviews() {
        let router = DeepLinkRouter()
        router.open(NavPath("review/412")!)
        XCTAssertEqual(router.pendingTab, .modules)
        XCTAssertEqual(router.pendingModuleKey, "reviews")
        XCTAssertEqual(router.pendingReviewID, 412)
        XCTAssertEqual(router.pendingModuleRoute?.itemId, "412")
    }

    func testAFilterPathOpensTheInboxOnThatFilter() {
        let route = ModuleRoute.from(NavPath("reviews?filter=urgent")!)
        XCTAssertEqual(route?.key, "reviews")
        XCTAssertEqual(route?.filter, "urgent")
        XCTAssertEqual(ReviewInboxFilter(key: route?.filter), .urgent)
        XCTAssertEqual(ReviewInboxFilter(key: "pending"), .toApprove)
        XCTAssertEqual(ReviewInboxFilter(key: "to_approve"), .toApprove)
        XCTAssertNil(ReviewInboxFilter(key: "inbox"))
    }

    func testARequestOpensItsLaborSectionExpanded() {
        XCTAssertEqual(ModuleRoute.from(NavPath("request/shift-5")!)?.section, "requests")
        XCTAssertEqual(ModuleRoute.from(NavPath("request/shift-5")!)?.itemId, "5")
        XCTAssertEqual(ModuleRoute.from(NavPath("request/time_off-9")!)?.section, "timeoff")
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/overtime")!)?.section, "overtime")
        XCTAssertEqual(ModuleRoute.from(NavPath("schedule/88")!)?.section, "schedule")
    }

    func testAPendingSendOpensItsUndoSheetNotLabor() {
        let router = DeepLinkRouter()
        router.open(NavPath("action/9")!)
        XCTAssertEqual(router.pendingActionId?.id, 9)
        XCTAssertNil(router.pendingTab)
        XCTAssertNil(router.pendingModuleKey)
    }

    func testTheNightPathOpensThatReport() {
        let router = DeepLinkRouter()
        router.open(NavPath("dsr/night/2026-09-24")!)
        XCTAssertEqual(router.pendingTab, .home)
        XCTAssertEqual(router.pendingDailyReport, .report(date: "2026-09-24"))
    }

    func testAnUnknownHeadDegradesToHome() {
        let router = DeepLinkRouter()
        router.open(NavPath("something-new/7")!)
        XCTAssertEqual(router.pendingTab, .home)
        XCTAssertNil(router.pendingModuleKey)
    }

    func testAPushsNavWinsOverItsModule() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "labor",
                                     nav: "request/shift-5")
        XCTAssertEqual(router.pendingModuleRoute?.section, "requests")
        // An older payload with no nav still routes by module.
        let old = DeepLinkRouter()
        old.handleNotificationTap(alertType: "", reviewId: nil, module: "labor")
        XCTAssertEqual(old.pendingModuleKey, "labor")
        XCTAssertNil(old.pendingModuleRoute)
    }

    func testTheModulesTabConsumesTheFocusedRouteOnce() {
        let router = DeepLinkRouter()
        router.open(NavPath("labor/requests")!)
        let route = router.consumePendingModuleRoute(labelFor: { _ in "Labor" })
        // The route now carries the nav it came from (re-audit F3-7); the focus is the same.
        XCTAssertEqual(route?.key, "labor")
        XCTAssertEqual(route?.section, "requests")
        XCTAssertEqual(route?.nav?.raw, "labor/requests")
        XCTAssertNil(router.consumePendingModuleRoute(labelFor: { $0 }))
    }

    // MARK: - #15 Ask about this sends

    func testAnAskPathSendsAndABriefBodyTapOnlyFillsIn() {
        let router = DeepLinkRouter()
        router.open(NavPath("ask?q=Why+was+Friday+slow%3F")!)
        XCTAssertEqual(router.pendingAskPrompt, "Why was Friday slow?")
        XCTAssertTrue(router.pendingAskAutoSend)

        let body = DeepLinkRouter()
        body.handleNotificationTap(alertType: "", reviewId: nil, askPrompt: "How did lunch go?", module: "ask")
        XCTAssertEqual(body.pendingAskPrompt, "How did lunch go?")
        XCTAssertFalse(body.pendingAskAutoSend)

        let button = DeepLinkRouter()
        button.handleNotificationTap(alertType: "", reviewId: nil, askPrompt: "How did lunch go?",
                                     module: "ask", askAutoSend: true)
        XCTAssertTrue(button.pendingAskAutoSend)
    }

    // MARK: - #22 acting from the notification

    func testTheLockScreenButtonsCallTheirRoutes() {
        let approve = PushManager.backgroundAction(for: PushManager.approvePostAction, cavnar: ["review_id": 42])
        XCTAssertEqual(approve?.path, "/mobile/api/reviews/42/approve")
        XCTAssertNil(approve?.decision)

        let undo = PushManager.backgroundAction(for: PushManager.undoAction, cavnar: ["delayed_action_id": "7"])
        XCTAssertEqual(undo?.path, "/mobile/api/actions/7/cancel")

        let yes = PushManager.backgroundAction(for: PushManager.approveRequestAction,
                                               cavnar: ["request_id": 5, "request_kind": "shift"])
        XCTAssertEqual(yes?.path, "/mobile/api/labor/shift-requests/5/decide")
        XCTAssertEqual(yes?.decision, "approve")

        let no = PushManager.backgroundAction(for: PushManager.denyRequestAction,
                                              cavnar: ["request_id": 5, "request_kind": "time_off"])
        XCTAssertEqual(no?.path, "/mobile/api/labor/time-off/5/decide")
        XCTAssertEqual(no?.decision, "deny")
    }

    func testAButtonWithoutItsIdOnlyOpens() {
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.approvePostAction, cavnar: [:]))
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.undoAction, cavnar: ["review_id": 1]))
        XCTAssertNil(PushManager.backgroundAction(for: UNNotificationDefaultActionIdentifier, cavnar: ["review_id": 1]))
    }

    func testThePayloadsNavIsRead() {
        XCTAssertEqual(PushManager.nav(["nav": " review/3 "]), "review/3")
        XCTAssertNil(PushManager.nav(["nav": ""]))
        XCTAssertNil(PushManager.nav([:]))
    }

    func testANotificationRowNamesItsUndoableKind() throws {
        let row = try JSONDecoder().decode(NotificationItem.self, from: Data("""
            {"type": "schedule_publish_pending", "label": "Next week's schedule going out",
             "fired_at": "2026-09-25 14:00:00", "nav": "action/4", "restaurant_id": 2}
            """.utf8))
        XCTAssertEqual(row.undoableKind, "schedule_publish")
        XCTAssertEqual(row.nav, "action/4")
        XCTAssertEqual(row.restaurantId, 2)
    }

    // MARK: - #9 the lock

    func testANewInstallGetsAMinutesGraceAndAChoiceIsKept() {
        XCTAssertEqual(AppPreferences.initialLockDelay(stored: nil), 60)
        XCTAssertEqual(AppPreferences.initialLockDelay(stored: 0), 0)
        XCTAssertEqual(AppPreferences.initialLockDelay(stored: 300), 300)
    }

    // MARK: - #21 the review queue

    func testOnlyAReplyThatMayGoOutUnreadGetsTheSwipe() throws {
        func review(_ extra: String) throws -> Review {
            try JSONDecoder().decode(Review.self, from: Data("""
                {"id": 1, "platform": "google", "author": "Ann", "rating": 5, "text": "Lovely",
                 "response_status": "drafted", "draft_response": "Thanks!", "urgency": "normal",
                 "categories": [] \(extra)}
                """.utf8))
        }
        XCTAssertTrue(ReviewsListViewModel.canQuickApprove(try review("")))
        XCTAssertFalse(ReviewsListViewModel.canQuickApprove(try review(#", "draft_needs_review": true"#)))
        let urgent = try JSONDecoder().decode(Review.self, from: Data("""
            {"id": 2, "platform": "google", "author": "Bo", "rating": 1, "text": "Cold",
             "response_status": "drafted", "draft_response": "Sorry", "urgency": "high", "categories": []}
            """.utf8))
        XCTAssertFalse(ReviewsListViewModel.canQuickApprove(urgent))
    }

    // MARK: - #50 the daily report steps a night at a time

    func testAdjacentNightsStopAtToday() {
        XCTAssertEqual(DailyReportView.adjacentNight("2026-09-24", by: -1, today: "2026-09-25"), "2026-09-23")
        XCTAssertEqual(DailyReportView.adjacentNight("2026-09-24", by: 1, today: "2026-09-25"), "2026-09-25")
        XCTAssertNil(DailyReportView.adjacentNight("2026-09-25", by: 1, today: "2026-09-25"))
        XCTAssertEqual(DailyReportView.adjacentNight("2026-03-01", by: -1, today: "2026-09-25"), "2026-02-28")
        XCTAssertNil(DailyReportView.adjacentNight(nil, by: 1, today: "2026-09-25"))
    }
}
