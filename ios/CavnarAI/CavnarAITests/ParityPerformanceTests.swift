import XCTest
@testable import CavnarAI

/// iOS parity audit 10/7/26 — performance, network and battery: the poll
/// cadence, stale-while-refresh module reads (#37), conditional GETs (#56),
/// ReviewById's fallback only for a missing route, and the reduced-activity
/// rule (#83).
final class ParityPerformanceTests: XCTestCase {
    private func makeClient() -> APIClient {
        APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    private struct Read: Decodable, Equatable { let ok: Bool; let insight: String? }

    // MARK: poll cadence

    func testPollsBackOffFromOneAndAHalfToThreeToFiveSeconds() {
        XCTAssertEqual(APIClient.pollDelay(after: 0), .milliseconds(1500))
        XCTAssertEqual(APIClient.pollDelay(after: 1), .milliseconds(3000))
        XCTAssertEqual(APIClient.pollDelay(after: 2), .milliseconds(5000))
        XCTAssertEqual(APIClient.pollDelay(after: 9), .milliseconds(5000))
    }

    func testAJobsSecondsLeftCapsTheWaitButNeverBelowTheFirstStep() {
        XCTAssertEqual(APIClient.pollDelay(after: 5, secondsLeft: 2), .milliseconds(2000))
        XCTAssertEqual(APIClient.pollDelay(after: 5, secondsLeft: 1), .milliseconds(1500))
        XCTAssertEqual(APIClient.pollDelay(after: 5, secondsLeft: 0), .milliseconds(5000))
        XCTAssertEqual(APIClient.pollDelay(after: 5, secondsLeft: 200), .milliseconds(5000))
    }

    // MARK: #37 stale-while-refresh

    func testAPendingReadDecodesItsPlaceholderAndItsJob() throws {
        let json = #"""
        {"ok": true, "pending": true, "refreshing": true, "status": "pending", "refresh_job": "j1",
         "insight": "Cavnar AI is writing this read", "insight_intro": "Cavnar AI is writing this read",
         "insight_recommendations": [], "insight_forecast": null}
        """#
        let state = try JSONDecoder.cavnar.decode(APIClient.InsightRefreshState.self, from: Data(json.utf8))
        XCTAssertTrue(state.isWaiting)
        XCTAssertTrue(state.isPending)
        XCTAssertEqual(state.refreshJob, "j1")
        // The read's own model still decodes it, so an older screen shows a sentence.
        let insight = try JSONDecoder.cavnar.decode(AIInsight.self, from: Data(json.utf8))
        XCTAssertEqual(insight.recommendations, [])
    }

    func testAStaleReadWhileRefreshingIsShownAndFollowed() throws {
        let json = #"{"ok": true, "insight": "Monday", "stale": true, "refreshing": true, "pending": false, "refresh_job": "j2"}"#
        let state = try JSONDecoder.cavnar.decode(APIClient.InsightRefreshState.self, from: Data(json.utf8))
        XCTAssertTrue(state.isWaiting)
        XCTAssertFalse(state.isPending)
    }

    func testAnOrdinaryReadIsNotWaiting() throws {
        let state = try JSONDecoder.cavnar.decode(APIClient.InsightRefreshState.self,
                                                  from: Data(#"{"ok": true, "insight": "x"}"#.utf8))
        XCTAssertFalse(state.isWaiting)
    }

    func testAnInsightReadAsksAsyncAndSendsTheJobBack() async throws {
        let urls = Box<[URL]>([])
        MockURLProtocol.requestHandler = { request in
            urls.value.append(request.url!)
            let ok = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (ok, Data(#"{"ok": true, "insight": "x"}"#.utf8))
        }
        let client = makeClient()
        let _: (value: Read, body: Data, refresh: APIClient.InsightRefreshState?) =
            try await client.sendInsight("/mobile/api/labor/insight", refreshJob: "j9")
        let items = URLComponents(url: urls.value[0], resolvingAgainstBaseURL: false)?.queryItems ?? []
        XCTAssertTrue(items.contains(URLQueryItem(name: "async", value: "1")))
        XCTAssertTrue(items.contains(URLQueryItem(name: "refresh_job", value: "j9")))
    }

    // MARK: #56 conditional GETs

    func testAConditionalCopyRoundTrips() {
        let copy = APIClient.ConditionalCopy(etag: #"W/"abc""#, body: Data(#"{"ok":true}"#.utf8))
        XCTAssertEqual(APIClient.decodeConditional(APIClient.encodeConditional(copy)), copy)
        XCTAssertNil(APIClient.decodeConditional(Data("no newline".utf8)))
    }

    func testATaggedReadIsRevalidatedAndA304ReusesTheHeldBody() async throws {
        let sent = Box<[String?]>([])
        let token = "etag-test-\(UUID().uuidString)"
        MockURLProtocol.requestHandler = { request in
            let inm = request.value(forHTTPHeaderField: "If-None-Match")
            sent.value.append(inm)
            if inm == #"W/"v1""# {
                return (HTTPURLResponse(url: request.url!, statusCode: 304, httpVersion: nil,
                                        headerFields: ["ETag": #"W/"v1""#])!, Data())
            }
            return (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                    headerFields: ["ETag": #"W/"v1""#, "Content-Type": "application/json"])!,
                    Data(#"{"ok": true, "insight": "home"}"#.utf8))
        }
        let client = makeClient()
        await client.setToken(token)
        let first: Read = try await client.send("/mobile/api/home")
        let second: (value: Read, body: Data) = try await client.sendKeepingBody("/mobile/api/home")
        XCTAssertEqual(sent.value, [nil, #"W/"v1""#])
        XCTAssertEqual(first, second.value)
        XCTAssertEqual(String(data: second.body, encoding: .utf8), #"{"ok": true, "insight": "home"}"#)
        await client.setToken(nil)
    }

    func testAnotherSessionNeverSendsTheFirstSessionsTag() async throws {
        let sent = Box<[String?]>([])
        MockURLProtocol.requestHandler = { request in
            sent.value.append(request.value(forHTTPHeaderField: "If-None-Match"))
            return (HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                    headerFields: ["ETag": #"W/"v2""#])!, Data(#"{"ok": true}"#.utf8))
        }
        let client = makeClient()
        await client.setToken("first-\(UUID().uuidString)")
        let _: Read = try await client.send("/mobile/api/labor")
        await client.setToken("second-\(UUID().uuidString)")
        let _: Read = try await client.send("/mobile/api/labor")
        XCTAssertEqual(sent.value, [nil, nil])
    }

    // MARK: ReviewById

    func testOnlyAMissingRouteFallsBackToPagingTheInbox() {
        func err(_ body: String?) -> APIClient.APIError {
            APIClient.APIError(message: "x", status: 404, body: body.map { Data($0.utf8) })
        }
        XCTAssertTrue(ReviewByIdViewModel.isMissingRoute(err(#"{"ok": false, "unknown_route": true, "error": "x"}"#)))
        XCTAssertTrue(ReviewByIdViewModel.isMissingRoute(
            err(#"{"ok": false, "error": "That endpoint doesn't exist. Please update the app."}"#)))
        XCTAssertTrue(ReviewByIdViewModel.isMissingRoute(err("<html>Not Found</html>")))
        XCTAssertFalse(ReviewByIdViewModel.isMissingRoute(
            err(#"{"ok": false, "error": "That review isn't in this restaurant's inbox."}"#)))
        XCTAssertFalse(ReviewByIdViewModel.isMissingRoute(APIClient.APIError(message: "x", status: 500)))
    }

    // MARK: #83 reduced activity

    func testLowPowerOrSeriousHeatReducesAmbientMotion() {
        XCTAssertFalse(CavnarEnvironment.reduces(lowPower: false, thermal: .nominal))
        XCTAssertFalse(CavnarEnvironment.reduces(lowPower: false, thermal: .fair))
        XCTAssertTrue(CavnarEnvironment.reduces(lowPower: false, thermal: .serious))
        XCTAssertTrue(CavnarEnvironment.reduces(lowPower: false, thermal: .critical))
        XCTAssertTrue(CavnarEnvironment.reduces(lowPower: true, thermal: .nominal))
    }
}
