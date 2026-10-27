import SwiftUI
import XCTest
@testable import CavnarAI

/// The app's half of release-safety audit #25 A06-02 / A06-04
/// (client_compat.py is the server's): every request names its build, a 426
/// opens the blocking update sheet's state, and an answer that cannot be
/// decoded is reported — route, status, coding path, never the body — at most
/// once per route per hour.
final class ClientCompatibilityTests: XCTestCase {

    private let base = URL(string: "https://example.com")!

    private func freshThrottle() -> DecodeFailureThrottle {
        let suite = "ClientCompatibilityTests.\(UUID().uuidString)"
        return DecodeFailureThrottle(defaults: UserDefaults(suiteName: suite)!, key: "sent")
    }

    private func reportingClient(_ throttle: DecodeFailureThrottle? = nil) -> APIClient {
        APIClient(baseURL: base, session: MockURLProtocol.makeSession(),
                  decodeReporting: DecodeFailureReporting(throttle: throttle ?? freshThrottle(),
                                                          deviceId: { "DEVICE-TEST" }))
    }

    override func setUp() async throws {
        await MainActor.run { AppUpdateGate.shared.resetForTesting() }
    }

    override func tearDown() async throws {
        await MainActor.run { AppUpdateGate.shared.resetForTesting() }
        MockURLProtocol.requestHandler = nil
    }

    // MARK: - The headers

    func testTheValueIsVersionThenBuild() {
        XCTAssertEqual(ClientIdentity.value(info: ["CFBundleShortVersionString": "1.0", "CFBundleVersion": "4210"]),
                       "1.0 (4210)")
        // An unknown build is the oldest, never a new one; a version with a
        // space would not parse on the server.
        XCTAssertEqual(ClientIdentity.value(info: ["CFBundleShortVersionString": "1.2"]), "1.2 (0)")
        XCTAssertEqual(ClientIdentity.value(info: ["CFBundleVersion": "abc"]), "0 (0)")
        XCTAssertEqual(ClientIdentity.value(info: ["CFBundleShortVersionString": "1 beta", "CFBundleVersion": "7"]),
                       "0 (7)")
        XCTAssertEqual(ClientIdentity.value(info: nil), "0 (0)")
        // This binary's own value reads as the server expects it.
        XCTAssertNotNil(ClientIdentity.appValue.range(of: #"^[0-9A-Za-z][0-9A-Za-z.\-]* \(\d+\)$"#,
                                                       options: .regularExpression), ClientIdentity.appValue)
    }

