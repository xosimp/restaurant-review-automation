import XCTest
@testable import CavnarAI

/// Blind re-audit 10/8/26 of the Home / performance work: Home's ActionItem
/// decodes a real /actions item's `count` (#2), and a module read the server
/// could not write ends the phone's wait with the server's own sentence (#3).
@MainActor
final class ReauditHomeTests: XCTestCase {
    private struct Envelope: Decodable { let ok: Bool; let items: [ActionItem] }

    // MARK: #2 — ActionItem.count

    /// The fixture is the item action_queue.items writes (held to it by
    /// tests/test_reaudit_home_1008.py).
    func testARealActionsItemDecodesItsCount() throws {
        let url = try XCTUnwrap(Bundle(for: Self.self).url(forResource: "actions_no_response", withExtension: "json"))
        let read = try JSONDecoder.cavnar.decode(Envelope.self, from: Data(contentsOf: url))
        let item = try XCTUnwrap(read.items.first)
        XCTAssertEqual(item.key, "no_response")
        XCTAssertEqual(item.count, 3)
        XCTAssertEqual(item.nav, "reviews?filter=pending")
        // What the widget reads from Home's own read of the queue.
        let waiting = WidgetSnapshotService.waiting(fromHome: read.items.map { (key: $0.key, count: $0.count) })
        XCTAssertEqual(waiting, WidgetSnapshotService.WaitingPart(count: 1, replies: 3))
    }

    func testAnItemWithNoCountDecodesNil() throws {
        let json = #"{"key": "issue:4", "kind": "issue", "title": "Walk-in is warm", "count": null}"#
        let item = try JSONDecoder.cavnar.decode(ActionItem.self, from: Data(json.utf8))
        XCTAssertNil(item.count)
    }

    // MARK: #3 — a failed read ends the wait

    func testAnErrorStatusIsTheServersSentenceAndATransportFailureIsWaitedOut() {
        XCTAssertEqual(InsightRefresh.failureMessage(
            APIClient.APIError(message: "Cavnar AI is paused for this month.", status: 429)),
            "Cavnar AI is paused for this month.")
        XCTAssertEqual(InsightRefresh.failureMessage(APIClient.APIError(message: " ", status: 503)),
                       InsightRefresh.fallbackMessage)
        XCTAssertNil(InsightRefresh.failureMessage(
            APIClient.APIError(kind: .offline, message: "You're offline", mayHaveReachedServer: false)))
        XCTAssertNil(InsightRefresh.failureMessage(URLError(.timedOut)))
    }

    func testAnErrorOnATwoHundredIsAFailureAndAReadIsNot() {
        XCTAssertEqual(InsightRefresh.failureMessage(body: Data(#"{"ok": false, "insight": "Unable to load analysis", "error": "Unable to load analysis"}"#.utf8)),
                       "Unable to load analysis")
        XCTAssertEqual(InsightRefresh.failureMessage(body: Data(#"{"ok": false, "insight": "Budget reached"}"#.utf8)),
                       "Budget reached")
        XCTAssertNil(InsightRefresh.failureMessage(body: Data(#"{"ok": true, "insight": "Labor ran 31%."}"#.utf8)))
        XCTAssertNil(InsightRefresh.failureMessage(body: Data(#"{"insight": "Monday", "stale": true, "error": null}"#.utf8)))
    }

    private struct Read: Decodable { let ok: Bool?; let insight: String? }

    func testFollowStopsOnAFailedReadAndHandsOverTheServersSentence() async {
        let calls = Box<Int>(0)
        MockURLProtocol.requestHandler = { request in
            calls.value += 1
            let refused = HTTPURLResponse(url: request.url!, statusCode: 429, httpVersion: nil, headerFields: nil)!
            return (refused, Data(#"{"ok": false, "insight": "Cavnar AI is paused", "error": "Cavnar AI is paused"}"#.utf8))
        }
        let client = APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
        let waiting = APIClient.InsightRefreshState(pending: true, refreshing: true, refreshJob: "j1")
        var said: String?
        var applied = 0
        let shown = await InsightRefresh.follow("/mobile/api/reviews/insight", from: waiting, client: client,
                                                failed: { said = $0 }) { (_: Read) in applied += 1 }
        XCTAssertEqual(said, "Cavnar AI is paused")
        XCTAssertFalse(shown)
        XCTAssertEqual(applied, 0)
        XCTAssertEqual(calls.value, 1, "a failed read is never re-asked")
    }
}
