import XCTest
@testable import CavnarAI

/// What these protect: texts and posts that go out to the public in the
/// restaurant's name. A guest blast must reach exactly the audience shown
/// on screen, exactly once; a social post must not be published twice; a
/// timed-out send must not be presented as safe to retry; a scheduled post
/// must go out on the restaurant's clock; and a failed load must never be
/// shown as "nobody opted in". XCTExpectFailure marks confirmed CLIENT-1 /
/// 9 / 10 / 33 / 34 / 49 / 58 defects; each flips when fixed.
@MainActor
final class EdgeMarketingTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
    }

    nonisolated private static let segments = """
    {"ok": true, "segments": [
       {"key": "all", "label": "Everyone", "help": "Every consented guest", "count": 40},
       {"key": "lapsed", "label": "Lapsed", "help": "No visit in 30 days", "count": 12}],
     "defaults": {"win_back": "lapsed"}}
    """

    // MARK: CLIENT-9 — "Goes to 0 guests" but Send goes to everyone

    func testSendIsRefusedWhenTheAudienceNeverLoaded() async {
        let sends = Box(0)
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/guest-campaign/send" {
                sends.value += 1
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "sent": 40, "total": 40}"#)
            }
            if request.url?.path == "/mobile/api/guest-segments" {
                return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadSegments()
        XCTAssertEqual(vm.selectedSegmentCount, 0, "the screen reads \"Goes to 0 guests\"")
        XCTAssertEqual(vm.selectedSegment, "all", "…while the segment sent is everyone")
        vm.draftMessage = "Half-price wings tonight"
        await vm.sendCampaign()
        XCTAssertEqual(sends.value, 0, "a send must not go out to an audience the owner was shown as zero")
    }

    func testAFailedAudienceLoadIsShownAsAnError() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 503, #"{"ok": false, "error": "Service unavailable"}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadSegments()
        XCTAssertTrue(vm.errorMessage != nil || vm.campaignError != nil)
    }

    func testTheSuggestedAudienceFollowsTheCampaignType() async {
        let client = EdgeHTTP.client { request in EdgeHTTP.reply(request, 200, Self.segments) }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadSegments()
        XCTAssertEqual(vm.selectedSegment, "lapsed")
        XCTAssertEqual(vm.selectedSegmentCount, 12)
    }

    func testTheSendButtonAsksForConfirmationWithTheCount() throws {
        // The web asks "Send this text to <segment>?" (dashboard.html); the
        // app's button calls sendCampaign directly.
        let view = try EdgeSource.read("Features/Marketing/GuestTextClubView.swift")
        let button = try XCTUnwrap(EdgeSource.slice(view, from: "Task { await viewModel.sendCampaign() }", length: 30))
        XCTAssertFalse(button.isEmpty)
        XCTAssertTrue(view.contains(".confirmationDialog") || view.contains(".alert("),
                      "a guest blast needs a confirmation that shows who it goes to")
    }

    // MARK: CLIENT-1 — the same blast twice

    func testTwoTapsOnSendTextEachGuestOnce() async {
        let sends = Box(0)
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/guest-campaign/send" {
                sends.value += 1
                Thread.sleep(forTimeInterval: 0.05)
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "sent": 40, "total": 40}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        vm.draftMessage = "Half-price wings tonight"
        async let a: Void = vm.sendCampaign()
        async let b: Void = vm.sendCampaign()
        _ = await (a, b)
        XCTAssertEqual(sends.value, 1)
    }

    func testATimedOutBlastIsNotPresentedAsSafeToRetry() async {
        // The server texts guests in a synchronous loop and may still be
        // sending when the phone gives up; "Tap to retry" invites the second
        // blast.
        let client = EdgeHTTP.client { _ in throw URLError(.timedOut) }
        let vm = GuestTextClubViewModel(client: client)
        vm.draftMessage = "Half-price wings tonight"
        await vm.sendCampaign()
        let message = vm.campaignError ?? ""
        XCTAssertFalse(message.isEmpty)
        XCTAssertFalse(message.lowercased().contains("retry"), message)
        XCTAssertTrue(message.lowercased().contains("history"), "the owner should check campaign history first")
    }

    // MARK: CLIENT-34 — deleting a contact

    func testAFailedDeleteKeepsTheContact() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        let contact = try JSONDecoder().decode(GuestContact.self, from: Data(
            #"{"id": 5, "name": "Maria", "phone": "+13125550100", "consent": true, "unsubscribed": false}"#.utf8))
        vm.contacts = [contact]
        await vm.deleteContact(contact)
        XCTAssertEqual(vm.contacts.map(\.id), [5])
        XCTAssertNotNil(vm.errorMessage)
    }

    func testDeletingAContactAsksFirst() throws {
        let view = try EdgeSource.read("Features/Marketing/GuestTextClubView.swift")
        let delete = try XCTUnwrap(EdgeSource.slice(view, from: "Task { await viewModel.deleteContact(contact) }", length: 20))
        XCTAssertFalse(delete.isEmpty)
        XCTAssertTrue(view.contains("confirmationDialog") && view.contains("deleteContact"),
                      "the trash button should confirm before deleting")
    }

    // MARK: CLIENT-58 — failed loads that look empty

    func testAFailedNewsletterLoadIsNotShownAsNoSubscribers() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadNewsletter()
        XCTAssertEqual(vm.subscriberCount, 0, "the screen reads \"Nobody has opted in to email yet\"")
        XCTAssertTrue(vm.newsletterError != nil || vm.errorMessage != nil)
    }

    func testAFailedHistoryLoadIsNotShownAsNoCampaigns() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Internal error"}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadHistory()
        XCTAssertTrue(vm.campaigns.isEmpty)
        XCTAssertTrue(vm.errorMessage != nil || vm.campaignError != nil)
    }

    func testACancelledContactsLoadSetsNoError() async {
        let client = EdgeHTTP.client { _ in throw URLError(.cancelled) }
        let vm = GuestTextClubViewModel(client: client)
        await EdgeHTTP.tornDown { await vm.load() }
        XCTAssertNil(vm.errorMessage)
    }

    // MARK: CLIENT-10 — social posts twice

    private func marketing(posts: Box<Int>, reply: String = #"{"ok": true, "post_id": "fb_1"}"#) -> MarketingViewModel {
        let client = EdgeHTTP.client { request in
            posts.value += 1
            Thread.sleep(forTimeInterval: 0.05)
            return EdgeHTTP.reply(request, 200, reply)
        }
        let vm = MarketingViewModel(client: client)
        vm.draft = "Friday fish fry is back."
        vm.hasDraft = true
        return vm
    }

    func testAPostThatAlreadyWentUpIsNotPostedAgain() async {
        let posts = Box(0)
        let vm = marketing(posts: posts)
        await vm.postToFacebook()
        XCTAssertEqual(vm.postedPlatform, "Facebook")
        await vm.postToFacebook()
        XCTAssertEqual(posts.value, 1)
    }

    func testADoubleTapPostsOnce() async {
        let posts = Box(0)
        let vm = marketing(posts: posts)
        async let a: Void = vm.postToFacebook()
        async let b: Void = vm.postToFacebook()
        _ = await (a, b)
        XCTAssertEqual(posts.value, 1)
    }

    func testATimedOutPostSaysToCheckThePageRatherThanRetry() async {
        let client = EdgeHTTP.client { _ in throw URLError(.timedOut) }
        let vm = MarketingViewModel(client: client)
        vm.draft = "Friday fish fry is back."
        vm.hasDraft = true
        await vm.postToFacebook()
        let message = vm.postError ?? ""
        XCTAssertFalse(message.isEmpty)
        XCTAssertFalse(message.lowercased().contains("retry"), message)
    }

    func testARefusedPostKeepsTheServersReason() async {
        let posts = Box(0)
        let vm = marketing(posts: posts, reply: #"{"ok": false, "error": "Facebook isn't connected."}"#)
        await vm.postToFacebook()
        XCTAssertEqual(vm.postError, "Facebook isn't connected.")
        XCTAssertNil(vm.postedPlatform)
    }

    // MARK: CLIENT-33 — scheduled posts on the restaurant's clock

    func testAScheduledSlotDoesNotDependOnThePhonesTimeZone() {
        // An owner picking "11:00 Friday" for a Chicago restaurant while in
        // Los Angeles. The wall-clock stamp sent must be the restaurant's,
        // so it cannot change with the phone's own zone.
        let instant = ISO8601DateFormatter().date(from: "2026-10-02T16:00:00Z")!
        let original = NSTimeZone.default
        defer { NSTimeZone.default = original }
        NSTimeZone.default = TimeZone(identifier: "America/Chicago")!
        let fromChicago = MarketingComposeViewModel.localStamp(instant)
        NSTimeZone.default = TimeZone(identifier: "America/Los_Angeles")!
        let fromLosAngeles = MarketingComposeViewModel.localStamp(instant)
        XCTAssertEqual(fromChicago, "2026-10-02T11:00:00")
        XCTAssertEqual(fromLosAngeles, fromChicago)
    }

    func testTheScheduledStampPinsItsLocale() throws {
        // A phone on the Buddhist or Japanese calendar would write a
        // different year into yyyy; the formatter needs en_US_POSIX.
        let source = try EdgeSource.read("Features/Marketing/MarketingComposeViewModel.swift")
        let stamp = try XCTUnwrap(EdgeSource.slice(source, from: "static func localStamp", length: 260))
        XCTAssertTrue(stamp.contains("en_US_POSIX"))
    }
}
