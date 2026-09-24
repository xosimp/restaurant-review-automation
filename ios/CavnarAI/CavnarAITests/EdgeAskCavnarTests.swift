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
        XCTAssertNotNil(vm.errorBanner, "the owner must be told it may already have happened and where to check")
    }

    func testARefusedConfirmCarriesTheServersReason() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 400, #"{"ok": false, "error": "Quiet hours — texts can't go out until 9am."}"#)
        }
        let vm = AskCavnarViewModel(client: client)
        let ok = await vm.confirm(try proposal())
        XCTAssertFalse(ok)
        XCTAssertEqual(vm.errorBanner, "Quiet hours — texts can't go out until 9am.")
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
        // Only the writes, in order: the view model may also refresh the chat
        // list (a GET) at any moment, which made this test fail at random.
        XCTAssertEqual(sent.value.filter { $0.hasPrefix("POST ") },
                       ["POST /mobile/api/guest-campaign/send", "POST /mobile/api/ask-cavnar/action"],
                       "the action runs first; the audit line is written only after it did")
    }

    // MARK: #23 — a card is one proposal, with what it commits to

    func testAProposalCarriesItsIdTheMoneyAndTheWordsThatGoOut() throws {
        let p = try JSONDecoder.cavnar.decode(AskProposal.self, from: Data("""
        {"action": "send_supplier_order", "summary": "Email the suggested order to every supplier",
         "route": {"mobile": "/mobile/api/food-cost/send-order", "method": "POST"}, "body": {},
         "proposal_id": 41, "at_stake": 500.5, "preview": null,
         "details": [{"label": "Sysco", "value": "3 items · $412.50 · to orders@sysco.test"},
                     {"label": "Order total", "value": "$500.50"}]}
        """.utf8))
        XCTAssertEqual(p.proposalId, 41)
        XCTAssertEqual(p.atStake, 500.5)
        XCTAssertEqual(p.details?.last?.value, "$500.50")
        XCTAssertEqual(p.id, "p41", "two cards with the same action are two proposals")
    }

    // MARK: NS5 C1 — every field shown, and only those posted

    func testFieldsShownDecodeAndLimitWhatConfirmPosts() throws {
        let p = try JSONDecoder.cavnar.decode(AskProposal.self, from: Data("""
        {"action": "set_auto_approve", "summary": "Turn auto-approve of 5-star replies ON",
         "route": {"mobile": "/mobile/api/account/auto-approve", "method": "POST"},
         "body": {"enabled": true, "daily_cap": 5, "earned": true},
         "fields_shown": [{"key": "enabled", "label": "Auto-approve", "value": "On"},
                          {"key": "daily_cap", "label": "At most a day", "value": "5"}]}
        """.utf8))
        XCTAssertEqual(p.fieldsShown?.map(\.label), ["Auto-approve", "At most a day"])
        XCTAssertEqual(Set(p.postedBody.keys), ["enabled", "daily_cap"],
                       "a field the card did not show is never posted")
    }

    func testAnOlderBackendWithoutFieldsShownPostsItsBodyAsBefore() throws {
        let p = try JSONDecoder.cavnar.decode(AskProposal.self, from: Data("""
        {"action": "publish_schedule", "summary": "Send the current schedule to staff",
         "route": {"mobile": "/mobile/api/labor/publish-schedule", "method": "POST"},
         "body": {"schedule_id": 9}}
        """.utf8))
        XCTAssertNil(p.fieldsShown)
        XCTAssertEqual(Set(p.postedBody.keys), ["schedule_id"])
    }

    func testNotNowSendsTheProposalIdAndTheReason() async throws {
        let bodies = Box<[[String: Any]]>([])
        let client = EdgeHTTP.client { request in
            if let json = EdgeHTTP.bodyJSON(request) { bodies.value.append(json) }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = AskCavnarViewModel(client: client)
        let p = try JSONDecoder.cavnar.decode(AskProposal.self, from: Data("""
        {"action": "send_guest_campaign", "summary": "Text the guest club",
         "route": {"mobile": "/mobile/api/guest-campaign/send", "method": "POST"},
         "body": {"message": "Half-price wings"}, "proposal_id": 7}
        """.utf8))
        await vm.dismiss(p, reason: "  Too soon after the last one ")
        XCTAssertEqual(bodies.value.first?["proposal_id"] as? Int, 7)
        XCTAssertEqual(bodies.value.first?["reason"] as? String, "Too soon after the last one")
        XCTAssertEqual(bodies.value.first?["outcome"] as? String, "dismissed")
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
        XCTAssertEqual(fallbackAsks.value, 0, "once progress arrived, the question must not be asked a second time")
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
