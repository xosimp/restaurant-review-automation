import XCTest
@testable import CavnarAI

/// iOS parity round (10/7/26), Marketing: the shapes the Campaign Studio,
/// the Text Club, the Campaigns tab and the Content tab send and decode.
/// A request body must carry every key its route reads — the root cause of
/// half the Critical findings — so each is pinned here.
@MainActor
final class MarketingParityTests: XCTestCase {

    override func tearDown() async throws {
        MockURLProtocol.requestHandler = nil
    }

    private func json(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(text.utf8))
    }

    // MARK: #12 — "Send to N" counts who can be texted now

    func testASegmentPromisesWhoATextReachesNow() throws {
        let s = try decode(GuestSegment.self, #"{"key": "all", "label": "Everyone", "help": "", "count": 40, "eligible": 31, "email_count": 18}"#)
        XCTAssertEqual(s.reach, 31)
        XCTAssertEqual(s.emailCount, 18)
        let old = try decode(GuestSegment.self, #"{"key": "all", "label": "Everyone", "help": "", "count": 40}"#)
        XCTAssertEqual(old.reach, 40, "an older server without eligible promises the count")
    }

    func testTheTextClubSendsToTheEligibleCount() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, """
            {"ok": true, "defaults": {}, "segments": [
              {"key": "all", "label": "Everyone", "help": "", "count": 40, "eligible": 31, "email_count": 0}]}
            """)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadSegments()
        XCTAssertEqual(vm.selectedSegmentCount, 31)
        XCTAssertEqual(vm.selectedSegmentTotal, 40)
    }

    // MARK: #39 — hold, target_day and rec_key on a text send

    func testATextClubSendOutsideTheWindowIsHeldWithEveryKey() async throws {
        let body = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/guest-overview":
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "sending_now": false, "window": "8:00 AM and 9:00 PM"}"#)
            case "/mobile/api/guest-campaign/send":
                body.value = EdgeHTTP.bodyJSON(request)
                return EdgeHTTP.reply(request, 202, """
                {"ok": true, "queued": true, "campaign_id": 3, "total": 12, "waiting": true, "waiting_until": "8:00 AM"}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "contacts": [], "campaigns": [], "segments": [], "defaults": {}}"#)
            }
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadOverview()
        XCTAssertFalse(vm.sendingNow)
        XCTAssertEqual(vm.opensAt, "8:00 AM")
        vm.draftMessage = "See you tomorrow"
        await vm.sendCampaign()
        let sent = try XCTUnwrap(body.value)
        XCTAssertEqual(sent["hold"] as? Bool, true)
        XCTAssertNotNil(sent["target_day"])
        XCTAssertNotNil(sent["rec_key"])
        XCTAssertEqual(sent["segment"] as? String, "all")
        XCTAssertEqual(vm.queuedCount, 12)
        XCTAssertEqual(vm.sendLine?.contains("wait until 8:00 AM"), true)
    }

    // MARK: #74 — the send gate's reasons

    func testAFlaggedTextOpensWhyWithItsReasons() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 400, """
            {"ok": false, "blocked": "gate_flagged", "error": "Cavnar AI held this text back.",
             "reasons": ["It promises a discount you didn't write"], "sent": 0, "total": 0}
            """)
        }
        let vm = GuestTextClubViewModel(client: client)
        vm.draftMessage = "50% off tonight"
        await vm.sendCampaign()
        let flag = try XCTUnwrap(vm.gateFlag)
        XCTAssertEqual(flag.channel, "text")
        XCTAssertEqual(flag.reasons, ["It promises a discount you didn't write"])
        XCTAssertFalse(vm.didSend)
        vm.discardDraft()
        XCTAssertEqual(vm.draftMessage, "")
    }

    // MARK: #30 — the Studio's send bodies

    func testTheStudioTextBodyCarriesEveryKeyTheRouteReads() throws {
        let b = CampaignTextSendBody(message: "Hi", segment: "lapsed_30", type: "win_back", targetDay: "Tuesday",
                                     linkUrl: "https://ex.com", hold: true, recKey: "slow_day:Tuesday", draftRef: nil)
        let j = try json(b)
        XCTAssertEqual(Set(j.keys), ["message", "segment", "type", "target_day", "link_url", "hold", "rec_key", "draft_ref"])
        XCTAssertTrue(j["draft_ref"] is NSNull, "draft_ref is sent, null when there was no model draft")
        XCTAssertEqual(j["hold"] as? Bool, true)
    }

    func testTheStudioEmailBodyCarriesDesignSegmentAndTheAddressOnlyWhenTyped() throws {
        var b = NewsletterSendBody(subject: "Fall", body: "Hello", design: NewsletterDesign(headline: "H", imageMediaId: 7),
                                   segment: "regulars", recKey: "list_idle:email", draftRef: 4)
        var j = try json(b)
        XCTAssertEqual(Set(j.keys), ["subject", "body", "design", "segment", "rec_key", "draft_ref"])
        let design = try XCTUnwrap(j["design"] as? [String: Any])
        XCTAssertEqual(Set(design.keys), ["headline", "preheader", "button_label", "button_url", "image_media_id"])
        XCTAssertEqual(design["image_media_id"] as? Int, 7)
        b.mailingAddress = "1 Main St, Chicago IL 60601"
        j = try json(b)
        XCTAssertEqual(j["mailing_address"] as? String, "1 Main St, Chicago IL 60601")
    }

    func testEachPlatformGetsTheBodyItsRouteReads() throws {
        let ig = try json(StudioPostBody(platform: "instagram", caption: "C", topic: "T", mediaId: 5,
                                         imageURL: "https://x/m/a.jpg", recKey: "k", contentLogId: 9))
        XCTAssertEqual(Set(ig.keys), ["caption", "topic", "image_url", "media_id", "rec_key", "content_log_id"])
        let fb = try json(StudioPostBody(platform: "facebook", caption: "C", topic: "T", mediaId: 5, recKey: "k"))
        XCTAssertEqual(Set(fb.keys), ["caption", "topic", "media_id", "rec_key"])
        let g = try json(StudioPostBody(platform: "google", caption: "C", topic: "T", mediaId: 5, recKey: ""))
        XCTAssertEqual(Set(g.keys), ["summary", "cta_type", "cta_url", "topic", "media_id", "rec_key"])
        XCTAssertEqual(StudioPostBody(platform: "google", caption: "", topic: "").path, "/mobile/api/marketing/google-post")
    }

    // MARK: #17 / #72 — honest newsletter results

    func testANewsletterResultNamesSentFailedSkippedAndQueued() throws {
        let r = try decode(NewsletterSendResult.self, """
        {"ok": true, "newsletter_id": 4, "sent": 3, "failed": 2, "retryable": 2, "skipped": 1, "total": 40, "queued": 34}
        """)
        XCTAssertEqual(r.summary, "Emailed 3 guests \u{00B7} 34 more queued, going out in the background \u{00B7} 2 failed \u{00B7} 1 skipped (unsubscribed or suppressed)")
        XCTAssertEqual(r.retryCount, 2)
        XCTAssertFalse(r.isClean)
    }

    func testAnEmailAlreadySentOffersTheNewSubscribers() throws {
        let r = try decode(NewsletterSendResult.self, """
        {"ok": false, "already_sent": true, "newsletter_id": 4, "new_subscribers": 3,
         "error": "Already sent on 10/7/26 \u{2014} 3 new subscribers since.", "sent": 40}
        """)
        XCTAssertEqual(r.newCount, 3)
        XCTAssertTrue(r.summary.hasPrefix("Email: Already sent on 10/7/26"))
    }

    func testTheStudioAsksForTheAddressAndOffersTheNewSubscribers() async throws {
        let paths = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            paths.value.append(request.url?.path ?? "")
            switch request.url?.path {
            case "/mobile/api/guest-newsletter":
                return EdgeHTTP.reply(request, 400, """
                {"ok": false, "needs_mailing_address": true, "error": "Add your restaurant's mailing address before sending."}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
            }
        }
        let vm = CampaignStudioViewModel(client: client)
        vm.overview = GuestOverview()
        vm.overview?.mailingAddressSet = true
        let snap = StudioSnapshot(segment: "all", email: NewsletterSendBody(subject: "S", body: "B",
                                                                            design: NewsletterDesign(), segment: "all"))
        await vm.send(snap)
        XCTAssertEqual(vm.results.count, 1)
        XCTAssertFalse(vm.results[0].good)
        XCTAssertEqual(vm.overview?.mailingAddressSet, false, "the address field opens again")
        XCTAssertTrue(paths.value.contains("/mobile/api/guest-newsletter"))
    }

    // MARK: #40 — campaign status and Stop sending

    func testACampaignSaysWhereItStandsAndNeverShowsAnISODate() throws {
        let c = try decode(GuestCampaign.self, """
        {"id": 9, "message": "Hi", "sent_count": 12, "failed_count": 0, "clicks": 0, "status": "waiting",
         "pending": 30, "total": 42, "waiting_until": "8:00 AM", "created_at": "garbled"}
        """)
        XCTAssertTrue(c.isOpen)
        XCTAssertEqual(c.statusLabel, "30 texts wait until 8:00 AM")
        XCTAssertEqual(c.whenLabel, "\u{2014}")
        let sending = try decode(GuestCampaign.self, """
        {"id": 9, "message": "Hi", "sent_count": 12, "failed_count": 0, "clicks": 0, "status": "sending",
         "pending": 30, "total": 42, "created_at": "2026-10-07 14:00:00"}
        """)
        XCTAssertEqual(sending.statusLabel, "Sending \u{00B7} 12 of 42")
        XCTAssertFalse(sending.whenLabel.contains("-"))
    }

    func testStopSendingPostsToTheCancelRoute() async throws {
        let lines = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            lines.value.append(EdgeHTTP.line(request))
            if request.url?.path == "/mobile/api/guest-campaign/9/cancel" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "cancelled": 30}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = CampaignsTabViewModel(client: client)
        let c = try decode(GuestCampaign.self, #"{"id": 9, "message": "Hi", "sent_count": 1, "failed_count": 0, "clicks": 0, "status": "sending", "pending": 30}"#)
        await vm.stop(c)
        XCTAssertTrue(lines.value.contains("POST /mobile/api/guest-campaign/9/cancel"))
        XCTAssertEqual(vm.notice, "30 texts won\u{2019}t go out")
    }

    // MARK: #60 — the overview, unknown never zero

    func testTheOverviewKeepsUnknownAsNil() throws {
        let o = try decode(GuestOverview.self, """
        {"ok": true, "subscribers": 40, "today": 1, "last_30": 6, "tap_rate": null, "back_rate": {"pct": 12.5, "campaigns": 3},
         "weekly": [{"week_start": "2026-09-14", "joined": 2}], "window": "8:00 AM and 9:00 PM", "sending_now": true}
        """)
        XCTAssertNil(o.tapRate)
        XCTAssertEqual(o.backRate?.label, "12.5%")
        XCTAssertEqual(o.weekly.first?.shortLabel, "9/14")
        XCTAssertEqual(SMSWindow.opens(o.window), "8:00 AM")
        XCTAssertEqual(SMSWindow.range(o.window), "8:00 AM \u{2013} 9:00 PM")
        let empty = try decode(GuestOverview.self, #"{"ok": true}"#)
        XCTAssertNil(empty.subscribers)
        XCTAssertNil(SMSWindow.opens(empty.window))
    }

    // MARK: SMS bubble counter

    func testTheCounterCountsTheNameTheLinkAndUnicode() {
        var sms = GuestOverview.SMS()
        sms.prefix = "Gia Mia: "
        sms.linkChars = 41
        sms.stopChars = 28
        sms.max = 300
        let plain = SMSMeter.measure(message: "See you tonight", hasLink: false, sms: sms)
        XCTAssertEqual(plain.head, "Gia Mia: ")
        XCTAssertEqual(plain.counted, 9 + 15)
        XCTAssertEqual(plain.parts, 1)
        let named = SMSMeter.measure(message: "Gia Mia: see you", hasLink: true, sms: sms)
        XCTAssertEqual(named.head, "", "a text that opens with the name isn't given it twice")
        XCTAssertEqual(named.counted, 16 + 41)
        let dash = SMSMeter.measure(message: String(repeating: "a", count: 60) + "\u{2014}", hasLink: false, sms: sms)
        XCTAssertTrue(dash.unicode)
        XCTAssertEqual(dash.parts, 2, "70 a part once Unicode")
    }

    // MARK: Studio checks and the head-count label

    func testTheSendLabelNamesHeadCountsAndTheMorningOutsideTheWindow() throws {
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": true}"#) })
        vm.overview = try decode(GuestOverview.self, #"{"ok": true, "subscribers": 40, "email_subscribers": 18, "sending_now": false, "window": "8:00 AM and 9:00 PM", "mailing_address_set": true}"#)
        vm.segments = [GuestSegment(key: "all", label: "Everyone consented", help: "", count: 40, eligible: 31, emailCount: 18)]
        vm.apply(StudioSeed(prompt: "Fill Tuesday", channels: [.text, .email]))
        vm.message = "See you Tuesday"
        vm.subject = "Tuesday"
        vm.letter = "Come in"
        let state = vm.checks
        XCTAssertEqual(state.ready, [.text, .email])
        XCTAssertEqual(state.label, "Text 31 at 8:00 AM \u{00B7} Email 18")
        let snap = vm.snapshot()
        XCTAssertEqual(snap.text?.hold, true)
        XCTAssertEqual(snap.text?.segment, "all")
    }

    func testACardThatCanReachNobodyIsSaidNotDrafted() throws {
        let vm = CampaignStudioViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": true}"#) })
        vm.overview = try decode(GuestOverview.self, #"{"ok": true, "subscribers": 0, "email_subscribers": 0}"#)
        vm.apply(StudioSeed(prompt: "Get a post out", channels: [.social], recKey: "post_this_week"))
        XCTAssertEqual(vm.seedError, CampaignStudioViewModel.noChannelLine([.social]))
    }

    // MARK: #28 — the feed

    func testTheFeedDecodesItsCardsAndSaysWhatWasChecked() throws {
        let feed = try decode(OpportunityFeed.self, """
        {"ok": true, "visible": 3, "items": [
          {"key": "slow_day:Tuesday", "kind": "slow_night", "title": "Fill Tuesday, 10/13/26", "why": "Tuesdays run 22% under",
           "facts": ["8 of 10 Tuesdays"], "stake": {"amount": 3500, "label": "a Tuesday night under a typical day"},
           "days_away": 1, "action": {"prompt": "Fill Tuesday dinner", "channels": ["text", "social"]}, "rec_id": 4},
          {"no_key": true}],
         "sources": [{"key": "sales", "label": "sales", "state": "checked"}, {"key": "web", "label": "website", "state": "failed"}],
         "checked": ["sales"]}
        """)
        XCTAssertEqual(feed.items.count, 1, "a malformed card is skipped, not the feed")
        let card = feed.items[0]
        XCTAssertEqual(card.kindLabel, "Slow night")
        XCTAssertEqual(card.whenLabel, "Tomorrow")
        XCTAssertEqual(card.stakeLine, "$3,500 a Tuesday night under a typical day")
        XCTAssertEqual(card.goal, "Fill Tuesday dinner")
        XCTAssertEqual(card.wantedChannels, ["text", "social"])
        let line = OpportunityFeed.emptyLine(sources: feed.sources, checked: feed.checked)
        XCTAssertTrue(line.warn)
        XCTAssertTrue(line.text.contains("Couldn\u{2019}t check website"))
    }

    // MARK: #6 — Facebook and Google carry the photo

    func testAFacebookPostCarriesThePhotoByItsLibraryID() async throws {
        let body = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            body.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "post_id": "fb_1"}"#)
        }
        let vm = MarketingViewModel(client: client)
        vm.draft = "Wings tonight"
        vm.hasDraft = true
        vm.channels = MarketingChannels(instagram: false, facebook: true)
        await vm.postToAll(media: MarketingMedia(id: 12, token: "tok", url: "https://x/m/tok.jpg"))
        XCTAssertEqual(body.value?["media_id"] as? Int, 12)
    }

    func testAGooglePostCarriesThePhotoByItsLibraryID() async throws {
        let body = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            body.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "post_id": "g_1"}"#)
        }
        let vm = MarketingViewModel(client: client)
        vm.draft = "Pie is back"
        vm.hasDraft = true
        await vm.postToGoogle(mediaId: 8)
        XCTAssertEqual(body.value?["media_id"] as? Int, 8)
        XCTAssertNotNil(body.value?["summary"])
    }

    // MARK: Content is social-only

    func testTheContentTabOffersSocialTypesOnly() throws {
        let vm = MarketingViewModel(client: EdgeHTTP.client { r in EdgeHTTP.reply(r, 200, #"{"ok": true}"#) })
        let ids = vm.socialContentTypes.map(\.id)
        XCTAssertFalse(ids.contains("weekly_email"))
        XCTAssertFalse(ids.contains("loyalty_nudge"))
        XCTAssertTrue(ids.contains("instagram_post"))
        let typed = try decode(MarketingContentType.self, #"{"id": "x", "label": "X", "description": "", "channel": "email"}"#)
        XCTAssertFalse(typed.isSocial)
        XCTAssertEqual(MarketingContentType.guestChannel(of: "loyalty_nudge"), "text")
    }

    // MARK: Top performing post

    func testTheTopPostWaitsForItsFloor() throws {
        let p = try decode(MarketingPerformance.self, """
        {"ok": true, "published": 3, "has_data": true, "total_reach": 10, "total_engagement": 2, "top_post": null,
         "top_post_floor": 6, "measured_posts": 3}
        """)
        XCTAssertEqual(p.topPostWaitLine, "A top post is named once 6 posts are measured \u{2014} 3 so far.")
        let named = try decode(MarketingPerformance.self, """
        {"ok": true, "published": 9, "has_data": true, "total_reach": 10, "total_engagement": 2,
         "top_post": {"topic": "Fall menu", "platform": "instagram", "reach": 1204, "likes": 38, "comments": 0, "shares": 0}}
        """)
        XCTAssertEqual(named.topPostTitle, "Fall menu \u{00B7} instagram")
        XCTAssertEqual(named.topPostMetrics, "1,204 reach \u{00B7} 38 likes")
    }
}
