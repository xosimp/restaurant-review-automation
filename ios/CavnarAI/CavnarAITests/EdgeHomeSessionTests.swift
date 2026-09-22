import XCTest
@testable import CavnarAI

/// What these protect: one restaurant's dashboard never reaching the next
/// person to use the phone, or the next location an owner switches to.
///
/// RootView owns a single HomeViewModel for the life of the process (so Home
/// survives the Face ID lock swap), and nothing resets it on sign-out or on
/// a location switch. These drive that same instance through the sequence a
/// shared back-office phone goes through — account A loads, A signs out (or
/// switches location), B's first load fails — and assert B never sees A's
/// numbers. XCTExpectFailure marks the confirmed CLIENT-2 / CLIENT-26 /
/// CLIENT-22 defects; each flips when fixed.
@MainActor
final class EdgeHomeSessionTests: XCTestCase {

    nonisolated private static func summary(restaurant: String) -> String {
        """
        {"username": "owner", "restaurant_name": "\(restaurant)", "location_name": null, "brand_color": null,
         "reviews_awaiting_approval": 0, "quiet_hours_active": false,
         "modules": [{"key": "labor", "label": "Labor", "icon": "labor", "status": "available", "kpi": null}],
         "needs_attention": [], "total_value_delivered": 0, "value_history": []}
        """
    }

    /// Which account the fake server is answering for, and whether /home
    /// is up. Everything else (/me, /logout) answers ok.
    private final class Server: @unchecked Sendable {
        private let lock = NSLock()
        private var _restaurant = "Account A Bistro"
        private var _homeStatus = 200
        private var _homeBody: String?
        var restaurant: String { get { lock.withLock { _restaurant } } set { lock.withLock { _restaurant = newValue } } }
        var homeStatus: Int { get { lock.withLock { _homeStatus } } set { lock.withLock { _homeStatus = newValue } } }
        var homeBody: String? { get { lock.withLock { _homeBody } } set { lock.withLock { _homeBody = newValue } } }
    }

