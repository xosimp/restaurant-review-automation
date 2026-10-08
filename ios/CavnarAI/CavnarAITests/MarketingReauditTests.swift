import XCTest
@testable import CavnarAI

/// Blind re-audit of the Marketing work (10/8/26): the quiet-night push
/// lands on its card AND its drafted post, a saved text or email opens with
/// its words, every flagged channel gets its sheet, one Send is one send,
/// the owner's audience pick stands, and the Campaigns tab offers only what
/// this login may do.
@MainActor
final class MarketingReauditTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
    }

    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(text.utf8))
    }

    /// Records what applyFocus asked of the screen, in order.
    final class Recorder: MarketingFocusTarget {
        enum Step: Equatable { case tab(MarketingSubTab), shelf(MarketingShelfDestination), card(String), draft(Int), feed }
        var steps: [Step] = []
        func land(on tab: MarketingSubTab) { steps.append(.tab(tab)) }
        func open(shelf: MarketingShelfDestination) { steps.append(.shelf(shelf)) }
        func focusCard(_ key: String) async { steps.append(.card(key)) }
        func openDraft(id: Int) async { steps.append(.draft(id)) }
        func scrollToFeed() { steps.append(.feed) }
    }

    private func land(_ path: String) async throws -> [Recorder.Step] {
        let route = try XCTUnwrap(ModuleRoute.from(XCTUnwrap(NavPath(path))))
        let target = Recorder()
        await MarketingFocus(route: route).plan.apply(to: target)
        return target.steps
    }

    // MARK: #3 — the quiet-night push: the card AND the drafted post

    func testTheQuietNightPushFocusesItsCardAndOpensItsDraft() async throws {
        let nav = try XCTUnwrap(NavPath("marketing/opportunities?card=slow_day%3ATuesday&post_draft_id=41"))
        let focus = MarketingFocus(route: ModuleRoute.from(nav))
        XCTAssertEqual(focus.card, "slow_day:Tuesday", "the post's id no longer stands in for the card")
        XCTAssertEqual(focus.draftId, 41)
        XCTAssertNil(focus.item)
        let steps = try await land("marketing/opportunities?card=slow_day%3ATuesday&post_draft_id=41")
        XCTAssertEqual(steps, [.tab(.content), .card("slow_day:Tuesday"), .draft(41)])
    }

    func testACardWithNoDraftLightsTheCardAndScrollsToTheFeed() async throws {
        let byQuery = try await land("marketing/opportunities?card=slow_day%3ATuesday")
        XCTAssertEqual(byQuery, [.tab(.content), .card("slow_day:Tuesday"), .feed])
        let byPath = try await land("marketing/opportunities/post_this_week")
        XCTAssertEqual(byPath, [.tab(.content), .card("post_this_week"), .feed])
    }

    func testTheOtherSectionsStillLandWhereTheyDid() async throws {
        let campaigns = try await land("marketing/campaigns")
        XCTAssertEqual(campaigns, [.tab(.campaigns)])
        let club = try await land("marketing/text-club")
        XCTAssertEqual(club, [.shelf(.guestTextClub)])
        let draft = try await land("marketing/drafts/12")
        XCTAssertEqual(draft, [.draft(12)])
        let drafts = try await land("marketing/drafts")
        XCTAssertEqual(drafts, [.shelf(.drafts)])
        let post = try await land("marketing?post_draft_id=7")
        XCTAssertEqual(post, [.draft(7)])
    }

    // MARK: #4 — a saved text or email opens with its words

    private func draft(_ type: String, topic: String, body: String) throws -> MarketingDraft {
        let object: [String: Any] = ["id": 3, "content_type": type, "topic": topic, "body": body, "status": "draft"]
        return try JSONDecoder.cavnar.decode(MarketingDraft.self, from: JSONSerialization.data(withJSONObject: object))
    }

    func testASavedEmailDraftOpensWithItsLetter() throws {
        let seed = try XCTUnwrap(StudioSeed.savedDraft(draft("weekly_email", topic: "Weekly email",
                                                             body: "SUBJECT LINE: Fall\nBODY:\nPumpkin pie is back.")))
        XCTAssertEqual(seed.savedEmail, "SUBJECT LINE: Fall\nBODY:\nPumpkin pie is back.")
        XCTAssertEqual(seed.channels, [.email])
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": false}"#) })
        vm.apply(seed)
        XCTAssertEqual(vm.letter, "SUBJECT LINE: Fall\nBODY:\nPumpkin pie is back.")
        XCTAssertTrue(vm.isOn(.email))
        XCTAssertFalse(vm.isOn(.text))
        XCTAssertEqual(vm.prompt, "Your weekly email")
    }

    func testASavedQuietNightTextOpensWithItsWordsAndItsGoal() throws {
        let seed = try XCTUnwrap(StudioSeed.savedDraft(draft("guest_sms", topic: "Tuesday night guest text",
                                                             body: "Half-price wings tonight!")))
        XCTAssertEqual(seed.prompt, "Fill Tuesday dinner")
        XCTAssertEqual(seed.savedText, "Half-price wings tonight!")
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": false}"#) })
        vm.apply(seed)
        XCTAssertEqual(vm.message, "Half-price wings tonight!")
        XCTAssertEqual(vm.prompt, "Fill Tuesday dinner")
        XCTAssertTrue(vm.isOn(.text))
        XCTAssertNil(StudioSeed.savedDraft(try draft("instagram_post", topic: "Fall", body: "Fall menu")),
                     "a post goes to the composer, not the Studio")
        XCTAssertEqual(StudioSeed.goal(fromTopic: "Re-engagement text"), "Re-engagement text")
    }

    // MARK: #6 — a text and an email both flagged: two sheets, in turn

    private func readyStudio(_ handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) throws
        -> CampaignStudioViewModel {
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client(handler))
        vm.overview = try decode(GuestOverview.self, #"{"ok": true, "subscribers": 40, "email_subscribers": 18, "sending_now": true, "mailing_address_set": true}"#)
        vm.segments = [GuestSegment(key: "all", label: "Everyone consented", help: "", count: 40, eligible: 31, emailCount: 18)]
        vm.apply(StudioSeed(prompt: "Fill Tuesday", channels: [.text, .email]))
        vm.message = "See you Tuesday"
        vm.subject = "Tuesday"
        vm.letter = "Come in"
        return vm
    }

    func testEveryFlaggedChannelGetsItsSheet() async throws {
        let vm = try readyStudio { r in
            let email = r.url?.path == "/mobile/api/guest-newsletter"
            return EdgeHTTP.reply(r, 400, email
                ? #"{"ok": false, "gate_flagged": true, "reasons": ["A price we can't check"], "error": "Held."}"#
                : #"{"ok": false, "blocked": "gate_flagged", "reasons": ["A claim"], "error": "Held."}"#)
        }
        let sent = await vm.send(vm.snapshot())
        XCTAssertTrue(sent)
        XCTAssertEqual(vm.gateFlags.map(\.channel), ["text", "email"])
        XCTAssertEqual(vm.gateFlag?.channel, "text")
        vm.gateFlag = nil
        XCTAssertEqual(vm.gateFlag?.channel, "email", "dismissing the first shows the next")
        vm.gateFlag = nil
        XCTAssertNil(vm.gateFlag)
    }

    // MARK: #8 — a second press while the first is in flight sends nothing

    func testASecondPressWhileSendingIsNotASend() async throws {
        let posts = Box<Int>(0)
        let vm = try readyStudio { r in
            if r.httpMethod == "POST" { posts.value += 1 }
            return EdgeHTTP.reply(r, 200, #"{"ok": true, "total": 31, "sent": 18, "failed": 0, "queued": 0}"#)
        }
        let snap = vm.snapshot()
        async let first = vm.send(snap)
        async let second = vm.send(snap)
        let (a, b) = await (first, second)
        XCTAssertEqual([a, b].filter { $0 }.count, 1, "exactly one press sends")
        XCTAssertEqual(posts.value, 2, "one text and one email, once")
    }

    // MARK: #9 — the owner's audience pick survives a plan that answers later

    func testTheOwnersAudiencePickIsNotReplacedByALatePlan() async throws {
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client { r in
            EdgeHTTP.reply(r, 200, #"{"ok": true, "message": "Wings tonight", "segment": "all", "type": "slow_night", "goal": "Fill Tuesday"}"#)
        })
        vm.pickSegment("lapsed_30")
        await vm.draft(.text, plan: true)
        XCTAssertEqual(vm.segment, "lapsed_30")
        XCTAssertFalse(vm.pickedByAI)
        XCTAssertEqual(vm.message, "Wings tonight")
        // With no pick, the plan still chooses.
        let fresh = CampaignStudioViewModel(client: EdgeHTTP.client { r in
            EdgeHTTP.reply(r, 200, #"{"ok": true, "message": "Wings tonight", "segment": "lapsed_60", "type": "win_back"}"#)
        })
        await fresh.draft(.text, plan: true)
        XCTAssertEqual(fresh.segment, "lapsed_60")
        XCTAssertTrue(fresh.pickedByAI)
    }

    // MARK: #7 — a flagged win-back opens "Cavnar AI flagged this"

    func testAFlaggedWinbackOpensTheFlagSheet() async throws {
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/guest-winback" {
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "available": true, "draft": {"id": 9, "segment_size": 42, "message": "Come back!",
                     "max_chars": 320}}
                    """)
            }
            if request.url?.path == "/mobile/api/guest-winback/9/send" {
                return EdgeHTTP.reply(request, 400, #"{"ok": false, "blocked": "gate_flagged", "reasons": ["A discount we can't check"], "error": "Cavnar AI held this text back."}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "campaigns": []}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadWinback()
        await vm.sendWinback()
        let flag = try XCTUnwrap(vm.gateFlag)
        XCTAssertEqual(flag.channel, "winback")
        XCTAssertEqual(flag.reasons, ["A discount we can't check"])
        XCTAssertNil(vm.winbackSentTotal)
        vm.discardWinbackText()
        XCTAssertEqual(vm.winbackMessage, "")
    }

    // MARK: #1 / #13 — what this login may do

    func testTheOverviewAndInvitesSayWhatThisLoginMayDo() throws {
        let o = try decode(GuestOverview.self, #"{"ok": true, "can_publish": false}"#)
        XCTAssertEqual(o.canPublish, false)
        XCTAssertNil(try decode(GuestOverview.self, #"{"ok": true}"#).canPublish)
        let inv = try decode(OptinInvitesState.self, #"{"ok": true, "enabled": false, "can_change": false}"#)
        XCTAssertEqual(inv.canChange, false)
        let admin = User(id: 1, username: "will", email: "w@x.test", restaurantId: 2, role: "client", isAdmin: true)
        XCTAssertTrue(admin.mayPublishMarketing)
        let member = User(id: 2, username: "t", email: "t@x.test", restaurantId: 2, role: "member", isAdmin: false)
        XCTAssertFalse(member.mayPublishMarketing)
        let manager = User(id: 3, username: "m", email: "m@x.test", restaurantId: 2, role: "manager", isAdmin: false)
        XCTAssertTrue(manager.mayPublishMarketing)
        XCTAssertFalse(manager.isAccountOwnerLogin)
        let adminElsewhere = User(id: 4, username: "a", email: "a@x.test", restaurantId: 2, role: "support", isAdmin: true)
        XCTAssertFalse(adminElsewhere.isAccountOwnerLogin, "the invite switch is the owner's, not an admin's")
    }

    // MARK: #11 — the month line: the restaurant's month, and "—" for no figure

    func testTheMonthLineSaysDashForNoFigureAndCountsTheLocalMonth() throws {
        let vm = CampaignsTabViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": true}"#) })
        vm.overview = try decode(GuestOverview.self, #"{"ok": true}"#)
        XCTAssertEqual(vm.monthLine, "\u{2014} texted this month")
        vm.overview = try decode(GuestOverview.self, #"{"ok": true, "texts_this_month": 0}"#)
        XCTAssertEqual(vm.monthLine, "0 texted this month", "a measured zero stays a zero")
        let stamp = "2026-11-01 02:00:00"
        let date = try XCTUnwrap(CavnarDate.timestamp(stamp))
        guard RestaurantClock.timeZone.secondsFromGMT(for: date) <= -3 * 3600 else {
            throw XCTSkip("needs a restaurant zone west of UTC")
        }
        XCTAssertEqual(CampaignsTabViewModel.localMonth(of: stamp), "2026-10",
                       "2 AM UTC on the 1st is still the last month in Chicago")
    }
}
