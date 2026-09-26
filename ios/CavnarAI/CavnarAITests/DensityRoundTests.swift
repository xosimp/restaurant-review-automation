import XCTest
import SwiftUI
@testable import CavnarAI

/// The density / 3-30-300 fix round (9/25/26, agent D): the pure rules
/// behind the reordered screens, pinned here rather than by one rendered
/// payload.
final class DensityRoundTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    // MARK: #1 — the brief headline and the night's verdict

    func testTheBriefHeadDecodesLenientlyAndTonesByState() throws {
        let head = try decode(HomeBriefHead.self, #"{"headline": "2 things need you now", "tone": "bad"}"#)
        XCTAssertEqual(head.headline, "2 things need you now")
        XCTAssertEqual(HomeView.briefToneColor(head.tone), .cavnarRed)
        XCTAssertEqual(HomeView.briefToneColor("warn"), .cavnarAmber)
        XCTAssertEqual(HomeView.briefToneColor("good"), .cavnarGreen)
        XCTAssertEqual(HomeView.briefToneColor("neutral"), .cavnarInk)
        let odd = try decode(HomeBriefHead.self, #"{"headline": 3}"#)
        XCTAssertNil(odd.headline)
    }

    func testTheNightSummaryCarriesTheSharedVerdictFieldsAsOptional() throws {
        let night = try decode(DSRSummary.self, """
            {"business_date": "2026-09-24", "verdict": "Good day", "tone": "good", "overall": 82,
             "vs_budget": 420.5, "first_risk": "Labor ran 2 pts over"}
            """)
        XCTAssertEqual(night.verdict, "Good day")
        XCTAssertEqual(night.overall, 82)
        XCTAssertEqual(night.vsBudget, 420.5)
        XCTAssertEqual(HomeLastNightCard.verdict(night, nil),
                       HomeLastNightCard.Verdict(label: "Good day", tone: "good", overall: 82))

        let older = try decode(DSRSummary.self, #"{"business_date": "2026-09-24"}"#)
        XCTAssertNil(older.verdict)
        XCTAssertNil(older.vsBudget)
        XCTAssertNil(HomeLastNightCard.verdict(older, nil), "no verdict is drawn when none was sent")
    }

    // MARK: #3 — one "Start here"

    func testNeedsAttentionSaysStartHereOnlyWithoutTheOneThing() {
        XCTAssertEqual(HomeView.attentionTitle(hasOneThing: false), "Start here")
        XCTAssertNotEqual(HomeView.attentionTitle(hasOneThing: true), "Start here")
    }

    // MARK: #4 — the Results closed row

    func testTheResultsRowNamesOnlyTheMeasuredFigure() {
        let net = HomeValueHeadline(figure: 1310, isNet: true, breakdown: "x")
        XCTAssertEqual(HomeResultsSummary.line(headline: net, improved: 3, worse: 1),
                       "3 improved \u{00B7} 1 got worse \u{00B7} $1,310/mo net measured")
        let plain = HomeValueHeadline(figure: 420, isNet: false, breakdown: nil)
        XCTAssertEqual(HomeResultsSummary.line(headline: plain, improved: 2, worse: 0),
                       "2 improved \u{00B7} $420/mo measured")
        let nothing = HomeValueHeadline(figure: 0, isNet: false, breakdown: nil)
        XCTAssertEqual(HomeResultsSummary.line(headline: nothing, improved: nil, worse: nil), "Nothing measured yet")
        XCTAssertEqual(HomeResultsSummary.tone(headline: nothing), .cavnarInk3, "never a green $0")
        let loss = HomeValueHeadline(figure: -200, isNet: true, breakdown: nil)
        XCTAssertEqual(HomeResultsSummary.tone(headline: loss), .cavnarRed)
        XCTAssertTrue(HomeResultsSummary.line(headline: loss, improved: 1, worse: 2).contains("\u{2212}$200/mo net"))
    }

    // MARK: #21 — freshness on the chips

    func testOnlyStaleOrDisconnectedSourcesMarkTheirChip() {
        let entries = [
            HomeFreshnessEntry(module: "reviews", source: "reviews", state: .current),
            HomeFreshnessEntry(module: "labor", source: "shifts", state: .stale),
            HomeFreshnessEntry(module: "inventory", source: "pos", state: .disconnected),
            HomeFreshnessEntry(module: "marketing", source: "meta", state: .aging),
            HomeFreshnessEntry(module: "intel", source: nil, state: .sample),
        ]
        XCTAssertEqual(HomePulseStrip.staleModules(entries), ["labor", "inventory"])
    }

    // MARK: #31 — a real "bad", and the grid sorted by it

    func testBadIsRedAndTheGridLeadsWithTrouble() {
        XCTAssertEqual(HomePulseStrip.toneColor("bad"), .cavnarRed)
        XCTAssertEqual(HomePulseStrip.toneColor("warn"), .cavnarAmber)
        XCTAssertEqual(HomePulseStrip.toneColor(nil), .cavnarEmber2)
        func m(_ key: String, _ tone: String?) -> ModuleSummary {
            ModuleSummary(key: key, label: key, icon: key, status: "available", kpi: nil,
                          pulse: ModulePulse(value: "1", label: "\(key) label", tone: tone))
        }
        let sorted = HomeModuleGrid.sorted([m("a", "good"), m("b", nil), m("c", "bad"), m("d", "warn"), m("e", "bad")])
        XCTAssertEqual(sorted.map(\.key), ["c", "e", "d", "a", "b"])
        XCTAssertEqual(KPITile.why(m("x", "bad")), "x label")
        XCTAssertNil(KPITile.why(m("x", "good")), "a healthy tile needs no why")
    }

    // MARK: #23 — one answer per recommendation row

    func testARecommendationRowHasOnePrimaryAnswer() throws {
        let tracked = try decode(HomeRecommendation.self,
                                 #"{"key":"k","title":"Trim Tuesday","metric":"labor_pct","dollars_monthly":420}"#)
        XCTAssertEqual(HomeRecommendations.primaryAnswer(tracked), .track)
        XCTAssertEqual(HomeRecommendations.stake(tracked), "$420/mo at stake")
        let plain = try decode(HomeRecommendation.self, #"{"key":"k2","title":"Call the supplier"}"#)
        XCTAssertEqual(HomeRecommendations.primaryAnswer(plain), .done)
        XCTAssertNil(HomeRecommendations.stake(plain))
    }

    // MARK: #32 — the inbox's why

    func testTheReviewsWhyLineReadsTheUrgentOnesTopic() throws {
        func review(_ id: Int, urgency: String, sentiment: String, _ cats: [String]) throws -> Review {
            try decode(Review.self, """
                {"id": \(id), "platform": "google", "rating": 2, "text": "x", "sentiment": "\(sentiment)",
                 "response_status": "pending", "urgency": "\(urgency)", "categories": \(cats)}
                """)
        }
        // Array.description of [String] is valid JSON: ["wait_time", "food"].
        let rows = [try review(1, urgency: "high", sentiment: "negative", ["wait_time"]),
                    try review(2, urgency: "high", sentiment: "negative", ["wait_time", "food"]),
                    try review(3, urgency: "normal", sentiment: "positive", [])]
        XCTAssertEqual(ReviewsWhyLine.make(urgent: 2, reviews: rows), "2 urgent \u{2014} both about wait time")
        XCTAssertNil(ReviewsWhyLine.make(urgent: 0, reviews: [rows[2]]))
    }

    func testTheServerWhyLineLeadsAndANullDeltaIsNeverFlat() throws {
        let full = try decode(ReviewsWhyPayload.self, """
            {"ok": true, "rating_delta": -0.1, "recent_n": 20, "prior_n": 18, "window_days": 56,
             "complaint": {"category": "food", "label": "Cold food", "mentions": 6, "window_days": 56,
                           "stale": false, "as_of": "9/24/26"}}
            """)
        XCTAssertEqual(ReviewsWhyLine.make(urgent: 0, server: full),
                       "Rating \u{25BC}0.1 over 8 weeks \u{00B7} top complaint: cold food (6 mentions)")
        let floor = try decode(ReviewsWhyPayload.self,
                               #"{"ok": true, "rating_delta": null, "window_days": 56, "complaint": null}"#)
        XCTAssertNil(ReviewsWhyLine.make(urgent: 0, server: floor), "below the floor says nothing, not 'level'")
        XCTAssertNil(ReviewsWhyLine.make(urgent: 2, server: floor), "the urgent count alone is the local line's")
    }

    func testTheMarketingHeaderDecodesLeniently() throws {
        let h = try decode(MarketingHeader.self, """
            {"ok": true, "status": "Nothing posted in 12 days", "tone": "warn", "days": 30,
             "next_scheduled": {"date": "9/26/26", "time": "5:00pm", "platform": "instagram"}}
            """)
        XCTAssertEqual(h.toneColor, .cavnarAmber)
        XCTAssertEqual(h.nextScheduled?.whenLabel, "9/26/26 \u{00B7} 5:00pm")
        let old = try decode(MarketingHeader.self, #"{"ok": true}"#)
        XCTAssertNil(old.status)
    }

    func testTheReviewsCaveatsAreOneLine() {
        XCTAssertNil(ReviewsAnalyticsSection.caveatSummary(unverified: false, stale: false))
        XCTAssertNotNil(ReviewsAnalyticsSection.caveatSummary(unverified: true, stale: true))
    }

    // MARK: #39 — Notifications open on what needs the owner

    func testNotificationsDefaultToNeedsYouWhenSomethingIsUrgent() throws {
        XCTAssertTrue(NotificationsListView.defaultsToUrgent(choice: nil, hasUrgent: true))
        XCTAssertFalse(NotificationsListView.defaultsToUrgent(choice: false, hasUrgent: true))
        XCTAssertFalse(NotificationsListView.defaultsToUrgent(choice: true, hasUrgent: false))
        let items = try decode([NotificationItem].self, """
            [{"type": "a", "label": "A", "fired_at": "2026-09-25 08:00:00", "urgent": true},
             {"type": "b", "label": "B", "fired_at": "2026-09-25 08:00:00", "urgent": false},
             {"type": "c", "label": "C", "fired_at": "2026-09-25 08:00:00", "urgent": false}]
            """)
        // The web bell's words (parity audit #7): what still needs someone,
        // by kind — not a count of the FYIs.
        XCTAssertEqual(NotificationsListView.summaryLine(items), "1 needs you: 1 urgent alert")
        let mixed = try decode([NotificationItem].self, """
            [{"type": "1star", "label": "A", "fired_at": "2026-09-25 08:00:00", "urgent": true,
              "can_approve": true, "draft": "Thank you"},
             {"type": "health", "label": "B", "fired_at": "2026-09-25 08:00:00", "urgent": true},
             {"type": "1star", "label": "C", "fired_at": "2026-09-25 08:00:00", "urgent": true, "resolved": true},
             {"type": "labor_over", "label": "D", "fired_at": "2026-09-25 08:00:00", "urgent": true}]
            """)
        XCTAssertEqual(NotificationsListView.summaryLine(mixed),
                       "3 need you: 1 reply ready to post, 1 health mention, 1 other alert")
        let calm = try decode([NotificationItem].self, """
            [{"type": "b", "label": "B", "fired_at": "2026-09-25 08:00:00", "urgent": false, "unread": true}]
            """)
        XCTAssertEqual(NotificationsListView.summaryLine(calm), "Nothing needs you \u{00B7} 1 unread")
        XCTAssertNil(NotificationsListView.summaryLine([]))
    }

    // MARK: #13 — the staff portal's next shift

    func testNextShiftIsTheFirstWorkedDayNamedTomorrowWhenItIs() throws {
        let week = try decode([StaffWeekDay].self, """
            [{"date": "9/25/26", "weekday": "Thursday", "is_today": true, "off": true, "shift": null},
             {"date": "9/26/26", "weekday": "Friday", "is_today": false, "off": false,
              "shift": {"shift_start": "10am", "shift_end": "4pm", "role": "Server"}}]
            """)
        let next = try XCTUnwrap(StaffPortalView.nextShift(week))
        XCTAssertEqual(next.when, "Tomorrow")
        XCTAssertEqual(next.day.shift?.role, "Server")
        XCTAssertNil(StaffPortalView.nextShift([week[0]]))
    }

    // MARK: #10 — Marketing's one outcome

    func testMarketingLeadsWithReachAgainstThePriorWindow() throws {
        let up = try decode(MarketingWindow.self, """
            {"days": 30, "posts": 4, "reach": 1800, "engagement": 90, "engagement_rate": 5.0,
             "previous": {"posts": 3, "reach": 1500, "engagement": 60, "engagement_rate": 4.0},
             "change": {"posts": 33.0, "reach": 18.0, "engagement": 50.0}, "by_platform": []}
            """)
        let o = MarketingView.outcome(up)
        XCTAssertEqual(o.change, "\u{25B2} 18% vs the 30 days before")
        XCTAssertEqual(o.tone, .cavnarGreen)
        let quiet = try decode(MarketingWindow.self, """
            {"days": 30, "posts": 0, "reach": 0, "engagement": 0, "engagement_rate": null,
             "previous": {"posts": 0, "reach": 0, "engagement": 0, "engagement_rate": null},
             "change": {"posts": null, "reach": null, "engagement": null}, "by_platform": []}
            """)
        XCTAssertEqual(MarketingView.outcome(quiet).tone, .cavnarAmber)
    }

    // MARK: #50 — the widget's verdict

    func testTheWidgetSnapshotCarriesTheVerdictAndStillReadsAnOldOne() throws {
        var snap = WidgetSnapshot.empty
        XCTAssertNil(snap.verdictLine)
        snap.nightVerdict = "Good day"
        snap.nightScore = 82
        XCTAssertEqual(snap.verdictLine, "Good day 82/100")
        let old = try JSONDecoder().decode(WidgetSnapshot.self, from: Data("""
            {"waitingCount": 2, "pendingReplies": 0, "updatedAt": 0}
            """.utf8))
        XCTAssertNil(old.nightVerdict, "a snapshot written before the verdict still decodes")
        let merged = WidgetSnapshotService.merge(
            previous: nil, restaurantId: 1, restaurantName: nil, waiting: nil,
            night: WidgetSnapshotService.NightPart(date: "2026-09-24", label: "9/24/26", net: "$4,210",
                                                   verdict: "Tough night", tone: "bad", score: 41),
            now: Date())
        XCTAssertEqual(merged?.nightTone, "bad")
        XCTAssertEqual(merged?.verdictLine, "Tough night 41/100")
    }
}
