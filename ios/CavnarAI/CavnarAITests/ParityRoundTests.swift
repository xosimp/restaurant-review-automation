import XCTest
@testable import CavnarAI

/// The web / iOS parity round (9/25/26): Home, the brief, recommendations,
/// notifications and locations. The pure rules behind each, pinned here.
final class ParityRoundTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func item(_ type: String, action: String? = "open_module", recKey: String? = nil,
                      nav: String? = nil) -> NeedsAttentionItem {
        NeedsAttentionItem(type: type, module: "reviews", title: type, detail: "", cta: "Open", secondary: nil,
                           action: action, recKey: recKey, dismissable: true, timesHidden: nil, count: nil,
                           evidence: nil, confidence: nil, nav: nav)
    }

    // MARK: #1 — the rest of the brief, leniently

    func testTheHomeSummaryReadsTheBriefExtrasAndSurvivesOddValues() throws {
        let base = """
            "restaurant_name": "Gia Mia", "reviews_awaiting_approval": 0, "modules": [], "needs_attention": [],
            "total_value_delivered": 0, "value_history": [], "quiet_hours_active": false
            """
        let s = try decode(HomeSummary.self, "{" + base + """
            , "quick_actions": [{"key": "publish", "label": "Publish 3 replies", "kind": "publish_replies",
                                 "module": "reviews", "count": 3, "nav": "reviews?filter=pending"}, 7],
              "changes": {"since_label": "since your last sign-in",
                          "items": [{"text": "2 new reviews — one 1★"}, {"text": "Labor 31%"}]},
              "charts": {"rating": [{"label": "9/14/26", "avg": 4.2, "total": 9}, {"label": "9/21/26", "avg": 4.4, "total": 7}],
                         "labor_days": [{"day": "Mon", "pct": 29.5}], "labor_target": 28, "waste": "nope"},
              "dismissed": [{"key": "trim_day", "title": "Trim Tuesday", "until": null}],
              "wins": [{"title": "Rating up"}]}
            """)
        XCTAssertEqual(s.quickActions?.items.map(\.key), ["publish"], "an odd element is skipped")
        XCTAssertEqual(s.changes?.line, "Since your last visit: 2 new reviews \u{00B7} Labor 31%")
        XCTAssertEqual(s.charts?.hasRatingTrend, true)
        XCTAssertEqual(s.charts?.waste.count, 0, "a series that isn't a list is empty")
        XCTAssertEqual(s.dismissed?.items.first?.key, "trim_day")
        XCTAssertEqual(s.wins?.items.count, 1)
        // An older server sends none of it.
        let old = try decode(HomeSummary.self, "{" + base + "}")
        XCTAssertNil(old.quickActions)
        XCTAssertNil(old.changes?.line)
        // And a cached summary round-trips.
        let again = try decode(HomeSummary.self, String(decoding: try JSONEncoder().encode(s), as: UTF8.self))
        XCTAssertEqual(again.changes?.line, s.changes?.line)
        XCTAssertEqual(again.charts, s.charts)
    }

    func testQuickActionsLeaveOutWhatNeedsAttentionAlreadyCarriesAndTheFabAndBell() {
        let qs = [HomeQuickAction(key: "publish", label: "Publish 3 replies", kind: "publish_replies", count: 3),
                  HomeQuickAction(key: "ask", label: "Ask Cavnar AI", kind: "ask"),
                  HomeQuickAction(key: "alerts", label: "All alerts", kind: "alerts"),
                  HomeQuickAction(key: "schedule", label: "Build next week's schedule", kind: "open_module",
                                  module: "labor", nav: "labor/schedule"),
                  HomeQuickAction(key: "order", label: "Review the order draft", kind: "open_module",
                                  module: "inventory", count: 4, nav: "inventory/order")]
        let attention = [item("reviews_awaiting_approval", action: "publish_replies"),
                         item("stock", nav: "inventory/order")]
        XCTAssertEqual(HomeQuickAction.unsaid(qs, attention: attention).map(\.key), ["schedule"])
        XCTAssertEqual(qs[4].chipLabel, "Review the order draft 4")
        XCTAssertEqual(qs[0].chipLabel, "Publish 3 replies", "a publish label already says its number")
    }

    func testTheOneThingLeadsInTheWebsOrder() throws {
        let rec = try decode(HomeRecommendation.self, #"{"key": "trim_tue", "title": "Trim Tuesday"}"#)
        XCTAssertNil(HomeFocusLead.pick(loaded: false, hasFinding: true, attention: [item("a")], recommendations: [rec]),
                     "nothing is promoted before the day's reads land")
        XCTAssertEqual(HomeFocusLead.pick(loaded: true, hasFinding: true, attention: [item("a")],
                                          recommendations: [rec])?.kind, "finding")
        XCTAssertEqual(HomeFocusLead.pick(loaded: true, hasFinding: false, attention: [item("a", recKey: "k1")],
                                          recommendations: [rec])?.key, "k1")
        XCTAssertEqual(HomeFocusLead.pick(loaded: true, hasFinding: false, attention: [],
                                          recommendations: [rec])?.kind, "recommendation")
        XCTAssertNil(HomeFocusLead.pick(loaded: true, hasFinding: false, attention: [], recommendations: []))
    }

    // MARK: #3 — the morning brief says each thing once

    func testTheBriefDropsWhatThePageAlreadySays() {
        typealias L = HomeDayViewModel.BriefLine
        let lines = [L(key: "fix_first", text: "a"), L(key: "money", text: "b"),
                     L(key: "reviews", text: "c", rec: "no_response"), L(key: "stock", text: "d"),
                     L(key: "yesterday", text: "e", source: "dsr"), L(key: "yesterday", text: "f"),
                     L(key: "schedule", text: "g")]
        let shown = HomeBriefFilter.shownKeys(attention: [item("reviews_awaiting_approval", recKey: "awaiting_approval"),
                                                          item("stock_low:salmon")], focusKey: nil)
        XCTAssertEqual(HomeBriefFilter.visible(lines, shown: shown).map(\.text), ["f", "g"])
        XCTAssertEqual(HomeBriefFilter.visible([L(key: "all_clear", text: "x")], shown: []).count, 0,
                       "an all-clear alone is not a brief")
    }

    func testABriefLineCarriesItsAction() throws {
        let brief = try decode(HomeDayViewModel.BriefLine.self, """
            {"key": "reviews", "text": "3 replies owed", "rec": "no_response",
             "action": {"label": "Reply now", "nav": "reviews?filter=urgent"}}
            """)
        XCTAssertEqual(brief.action?.nav, "reviews?filter=urgent")
        XCTAssertEqual(brief.rec, "no_response")
        let odd = try decode(HomeDayViewModel.BriefLine.self, #"{"text": "x", "action": "nope"}"#)
        XCTAssertNil(odd.action)
    }

    // MARK: #6 — the record in three figures

    func testTheRecordsTilesShowARateOnlyAboveItsFloor() throws {
        let enough = try decode(RecSummary.Totals.self, """
            {"shown": 20, "settled": 14, "taken": 9, "open": 6, "measured": 6, "improved": 4,
             "taken_enough": true, "measured_enough": true}
            """)
        let tiles = RecSummaryFormat.tiles(enough, days: 90, minSettled: 10, minMeasured: 5)
        XCTAssertEqual(tiles.map(\.label), ["Acted on", "Measured better", "Open now"])
        XCTAssertEqual(tiles.map(\.value), ["9 of 14", "4 of 6", "6"])
        XCTAssertTrue(tiles[1].good)
        let thin = try decode(RecSummary.Totals.self, #"{"settled": 3, "measured": 1}"#)
        let t2 = RecSummaryFormat.tiles(thin, days: 30, minSettled: 10, minMeasured: 5)
        XCTAssertEqual(t2[0].value, "\u{2014}")
        XCTAssertEqual(t2[0].detail, "3 of 10 settled \u{2014} not enough yet")
        XCTAssertEqual(t2[1].value, "\u{2014}")
    }

    // MARK: #8 — the switcher's rows

    func testTheGroupBriefGivesEachLocationItsStatusAttentionAndNet() throws {
        let g = try decode(LocationGroupBrief.self, """
            {"ok": true, "headline": "1 location needs a look", "tone": "warn",
             "locations": [{"id": 1, "name": "Downtown", "health": "critical", "attention": 2,
                            "last_night": {"net": 8420.4}},
                           {"id": 2, "name": "Uptown", "health": "healthy", "attention": 0, "last_night": null}],
             "attention": [{"text": "3 urgent reviews unanswered", "severity": "critical", "location": "Downtown",
                            "restaurant_id": 1, "nav": "reviews?filter=urgent", "action_label": "Reply now"}],
             "portfolio": {"total": 2, "healthy": 1, "needing": 1,
                           "last_night": {"label": "9/24/26", "net": 12000, "locations": 2, "of": 2}}}
            """)
        XCTAssertEqual(g.locations[0].detail, "2 need you \u{00B7} $8,420 net")
        XCTAssertNil(g.locations[1].detail)
        XCTAssertEqual(g.portfolio?.line, "1 of 2 healthy \u{00B7} 9/24/26 $12,000 net across 2 of 2 \u{00B7} 1 needs a look")
        XCTAssertEqual(LocationSwitcherView.healthColor("critical"), .cavnarRed)
    }

    // MARK: #11 — the changelog's date

    func testAChangelogEntryShowsItsDateMDYY() throws {
        let e = try decode(ChangelogEntry.self, #"{"id": 1, "title": "t", "published_at": "2026-09-05 10:00:00"}"#)
        XCTAssertEqual(e.displayDate, "9/5/26")
        let none = try decode(ChangelogEntry.self, #"{"id": 2, "title": "t"}"#)
        XCTAssertNil(none.displayDate)
    }

    // MARK: #5 — one range set

    func testValueRangesAreTheWebsSet() {
        XCTAssertEqual(ChartRange.allCases.map(\.rawValue), ["1M", "3M", "6M", "1Y"])
    }
}
