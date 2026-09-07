import XCTest
@testable import CavnarAI

/// Launch-time session validation, added Sep 7 2026 after a real incident:
/// a Debug build's dev-server token survived a Release reinstall (Keychain
/// persists across a reinstall with the same bundle id), so the app
/// skipped straight past login into a UI that could never load anything,
/// with no way back short of finding Sign Out on a screen that couldn't
/// load either. These pin the fix — only a confirmed server rejection
/// clears a stored session; offline/timeout leaves it be.
///
/// Uses SessionStore's `storedToken:` test initializer rather than real
/// Keychain: this XCTest bundle doesn't share a keychain-access-group with
/// a signed app, so a `Keychain.set` here doesn't read back within the
/// same process. That's a property of this test target, not of
/// Keychain.swift, which is exercised for real by every login on-device.
@MainActor
final class SessionStoreTests: XCTestCase {
    private func makeClient() -> APIClient {
        APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    /// SessionStore's init fires its validation Task on the actor without
    /// awaiting it — tests give it a moment to land rather than asserting
    /// on a race. A tiny, fixed sleep is fine here: MockURLProtocol answers
    /// synchronously, so this is generous, not fragile.
    private func settle() async {
        try? await Task.sleep(nanoseconds: 150_000_000)
    }

    func testNoStoredTokenNeverCallsTheNetwork() async {
        MockURLProtocol.requestHandler = { _ in
            XCTFail("should not have made a request with no stored token")
            throw URLError(.unknown)
        }
        let store = SessionStore(client: makeClient(), storedToken: nil)
        await settle()
        XCTAssertFalse(store.isAuthenticated)
    }

    func testValidStoredTokenPopulatesCurrentUserAndStaysLoggedIn() async {
        let user = User(id: 1, username: "will", email: "will@cavnar.ai", restaurantId: 1, role: "owner", isAdmin: true)
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/mobile/api/me")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer a-real-token")
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            let body = Data("""
            {"ok": true, "user": {"id": 1, "username": "will", "email": "will@cavnar.ai", "restaurant_id": 1, "role": "owner", "is_admin": true}}
            """.utf8)
            return (response, body)
        }
        let store = SessionStore(client: makeClient(), storedToken: "a-real-token")
        await settle()
        XCTAssertTrue(store.isAuthenticated)
        XCTAssertEqual(store.currentUser, user)
    }

    /// The exact incident: a token the production server has never heard
    /// of. mobile_login_required always tags this session_expired: true
    /// regardless of *why* the user lookup came back empty, so this is the
    /// same response shape a genuinely-expired token gets.
    func testForeignOrInvalidTokenIsClearedAndLogsOut() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!
            let body = Data("""
            {"ok": false, "error": "Your session expired — please log in again.", "session_expired": true}
            """.utf8)
            return (response, body)
        }
        let store = SessionStore(client: makeClient(), storedToken: "token-from-a-different-backend")
        await settle()
        XCTAssertFalse(store.isAuthenticated, "a token the server rejects must not leave the app in a permanently-authenticated, permanently-broken state")
        XCTAssertNil(store.currentUser)
        XCTAssertNotNil(store.lastError)
    }

    /// A 401 that omits the flag (some other rejection reason) must still
    /// not be trusted going forward — the narrowing is "don't clear on a
    /// network problem", not "only clear on the exact expired-session shape".
    func testRejectionWithoutSessionExpiredFlagStillClearsTheSession() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": false, "error": "Not authorized."}
            """.utf8))
        }
        let store = SessionStore(client: makeClient(), storedToken: "some-token")
        await settle()
        XCTAssertFalse(store.isAuthenticated)
    }

    /// A phone with no signal (or a slow hotspot) at the exact moment of
    /// launch must still open to whatever it last knew — this app's whole
    /// offline-first design depends on a transient network failure never
    /// being read as "your session is bad".
    func testOfflineAtLaunchLeavesAValidLookingSessionAlone() async {
        MockURLProtocol.requestHandler = { _ in
            throw URLError(.notConnectedToInternet)
        }
        let store = SessionStore(client: makeClient(), storedToken: "token-that-would-be-fine-if-only-we-could-ask")
        await settle()
        XCTAssertTrue(store.isAuthenticated, "a network failure must not be mistaken for a rejected session")
        XCTAssertNil(store.lastError)
    }

    func testTimeoutAtLaunchLeavesAValidLookingSessionAlone() async {
        MockURLProtocol.requestHandler = { _ in
            throw URLError(.timedOut)
        }
        let store = SessionStore(client: makeClient(), storedToken: "token-that-would-be-fine-if-only-we-could-ask")
        await settle()
        XCTAssertTrue(store.isAuthenticated)
    }
}