    func testEveryTransportSendsTheBuildAndThePlatform() async throws {
        let seen = Box<[URLRequest]>([])
        MockURLProtocol.requestHandler = { request in
            seen.value.append(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "sheets": [], "manager": false}"#)
        }
        let client = APIClient(baseURL: base, session: MockURLProtocol.makeSession())
        await client.setToken("owner-token")
        let _: APIClient.OKResponse = try await client.send("/mobile/api/home")
        let _: APIClient.OKResponse = try await client.send("/mobile/api/reviews/1/approve", method: .post)
        let _: APIClient.OKResponse = try await client.sendWithBearer("/staff/api/me", bearer: "staff-token")
        let _: APIClient.OKResponse = try await client.sendUnauthenticated("/staff/api/roster")
        let api = StaffTasksAPI(baseURL: base, session: MockURLProtocol.makeSession())
        _ = try await api.tasks(bearer: "staff-token")
        XCTAssertEqual(seen.value.count, 5)
        for request in seen.value {
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Cavnar-App"), ClientIdentity.appValue,
                           request.url?.path ?? "")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Cavnar-Platform"), "ios", request.url?.path ?? "")
        }
    }

    // MARK: - 426: the blocking update sheet

    func testTheRequirementReadsTheServersAnswer() {
        let body = Data(#"{"ok": false, "update_required": true, "error": "Too old.", "message": "Update now.", "min_build": 4100, "app_store_url": "https://apps.apple.com/app/id1"}"#.utf8)
        let need = AppUpdateRequirement.from(status: 426, body: body)
        XCTAssertEqual(need?.message, "Update now.")
        XCTAssertEqual(need?.minBuild, 4100)
        XCTAssertEqual(need?.openURL, URL(string: "https://apps.apple.com/app/id1"))
        // Any 426 is one, with the fallback sentence; any other refusal is not.
        XCTAssertEqual(AppUpdateRequirement.from(status: 426, body: Data())?.message,
                       AppUpdateRequirement.fallbackMessage)
        XCTAssertNil(AppUpdateRequirement.from(status: 403, body: Data(#"{"ok": false, "error": "No."}"#.utf8)))
        // A store link is only ever the App Store or TestFlight.
        XCTAssertNil(AppUpdateRequirement.trustedStoreURL("https://evil.example/apps.apple.com"))
        XCTAssertNil(AppUpdateRequirement.trustedStoreURL("javascript:alert(1)"))
        XCTAssertNotNil(AppUpdateRequirement.trustedStoreURL("itms-apps://apps.apple.com/app/id1"))
        XCTAssertNotNil(AppUpdateRequirement.trustedStoreURL("https://testflight.apple.com/join/abc"))
        let unsafe = AppUpdateRequirement.from(status: 426, body: Data(#"{"update_required": true, "app_store_url": "https://evil.example/x"}"#.utf8))
        XCTAssertEqual(unsafe?.openURL, URL(string: "itms-apps://apps.apple.com/"))
    }

    @MainActor
    func testA426OpensTheUpdateSheetAndFailsTheCallAsUpdateRequired() async throws {
        MockURLProtocol.requestHandler = { request in
            EdgeHTTP.reply(request, 426, #"{"ok": false, "update_required": true, "error": "Update Cavnar AI.", "message": "Update Cavnar AI.", "min_build": 9000}"#)
        }
        let client = APIClient(baseURL: base, session: MockURLProtocol.makeSession())
        XCTAssertNil(AppUpdateGate.shared.requirement)
        do {
            let _: APIClient.OKResponse = try await client.send("/mobile/api/home")
            XCTFail("a 426 is never a success")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.kind, .updateRequired)
            XCTAssertEqual(error.status, 426)
            XCTAssertEqual(error.message, "Update Cavnar AI.")
            XCTAssertFalse(error.isTransientForPolling, "a poll stops on 426")
        }
        XCTAssertEqual(AppUpdateGate.shared.requirement?.minBuild, 9000)
        XCTAssertEqual(AppUpdateGate.shared.requirement?.message, "Update Cavnar AI.")
    }

    @MainActor
    func testTheStaffTransportsOpenTheSheetToo() async throws {
        MockURLProtocol.requestHandler = { request in
            EdgeHTTP.reply(request, 426, #"{"ok": false, "update_required": true, "error": "Update Cavnar AI."}"#)
        }
        let client = APIClient(baseURL: base, session: MockURLProtocol.makeSession())
        do {
            let _: APIClient.OKResponse = try await client.sendWithBearer("/staff/api/shifts", bearer: "staff-token")
            XCTFail("a 426 is never a success")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.kind, .updateRequired, "never read as an ended staff session")
        }
        XCTAssertNotNil(AppUpdateGate.shared.requirement)
        AppUpdateGate.shared.resetForTesting()
        let api = StaffTasksAPI(baseURL: base, session: MockURLProtocol.makeSession())
        do {
            _ = try await api.tasks(bearer: "staff-token")
            XCTFail("a 426 is never a success")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.kind, .updateRequired)
        }
        XCTAssertNotNil(AppUpdateGate.shared.requirement)
    }

    @MainActor
    func testTheFirstRequirementStands() {
        let first = AppUpdateRequirement(message: "first", minBuild: 1, storeURL: nil)
        AppUpdateGate.shared.require(first)
        AppUpdateGate.shared.require(AppUpdateRequirement(message: "second", minBuild: 2, storeURL: nil))
        XCTAssertEqual(AppUpdateGate.shared.requirement, first)
    }

    func testAWriteRefusedFor426IsKeptToReplayAfterTheUpdate() {
        XCTAssertFalse(PendingWriteQueue.isRefusal(status: 426))
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 400))
        let tooOld = APIClient.APIError(kind: .updateRequired, message: "Update", status: 426)
        XCTAssertNil(StaffOfflineQueue.refusalReason(tooOld))
        XCTAssertTrue(StaffOfflineQueue.isTransport(tooOld), "the tick stays parked")
    }

    @MainActor
    func testTheSheetHasOnePrimaryActionAndNoWayPast() {
        let view = AppUpdateRequiredView(requirement: AppUpdateRequirement(message: "Update it.", minBuild: 2,
                                                                           storeURL: nil))
        XCTAssertEqual(AppUpdateRequiredView.title, "Update Cavnar AI")
        XCTAssertEqual(AppUpdateRequiredView.buttonTitle, "Open the App Store")
        XCTAssertEqual(AppUpdateRequiredView.checkTitle, "Check again")
        let renderer = ImageRenderer(content: view.frame(width: 390, height: 844))
        XCTAssertNotNil(renderer.uiImage, "the sheet renders")
        let checking = AppUpdateRequiredView(requirement: AppUpdateRequirement(message: "Update it.", minBuild: 2,
                                                                               storeURL: nil), isChecking: true)
        XCTAssertNotNil(ImageRenderer(content: checking.frame(width: 390, height: 844)).uiImage)
    }

    // MARK: - The gate opens again when the server stops saying 426 (blind re-audit 10/27/26)

    @MainActor
    func testTheGateClearsOnlyWhenTheServerServesTheBuildAgain() async {
        let gate = AppUpdateGate.shared
        gate.require(AppUpdateRequirement(message: "Update", minBuild: 99_999, storeURL: nil))
        await gate.recheck { true }                       // still 426
        XCTAssertNotNil(gate.requirement)
        await gate.recheck { nil }                        // offline: nothing known
        XCTAssertNotNil(gate.requirement)
        await gate.recheck { false }                      // the minimum came down
        XCTAssertNil(gate.requirement)
        XCTAssertFalse(gate.isChecking)
        // Nothing held: nothing is asked.
        let asked = Box<Int>(0)
        await gate.recheck { asked.value += 1; return false }
        XCTAssertEqual(asked.value, 0)
    }

    func testTheProbeReadsTheGatesAnswerWithNoSession() async throws {
        let seen = Box<[URLRequest]>([])
        let status = Box<Int>(426)
        MockURLProtocol.requestHandler = { request in
            seen.value.append(request)
            return status.value == 426
                ? EdgeHTTP.reply(request, 426, #"{"ok": false, "update_required": true, "error": "Update."}"#)
                : EdgeHTTP.reply(request, 401, #"{"ok": false, "session_expired": true}"#)
        }
        let client = APIClient(baseURL: base, session: MockURLProtocol.makeSession())
        await client.setToken("owner-token")
        let still = await client.stillRequiresUpdate()
        XCTAssertEqual(still, true)
        status.value = 401
        let served = await client.stillRequiresUpdate()
        XCTAssertEqual(served, false, "any answer but 426 means the build is served")
        XCTAssertEqual(seen.value.count, 2)
        for request in seen.value {
            XCTAssertEqual(request.url?.path, APIClient.updateProbePath)
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"), "the probe carries no session")
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-Cavnar-App"), ClientIdentity.appValue)
        }
    }

    // MARK: - Decode-failure reports

    private struct Strict: Decodable { let ok: Bool; let name: String }

    func testAnAnswerThatCannotBeDecodedIsReportedWithItsPathNeverItsBody() async throws {
        let reports = Box<[URLRequest]>([])
        MockURLProtocol.requestHandler = { request in
            if request.url?.path == "/mobile/api/client-errors" {
                reports.value.append(request)
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "stored": true}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "secret": "SENTINEL-BODY-TEXT"}"#)
        }
        let client = reportingClient()
        do {
            let _: Strict = try await client.send("/mobile/api/home")
            XCTFail("the answer has no name")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.kind, .decoding)
        }
        await client.lastDecodeReport?.value
        XCTAssertEqual(reports.value.count, 1)
        let report = try XCTUnwrap(reports.value.first)
        XCTAssertEqual(report.httpMethod, "POST")
        XCTAssertEqual(report.value(forHTTPHeaderField: "X-Cavnar-App"), ClientIdentity.appValue)
        let raw = try XCTUnwrap(EdgeHTTP.body(report))
        let json = try XCTUnwrap(JSONSerialization.jsonObject(with: raw) as? [String: Any])
        XCTAssertEqual(json["route"] as? String, "/mobile/api/home")
        XCTAssertEqual(json["method"] as? String, "GET")
        XCTAssertEqual(json["status"] as? Int, 200)
        XCTAssertEqual(json["coding_path"] as? String, "$.name")
        XCTAssertEqual(json["kind"] as? String, "decoding")
        XCTAssertEqual(json["device_id"] as? String, "DEVICE-TEST")
        XCTAssertTrue((json["description"] as? String ?? "").contains("name"))
        XCTAssertFalse(String(decoding: raw, as: UTF8.self).contains("SENTINEL-BODY-TEXT"), "never the body")
        XCTAssertEqual(Set(json.keys), ["route", "method", "status", "coding_path", "description", "kind", "device_id"])
    }

    func testOneReportPerRoutePerHour() async throws {
        let reports = Box<Int>(0)
        MockURLProtocol.requestHandler = { request in
            if request.url?.path == "/mobile/api/client-errors" {
                reports.value += 1
                return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let client = reportingClient()
        for _ in 0..<3 {
            let _: Strict? = try? await client.send("/mobile/api/reviews/412")
            await client.lastDecodeReport?.value
        }
        let _: Strict? = try? await client.send("/mobile/api/reviews/413")   // the same route, another id
        await client.lastDecodeReport?.value
        XCTAssertEqual(reports.value, 1)
    }

    /// begin + a delivered finish: the old `claim`.
    private func sent(_ throttle: DecodeFailureThrottle, _ route: String, at now: Date) -> Bool {
        guard throttle.begin(route: route, now: now) else { return false }
        throttle.finish(route: route, delivered: true, now: now)
        return true
    }

    func testTheThrottleCountsAnHourPerRoute() {
        let throttle = freshThrottle()
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertTrue(sent(throttle, "/mobile/api/dsr/2026-10-25?peek=1", at: t0))
        XCTAssertFalse(sent(throttle, "/mobile/api/dsr/2026-10-26", at: t0.addingTimeInterval(600)))
        XCTAssertTrue(sent(throttle, "/mobile/api/home", at: t0.addingTimeInterval(600)))
        XCTAssertTrue(sent(throttle, "/mobile/api/dsr/2026-10-27", at: t0.addingTimeInterval(3601)))
        XCTAssertEqual(DecodeFailureThrottle.routeKey("/mobile/api/reviews/412?x=1"), "/mobile/api/reviews/:id")
        XCTAssertEqual(DecodeFailureThrottle.routeKey("/mobile/api/labor"), "/mobile/api/labor")
    }

    /// The hour starts only once a report was delivered (blind re-audit
    /// 10/27/26), and a report in flight is not sent twice.
    func testTheHourIsSpentOnlyByADeliveredReport() {
        let throttle = freshThrottle()
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertTrue(throttle.begin(route: "/mobile/api/home", now: t0))
        XCTAssertFalse(throttle.begin(route: "/mobile/api/home", now: t0), "one report in flight per route")
        throttle.finish(route: "/mobile/api/home", delivered: false, now: t0)
        XCTAssertTrue(throttle.begin(route: "/mobile/api/home", now: t0.addingTimeInterval(5)),
                      "a report that failed leaves the hour unspent")
        throttle.finish(route: "/mobile/api/home", delivered: true, now: t0.addingTimeInterval(5))
        XCTAssertFalse(throttle.begin(route: "/mobile/api/home", now: t0.addingTimeInterval(60)))
    }

    func testAReportThatFailsIsSentAgainOnTheNextFailure() async throws {
        let attempts = Box<Int>(0)
        MockURLProtocol.requestHandler = { request in
            if request.url?.path == "/mobile/api/client-errors" {
                attempts.value += 1
                // The first report meets a gateway answering for a restarting
                // server; the second gets through.
                return attempts.value == 1 ? EdgeHTTP.reply(request, 503, #"{"ok": false}"#)
                    : EdgeHTTP.reply(request, 200, #"{"ok": true, "stored": true}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let client = reportingClient()
        for _ in 0..<3 {
            let _: Strict? = try? await client.send("/mobile/api/labor")
            await client.lastDecodeReport?.value
        }
        XCTAssertEqual(attempts.value, 2, "the 503 spent nothing; the delivered one starts the hour")
    }

    func testARefusalNoRetryWouldChangeSpendsTheHour() async throws {
        let attempts = Box<Int>(0)
        MockURLProtocol.requestHandler = { request in
            if request.url?.path == "/mobile/api/client-errors" {
                attempts.value += 1
                return EdgeHTTP.reply(request, 400, #"{"ok": false, "error": "That isn't one of the app's routes."}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let client = reportingClient()
        for _ in 0..<2 {
            let _: Strict? = try? await client.send("/mobile/api/labor")
            await client.lastDecodeReport?.value
        }
        XCTAssertEqual(attempts.value, 1)
    }

    func testTheCodingPathNamesKeysAndIndexes() throws {
        struct Row: Decodable { let n: Int }
        struct Page: Decodable { let rows: [Row] }
        do {
            _ = try JSONDecoder().decode(Page.self, from: Data(#"{"rows": [{"n": 1}, {"n": 2.5}]}"#.utf8))
            XCTFail("2.5 is not an Int")
        } catch {
            let (path, text) = ClientErrorReport.describe(error)
            XCTAssertEqual(path, "$.rows[1].n")
            XCTAssertFalse(text.contains("2.5") && text.contains("{"), "no body in the description")
        }
    }

    func testTheReportItselfIsNeverReportedAndATestsMockNeverReports() async {
        let client = reportingClient()
        let none = await client.reportDecodeFailure(described: (path: "$", description: "x"),
                                                    route: "/mobile/api/client-errors", verb: "POST", status: 200)
        XCTAssertNil(none)
        let plain = APIClient(baseURL: base, session: MockURLProtocol.makeSession())
        let silent = await plain.reportDecodeFailure(described: (path: "$", description: "x"),
                                                     route: "/mobile/api/home", verb: "GET", status: 200)
        XCTAssertNil(silent, "a client on a test's session reports only when asked to")
    }
}
