import XCTest
@testable import CavnarAI

/// Re-audit A-7 / A-14: a notification opens the module the server names
/// (push.NOTIFICATION_MODULE, in every payload and history row), and a tap
/// on another location's alert switches there before routing.
@MainActor
final class DeepLinkRoutingTests: XCTestCase {

    // alert_type is left empty where routing is the point: a non-empty type
    // records the open through APIClient.shared, which is a real request.

    func testThePayloadsModuleWins() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "inventory")
        XCTAssertEqual(router.pendingTab, .modules)
        XCTAssertEqual(router.pendingModuleKey, "inventory")
    }

    func testTheWebsCompetitorModuleOpensIntel() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "competitor")
        XCTAssertEqual(router.pendingModuleKey, "intel")
    }

    func testAnAskModuleOpensTheAssistant() {
        let router = DeepLinkRouter()
        router.handleNotificationTap(alertType: "", reviewId: nil, module: "ask")
        XCTAssertEqual(router.pendingTab, .ask)
        XCTAssertNil(router.pendingModuleKey)
    }

    func testTheFallbackMirrorsTheServersMap() {
        XCTAssertEqual(DeepLinkRouter.webModule(for: "critical_low"), "inventory")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "price_spike"), "inventory")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "order_send_pending"), "inventory")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "coverage"), "labor")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "shift_request"), "labor")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "competitor_move"), "competitor")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "demand_opportunity"), "marketing")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "daily_briefing"), "ask")
        XCTAssertEqual(DeepLinkRouter.webModule(for: "1star"), "reviews")
    }

    func testAnotherLocationsAlertSwitchesBeforeRouting() async {
        let router = DeepLinkRouter()
        var switchedTo: Int?
        router.activeRestaurantId = { 2 }
        router.switchLocation = { target in switchedTo = target; return true }
        router.handleNotificationTap(alertType: "", reviewId: 42, module: "reviews", restaurantId: 7)
        for _ in 0..<50 where router.pendingReviewID == nil {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTAssertEqual(switchedTo, 7)
        // Home's reload is no longer the router's to trigger: SessionStore
        // calls onLocationSwitched for every switch and RootView resets
        // (F3-4 / F3-11), so a stubbed switch leaves the counter alone.
        XCTAssertEqual(router.pendingReviewID, 42)
    }

    func testThisLocationsAlertDoesNotSwitch() {
        let router = DeepLinkRouter()
        var switched = false
        router.activeRestaurantId = { 7 }
        router.switchLocation = { _ in switched = true; return true }
        router.handleNotificationTap(alertType: "", reviewId: 42, module: "reviews", restaurantId: 7)
        XCTAssertFalse(switched)
        XCTAssertEqual(router.pendingReviewID, 42)
    }
}
