import XCTest
@testable import CavnarAI

/// Thread-safe box for capturing values from inside MockURLProtocol's
/// @Sendable request handler, which runs synchronously on the URL loading
/// system's own thread — a lock avoids any race with the test's assertions.
final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: T
    init(_ value: T) { _value = value }
    var value: T {
        get { lock.withLock { _value } }
        set { lock.withLock { _value = newValue } }
    }
}

final class APIClientTests: XCTestCase {
    private func makeClient() -> APIClient {
        APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    private struct OKResponse: Codable { let ok: Bool }

    func testAttachesBearerTokenWhenSet() async throws {
        let capturedRequest = Box<URLRequest?>(nil)
        MockURLProtocol.requestHandler = { request in
            capturedRequest.value = request
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, try JSONEncoder().encode(OKResponse(ok: true)))
        }
        let client = makeClient()
        await client.setToken("test-token-123")

        let _: OKResponse = try await client.send("/mobile/api/home")

        XCTAssertEqual(capturedRequest.value?.value(forHTTPHeaderField: "Authorization"), "Bearer test-token-123")
    }

    func testOmitsAuthorizationHeaderWhenNoTokenSet() async throws {
        let capturedRequest = Box<URLRequest?>(nil)
        MockURLProtocol.requestHandler = { request in
            capturedRequest.value = request
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, try JSONEncoder().encode(OKResponse(ok: true)))
        }
        let client = makeClient()

        let _: OKResponse = try await client.send("/mobile/api/home")

        XCTAssertNil(capturedRequest.value?.value(forHTTPHeaderField: "Authorization"))
    }

    func testSessionExpiredResponseThrowsAndFiresHandler() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 401, httpVersion: nil, headerFields: nil)!
            let body = Data("""
            {"ok": false, "error": "expired", "session_expired": true}
            """.utf8)
            return (response, body)
        }
        let client = makeClient()
        let handlerCalled = Box(false)
        await client.setSessionExpiredHandler {
            handlerCalled.value = true
        }

        do {
            let _: OKResponse = try await client.send("/mobile/api/home")
            XCTFail("expected SessionExpiredError to be thrown")
        } catch is APIClient.SessionExpiredError {
            // expected
        } catch {
            XCTFail("expected SessionExpiredError, got \(error)")
        }

        XCTAssertTrue(handlerCalled.value)
    }

    func testGenericErrorSurfacesServerMessage() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil)!
            let body = Data("""
            {"ok": false, "error": "That question is too long."}
            """.utf8)
            return (response, body)
        }
        let client = makeClient()

        do {
            let _: OKResponse = try await client.send("/mobile/api/ask-cavnar", method: .post)
            XCTFail("expected an error to be thrown")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.message, "That question is too long.")
        } catch {
            XCTFail("expected APIError, got \(error)")
        }
    }

    func testNetworkFailureSurfacesFriendlyMessage() async {
        MockURLProtocol.requestHandler = { _ in
            throw URLError(.notConnectedToInternet)
        }
        let client = makeClient()

        do {
            let _: OKResponse = try await client.send("/mobile/api/home")
            XCTFail("expected an error to be thrown")
        } catch let error as APIClient.APIError {
            // -1009 alone does NOT mean offline. URLSession raises it
            // whenever path evaluation is unsatisfied at that instant, which
            // happens routinely on the first request after launch or a
            // Wi-Fi/cell handoff on a device that is plainly online — so the
            // app asks NetworkMonitor instead of trusting the code, and this
            // test machine is online. (This assertion used to demand
            // .offline; it was written before that fix and had been failing
            // ever since, unnoticed because CI runs pytest only.)
            XCTAssertEqual(error.kind, .timedOut)
            XCTAssertTrue(error.isRetryable)
        } catch {
            XCTFail("expected APIError, got \(error)")
        }
    }

    // Both branches of the offline decision, driven directly — reaching
    // classify() through send() only ever exercises whichever state the
    // machine running the tests happens to be in.

    func testAGenuinelyOfflineDeviceIsToldItIsOffline() {
        let error = APIClient.classify(URLError(.notConnectedToInternet), deviceIsOffline: true)
        XCTAssertEqual(error.kind, .offline)
        XCTAssertTrue(error.message.lowercased().contains("offline"))
    }

    func testAnOnlineDeviceIsAskedToRetryRatherThanToldItIsOffline() {
        let error = APIClient.classify(URLError(.notConnectedToInternet), deviceIsOffline: false)
        XCTAssertEqual(error.kind, .timedOut)
        XCTAssertFalse(error.message.lowercased().contains("offline"),
                       "telling someone on their own Wi-Fi that they are offline sends them to the router")
    }

    func testAnUnreachableServerReadsAsAConnectionProblemNotAnOfflineDevice() {
        // The exact shape of a device pointed at a dev tunnel that has since
        // died: the phone is online, the host simply isn't there.
        for code in [URLError.cannotConnectToHost, .cannotFindHost, .networkConnectionLost] {
            let error = APIClient.classify(URLError(code), deviceIsOffline: false)
            XCTAssertEqual(error.kind, .timedOut, "\(code) should be retryable")
            XCTAssertTrue(error.message.lowercased().contains("connection"))
        }
    }
}
