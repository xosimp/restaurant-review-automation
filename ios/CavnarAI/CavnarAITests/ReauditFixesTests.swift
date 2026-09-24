import XCTest
@testable import CavnarAI

/// The iOS half of the rec-ROI re-audit (D1–D14, D22). Each test pins the
/// rule the screen follows — the pure function or the request the view
/// model sends — against the server's real payloads, and would fail on the
/// build before its fix.
final class ReauditFixesTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private let utc = TimeZone(identifier: "UTC")!
    private let chicago = TimeZone(identifier: "America/Chicago")!

    // MARK: - D1: the check-in reply decodes

    /// Verbatim from the live server (reaudit5/checkin_reply.json).
    static let realCheckInReply = """
        {"checkin":{"attribution":{"confounded":true,"discount":true,"implemented":"yes"},"conditions_changed":true,"did_it":"yes","note":null,"tracker_id":6},"ok":true,"recorded":true}
        """

    func testTheServersRealCheckInReplyDecodes() throws {
        let r = try decode(APIClient.RecCheckInResponse.self, Self.realCheckInReply)
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.recorded, true)
        XCTAssertEqual(r.checkin?.didIt, "yes")
        XCTAssertEqual(r.checkin?.conditionsChanged, true)
        XCTAssertEqual(r.checkin?.trackerId, 6)
        XCTAssertNil(r.checkin?.note)
        XCTAssertEqual(r.checkin?.attribution?.implemented, "yes")
        XCTAssertEqual(r.checkin?.attribution?.confounded, true)
        XCTAssertEqual(r.checkin?.attribution?.discount, true)
    }

    func testAnOlderBooleanImplementedStillReads() throws {
        let yes = try decode(APIClient.RecCheckInResponse.self,
                             #"{"ok": true, "checkin": {"attribution": {"implemented": true}}}"#)
        XCTAssertEqual(yes.checkin?.attribution?.implemented, "yes")
        let no = try decode(APIClient.RecCheckInResponse.self,
                            #"{"ok": true, "checkin": {"attribution": {"implemented": false}}}"#)
        XCTAssertEqual(no.checkin?.attribution?.implemented, "no")
        // An odd checkin block never turns a saved answer into a failure.
        let odd = try decode(APIClient.RecCheckInResponse.self, #"{"ok": true, "recorded": true, "checkin": "?"}"#)
        XCTAssertTrue(odd.ok)
        XCTAssertNil(odd.checkin)
    }

    // MARK: - D2: every check-in names its tracker

    func testEveryCheckInSendsTheTrackerItIsAbout() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, ReauditFixesTests.realCheckInReply)
        }
        let r = try await client.checkIn(key: "trim_day:Tuesday", trackerId: 6, didIt: "yes",
                                         conditionsChanged: true, surface: "home")
        XCTAssertTrue(r.ok)
        XCTAssertEqual(captured.value?["tracker_id"] as? Int, 6)
        XCTAssertEqual(captured.value?["key"] as? String, "trim_day:Tuesday")

        let body = APIClient.RecCheckInBody(key: "k", trackerId: 41, didIt: "no", conditionsChanged: false,
                                            surface: "ios")
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
        XCTAssertEqual(json["tracker_id"] as? Int, 41)
    }

    // MARK: - D3: a re-check still ahead

    func testAReCheckStillAheadReadsAsDueNotDone() throws {
        let o = try decode(RecOutcome.self, RecOutcomeROITests.evaluatedRow)      // recheck_on 2026-10-28
        XCTAssertEqual(o.recheckLine(asOf: RecOutcomeROITests.noon("2026-09-24"), in: utc), "Re-check on 10/28/26")
        XCTAssertEqual(o.recheckLine(asOf: RecOutcomeROITests.noon("2026-10-27"), in: utc), "Re-check on 10/28/26")
        XCTAssertEqual(o.recheckLine(asOf: RecOutcomeROITests.noon("2026-10-28"), in: utc), "Re-checked on 10/28/26")
        XCTAssertEqual(o.recheckLine(asOf: RecOutcomeROITests.noon("2026-11-02"), in: utc), "Re-checked on 10/28/26")
        // A verdict says what happened, whatever the date.
        let held = try decode(RecOutcome.self, #"{"id": 1, "status": "evaluated", "recheck_on": "2026-10-28", "recheck_verdict": "held"}"#)
        XCTAssertEqual(held.recheckLine(asOf: RecOutcomeROITests.noon("2026-09-24"), in: utc), "Held at the re-check")
    }

    // MARK: - D4: timeline dates on the phone's day

    func testServerTimestampsReadOnThePhonesCalendarDay() {
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02 03:10:00", in: chicago), "8/1/26")
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02 03:10:00", in: utc), "8/2/26")
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02T03:10:00Z", in: chicago), "8/1/26")
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02T03:10:00+00:00", in: chicago), "8/1/26")
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02 03:10:00.123456", in: chicago), "8/1/26")
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02T01:10:00-05:00", in: utc), "8/2/26")
        // A bare date has no time to shift; junk comes back as given.
        XCTAssertEqual(CavnarDate.mdyLocal("2026-08-02", in: chicago), "8/2/26")
        XCTAssertEqual(CavnarDate.mdyLocal("soon", in: chicago), "soon")
        XCTAssertNil(CavnarDate.timestamp("2026-08-02"))
    }

    func testTheTimelineRowsDatesAreLocal() throws {
        let item = try decode(RecTimelineItem.self, """
            {"key": "trim_day:Tuesday", "title": "Trim Tuesday lunch", "module": "labor", "answer": "accepted",
             "first_shown_at": "2026-08-01 02:30:00", "answered_at": "2026-08-03 04:10:00",
             "implemented_at": "2026-08-05 03:00:00", "tracker_id": 7}
            """)
        XCTAssertEqual(item.metaLine(in: chicago), "Labor \u{00B7} shown 7/31/26")
        XCTAssertEqual(item.answerChip(in: chicago), "Tracked \u{00B7} 8/2/26")
        XCTAssertEqual(item.madeTheChangeLine(in: chicago), "Made the change 8/4/26")
        XCTAssertEqual(item.metaLine(in: utc), "Labor \u{00B7} shown 8/1/26")
    }

    // MARK: - D5: Accepted is not Tracked without a tracker

    func testAcceptedReadsTrackedOnlyWithATracker() throws {
        let tracked = try decode(RecTimelineItem.self, #"{"key": "a", "answer": "accepted", "tracker_id": 7}"#)
        XCTAssertEqual(tracked.answerLabel, "Tracked")
        let accepted = try decode(RecTimelineItem.self, #"{"key": "b", "answer": "accepted", "tracker_id": null}"#)
        XCTAssertEqual(accepted.answerLabel, "Accepted")
        let older = try decode(RecTimelineItem.self, #"{"key": "c", "answer": "accepted"}"#)
        XCTAssertEqual(older.answerLabel, "Accepted")
    }

    // MARK: - D6: a % metric's partial reading moves in points

    func testAPercentMetricsPartialReadingIsInPoints() throws {
        let o = try decode(RecOutcome.self, """
            {"id": 12, "status": "tracking", "metric": "labor_pct", "metric_label": "Labor %", "unit": "%",
             "evaluate_on": "2026-10-21",
             "interim": {"value": 30.1, "delta": -1.1, "delta_pct": -3.5, "as_of": "2026-09-22", "days_in": 9}}
            """)
        XCTAssertEqual(o.interimLine, "Partial reading after 9 days: labor % 30.1%, \u{2212}1.1 pts so far")
        // Only the relative change sent: it says so.
        let rel = try decode(RecOutcome.self, """
            {"id": 13, "status": "tracking", "metric_label": "Food cost %", "unit": "%",
             "interim": {"value": 29.0, "delta_pct": -3.5, "days_in": 1}}
            """)
        XCTAssertEqual(rel.interimLine, "Partial reading after 1 day: food cost % 29%, \u{2212}3.5% relative so far")
        // Any other unit keeps its relative change.
        XCTAssertEqual(RecOutcome.interimMove(delta: 0.2, deltaPct: 4.8, unit: "\u{2605}"), "+4.8%")
        XCTAssertEqual(RecOutcome.interimMove(delta: 0.4, deltaPct: nil, unit: "%"), "+0.4 pts")
        XCTAssertNil(RecOutcome.interimMove(delta: nil, deltaPct: nil, unit: "%"))
    }

    // MARK: - D7: unpriced wins on the worth card

    /// The real /api/value delivered block (reaudit1/value.json).
    static let realDelivered = """
        {"annual":18200.04,"basis":"measured before and after each change, over the metric's own window",
         "biggest":{"attribution":"consistent","metric":"Labor %","module":"labor","monthly":1516.67,
                    "summary":"Trim: Labor % went from 30% to 25% \\u2014 improved, roughly $1,517/month.","title":"Trim"},
         "by_module":{"labor":1516.67},
         "caveat":"Measured before and after, not proven cause: other changes in the same weeks move the same number.",
         "cumulative":{"basis":"summed over days actually measured","by_module":{"inventory":-553.84,"labor":2250.0},
                       "days":73,"gained":2250.0,"lost":553.85,"measured_days":73,"since":"2026-06-01",
                       "total":1696.16,"until":"2026-09-23"},
         "evaluated":3,"faded":0,"in_flight":0,"monthly":1516.67,"net_by_module":{"inventory":-600.0,"labor":1516.67},
         "net_monthly":916.67,"net_note":"$1,517/month of improvements less $600/month from changes that got worse.",
         "no_clear_change":0,"unmeasurable":0,
         "unpriced_wins":[{"line":"Average rating 4.2\\u2605 \\u2192 4.5\\u2605, improved","module":"reviews","title":"Reply faster"}],
         "validated":0,"validated_monthly":0,"wins":1,"wins_by_module":{"labor":1,"reviews":1},"wins_measured":2,
         "worsened":{"count":1,"monthly":600.0}}
        """

    private func summary(_ delivered: String) throws -> HomeFollowThroughViewModel.ValueSummary {
        try decode(HomeFollowThroughViewModel.ValueSummary.self, #"{"ok": true, "delivered": \#(delivered)}"#)
    }

    func testTheRealValueBlockCarriesItsUnpricedWin() throws {
        let v = try summary(Self.realDelivered)
        let d = try XCTUnwrap(v.delivered)
        XCTAssertEqual(d.unpricedWins?.count, 1)
        XCTAssertEqual(RecValueFormat.unpricedWinLines(d),
                       ["Reply faster: Average rating 4.2\u{2605} \u{2192} 4.5\u{2605}, improved \u{2014} measured, no dollar figure."])
        XCTAssertTrue(v.showsWorthCard)
    }

    func testARestaurantWhoseOnlyWinIsARatingRiseStillSeesTheCard() throws {
        let v = try summary("""
            {"monthly": 0, "wins": 0, "evaluated": 1, "in_flight": 0, "worsened": {"count": 0, "monthly": 0.0},
             "cumulative": {"total": null},
             "unpriced_wins": [{"line": "Average rating 4.2\u{2605} \u{2192} 4.5\u{2605}, improved", "module": "reviews",
                                "title": "Reply faster"}]}
            """)
        let d = try XCTUnwrap(v.delivered)
        XCTAssertTrue(v.showsWorthCard)
        // Not "Nothing measured yet" — something was.
        XCTAssertNil(RecValueFormat.nothingPricedLine(d, unpricedWins: 1))
        XCTAssertEqual(RecValueFormat.unpricedWinLines(d).count, 1)

        let none = try summary(#"{"monthly": 0, "wins": 0, "in_flight": 0, "worsened": {"count": 0}, "unpriced_wins": []}"#)
        XCTAssertFalse(none.showsWorthCard)
        XCTAssertEqual(RecValueFormat.nothingPricedLine(try XCTUnwrap(none.delivered), unpricedWins: 0),
                       "Nothing measured yet. Track a recommendation and its result lands here.")
        // An older server: no field, the card's rule as before.
        let older = try summary(#"{"monthly": 400, "wins": 1}"#)
        XCTAssertNil(older.delivered?.unpricedWins)
        XCTAssertTrue(older.showsWorthCard)
    }

    func testUnpricedWinLinesUseWhatTheRowCarries() {
        XCTAssertEqual(RecValueFormat.unpricedWinLine(line: "Average rating 4.2\u{2605} \u{2192} 4.5\u{2605}, improved", title: nil),
                       "Average rating 4.2\u{2605} \u{2192} 4.5\u{2605}, improved \u{2014} measured, no dollar figure.")
        XCTAssertEqual(RecValueFormat.unpricedWinLine(line: nil, title: "Reply faster"),
                       "Reply faster \u{2014} improved \u{2014} measured, no dollar figure.")
        XCTAssertNil(RecValueFormat.unpricedWinLine(line: " ", title: nil))
    }

    // MARK: - D8: the value band and chart show the net

    private static let homeBase = """
        "username": "brian", "restaurant_name": "Gia Mia", "location_name": null, "brand_color": null,
        "reviews_awaiting_approval": 0, "quiet_hours_active": false,
        "modules": [{"key": "labor", "label": "Labor", "icon": "labor", "status": "available", "kpi": null}],
        "needs_attention": [], "total_value_delivered": 1517,
        "value_history": [{"date": "2026-09-01", "value": 1200}, {"date": "2026-09-20", "value": 1517}]
        """

    private func home(_ extra: String = "") throws -> HomeSummary {
        try decode(HomeSummary.self, "{" + Self.homeBase + (extra.isEmpty ? "" : ", " + extra) + "}")
    }

    func testTheHomeValueBlockShowsTheNetWhenSomethingGotWorse() throws {
        let s = try home("""
            "value": {"monthly": 1517, "net_monthly": 917, "worsened": {"count": 1, "monthly": 600.0, "priced_count": 1},
                      "cumulative": {"total": 1696.16, "since": "2026-06-01", "days": 73},
                      "unpriced_wins": [{"line": "Average rating 4.2\u{2605} \u{2192} 4.5\u{2605}, improved",
                                         "module": "reviews", "title": "Reply faster"}]}
            """)
        XCTAssertEqual(s.value?.unpricedWins?.first?.title, "Reply faster")
        XCTAssertEqual(s.value?.cumulative?.total, 1696.16)
        XCTAssertEqual(s.valueHeadline,
                       HomeValueHeadline(figure: 917, isNet: true,
                                         breakdown: "$1,517 improved, less $600 from 1 that got worse"))
    }

    func testNothingWorseOrAnOlderServerKeepsTodaysFigure() throws {
        let clean = try home(#""value": {"monthly": 1517, "net_monthly": 1517, "worsened": {"count": 0, "monthly": 0}}"#)
        XCTAssertEqual(clean.valueHeadline, HomeValueHeadline(figure: 1517, isNet: false, breakdown: nil))
        let older = try home()
        XCTAssertNil(older.value)
        XCTAssertEqual(older.valueHeadline, HomeValueHeadline(figure: 1517, isNet: false, breakdown: nil))
        // A value that isn't even an object never fails Home.
        let odd = try home(#""value": 12"#)
        XCTAssertEqual(odd.valueHeadline.isNet, false)
        XCTAssertEqual(odd.totalValueDelivered, 1517)
    }

    func testANetBelowZeroIsDrawnAsMeasured() {
        let h = HomeValueHeadline.make(total: 100, value: HomeValueBlock(
            monthly: 100, netMonthly: -300,
            worsened: .init(count: 2, monthly: 400, pricedCount: 2)))
        XCTAssertEqual(h, HomeValueHeadline(figure: -300, isNet: true,
                                            breakdown: "$100 improved, less $400 from 2 that got worse"))
    }

    func testTheHomeValueBlockSurvivesTheSummaryCache() throws {
        let s = try home(#""value": {"monthly": 1517, "net_monthly": 917, "worsened": {"count": 1, "monthly": 600, "priced_count": 1}}"#)
        let again = try JSONDecoder.cavnar.decode(HomeSummary.self, from: JSONEncoder().encode(s))
        XCTAssertEqual(again.valueHeadline, s.valueHeadline)
    }

    // MARK: - D22: N is the priced results

    func testTheWorseCountBesideTheDollarsIsThePricedOnes() throws {
        XCTAssertEqual(RecValueFormat.netBreakdown(improved: 1517, worseMonthly: 600, count: 3, pricedCount: 1),
                       "$1,517 improved, less $600 from 1 that got worse")
        // An older server with no priced_count: count stands in.
        XCTAssertEqual(RecValueFormat.netBreakdown(improved: 1517, worseMonthly: 600, count: 2, pricedCount: nil),
                       "$1,517 improved, less $600 from 2 that got worse")
        // None priced: nothing was netted, and it says why — never "less $0 from 0".
        XCTAssertEqual(RecValueFormat.netBreakdown(improved: 1517, worseMonthly: 0, count: 1, pricedCount: 0),
                       "$1,517 improved \u{2014} the 1 that got worse has no dollar figure")

        let d = try XCTUnwrap(try summary("""
            {"monthly": 1517, "wins": 1, "net_monthly": 917, "worsened": {"count": 3, "monthly": 600, "priced_count": 1}}
            """).delivered)
        XCTAssertEqual(RecValueFormat.netLine(d), "Net of 1 change that got worse: $917/month ($600/month worse).")
        XCTAssertEqual(RecValueFormat.countsLine(d), "3 got worse")
        let unpricedOnly = try XCTUnwrap(try summary("""
            {"monthly": 1517, "wins": 1, "net_monthly": 1517, "worsened": {"count": 1, "monthly": 0, "priced_count": 0}}
            """).delivered)
        XCTAssertNil(RecValueFormat.netLine(unpricedOnly))
        XCTAssertEqual(RecValueFormat.countsLine(unpricedOnly), "1 got worse")
    }

    // MARK: - D9: the reason picker on the win-back and the schedule ✕

    @MainActor
    func testTheWinbacksNotForUsSendsItsReasonCode() async throws {
        let captured = Box<[String: Any]?>(nil)
        let paths = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            paths.value.append(EdgeHTTP.line(request))
            if request.url?.path == "/mobile/api/guest-winback" {
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "available": true, "draft": {"id": 9, "segment_size": 42, "message": "Come back!",
                     "max_chars": 320}}
                    """)
            }
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadWinback()
        await vm.dismissWinback(reasonCode: RecReason.tooCostly.code)
        XCTAssertTrue(paths.value.contains("POST /mobile/api/guest-winback/9/dismiss"))
        XCTAssertEqual(captured.value?["kind"] as? String, "not_for_us")
        XCTAssertEqual(captured.value?["reason_code"] as? String, "too_costly")
        XCTAssertTrue(vm.winbackDismissed)
        // Skip: the dismissal with no code at all.
        await vm.dismissWinback()
        XCTAssertEqual(captured.value?["kind"] as? String, "not_for_us")
        XCTAssertNil(captured.value?["reason_code"])
    }

    @MainActor
    func testTheScheduleRecommendationsCrossSendsItsReasonCode() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "suppressed_kinds": []}"#)
        }
        let vm = LaborViewModel(client: client)
        await vm.recordRecommendation("Cut a closer on Tuesday", accepted: false, reasonCode: RecReason.badTiming.code)
        XCTAssertEqual(captured.value?["action"] as? String, "dismissed")
        XCTAssertEqual(captured.value?["reason_code"] as? String, "bad_timing")
        XCTAssertEqual(vm.recommendationDecisions["Cut a closer on Tuesday"], "dismissed")
        // Skip: dismissed, no code.
        await vm.recordRecommendation("Cut a closer on Tuesday", accepted: false)
        XCTAssertNil(captured.value?["reason_code"])
        // A ✓ never carries a why.
        await vm.recordRecommendation("Cut a closer on Tuesday", accepted: true, reasonCode: RecReason.other.code)
        XCTAssertEqual(captured.value?["action"] as? String, "accepted")
        XCTAssertNil(captured.value?["reason_code"])
    }

    // MARK: - D10: the modules grid asks for the modules alone

    private static let modulesJSON = """
        [{"key": "reviews", "label": "Reviews", "icon": "reviews", "status": "available",
          "kpi": {"value": "12/14", "sublabel": "86% response rate"}},
         {"key": "labor", "label": "Labor", "icon": "labor", "status": "available", "kpi": null}]
        """

    @MainActor
    func testTheModulesGridReadsTheModulesRouteNotHome() async throws {
        let requests = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            requests.value.append(request.url?.path ?? "")
            if request.url?.path == "/mobile/api/home/modules" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "modules": \#(ReauditFixesTests.modulesJSON)}"#)
            }
            return EdgeHTTP.reply(request, 500, #"{"ok": false}"#)
        }
        let vm = ModulesGridViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.modules.map(\.key), ["reviews", "labor"])
        XCTAssertEqual(requests.value, ["/mobile/api/home/modules"])      // Home is never built for the grid
        XCTAssertNil(vm.errorMessage)
    }

    @MainActor
    func testAnOlderServerWithoutTheRouteFallsBackToHome() async throws {
        let requests = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            requests.value.append(request.url?.path ?? "")
            switch request.url?.path {
            case "/mobile/api/home/modules":
                return EdgeHTTP.reply(request, 404, #"{"ok": false, "error": "Not found"}"#)
            case "/mobile/api/home":
                return EdgeHTTP.reply(request, 200, """
                    {"restaurant_name": "Gia Mia", "reviews_awaiting_approval": 0, "quiet_hours_active": false,
                     "modules": \(ReauditFixesTests.modulesJSON), "needs_attention": [],
                     "total_value_delivered": 0, "value_history": []}
                    """)
            default:
                return EdgeHTTP.reply(request, 404, #"{"ok": false}"#)
            }
        }
        let vm = ModulesGridViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.modules.map(\.key), ["reviews", "labor"])
        XCTAssertEqual(requests.value, ["/mobile/api/home/modules", "/mobile/api/home"])
    }

    @MainActor
    func testAFailureOtherThanNotFoundIsReportedNotFallenBackFrom() async throws {
        let requests = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            requests.value.append(request.url?.path ?? "")
            return EdgeHTTP.reply(request, 500, #"{"ok": false, "error": "Server trouble"}"#)
        }
        let vm = ModulesGridViewModel(client: client)
        await vm.load()
        XCTAssertEqual(requests.value, ["/mobile/api/home/modules"])
        XCTAssertEqual(vm.errorMessage, "Server trouble")
    }

    // MARK: - D11: Food Cost analytics loads on its own tab, once

    @MainActor
    func testFoodCostAnalyticsLoadsOnlyWhenItsTabFirstShows() async {
        let analyticsReads = Box<Int>(0)
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/food-cost/analytics" { analyticsReads.value += 1 }
            return EdgeHTTP.reply(request, 404, #"{"ok": false}"#)
        }
        let vm = FoodCostAnalyticsViewModel(client: client)
        XCTAssertFalse(vm.hasRequestedFirstLoad)
        XCTAssertEqual(analyticsReads.value, 0)                             // opening the module fetched nothing
        await vm.loadOnFirstShow()
        XCTAssertTrue(vm.hasRequestedFirstLoad)
        XCTAssertEqual(analyticsReads.value, 1)
        await vm.loadOnFirstShow()                                          // back to the tab: no second read
        XCTAssertEqual(analyticsReads.value, 1)
    }

    // MARK: - D12: the morning brief's lines are answerable

    func testABriefLineCarriesItsKeyAndAnswersForItsModule() throws {
        let stock = try decode(HomeDayViewModel.BriefLine.self, """
            {"key": "stock", "tone": "bad", "rec": "stock_low:ab", "recs": ["stock_low:ab"],
             "text": "Running low: salmon", "rec_key": "stock_low:ab", "answerable": true}
            """)
        XCTAssertEqual(stock.answerKey, "stock_low:ab")
        XCTAssertEqual(stock.answerModule, "food")

        let money = try decode(HomeDayViewModel.BriefLine.self, """
            {"key": "money", "tone": "neutral", "text": "Biggest opportunity: scheduling", "rec_key": "money:sched",
             "answerable": false}
            """)
        XCTAssertNil(money.answerKey)                                       // not answerable: no controls
        let older = try decode(HomeDayViewModel.BriefLine.self, #"{"key": "reviews", "text": "3 reviews owe a reply"}"#)
        XCTAssertNil(older.answerKey)

        XCTAssertEqual(["reviews", "stock", "schedule", "slow_day", "loss", "fix_first", nil]
                        .map(HomeDayViewModel.BriefLine.module(forLineKey:)),
                       ["reviews", "food", "labor", "marketing", "ops", "home", "home"])
    }

    @MainActor
    func testTheBriefLoadsItsKeysAndDoneAnswersOnHome() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/morning-brief":
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "brief": {"lines": [
                      {"key": "schedule", "tone": "action", "rec": "schedule:next-week", "text": "Next week isn't built",
                       "rec_key": "schedule:next-week", "answerable": true},
                      {"key": "yesterday", "tone": "good", "text": "Yesterday ran $6,975"}]}}
                    """)
            case "/mobile/api/recs/event":
                captured.value = EdgeHTTP.bodyJSON(request)
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "issues": []}"#)
            }
        }
        let vm = HomeDayViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.lines.map(\.answerKey), ["schedule:next-week", nil])
        let line = try XCTUnwrap(vm.lines.first)
        // What the row sends: the rec-event, on Home, for the line's module.
        _ = try await client.answerRecommendation(key: try XCTUnwrap(line.answerKey), answer: .completed,
                                                  surface: "home", module: line.answerModule)
        XCTAssertEqual(captured.value?["key"] as? String, "schedule:next-week")
        XCTAssertEqual(captured.value?["surface"] as? String, "home")
        XCTAssertEqual(captured.value?["module"] as? String, "labor")
        XCTAssertEqual(captured.value?["event"] as? String, "completed")
    }

    // MARK: - D13: colour follows `counts`

    func testAResultThatNoLongerCountsIsNeutral() throws {
        XCTAssertEqual(RecOutcome.standing(verdict: "improved", counts: true), .good)
        XCTAssertEqual(RecOutcome.standing(verdict: "worsened", counts: true), .bad)
        XCTAssertEqual(RecOutcome.standing(verdict: "improved", counts: false), .neutral)   // disowned / faded
        XCTAssertEqual(RecOutcome.standing(verdict: "worsened", counts: false), .neutral)
        XCTAssertEqual(RecOutcome.standing(verdict: "no_clear_change", counts: true), .neutral)
        // An older row with no `counts`: the verdict alone.
        XCTAssertEqual(RecOutcome.standing(verdict: "improved", counts: nil), .good)
        XCTAssertEqual(RecOutcome.standing(verdict: "worsened", counts: nil), .bad)

        let disowned = try decode(RecOutcome.self, """
            {"id": 7, "status": "evaluated", "verdict": "improved", "counts": false,
             "owner_checkin": {"did_it": "no", "conditions_changed": false}}
            """)
        XCTAssertEqual(disowned.standing, .neutral)
        let monthly = try decode(HomeMonthlyReview.Result.self,
                                 #"{"id": 3, "title": "Trim", "summary": "s", "verdict": "improved", "counts": false}"#)
        XCTAssertEqual(monthly.standing, .neutral)
        let monthlyOlder = try decode(HomeMonthlyReview.Result.self, #"{"id": 4, "verdict": "worsened"}"#)
        XCTAssertEqual(monthlyOlder.standing, .bad)
    }

    // MARK: - D14: the Daily Report's gross and net

    private func salesBlock(_ json: String) throws -> DSRBlock {
        DSRBlock(json: try decode(JSONValue.self, json))
    }

    func testAnAllGrossPutsTaxInGrossAndSaysWhatNetIs() throws {
        let b = try salesBlock("""
            {"status": "ready", "metrics": {"gross": 7998.0, "net": 6975.0, "tax": 589.0},
             "detail": {"definition": {"gross": "everything rung: items at the price rung before discounts and comps, plus tax and voided lines",
                                       "net": "gross less comps, discounts, voids and tax",
                                       "gross_basis": "all", "gross_missing": [], "net_deductions": ["discounts", "comps"]}}}
            """)
        XCTAssertEqual(b.taxCaption, "in gross, not net")
        XCTAssertEqual(b.definitionNote,
                       "Gross is everything rung: items at the price rung before discounts and comps, plus tax and voided lines. "
                       + "Net is gross less comps, discounts, voids and tax.")
        XCTAssertNil(b.grossNotMeasured)
    }

    func testAnAllGrossThePOSCouldNotCompleteIsNotMeasured() throws {
        let b = try salesBlock("""
            {"status": "ready", "metrics": {"gross": null, "gross_items": 7415.0, "net": 6975.0, "tax": null},
             "detail": {"definition": {"gross": "everything rung", "net": "gross less comps, discounts, voids and tax",
                                       "gross_basis": "all", "gross_missing": ["tax", "voids"]}}}
            """)
        XCTAssertEqual(b.grossNotMeasured, "Gross not measured \u{2014} the POS didn\u{2019}t report tax and voids")
        let one = try salesBlock("""
            {"metrics": {"gross": null}, "detail": {"definition": {"gross_basis": "all", "gross_missing": ["voids"]}}}
            """)
        XCTAssertEqual(one.grossNotMeasured, "Gross not measured \u{2014} the POS didn\u{2019}t report voids")
        // Null gross with nothing named missing: the plain dash, no sentence.
        let plain = try salesBlock(#"{"metrics": {"gross": null}, "detail": {}}"#)
        XCTAssertNil(plain.grossNotMeasured)
    }

    func testAnItemsGrossAndAnOlderReportReadAsBefore() throws {
        let items = try salesBlock("""
            {"metrics": {"gross": 7415.0, "tax": 589.0},
             "detail": {"definition": {"gross": "items at the price rung", "net": "gross less discounts and comps",
                                       "gross_basis": "items", "gross_missing": []}}}
            """)
        XCTAssertEqual(items.taxCaption, "not in gross or net")
        XCTAssertEqual(items.definitionNote, "Gross is items at the price rung. Net is gross less discounts and comps.")

        // The server's own older report (Fixtures/dsr_preview.json): no `net`
        // sentence, so the deductions list stands in.
        let r = try JSONDecoder.cavnar.decode(DSRReport.self, from: DailyReportDecodingTests.payload("owner"))
        let sales = try XCTUnwrap(r.facts.blocks["sales"])
        XCTAssertNil(sales.grossBasis)
        XCTAssertEqual(sales.taxCaption, "not in gross or net")
        XCTAssertEqual(sales.definitionNote,
                       "Gross is items at the price rung, before discounts and comps; no tax, tips, gift cards, refunds or voids. "
                       + "Net takes off discounts and comps.")
    }
}