    private func makeClient(_ server: Server) -> APIClient {
        EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/home":
                if server.homeStatus == 200 {
                    return EdgeHTTP.reply(request, 200, Self.summary(restaurant: server.restaurant))
                }
                return EdgeHTTP.reply(request, server.homeStatus,
                                      server.homeBody ?? #"{"ok": false, "error": "Service unavailable"}"#)
            case "/mobile/api/me":
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "user": {"id": 1, "username": "a", "email": "a@x.com", "restaurant_id": 7, "role": "owner", "is_admin": false}}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
            }
        }
    }

    override func setUp() async throws {
        SecureCache.purgeAll()
        await PendingWriteQueue.shared.clear()
    }

    override func tearDown() async throws {
        SecureCache.purgeAll()
        await PendingWriteQueue.shared.clear()
        await PendingWriteQueue.shared.setActiveRestaurant(nil)
        MockURLProtocol.requestHandler = nil
    }

    private func settle() async { try? await Task.sleep(nanoseconds: 200_000_000) }

    // MARK: CLIENT-2 / CLIENT-42 — sign-out

    func testAFirstLoadWithNothingCachedShowsAnError() async {
        // Pins the half that works: with nothing to show, a failure says so.
        let server = Server()
        server.homeStatus = 503
        let vm = HomeViewModel(client: makeClient(server))
        await vm.load()
        XCTAssertNil(vm.summary)
        XCTAssertNotNil(vm.errorMessage)
    }

    func testTheNextAccountNeverSeesThePreviousAccountsDashboardAfterSignOut() async {
        let server = Server()
        let client = makeClient(server)
        let home = HomeViewModel(client: client)
        let session = SessionStore(client: client, storedToken: "token-for-A")
        await settle()
        await home.load()
        XCTAssertEqual(home.summary?.restaurantName, "Account A Bistro")

        // A signs out on the shared phone; B signs in and B's first fetch
        // fails (the walk-in, a timeout, a paused account).
        await session.logout()
        XCTAssertFalse(session.isAuthenticated)
        server.restaurant = "Account B Grill"
        server.homeStatus = 503
        await home.load()

        XCTExpectFailure("CLIENT-2: RootView's HomeViewModel keeps A's summary across sign-out; B's failed load leaves it on screen", strict: true) {
            XCTAssertNotEqual(home.summary?.restaurantName, "Account A Bistro",
                              "another tenant's revenue and reviews must not render for B")
            XCTAssertNotNil(home.errorMessage, "B has nothing of their own to show, so B is told the load failed")
        }
    }

    func testSignOutClearsTheInMemoryLoadTimestamp() async {
        let server = Server()
        let client = makeClient(server)
        let home = HomeViewModel(client: client)
        let session = SessionStore(client: client, storedToken: "token-for-A")
        await settle()
        await home.load()
        XCTAssertNotNil(home.lastLoadedAt)
        await session.logout()
        server.homeStatus = 503
        await home.load()
        XCTExpectFailure("CLIENT-2: lastLoadedAt is still A's, so no staleness notice explains the numbers on screen", strict: true) {
            XCTAssertNil(home.lastLoadedAt)
        }
    }

    func testTheHomeCacheKeyIsScopedToTheUserAndRestaurant() throws {
        let home = HomeViewModel(client: makeClient(Server()))
        let cache = try XCTUnwrap(Mirror(reflecting: home).children.first { $0.label == "cache" }?.value)
        let key = try XCTUnwrap(Mirror(reflecting: cache).children.first { $0.label == "key" }?.value as? String)
        XCTExpectFailure("CLIENT-2: the Home cache key is the unscoped \"home.summary\", shared by every account on the device", strict: true) {
            XCTAssertNotEqual(key, "home.summary")
            XCTAssertTrue(key.contains { $0.isNumber }, "the key should carry the user and restaurant ids")
        }
    }

    func testALoadInFlightAtSignOutDoesNotRewriteTheCacheAfterThePurge() async {
        EdgeHeldURLProtocol.reset()
        defer { EdgeHeldURLProtocol.reset() }
        EdgeHeldURLProtocol.handler = { request in
            request.url?.path == "/mobile/api/home"
                ? EdgeHTTP.reply(request, 200, Self.summary(restaurant: "Account A Bistro"))
                : EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        EdgeHeldURLProtocol.shouldHold = { $0.url?.path == "/mobile/api/home" }
        let client = EdgeHeldURLProtocol.makeClient()
        let home = HomeViewModel(client: client)
        let session = SessionStore(client: client, storedToken: nil)
        let load = Task { await home.load() }
        await EdgeHTTP.waitUntil { EdgeHeldURLProtocol.held.value }
        await session.logout()               // purges SecureCache while A's fetch is in flight
        XCTAssertNil(SecureCache.read(key: "home.summary"), "the purge ran")
        EdgeHeldURLProtocol.release.signal()
        await load.value
        let written = SecureCache.read(key: "home.summary")
        XCTAssertTrue(EdgeHeldURLProtocol.held.value)
        XCTExpectFailure("CLIENT-2: an in-flight Home load re-saves the old account's summary after purgeAll()", strict: true) {
            XCTAssertNil(written, "nothing from the signed-out account may be written back to disk")
        }
    }

    // MARK: CLIENT-26 — location switch

    func testAFailedReloadAfterALocationSwitchDoesNotShowTheOldLocation() async {
        let server = Server()
        server.restaurant = "Chicago"
        let client = makeClient(server)
        let home = HomeViewModel(client: client)
        let session = SessionStore(client: client, storedToken: "token")
        await settle()
        await home.load()
        XCTAssertEqual(home.summary?.restaurantName, "Chicago")

        await session.didSwitchLocation(to: 9, name: "Dallas")
        server.restaurant = "Dallas"
        server.homeStatus = 504
        await home.load()

        XCTExpectFailure("CLIENT-26: the in-memory summary survives a location switch; a failed reload shows Chicago's numbers as Dallas", strict: true) {
            XCTAssertNotEqual(home.summary?.restaurantName, "Chicago")
        }
    }

    // MARK: CLIENT-22 — billing refusal hidden behind cached numbers

    func testAPausedAccountIsToldEvenWhenOldNumbersAreOnScreen() async {
        let server = Server()
        let home = HomeViewModel(client: makeClient(server))
        await home.load()
        XCTAssertNotNil(home.summary)
        server.homeStatus = 402
        server.homeBody = #"{"ok": false, "error": "Your subscription is paused.", "billing_inactive": true}"#
        await home.load()
        XCTExpectFailure("CLIENT-22: Home shows an error only when summary == nil, so a 402 hides behind stale numbers", strict: true) {
            XCTAssertEqual(home.errorMessage, "Your subscription is paused.")
        }
    }

    func testATransientFailureKeepsTheCachedDashboardWithoutAnError() async {
        // The intended offline-first behaviour for the SAME account: a
        // timeout keeps what is on screen and says nothing extra.
        let server = Server()
        let home = HomeViewModel(client: makeClient(server))
        await home.load()
        MockURLProtocol.requestHandler = { _ in throw URLError(.timedOut) }
        await home.load()
        XCTAssertEqual(home.summary?.restaurantName, "Account A Bistro")
        XCTAssertNil(home.errorMessage)
    }
}
