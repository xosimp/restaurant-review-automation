import XCTest
import UserNotifications
@testable import CavnarAI

// MARK: - Shared helpers for the Edge*Tests files

/// Reads the app's own Swift sources, for the handful of edge cases whose
/// only seam is the source itself (a view modifier, an entitlement-level
/// attribute, a singleton that must not be duplicated). The simulator test
/// process can read the host filesystem, and `#filePath` pins the location at
/// compile time, so this never depends on the working directory.
///
/// `read` throws when the file is missing or empty. Call it OUTSIDE any
/// XCTExpectFailure block: a missing file must fail the test loudly, not be
/// swallowed as the expected failure.
enum EdgeSource {
    static var appRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()          // CavnarAITests/
            .deletingLastPathComponent()          // ios/CavnarAI/
            .appendingPathComponent("CavnarAI")   // the app target's sources
    }

    struct Missing: Error, CustomStringConvertible {
        let path: String
        var description: String { "source file not found or empty: \(path)" }
    }

    static func read(_ relative: String) throws -> String {
        let url = appRoot.appendingPathComponent(relative)
        guard let text = try? String(contentsOf: url, encoding: .utf8), !text.isEmpty else {
            throw Missing(path: url.path)
        }
        return text
    }

    /// Every Swift file in the app target, as (relative path, contents).
    static func allSwiftFiles() throws -> [(String, String)] {
        let root = appRoot
        guard let walker = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil) else {
            throw Missing(path: root.path)
        }
        var out: [(String, String)] = []
        for case let url as URL in walker where url.pathExtension == "swift" {
            let rel = String(url.path.dropFirst(root.path.count + 1))
            out.append((rel, try read(rel)))
        }
        if out.count < 50 { throw Missing(path: root.path + " (found only \(out.count) swift files)") }
        return out
    }

    /// The text of `source` from the first occurrence of `start` for
    /// `length` characters — enough to see a view's modifier chain.
    static func slice(_ source: String, from start: String, length: Int = 400) -> String? {
        guard let r = source.range(of: start) else { return nil }
        let end = source.index(r.lowerBound, offsetBy: length, limitedBy: source.endIndex) ?? source.endIndex
        return String(source[r.lowerBound..<end])
    }
}

/// MockURLProtocol plumbing shared by the Edge* files.
enum EdgeHTTP {
    static func client(_ handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) -> APIClient {
        MockURLProtocol.requestHandler = handler
        return APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    static func reply(_ request: URLRequest, _ status: Int = 200, _ json: String) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                                       headerFields: ["Content-Type": "application/json"])!
        return (response, Data(json.utf8))
    }

    /// URLProtocol sees a POST body as a stream, not `httpBody`.
    static func body(_ request: URLRequest) -> Data? {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let n = stream.read(&buffer, maxLength: buffer.count)
            if n <= 0 { break }
            data.append(buffer, count: n)
        }
        return data
    }

    static func bodyJSON(_ request: URLRequest) -> [String: Any]? {
        guard let data = body(request) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    /// "POST /mobile/api/..." — for counting what actually went out.
    static func line(_ request: URLRequest) -> String {
        "\(request.httpMethod ?? "GET") \(request.url?.path ?? "")"
    }

    /// Waits (bounded) until `condition` holds — for a request to reach a
    /// handler that is deliberately holding it open.
    static func waitUntil(_ condition: () -> Bool, timeout: TimeInterval = 3) async {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try? await Task.sleep(nanoseconds: 10_000_000)
        }
    }
}

/// Like MockURLProtocol, but one chosen request can be HELD — its answer
/// delivered only when the test releases it — without blocking the loader
/// thread, so other requests keep flowing meanwhile. MockURLProtocol answers
/// on the loader thread itself; blocking there stalls every other request
/// in the process, which is not the race these tests need to stage.
final class EdgeHeldURLProtocol: URLProtocol {
    static var handler: (@Sendable (URLRequest) -> (HTTPURLResponse, Data))?
    static var shouldHold: (@Sendable (URLRequest) -> Bool)?
    static var release = DispatchSemaphore(value: 0)
    static let held = Box(false)

    static func reset() {
        handler = nil
        shouldHold = nil
        release = DispatchSemaphore(value: 0)
        held.value = false
    }

