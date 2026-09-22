import XCTest
@testable import CavnarAI

/// What these protect: Ask Cavnar's side-effecting proposals and its paid
/// model calls. A confirmed proposal (a guest blast, a supplier order, a
/// published schedule) whose request timed out must not be quietly offered
/// again as if nothing happened, and a refusal must carry the server's
/// reason; a stream that already ran tools must not be re-asked from
/// scratch. XCTExpectFailure marks confirmed CLIENT-19 / CLIENT-28 defects;
/// each flips when fixed.
@MainActor
final class EdgeAskCavnarTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
    }

    private func proposal(_ path: String = "/mobile/api/guest-campaign/send") throws -> AskProposal {
        try JSONDecoder.cavnar.decode(AskProposal.self, from: Data("""
        {"action": "send_guest_campaign", "summary": "Text 40 guests about wing night",
         "route": {"mobile": "\(path)", "method": "POST"},
         "body": {"message": "Half-price wings tonight", "segment": "all"}}
        """.utf8))
    }

    // MARK: CLIENT-19 — Confirm after a failure

    func testATimedOutConfirmSaysTheOutcomeIsUnknown() async throws {
        let client = EdgeHTTP.client { _ in throw URLError(.timedOut) }
        let vm = AskCavnarViewModel(client: client)
        let ok = await vm.confirm(try proposal())
        XCTAssertFalse(ok)
        XCTExpectFailure("CLIENT-19: a transport failure on Confirm returns false with no message, and the card re-enables Confirm", strict: true) {
            XCTAssertNotNil(vm.errorBanner, "the owner must be told it may already have happened and where to check")
        }
    }

    func testARefusedConfirmCarriesTheServersReason() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 400, #"{"ok": false, "error": "Quiet hours — texts can't go out until 9am."}"#)
        }
        let vm = AskCavnarViewModel(client: client)
        let ok = await vm.confirm(try proposal())
        XCTAssertFalse(ok)
        XCTExpectFailure("CLIENT-19: confirm()'s bare catch drops the server's error (quiet hours, limits)", strict: true) {
            XCTAssertEqual(vm.errorBanner, "Quiet hours — texts can't go out until 9am.")
        }
    }

    func testASuccessfulConfirmRecordsTheOutcomeAfterRunning() async throws {
        let sent = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            sent.value.append(EdgeHTTP.line(request))
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = AskCavnarViewModel(client: client)
        let ok = await vm.confirm(try proposal())
        XCTAssertTrue(ok)
        XCTAssertEqual(sent.value, ["POST /mobile/api/guest-campaign/send", "POST /mobile/api/ask-cavnar/action"],
                       "the action runs first; the audit line is written only after it did")
    }

    // MARK: CLIENT-28 — fallback after progress

    func testAStreamThatAlreadyRanToolsIsNotReAskedFromScratch() async {
        let fallbackAsks = Box(0)
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/ask-cavnar/stream":
                // Progress arrives (tools ran server-side), then the stream
                // ends without an answer — a proxy cut, a dropped cell link.
                let body = "data: {\"type\": \"progress\", \"label\": \"Reading your labor numbers\", \"state\": \"thinking\"}\n\n"
                let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil,
                                               headerFields: ["Content-Type": "text/event-stream"])!
                return (response, Data(body.utf8))
            case "/mobile/api/ask-cavnar":
                fallbackAsks.value += 1
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "answer": "Friday ran 31% labor."}"#)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "conversations": []}"#)
            }
        }
        let vm = AskCavnarViewModel(client: client)
        vm.question = "How did labor look on Friday?"
        await vm.submit()
        XCTExpectFailure("CLIENT-28: the plain-request fallback always runs after a stream error, re-billing the model and its tools", strict: true) {
            XCTAssertEqual(fallbackAsks.value, 0, "once progress arrived, the question must not be asked a second time")
        }
    }

    func testAStreamThatFailsBeforeAnyProgressFallsBack() async {
        // The fallback's intended case: nothing ran yet.
        let fallbackAsks = Box(0)
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/ask-cavnar/stream":
                return EdgeHTTP.reply(request, 502, "<html>Bad Gateway</html>")
            case "/mobile/api/ask-cavnar":
                fallbackAsks.value += 1
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "answer": "Friday ran 31% labor."}"#)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "conversations": []}"#)
            }
        }
        let vm = AskCavnarViewModel(client: client)
        vm.question = "How did labor look on Friday?"
        await vm.submit()
        XCTAssertEqual(fallbackAsks.value, 1)
        XCTAssertEqual(vm.messages.last?.text, "Friday ran 31% labor.")
    }
}
