import XCTest
@testable import CavnarAI

/// Parity-round follow-ups (10/7/26): the admin platform sheet, the
/// coverage-only cover button, the drafted push while watching, the shift
/// permission, Marketing's three-tab back, its dates, and the bell's approve.
@MainActor
final class ParityFollowupTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: 1 — the platform alert opens its own sheet, admins only

    func testThePlatformAlertPayloadReadsItsLinesAndTime() throws {
        let alert = PlatformAlert(cavnar: [
            "alert_type": "platform_alert", "nav": "admin/platform", "subject": "Scheduler stopped",
            "lines": ["No tick for 12 minutes.", "  ", "Jobs are overdue.", 4],
            "alert_at": "2026-10-07T14:05:00Z"])
        XCTAssertEqual(alert.subject, "Scheduler stopped")
        XCTAssertEqual(alert.lines, ["No tick for 12 minutes.", "Jobs are overdue."])
        let chicago = try XCTUnwrap(TimeZone(identifier: "America/Chicago"))
        XCTAssertEqual(alert.sentLine(in: chicago), "Sent 10/7/26 \u{00B7} 9:05am")
        XCTAssertEqual(PlatformAlert.consoleURL.absoluteString, "https://dashboard.cavnar.ai/admin#operations")
        // Nothing usable: no subject, no lines, no time — never a raw stamp.
        let empty = PlatformAlert(cavnar: ["alert_at": "soon"])
        XCTAssertNil(empty.subject)
        XCTAssertNil(empty.sentLine())
    }

    func testAnAdminTapOpensThePlatformSheet() throws {
        let router = DeepLinkRouter()
        router.isAdminSession = { true }
        let alert = PlatformAlert(subject: "Down", lines: ["x"], alertAt: nil)
        router.handleNotificationTap(alertType: "platform_alert", reviewId: nil, module: "home",
                                     nav: "admin/platform", platformAlert: alert)
        XCTAssertEqual(router.pendingPlatformAlert, alert)
        XCTAssertNil(router.pendingTab)
        XCTAssertNil(router.pendingModuleKey)
    }

    func testANonAdminNeverGetsThePlatformSheet() throws {
        let router = DeepLinkRouter()
        router.isAdminSession = { false }
        router.handleNotificationTap(alertType: "platform_alert", reviewId: nil, module: "home",
                                     nav: "admin/platform",
                                     platformAlert: PlatformAlert(subject: "Down", lines: [], alertAt: nil))
        XCTAssertNil(router.pendingPlatformAlert)
        XCTAssertEqual(router.pendingTab, .home)
        // Held while /me is unanswered, then dropped once it says "not admin".
        let cold = DeepLinkRouter()
        cold.isAdminSession = { nil }
        cold.open(try XCTUnwrap(NavPath("admin/platform")))
        XCTAssertNotNil(cold.pendingPlatformAlert)
        cold.dropPlatformAlertForNonAdmin()
        XCTAssertNil(cold.pendingPlatformAlert)
        XCTAssertEqual(cold.pendingTab, .home)
    }

    func testAPlatformAlertWithNoNavStillOpensItsSheet() {
        let router = DeepLinkRouter()
        router.isAdminSession = { true }
        router.handleNotificationTap(alertType: "platform_alert", reviewId: nil, module: "home")
        XCTAssertNotNil(router.pendingPlatformAlert)
        XCTAssertNil(router.pendingModuleKey)
    }

    // MARK: 2 — Ask someone to cover only on a shift to cover

    func testOnlyTheCoverageCategoryAsksSomeoneToCover() throws {
        XCTAssertEqual(PushManager.coverageActions.map(\.identifier),
                       [PushManager.askCoverAction, PushManager.resolveIssueAction])
        XCTAssertEqual(PushManager.issueActions.map(\.identifier), [PushManager.resolveIssueAction])
        let src = try EdgeSource.read("Push/PushManager.swift")
        XCTAssertTrue(src.contains("\"CAVNAR_COVERAGE\""))
        XCTAssertTrue(src.contains("identifier: coverageCategory, actions: Self.coverageActions"))
        XCTAssertTrue(src.contains("identifier: issueCategory, actions: Self.issueActions"))
    }

    // MARK: 3 — no drafted banner over the week the owner is watching land

    func testTheDraftedBannerIsKeptDownWhileWatchingThatGeneration() {
        let push: [String: Any] = ["alert_type": "schedule_drafted", "schedule_id": 41, "job_id": "job-a"]
        var watch = ScheduleDraftWatch.Snapshot(laborOnScreen: true, pollingJobId: "job-a", onScreenScheduleId: nil)
        XCTAssertTrue(PushManager.suppressesBanner(push, watch: watch))
        // The poll already landed the week on screen.
        watch = .init(laborOnScreen: true, pollingJobId: nil, onScreenScheduleId: 41)
        XCTAssertTrue(PushManager.suppressesBanner(push, watch: watch))
        // Another generation, another week, or Labor not on screen: shown.
        watch = .init(laborOnScreen: true, pollingJobId: "job-b", onScreenScheduleId: 40)
        XCTAssertFalse(PushManager.suppressesBanner(push, watch: watch))
        watch = .init(laborOnScreen: false, pollingJobId: "job-a", onScreenScheduleId: 41)
        XCTAssertFalse(PushManager.suppressesBanner(push, watch: watch))
        // Never any other type.
        watch = .init(laborOnScreen: true, pollingJobId: "job-a", onScreenScheduleId: 41)
        XCTAssertFalse(PushManager.suppressesBanner(["alert_type": "labor_over", "schedule_id": 41, "job_id": "job-a"],
                                                    watch: watch))
    }

    func testAnEndedPollOnlyClearsItsOwnJob() {
        let watch = ScheduleDraftWatch()
        watch.pollingJobId = "job-b"
        watch.pollEnded("job-a")
        XCTAssertEqual(watch.pollingJobId, "job-b")
        watch.pollEnded("job-b")
        XCTAssertNil(watch.pollingJobId)
    }

    // MARK: 4 — the shift controls follow the server's SCHEDULE_DRAFT

    func testTheShiftRequestListDecodesCanDecide() throws {
        let yes = try decode(ScheduleSetupViewModel.RequestsResponse.self,
                             #"{"ok": true, "requests": [], "open": [], "offers": [], "can_decide": true}"#)
        XCTAssertTrue(ScheduleSetupViewModel.canDecide(yes))
        let no = try decode(ScheduleSetupViewModel.RequestsResponse.self,
                            #"{"ok": true, "requests": [], "open": [], "can_decide": false}"#)
        XCTAssertFalse(ScheduleSetupViewModel.canDecide(no))
        // An older server says nothing: no controls it may refuse.
        let old = try decode(ScheduleSetupViewModel.RequestsResponse.self, #"{"ok": true, "requests": [], "open": []}"#)
        XCTAssertFalse(ScheduleSetupViewModel.canDecide(old))
        let src = try EdgeSource.read("Features/Labor/ShiftRequestsSection.swift")
        XCTAssertFalse(src.contains("canEditRoster"))
        XCTAssertTrue(src.contains("req.status == \"pending\" && viewModel.canDecideShifts"))
    }

    // MARK: 7 — back from any Marketing tab but Content returns to Content

    func testBackFromCampaignsOrAnalyticsReturnsToTheFirstTab() {
        let tabs = ["Content", "Campaigns", "Analytics"]
        XCTAssertEqual(CavnarTabSwipe.back(from: "Campaigns", tabs: tabs), "Content")
        XCTAssertEqual(CavnarTabSwipe.back(from: "Analytics", tabs: tabs), "Content")
        XCTAssertNil(CavnarTabSwipe.back(from: "Content", tabs: tabs))
        XCTAssertEqual(CavnarTabSwipe.next(from: "Content", tabs: tabs), "Campaigns")
        XCTAssertEqual(CavnarTabSwipe.next(from: "Campaigns", tabs: tabs), "Analytics")
        XCTAssertNil(CavnarTabSwipe.next(from: "Analytics", tabs: tabs))
        // Two tabs behave as before.
        XCTAssertEqual(CavnarTabSwipe.back(from: "Analytics", tabs: ["Overview", "Analytics"]), "Overview")
        XCTAssertEqual(CavnarTabSwipe.next(from: "Overview", tabs: ["Overview", "Analytics"]), "Analytics")
    }

    // MARK: 8 — a scheduled post's date is M/D/YY or "—"

    func testAScheduledPostNeverEchoesAnUnparseableStamp() {
        XCTAssertEqual(ScheduledPost.whenLabel("2026-09-06 11:00:00"), "9/6/26 \u{00B7} 11:00am")
        XCTAssertEqual(ScheduledPost.whenLabel("2026-09-06T18:30"), "9/6/26 \u{00B7} 6:30pm")
        XCTAssertEqual(ScheduledPost.whenLabel("2026-09-06"), "9/6/26")
        XCTAssertEqual(ScheduledPost.whenLabel("2026-09-06Tsoon-ish"), "9/6/26")
        XCTAssertEqual(ScheduledPost.whenLabel("next Tuesday"), "\u{2014}")
        XCTAssertEqual(ScheduledPost.whenLabel(""), "\u{2014}")
        XCTAssertEqual(ScheduledPost.whenLabel("2026-13-40 10:00"), "\u{2014}")
    }

    // MARK: 10 — the bell's approve names the reply the row showed

    func testTheBellApproveSendsTheDraftAndItsHash() throws {
        let whole = try decode(NotificationItem.self, """
            {"type": "no_response", "label": "Reply waiting", "fired_at": "2026-10-07 12:00:00",
             "review_id": 9, "priority": 2, "urgent": false, "module": "reviews", "can_approve": true,
             "draft": "Thanks, Ann!", "draft_complete": true, "draft_hash": "abc123"}
            """)
        XCTAssertEqual(whole.draftComplete, true)
        let body = try object(NotificationsListViewModel.approveBody(whole))
        XCTAssertEqual(body["expected_draft"] as? String, "Thanks, Ann!")
        XCTAssertEqual(body["expected_draft_hash"] as? String, "abc123")

        let clipped = try decode(NotificationItem.self, """
            {"type": "no_response", "label": "Reply waiting", "fired_at": "2026-10-07 12:00:00",
             "review_id": 9, "priority": 2, "urgent": false, "module": "reviews", "can_approve": true,
             "draft": "Thank you…", "draft_complete": false, "draft_hash": "def456"}
            """)
        // Never the clipped text as the reply approved.
        let cbody = try object(NotificationsListViewModel.approveBody(clipped))
        XCTAssertNil(cbody["expected_draft"])
        XCTAssertEqual(cbody["expected_draft_hash"] as? String, "def456")
        // And a clipped reply is not approved from the row at all.
        let vm = NotificationsListViewModel()
        XCTAssertTrue(vm.approvable(whole))
        XCTAssertFalse(vm.approvable(clipped))
    }
}