    static func makeClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [EdgeHeldURLProtocol.self]
        return APIClient(baseURL: URL(string: "https://example.com")!, session: URLSession(configuration: config))
    }

    private var reply: (HTTPURLResponse, Data)?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        reply = handler(request)
        guard Self.shouldHold?(request) == true else { deliver(); return }
        let thread = Thread.current
        let gate = Self.release
        Self.held.value = true
        DispatchQueue.global().async {
            _ = gate.wait(timeout: .now() + 5)
            self.perform(#selector(self.deliver), on: thread, with: nil, waitUntilDone: false,
                         modes: [RunLoop.Mode.common.rawValue])
        }
    }

    @objc private func deliver() {
        guard let (response, data) = reply else { return }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

// MARK: - iOS App Core edge cases (CLIENT audit: APIClient, SessionStore,
// the offline queue, Keychain, push, network monitor, staff sign-in)

/// What these protect: the app's transport and session layer under the
/// failures a restaurant phone actually sees — a deploy's 502 at launch, a
/// late 401 from a request that outlived its session, a billing refusal, a
/// pin rejection, a stream that dies — plus the push and offline-queue
/// plumbing that has no screen of its own. A test wrapped in
/// XCTExpectFailure asserts the CORRECT behaviour for a confirmed defect in
/// the CLIENT audit; it flips to a failure the day the defect is fixed, and
/// the wrapper is removed with the fix.
@MainActor
final class EdgeAppCoreTests: XCTestCase {

    private struct OKResponse: Codable { let ok: Bool }

    /// SessionStore's init fires its validation Task without awaiting it.
    private func settle(_ ns: UInt64 = 250_000_000) async {
        try? await Task.sleep(nanoseconds: ns)
    }

    override func setUp() async throws {
        await PendingWriteQueue.shared.clear()
    }

    override func tearDown() async throws {
        await PendingWriteQueue.shared.clear()
        await PendingWriteQueue.shared.setActiveRestaurant(nil)
        MockURLProtocol.requestHandler = nil
    }

    // MARK: Launch validation (CLIENT-4)

    private func launch(answering status: Int, body: String) async -> SessionStore {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, status, body) }
        let store = SessionStore(client: client, storedToken: "token-that-is-fine")
        await settle()
        return store
    }

    func testA500OnMeAtLaunchLeavesTheSessionAlone() async {
        let store = await launch(answering: 500, body: #"{"ok": false, "error": "Internal error"}"#)
        XCTAssertTrue(store.isAuthenticated, "a server error is not a rejected session")
    }

    func testA502DuringADeployLeavesTheSessionAlone() async {
        // Railway's edge answers 502 with an HTML page while the container
        // restarts — every iPhone opened during a deploy.
        let store = await launch(answering: 502, body: "<html><body>Bad Gateway</body></html>")
        XCTAssertTrue(store.isAuthenticated)
    }

    func testA429AtLaunchLeavesTheSessionAlone() async {
        let store = await launch(answering: 429, body: #"{"ok": false, "error": "Too many requests"}"#)
        XCTAssertTrue(store.isAuthenticated)
    }

    func testAnUndecodableMeBodyAtLaunchLeavesTheSessionAlone() async {
        // A 200 whose body is not the /me shape (a captive portal, a proxy
        // page) is not the server rejecting this token.
        let store = await launch(answering: 200, body: #"{"unexpected": true}"#)
        XCTAssertTrue(store.isAuthenticated)
    }

    func testAServerErrorAtLaunchKeepsTheOfflineQueue() async {
        await PendingWriteQueue.shared.setActiveRestaurant(7)
        await PendingWriteQueue.shared.enqueue(path: "/mobile/api/reviews/1/approve", method: "POST",
                                               bodyJSON: nil, label: "Approve made in the walk-in")
        _ = await launch(answering: 503, body: #"{"ok": false, "error": "Service unavailable"}"#)
        await settle(150_000_000)   // clearLocalSession empties the queue in its own Task
        let remaining = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(remaining, 1, "work queued offline must survive a server hiccup at launch")
    }

    func testAConfirmedRejectionAtLaunchStillSignsOut() async {
        // The other half of the rule: a real session_expired still clears it.
        let store = await launch(answering: 401,
                                 body: #"{"ok": false, "error": "expired", "session_expired": true}"#)
        XCTAssertFalse(store.isAuthenticated)
    }

    // MARK: Error classification (CLIENT-22, 23, 24, 50)

    func testA402IsToldApartFromAGenericServerError() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 402, #"{"ok": false, "error": "Your subscription is paused.", "billing_inactive": true}"#)
        }
        var caught: APIClient.APIError?
        do { let _: OKResponse = try await client.send("/mobile/api/home") } catch let e as APIClient.APIError { caught = e } catch {}
        let error = try? XCTUnwrap(caught)
        XCTAssertEqual(error?.status, 402)
        XCTAssertTrue(String(describing: error?.kind as Any).lowercased().contains("billing"),
                      "a paused account needs its own error kind so the app can show a billing banner")
    }

    func testA403ModuleLockedIsToldApartFromAGenericServerError() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 403, #"{"ok": false, "error": "Labor isn't on your plan.", "module_locked": true}"#)
        }
        var caught: APIClient.APIError?
        do { let _: OKResponse = try await client.send("/mobile/api/labor") } catch let e as APIClient.APIError { caught = e } catch {}
        XCTAssertEqual(caught?.message, "Labor isn't on your plan.")
        XCTAssertTrue(String(describing: caught?.kind as Any).lowercased().contains("module"))
    }

    func testAnExpiryForASupersededTokenDoesNotSignOutTheNewSession() async {
        // A long request started as user A finishes after the phone has
        // signed in as user B; its 401 describes A's token, not B's.
        let arrived = Box(false)
        let gate = DispatchSemaphore(value: 0)
        let client = EdgeHTTP.client { request in
            arrived.value = true
            _ = gate.wait(timeout: .now() + 5)
            return EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "expired", "session_expired": true}"#)
        }
        await client.setToken("token-for-user-A")
        let fired = Box(false)
        await client.setSessionExpiredHandler { fired.value = true }

        let inFlight = Task { () -> Void in
            let _: OKResponse? = try? await client.send("/mobile/api/marketing/generate", method: .post)
        }
        await EdgeHTTP.waitUntil { arrived.value }
        await client.setToken("token-for-user-B")
        gate.signal()
        await inFlight.value

        XCTAssertTrue(arrived.value)
        XCTExpectFailure("CLIENT-23: a late 401 from a superseded token fires onSessionExpired and logs out the new user", strict: true) {
            XCTAssertFalse(fired.value)
        }
    }

    func testAnExpiryForTheCurrentTokenStillFiresTheHandler() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "expired", "session_expired": true}"#)
        }
        await client.setToken("current")
        let fired = Box(false)
        await client.setSessionExpiredHandler { fired.value = true }
        let _: OKResponse? = try? await client.send("/mobile/api/home")
        XCTAssertTrue(fired.value)
    }

    func testAPinRejectionIsNotReportedAsACancellation() async {
        // PinnedSessionDelegate answers a bad chain with
        // .cancelAuthenticationChallenge, which URLSession surfaces as
        // URLError.cancelled on a task nobody cancelled.
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        var caught: Error?
        do { let _: OKResponse = try await client.send("/mobile/api/home") } catch { caught = error }
        XCTAssertNotNil(caught)
        XCTExpectFailure("CLIENT-24: an un-requested URLError.cancelled (pin rejection) is rethrown as a silent CancellationError", strict: true) {
            XCTAssertTrue(caught is APIClient.APIError,
                          "a secure-connection failure must reach the screen as an error, not be swallowed as a cancel")
        }
    }

    func testTheSessionExpiredErrorHasAFriendlyDescription() {
        let text = (APIClient.SessionExpiredError() as Error).localizedDescription
        XCTExpectFailure("CLIENT-50: SessionExpiredError isn't LocalizedError, so screens show \"The operation couldn't be completed\"", strict: true) {
            XCTAssertFalse(text.contains("SessionExpiredError"), text)
            XCTAssertFalse(text.lowercased().contains("operation couldn"), text)
        }
    }

    // MARK: Timeouts (CLIENT-20)

    func testALongCallSessionOutlastsTheGenerationTimeout() throws {
        // send(timeout: 90) only sets the per-request idle timeout; the
        // session's resource timeout (45s) still ends the whole transfer.
        let client = APIClient(baseURL: URL(string: "https://example.com")!)
        let sessions = Mirror(reflecting: client).children.compactMap { child -> (String, URLSession)? in
            guard let s = child.value as? URLSession else { return nil }
            return (child.label ?? "", s)
        }
        XCTAssertFalse(sessions.isEmpty, "APIClient's sessions should be reflectable")
        let upload = sessions.first { $0.0 == "uploadSession" }
        XCTAssertGreaterThanOrEqual(try XCTUnwrap(upload).1.configuration.timeoutIntervalForResource, 120,
                                    "uploads already have a long session")
        let generation = MarketingViewModel.generationTimeout
        let longEnough = sessions.filter {
            $0.0 != "uploadSession" && $0.1.configuration.timeoutIntervalForResource >= generation
        }
        XCTExpectFailure("CLIENT-20: every non-upload call is capped at the 45s resource timeout, below the 90s generation timeout", strict: true) {
            XCTAssertFalse(longEnough.isEmpty)
        }
    }

    // MARK: Ask stream classification (CLIENT-53)

    private func streamError(status: Int, body: String) async -> (Error?, Bool) {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, status, body) }
        let fired = Box(false)
        await client.setSessionExpiredHandler { fired.value = true }
        var caught: Error?
        do {
            for try await _ in await client.stream("/mobile/api/ask-cavnar/stream", body: ["question": "How was Friday?"]) {}
        } catch { caught = error }
        return (caught, fired.value)
    }

    func testAStream401WithoutTheSessionExpiredFlagDoesNotSignOut() async {
        let (_, fired) = await streamError(status: 401, body: #"{"ok": false, "error": "Not allowed."}"#)
        XCTExpectFailure("CLIENT-53: the Ask stream treats any 401 as session expiry", strict: true) {
            XCTAssertFalse(fired)
        }
    }

    func testAStream401WithTheSessionExpiredFlagStillSignsOut() async {
        let (error, fired) = await streamError(status: 401,
                                               body: #"{"ok": false, "error": "expired", "session_expired": true}"#)
        XCTAssertTrue(fired)
        XCTAssertTrue(error is APIClient.SessionExpiredError)
    }

    func testAStreamRefusalCarriesTheServersOwnMessage() async {
        let (error, _) = await streamError(status: 403,
                                           body: #"{"ok": false, "error": "Ask Cavnar isn't on your plan yet."}"#)
        XCTExpectFailure("CLIENT-53: 402/403 on the stream become \"Couldn't reach Cavnar AI\", dropping the server's reason", strict: true) {
            XCTAssertEqual((error as? APIClient.APIError)?.message, "Ask Cavnar isn't on your plan yet.")
        }
    }

    func testTheStreamAsksNetworkMonitorWhetherTheDeviceIsOffline() throws {
        let source = try EdgeSource.read("Core/APIClient.swift")
        let stream = try XCTUnwrap(EdgeSource.slice(source, from: "func stream<Body", length: 2400))
        XCTExpectFailure("CLIENT-53: stream() hard-codes deviceIsOffline: false, so an offline device is never told so", strict: true) {
            XCTAssertFalse(stream.contains("deviceIsOffline: false"))
        }
    }

    // MARK: Keychain (CLIENT-25)

    func testKeychainItemsAreBoundToThisDevice() throws {
        // Not testable at runtime here: this XCTest bundle shares no
        // keychain-access-group with the app, so a set does not read back
        // (see SessionStoreTests). The attribute is the whole fix.
        let source = try EdgeSource.read("Core/Keychain.swift")
        XCTAssertTrue(source.contains("kSecAttrAccessible"))
        XCTExpectFailure("CLIENT-25: Keychain uses AfterFirstUnlock, not ThisDeviceOnly, so backups carry sessions and 2FA tokens to a new phone", strict: true) {
            XCTAssertTrue(source.contains("ThisDeviceOnly"))
        }
    }

    func testKeychainWritesCheckTheirStatus() throws {
        let source = try EdgeSource.read("Core/Keychain.swift")
        XCTExpectFailure("CLIENT-25: SecItemAdd's status is ignored, so a failed write is silent", strict: true) {
            XCTAssertFalse(source.contains("SecItemAdd(attributes as CFDictionary, nil)\n"),
                           "the status of SecItemAdd should be read, not discarded")
        }
    }

    // MARK: Offline queue (CLIENT-5)

    func testAQueuedWriteSurvivesARelaunch() async {
        // Pins the relaunch mechanic the next test relies on: a fresh queue
        // instance restores what the last one persisted.
        await PendingWriteQueue.shared.setActiveRestaurant(9)
        await PendingWriteQueue.shared.enqueue(path: "/mobile/api/reviews/2/approve", method: "POST",
                                               bodyJSON: nil, label: "Approve")
        let relaunched = PendingWriteQueue()
        let count = await relaunched.pendingCount
        XCTAssertEqual(count, 1)
    }

    func testSwitchingLocationDoesNotDeleteTheQueuedWritesForTheNewLocation() async {
        // didSwitchLocation persists the trimmed queue, then purges every
        // SecureCache file — pending-writes.json included.
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, 200, #"{"ok": true}"#) }
        let store = SessionStore(client: client, storedToken: nil)
        await PendingWriteQueue.shared.setActiveRestaurant(9)
        await PendingWriteQueue.shared.enqueue(path: "/mobile/api/reviews/3/approve", method: "POST",
                                               bodyJSON: nil, label: "Dallas approve")
        await store.didSwitchLocation(to: 9, name: "Dallas")
        let inMemory = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(inMemory, 1, "a write for the location being switched to is kept in memory")
        let relaunched = await PendingWriteQueue().pendingCount
        XCTExpectFailure("CLIENT-5: SecureCache.purgeAll() on a location switch deletes pending-writes.json", strict: true) {
            XCTAssertEqual(relaunched, 1, "the kept write must also survive the next relaunch")
        }
    }

    func testThePendingQueueStateIsShownSomewhere() throws {
        let files = try EdgeSource.allSwiftFiles()
        let readers = files.filter { path, text in
            path != "Core/PendingWriteQueue.swift"
                && (text.contains("PendingWriteQueue.shared.pendingCount")
                    || text.contains("PendingWriteQueue.shared.pendingLabels"))
        }
        XCTExpectFailure("CLIENT-5: pendingCount / pendingLabels have no reader, so no screen shows unsent work", strict: true) {
            XCTAssertFalse(readers.isEmpty)
        }
    }

    // MARK: Push (CLIENT-7, 8, 51)

    func testTheNotificationDelegateIsAssignedAtLaunch() {
        // Apple hands the launching tap only to a delegate set before
        // didFinishLaunching returns; PushManager sets it after sign-in.
        let center = UNUserNotificationCenter.current()
        let original = center.delegate
        defer { center.delegate = original }
        center.delegate = nil
        _ = AppDelegate().application(UIApplication.shared, didFinishLaunchingWithOptions: nil)
        let assigned = center.delegate
        XCTExpectFailure("CLIENT-7: the delegate is set only in requestAuthorizationAndRegister (after unlock), so a cold-launch tap is lost", strict: true) {
            XCTAssertTrue(assigned === PushManager.shared)
        }
    }

    /// A tap on a notification, built the only way UNNotificationResponse
    /// can be outside the system (it has no public initialiser).
    private func tap(userInfo: [AnyHashable: Any]) -> UNNotificationResponse? {
        let content = UNMutableNotificationContent()
        content.userInfo = userInfo
        let request = UNNotificationRequest(identifier: "edge", content: content, trigger: nil)
        // An empty keyed archive read back gives a coder whose every key is
        // absent, which both classes accept; the fields are then set by KVC.
        let archiver = NSKeyedArchiver(requiringSecureCoding: false)
        archiver.finishEncoding()
        func coder() -> NSKeyedUnarchiver? {
            let u = try? NSKeyedUnarchiver(forReadingFrom: archiver.encodedData)
            u?.requiresSecureCoding = false
            return u
        }
        guard let c1 = coder(), let c2 = coder(),
              let notification = UNNotification(coder: c1),
              let response = UNNotificationResponse(coder: c2) else { return nil }
        notification.setValue(request, forKey: "request")
        response.setValue(notification, forKey: "notification")
        response.setValue(UNNotificationDefaultActionIdentifier, forKey: "actionIdentifier")
        return response
    }

    private func route(_ userInfo: [AnyHashable: Any]) async throws -> DeepLinkRouter {
        let router = DeepLinkRouter()
        let previous = PushManager.shared.router
        PushManager.shared.router = router
        defer { PushManager.shared.router = previous }
        let response = try XCTUnwrap(tap(userInfo: userInfo))
        await PushManager.shared.userNotificationCenter(UNUserNotificationCenter.current(), didReceive: response)
        return router
    }

    // alert_type is left empty in these two on purpose: a non-empty type
    // records the open through APIClient.shared, which is a real request.
    func testAPushWithAnIntegerReviewIdRoutesToThatReview() async throws {
        let router = try await route(["cavnar": ["alert_type": "", "review_id": 42]])
        XCTAssertEqual(router.pendingTab, .modules)
        XCTAssertEqual(router.pendingModuleKey, "reviews")
        XCTAssertEqual(router.pendingReviewID, 42)
    }

    func testAPushWithAStringReviewIdStillRoutesToThatReview() async throws {
        let router = try await route(["cavnar": ["alert_type": "", "review_id": "42"]])
        XCTAssertEqual(router.pendingTab, .modules)
        XCTExpectFailure("CLIENT-51: review_id sent as a string is dropped by `as? Int`", strict: true) {
            XCTAssertEqual(router.pendingReviewID, 42)
        }
    }

    func testTheApnsTokenIsPersistedSoALockScreenSignOutCanUnregisterIt() throws {
        // Not automatable end to end here: the simulator has no APNs token,
        // PushManager talks only to APIClient.shared, and the test bundle's
        // Keychain does not read back. The fix is persisting the token.
        let source = try EdgeSource.read("Push/PushManager.swift")
        XCTExpectFailure("CLIENT-8: registeredToken lives only in memory for this launch, so LockedView's sign-out sends apns_token: nil", strict: true) {
            XCTAssertTrue(source.contains("Keychain"))
        }
    }

    func testSwitchingLocationReRegistersThePushToken() throws {
        let source = try EdgeSource.read("Core/SessionStore.swift")
        let switchBody = try XCTUnwrap(EdgeSource.slice(source, from: "func didSwitchLocation", length: 900))
        XCTExpectFailure("CLIENT-8: the device token stays registered to the launch-time location after a switch", strict: true) {
            XCTAssertTrue(switchBody.contains("PushManager"))
        }
    }

    // MARK: Network monitor (CLIENT-52)

    func testTheAppHasOneNetworkMonitor() throws {
        let root = try EdgeSource.read("RootView.swift")
        XCTExpectFailure("CLIENT-52: RootView builds its own NetworkMonitor(); APIClient reads .shared, which starts \"online\"", strict: true) {
            XCTAssertFalse(root.contains("NetworkMonitor()"))
        }
    }

    // MARK: Staff sign-in (CLIENT-3, 57)

    func testASixDigitStaffPinCanBeEnteredOnThePad() throws {
        // The server accepts 4-8 digits (auth.PIN_MIN_LENGTH/PIN_MAX_LENGTH);
        // the pad auto-submits at four, so a longer PIN can never be sent.
        let source = try EdgeSource.read("Features/Staff/StaffLoginView.swift")
        XCTAssertTrue(source.contains("pin.count < 8"), "the pad still allows up to 8 digits")
        XCTExpectFailure("CLIENT-3: the iOS PIN pad submits after 4 digits, so a 5-8 digit PIN can never sign in", strict: true) {
            XCTAssertFalse(source.contains("pin.count == 4"))
        }
    }

    func testStaffSignupResendIsDisabledWhileLoading() throws {
        let source = try EdgeSource.read("Features/Staff/StaffSignupView.swift")
        let chain = try XCTUnwrap(EdgeSource.slice(source, from: "Button(\"Send it again\")", length: 260))
        XCTExpectFailure("CLIENT-57: \"Send it again\" has no .disabled, so each tap sends another verification SMS", strict: true) {
            XCTAssertTrue(chain.contains(".disabled("))
        }
    }

    // MARK: Background work (CLIENT-21)

    func testUploadsAskForBackgroundTime() throws {
        let files = try EdgeSource.allSwiftFiles()
        let users = files.filter { $0.1.contains("beginBackgroundTask") }
        XCTExpectFailure("CLIENT-21: nothing keeps an invoice upload or AI generation alive when the app is backgrounded", strict: true) {
            XCTAssertFalse(users.isEmpty)
        }
    }

    func testUploadsCarryAnIdempotencyKey() throws {
        // upload() runs on APIClient's own uploadSession, which cannot take
        // MockURLProtocol, so the header is checked at the source.
        let source = try EdgeSource.read("Core/APIClient.swift")
        let upload = try XCTUnwrap(EdgeSource.slice(source, from: "func upload<Response", length: 1800))
        XCTExpectFailure("CLIENT-21: a retried invoice scan is a second paid model call with nothing to dedupe it", strict: true) {
            XCTAssertTrue(upload.lowercased().contains("idempotency"))
        }
    }
}
