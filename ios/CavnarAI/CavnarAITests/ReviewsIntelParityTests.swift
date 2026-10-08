import XCTest
import Contacts
@testable import CavnarAI

/// Web vs iOS parity audit (10/7/26), Reviews and Intel: the decoding and
/// request shapes behind #9, #21, #34, #52, #53, #75 and #91.
final class ReviewsIntelParityTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func review(_ extra: String = "", status: String = "drafted", platform: String = "google",
                        rating: Int = 4, urgency: String = "normal") throws -> Review {
        try decode(Review.self, """
            {"id": 9, "platform": "\(platform)", "author": "Ann", "rating": \(rating), "text": "Fine",
             "review_date": "2026-10-01T18:00:00", "urgency": "\(urgency)", "draft_response": "Thanks!",
             "response_status": "\(status)", "categories": [] \(extra)}
            """)
    }

    private func object(_ value: some Encodable) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any] ?? [:]
    }

    // MARK: #9 the reply actually posted

    func testAnsweredElsewhereCarriesTheRealReplyItsSourceAndDate() throws {
        let r = try review(#", "replied_elsewhere": true, "external_reply": "Thanks Ann — Danny", "external_reply_source": "google", "external_reply_at": "2026-10-03", "posted_at": "2026-10-04 12:00:00""#,
                           status: "posted")
        XCTAssertTrue(r.repliedElsewhere)
        XCTAssertEqual(r.externalReply, "Thanks Ann — Danny")
        XCTAssertEqual(r.externalReplyLine, "Read from Google \u{00B7} 10/3/26")
        XCTAssertEqual(r.statusPill?.label, "Replied on Google")
        let marked = try review(#", "replied_elsewhere": 1, "external_reply_source": "owner""#, status: "posted")
        XCTAssertEqual(marked.externalReplyLine, "Marked by your team")
        XCTAssertNil(marked.externalReply)
    }

    // MARK: #21 failed posts and the 10/6 pills

    @MainActor
    func testPostFailedIsTheServersAndSurvivesStatusChanges() throws {
        let failed = try review(#", "post_failed": true"#, status: "approved")
        XCTAssertTrue(failed.postFailed)
        XCTAssertEqual(failed.statusPill?.label, "Couldn\u{2019}t post to Google")
        XCTAssertEqual(failed.statusPill?.tone, .bad)
        // The detail screen opens on the failure and its Retry.
        let vm = ReviewDetailViewModel(review: failed)
        XCTAssertTrue(vm.postFailedOnGoogle)
        XCTAssertEqual(vm.postFailure, ReviewDetailViewModel.savedFailureNote)
        XCTAssertEqual(vm.listStatus, "approved-failed")
        // Not connected: approved, waiting — no failure.
        let waiting = try review(#", "post_failed": 0"#, status: "approved")
        XCTAssertFalse(waiting.postFailed)
        XCTAssertEqual(waiting.statusPill?.label, "Approved \u{00B7} not on Google yet")
        XCTAssertEqual(waiting.statusPill?.tone, .warning)
        XCTAssertNil(ReviewDetailViewModel(review: waiting).postFailure)
        // A swipe-approve that Google refused marks the row the same way.
        let drafted = try review()
        XCTAssertTrue(drafted.withStatus("approved-failed").postFailed)
        XCTAssertEqual(drafted.withStatus("approved-failed").responseStatus, "approved")
        XCTAssertFalse(drafted.withStatus("approved").postFailed)
    }

    func testThePillsMatchTheWebCardsWordsAndTones() throws {
        XCTAssertEqual(try review(status: "posted").statusPill?.label, "Live on Google")
        XCTAssertEqual(try review(status: "posted", platform: "yelp").statusPill?.label, "Live on Yelp")
        XCTAssertEqual(try review(status: "skipped").statusPill?.label, "Skipped \u{00B7} no reply sent")
        XCTAssertEqual(try review(status: "skipped").statusPill?.tone, .neutral)
        XCTAssertNil(try review(status: "drafted").statusPill, "a review still waiting has no pill")
        XCTAssertNil(try review(status: "pending").statusPill)
        XCTAssertTrue(try review(status: "posted").isHandled)
        XCTAssertFalse(try review(status: "drafted").isHandled)
    }

    // MARK: #91 server urgency, severity reason

    func testUrgencyIsTheServersNeverRecomputed() throws {
        // A 2-star review the server does not call urgent is not urgent here.
        XCTAssertFalse(try review(#", "urgent": false"#, rating: 2).isUrgent)
        XCTAssertTrue(try review(#", "urgent": true"#, rating: 5).isUrgent)
        // An older payload without the field: the analyser's call only.
        XCTAssertTrue(try review(rating: 4, urgency: "high").isUrgent)
        XCTAssertFalse(try review(rating: 1).isUrgent)
        // Answered is never urgent.
        XCTAssertFalse(try review(#", "urgent": true"#).withStatus("posted").isUrgent)
        let sev = try review(#", "severity": "safety", "severity_label": "Safety", "severity_reason": "Safety: raw chicken. Guest health.""#)
        XCTAssertEqual(sev.severityReason, "Safety: raw chicken. Guest health.")
    }

    // MARK: #34 bulk actions and Publish N ready

    @MainActor
    func testBulkApproveSendsReviewIdsInChunksOfTwentyFive() throws {
        let bodies = ReviewsListViewModel.bulkApproveBodies(Array(1...30))
        XCTAssertEqual(bodies.count, 2)
        let first = try JSONSerialization.jsonObject(with: bodies[0]) as? [String: Any]
        XCTAssertEqual((first?["review_ids"] as? [Int])?.count, 25)
        XCTAssertEqual(first?["limit"] as? Int, 25)
        let second = try JSONSerialization.jsonObject(with: bodies[1]) as? [String: Any]
        XCTAssertEqual(second?["review_ids"] as? [Int], [26, 27, 28, 29, 30])
        XCTAssertEqual(second?["limit"] as? Int, 5)
    }

    @MainActor
    func testOnlyAWaitingReviewIsSelectableAndOnlyAQuickApprovableOneGoesInBulk() throws {
        XCTAssertTrue(ReviewsListViewModel.isSelectable(try review(status: "drafted")))
        XCTAssertTrue(ReviewsListViewModel.isSelectable(try review(status: "skipped")))
        XCTAssertFalse(ReviewsListViewModel.isSelectable(try review(status: "posted")))
        XCTAssertFalse(ReviewsListViewModel.isSelectable(try review(status: "approved")))
        XCTAssertFalse(ReviewsListViewModel.isSkippable(try review(status: "skipped")))
        XCTAssertTrue(ReviewsListViewModel.isSkippable(try review(status: "pending")))
        XCTAssertEqual(PublishReadyViewModel.proposeBody().flatMap {
            try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["action"] as? String,
                       "approve_all_reviews")
    }

    func testReviewStatsCarryPublishableAndTheReceivedLine() throws {
        let stats = try decode(ReviewStats.self, """
            {"total": 40, "positive": 30, "positive_pct": 75, "negative": 5, "neutral": 5, "urgent": 1,
             "avg_rating": 4.4, "avg_rating_30d": 4.5, "awaiting_approval": 3, "needs_response": 1,
             "posted": 30, "responded": 31, "skipped": 2, "this_month": 6, "received_this_month": 6,
             "last_30d": 9, "response_rate": 80, "avg_response_hours": 5.3,
             "publishable": 2, "publish_held": 1}
            """)
        XCTAssertEqual(stats.publishable, 2)
        XCTAssertEqual(stats.publishHeld, 1)
        XCTAssertEqual(stats.receivedLine, "6 new this month \u{00B7} avg reply 5.3h")
        XCTAssertEqual(ReviewStats.replyTime(nil), "\u{2014}")
        XCTAssertEqual(ReviewStats.replyTime(800), "\u{2014}", "over 30 days is not a reply time")
        XCTAssertEqual(ReviewStats.replyTime(50), "2d")
        XCTAssertEqual(ReviewStats.replyTime(400), "2w")
    }

    // MARK: #53 Yelp

    func testYelpOpensYelpForBusiness() {
        XCTAssertEqual(ReviewDetailView.yelpForBusinessURL.host, "business.yelp.com")
    }

    // MARK: #75 AI visibility history and the job

    func testQuestionHistoryDecodesDotsWithGapsAndMatchesTheAnswer() throws {
        let h = try decode(AIVisibilityQueryHistory.self, """
            {"ok": true, "runs": [{"run_id": "a", "at": "2026-09-21 09:00:00"}, {"run_id": "b", "at": "2026-09-28 09:00:00"}],
             "queries": [{"query": "Best pizza in St Charles", "kind": "discovery", "appeared": [true, null],
                          "appearances": 1, "asked": 1}]}
            """)
        let q = try XCTUnwrap(h.question(for: "best  pizza in st charles"))
        XCTAssertEqual(q.appeared, [true, nil])
        XCTAssertEqual(AIVisibilityQueryHistory.line(q), "named in 1 of 1 check")
        XCTAssertNil(h.question(for: "something else"))
    }

    func testTheCheckAsksForAJob() throws {
        let body = try object(AIVisibilityViewModel.CheckBody())
        XCTAssertEqual(body["async"] as? Bool, true)
    }

    // MARK: #52 website analytics

    func testWebsiteSummaryAndConnectionDecode() throws {
        let s = try decode(WebsiteSummary.self, """
            {"ok": true, "connected": true, "configured": true, "available": true, "synced_at": "2026-10-06 12:00:00",
             "totals": [{"metric": "sessions", "label": "Website visits", "value": 1240, "through": "10/5/26", "prev": null, "pct": null},
                        {"metric": "booking_clicks", "label": "Clicks to book", "value": 80, "pct": -12}],
             "line": [{"day": "2026-10-01", "value": 40}, {"day": "2026-10-02", "value": null}],
             "channels": [{"name": "Organic Search", "value": 600, "share": 48, "family": null}],
             "clicks": [{"name": "opentable.com", "value": 70, "share": 90, "family": "booking_clicks"}],
             "queries": [{"query": "simple ejs", "clicks": 120, "impressions": 900, "position": 1.2}],
             "queries_through": "10/4/26",
             "signals": [{"kind": "spike", "metric": "sessions", "day": "2026-10-03", "text": "Website visits jumped to 90 on Friday 10/3/26, against a typical Friday's 40 (+125%).", "context": ["Sox game 6:10pm"]}],
             "signals_basis": "each day against the median of the same weekday over the 6 weeks before; what else happened that day moved with it, which is not proof it caused it"}
            """)
        XCTAssertTrue(s.available)
        XCTAssertEqual(WebsiteSummary.changeLine(s.totals[0]), "last 28 days", "no prior period is never 0%")
        XCTAssertEqual(WebsiteSummary.changeLine(s.totals[1]), "-12% vs the 28 days before")
        XCTAssertNil(s.line[1].value, "a missing day stays missing")
        XCTAssertEqual(WebsiteSummary.clickLabel(s.clicks[0]), "opentable.com \u{00B7} booking")
        XCTAssertTrue(s.signals[0].isUp)
        XCTAssertTrue(s.basisSentence?.hasPrefix("Each day against") == true)
        XCTAssertTrue(s.basisSentence?.hasSuffix("caused it.") == true)

        let c = try decode(WebsiteConnection.self, """
            {"ok": true, "configured": true, "service_email": "reader@cavnar.iam.gserviceaccount.com",
             "ga4_property_id": "412345678", "gsc_site_url": null, "can_edit": true,
             "checks": {"ga4": {"ok": true}, "gsc": {"ok": false, "error": "Add the address in Search Console."}}}
            """)
        XCTAssertTrue(c.isConnected)
        XCTAssertEqual(c.checkLines.count, 2)
        XCTAssertFalse(c.checkLines[1].ok)
        let body = try object(WebsiteConnectionBody(ga4PropertyId: "412345678", gscSiteUrl: ""))
        XCTAssertEqual(Set(body.keys), ["ga4_property_id", "gsc_site_url"])
    }

    // MARK: Intel focus and distance

    func testIntelOpensOnTheSectionTheServerNamed() {
        XCTAssertEqual(IntelSubTab(section: "ai-visibility"), .aiVisibility)
        XCTAssertEqual(IntelSubTab(section: "ai_visibility"), .aiVisibility)
        XCTAssertEqual(IntelSubTab(section: "competitors"), .competitors)
        XCTAssertEqual(IntelSubTab(section: "intel"), .competitors)
        XCTAssertNil(IntelSubTab(section: nil))
    }

    func testCompetitorDistanceReadsInMilesForAUSPhone() {
        let us = IntelView.distanceText(meters: 3219, locale: Locale(identifier: "en_US"))
        XCTAssertTrue(us.contains("mi"), us)
        XCTAssertFalse(us.contains("km"), us)
        let fr = IntelView.distanceText(meters: 3219, locale: Locale(identifier: "fr_FR"))
        XCTAssertTrue(fr.contains("km"), fr)
    }

    // MARK: #91 the contacts picker

    func testAPickedContactFillsTheFormButNeverConsent() {
        let c = CNMutableContact()
        c.givenName = "Dana"
        c.familyName = "Lee"
        c.emailAddresses = [CNLabeledValue(label: CNLabelHome, value: "dana@example.com" as NSString)]
        c.phoneNumbers = [CNLabeledValue(label: CNLabelPhoneNumberMobile, value: CNPhoneNumber(stringValue: "312-555-0100"))]
        let pick = GuestContactPick.from(c)
        XCTAssertEqual(pick, GuestContactPick(name: "Dana Lee", email: "dana@example.com", phone: "312-555-0100"))
    }
}
