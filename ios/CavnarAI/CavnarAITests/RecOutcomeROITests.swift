import XCTest
@testable import CavnarAI

/// Recommendation ROI, wave 2 (iOS): the payloads the phone now reads, the
/// rules its screens follow, and the requests its answers send. Every rule
/// is asserted against the pure function the screen calls, not one
/// rendered fixture (feedback: fixture-shaped tests).
final class RecOutcomeROITests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    // MARK: - Structured reasons (#22)

    func testTheSixReasonCodesAreTheServersInOwnerWording() {
        XCTAssertEqual(RecReason.allCases.map(\.code),
                       ["already_doing", "doesnt_fit", "too_costly", "bad_timing", "dont_trust_data", "other"])
        XCTAssertEqual(RecReason.allCases.map(\.label),
                       ["Already doing this", "Doesn\u{2019}t fit us", "Too costly", "Bad timing",
                        "Don\u{2019}t trust the numbers", "Other"])
        XCTAssertEqual(RecReason.label(for: "too_costly"), "Too costly")
        XCTAssertNil(RecReason.label(for: "nonsense"))
        XCTAssertNil(RecReason.label(for: nil))
    }

    func testNotForUsSendsItsReasonCodeAndNoOtherAnswerDoes() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
        }
        _ = try await client.answerRecommendation(key: "k", answer: .notForUs, surface: "reviews",
                                                  reasonCode: RecReason.badTiming.code)
        XCTAssertEqual(captured.value?["event"] as? String, "dismissed")
        XCTAssertEqual(captured.value?["kind"] as? String, "not_for_us")
        XCTAssertEqual(captured.value?["reason_code"] as? String, "bad_timing")
        // A code handed to Done is not sent: only a dismissal carries a why.
        _ = try await client.answerRecommendation(key: "k", answer: .completed, surface: "reviews",
                                                  reasonCode: RecReason.other.code)
        XCTAssertNil(captured.value?["reason_code"])
        // And a Not for us without one sends no code at all.
        _ = try await client.answerRecommendation(key: "k", answer: .notForUs, surface: "reviews")
        XCTAssertNil(captured.value?["reason_code"])
    }

    @MainActor
    func testHomeNotForUsAndTheSecondHideSendTheReasonCode() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let vm = HomeFollowThroughViewModel(client: client)
        let item = try decode(NeedsAttentionItem.self, """
            {"type": "labor_overtime", "module": "labor", "title": "Overtime this week", "detail": "2 people",
             "rec_key": "labor_overtime:2026-09-21", "dismissable": true, "times_hidden": 1}
            """)
        let ok = await vm.answerAttention(item, kind: "not_for_us", reasonCode: RecReason.alreadyDoing.code)
        XCTAssertTrue(ok)
        XCTAssertEqual(captured.value?["key"] as? String, "labor_overtime:2026-09-21")
        XCTAssertEqual(captured.value?["kind"] as? String, "not_for_us")
        XCTAssertEqual(captured.value?["reason_code"] as? String, "already_doing")
        // The reason travels with the item's title, as the web's does.
        XCTAssertEqual(captured.value?["title"] as? String, "Overtime this week")
        // A plain hide sends neither.
        _ = await vm.answerAttention(item, kind: "recommendation")
        XCTAssertNil(captured.value?["reason_code"])
        XCTAssertNil(captured.value?["title"])
    }

    func testMarketingNoLongerOffersTrack() {
        // rec-ROI #11: a post has no honest metric; Track there started a
        // sales tracker credited to Labor.
        XCTAssertEqual(RecAnswer.defaults(for: "marketing"), [.completed, .notForUs])
        XCTAssertEqual(RecAnswer.defaults(for: "labor"), RecAnswer.allCases)
        XCTAssertEqual(RecAnswer.defaults(for: "dsr"), [.completed, .notForUs])
        XCTAssertEqual(RecAnswer.trackableModules, ["reviews", "food", "labor"])
    }

    // MARK: - Tracker replies (#3 on the phone)

    func testATrackerReplyReadsAsWhatIsMeasuredUntilWhen() throws {
        let r = try decode(APIClient.RecEventResponse.self, """
            {"ok": true, "recorded": true,
             "message": "Tracking — Cavnar will compare labor % over the next 28 days with the 28 before",
             "tracker": {"id": 41, "metric": "labor_pct", "label": "Labor %", "evaluate_on": "2026-10-21",
                         "window_days": 28, "baseline_kind": "matched weekdays", "module": "labor",
                         "label_text": "measuring labor % until 10/21/26"}}
            """)
        XCTAssertEqual(r.tracker?.id, 41)
        XCTAssertNil(r.trackerRefused)
        XCTAssertEqual(RecTrackerNote.line(tracker: r.tracker, refused: nil), "Measuring labor % until 10/21/26")
        // Track's message doesn't say the date — the extra line does.
        XCTAssertEqual(RecTrackerNote.extraLine(message: r.message, tracker: r.tracker, refused: nil),
                       "Measuring labor % until 10/21/26")
    }

    func testDonesMessageAlreadyCarriesTheTrackerSoItIsNotSaidTwice() {
        let tracker = RecTracker(id: 1, metric: "labor_pct", label: "Labor %", evaluateOn: "2026-10-21",
                                 windowDays: 28, module: "labor", labelText: "measuring labor % until 10/21/26")
        let message = "Done \u{2014} Cavnar won\u{2019}t suggest it again. Now measuring labor % until 10/21/26"
        XCTAssertNil(RecTrackerNote.extraLine(message: message, tracker: tracker, refused: nil))
    }

    func testARefusalSaysWhyNothingStarted() throws {
        let r = try decode(APIClient.RecEventResponse.self, """
            {"ok": true, "recorded": true, "message": "Noted — hidden for 14 days. There is nothing here Cavnar can measure it against yet",
             "tracker_refused": {"code": "no_metric", "reason": "There is nothing here Cavnar can measure it against yet",
                                 "in_flight_until": null}}
            """)
        XCTAssertEqual(r.trackerRefused?.code, "no_metric")
        XCTAssertNil(r.trackerRefused?.inFlightUntil)
        XCTAssertEqual(RecTrackerNote.line(tracker: nil, refused: r.trackerRefused),
                       "There is nothing here Cavnar can measure it against yet")
        // Already in the message: no second line.
        XCTAssertNil(RecTrackerNote.extraLine(message: r.message, tracker: nil, refused: r.trackerRefused))
        // An older server with no label_text: built from label + evaluate_on, M/D/YY.
        let old = RecTracker(id: 2, metric: "avg_rating", label: "Average rating", evaluateOn: "2026-11-02",
                             windowDays: 30, module: "reviews", labelText: nil)
        XCTAssertEqual(RecTrackerNote.line(tracker: old, refused: nil), "Measuring average rating until 11/2/26")
        XCTAssertNil(RecTrackerNote.line(tracker: nil, refused: nil))
    }

    @MainActor
    func testHomeTrackSaysMeasuringUntilOrTheRefusal() async throws {
        let reply = Box<String>("")
        let client = EdgeHTTP.client { request in
            if request.httpMethod == "POST" { return EdgeHTTP.reply(request, 200, reply.value) }
            return EdgeHTTP.reply(request, 404, #"{"ok": false}"#)
        }
        let vm = HomeFollowThroughViewModel(client: client)
        let rec = try decode(HomeRecommendation.self, """
            {"key": "trim_day:Tuesday", "title": "Trim Tuesday lunch", "metric": "labor_pct"}
            """)
        reply.value = """
            {"ok": true, "outcome": {"id": 9, "evaluate_on": "2026-10-21"}, "warning": null,
             "tracker": {"id": 9, "metric": "labor_pct", "label": "Labor %", "evaluate_on": "2026-10-21",
                         "label_text": "measuring labor % until 10/21/26"}}
            """
        let started = await vm.track(rec)
        XCTAssertEqual(started, "Measuring labor % until 10/21/26")
        reply.value = """
            {"ok": true, "warning": "Already measuring labor % until 10/21/26. One change per number at a time.",
             "message": "Already measuring labor % until 10/21/26. One change per number at a time.",
             "tracker_refused": {"code": "in_flight", "reason": "Already measuring labor % until 10/21/26. One change per number at a time.",
                                 "in_flight_until": "2026-10-21"}}
            """
        let refused = await vm.track(rec)
        XCTAssertEqual(refused, "Already measuring labor % until 10/21/26. One change per number at a time.")
    }

    // MARK: - Outcome rows

    static let evaluatedRow = """
        {"id": 7, "title": "Trim Tuesday lunch", "source": "recommendation", "source_key": "trim_day:Tuesday",
         "metric": "labor_pct", "metric_label": "Labor %", "unit": "%", "status": "evaluated",
         "verdict": "improved", "module": "labor", "started_on": "2026-08-01", "evaluate_on": "2026-08-29",
         "baseline_value": 31.2, "after_value": 29.8, "delta": -1.4, "delta_pct": -4.5,
         "baseline_kind": "matched weekdays", "attribution": "consistent",
         "attribution_label": "Consistent with the change — it moved past the noise band",
         "concurrent": [{"kind": "price_change", "label": "a price change", "date": "2026-08-12"}],
         "recheck_on": "2026-10-28", "recheck_verdict": null, "validated": false, "counts": true,
         "result_line": "Labor % 31.2% → 29.8%, improved", "summary": "Labor % fell 1.4 points",
         "informational": false, "dollars_monthly": 420.0, "owner_checkin": null, "interim": null}
        """

    func testAnEvaluatedRowDecodesWithItsNulls() throws {
        let o = try decode(RecOutcome.self, Self.evaluatedRow)
        XCTAssertEqual(o.id, 7)
        XCTAssertTrue(o.isEvaluated)
        XCTAssertEqual(o.deltaPct, -4.5)
        XCTAssertEqual(o.baselineKind, "matched weekdays")
        XCTAssertEqual(o.concurrent.first?.kind, "price_change")
        XCTAssertNil(o.recheckVerdict)
        XCTAssertNil(o.ownerCheckin)
        XCTAssertNil(o.interim)
        XCTAssertEqual(o.resultLine, "Labor % 31.2% → 29.8%, improved")
        XCTAssertEqual(o.otherChangesLine, "Also changed these weeks: a price change (8/12/26)")
        XCTAssertEqual(o.recheckLine, "Re-checked on 10/28/26")
        XCTAssertNil(o.interimLine)
        XCTAssertNil(o.measuringLine)
    }

    func testATrackingRowCarriesAPartialReading() throws {
        let o = try decode(RecOutcome.self, """
            {"id": 8, "title": "Reply within a day", "source_key": "insight_review:1a", "metric": "avg_rating",
             "metric_label": "Average rating", "unit": "★", "status": "tracking", "verdict": null,
             "evaluate_on": "2026-10-21", "baseline_value": null, "after_value": null, "delta": null,
             "delta_pct": null, "attribution": null, "attribution_label": null, "concurrent": [],
             "validated": false, "counts": false, "result_line": null,
             "interim": {"value": 4.4, "delta": 0.2, "delta_pct": 4.8, "as_of": "2026-09-22", "days_in": 9,
                         "days": 9, "verdict": "no_clear_change", "baseline": 4.2, "band": 0.1, "multiple": 2.0}}
            """)
        XCTAssertTrue(o.isTracking)
        XCTAssertEqual(o.measuringLine, "Measuring average rating until 10/21/26")
        XCTAssertEqual(o.interimLine, "Partial reading after 9 days: average rating 4.4★, +4.8% so far")
        XCTAssertNil(o.recheckLine)
        // A tracker whose first day has not closed has no reading yet — no line.
        let fresh = try decode(RecOutcome.self, #"{"id": 9, "status": "tracking", "interim": null}"#)
        XCTAssertNil(fresh.interimLine)
        XCTAssertTrue(fresh.concurrent.isEmpty)
    }

    func testTheOutcomesListSurvivesAnOddRow() throws {
        let r = try decode(RecOutcomesResponse.self, """
            {"ok": true, "caveat": "Measured before and after, not proven cause.",
             "outcomes": [\(Self.evaluatedRow), {"id": 3, "status": "abandoned"}]}
            """)
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.outcomes.map(\.id), [7, 3])
        XCTAssertEqual(r.caveat, "Measured before and after, not proven cause.")
        XCTAssertEqual(RecOutcome.reading(1240, unit: "$"), "$1,240")
        XCTAssertEqual(RecOutcome.reading(6.5, unit: "h"), "6.5h")
        XCTAssertEqual(RecOutcome.reading(30, unit: "%"), "30%")
        XCTAssertNil(RecOutcome.reading(nil, unit: "%"))
    }

    // MARK: - The check-in (#21)

    func testACheckInIsDueOnlyForALandedResultWithNoAnswer() throws {
        let due = try decode(RecOutcome.self, Self.evaluatedRow)
        XCTAssertTrue(RecCheckIn.isDue(due))
        XCTAssertEqual(RecCheckIn.key(for: due), "trim_day:Tuesday")

        // The same row with one field changed.
        func variant(_ patch: String) throws -> RecOutcome {
            var row = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(Self.evaluatedRow.utf8)) as? [String: Any])
            let change = try XCTUnwrap(try JSONSerialization.jsonObject(with: Data("{\(patch)}".utf8)) as? [String: Any])
            row.merge(change) { _, new in new }
            return try JSONDecoder.cavnar.decode(RecOutcome.self, from: JSONSerialization.data(withJSONObject: row))
        }
        // Already answered.
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""owner_checkin": {"did_it": "yes", "conditions_changed": false, "at": "2026-09-01 10:00:00"}"#)))
        // Still measuring.
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""status": "tracking""#)))
        // An alert opened, not a change the owner made.
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""informational": true"#)))
        // Couldn't be measured — nothing to attribute.
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""verdict": "unknown""#)))
        // A hand-started, Ask-started or campaign tracker has no
        // recommendation episode to check in on.
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""source_key": "manual:cut lunch""#)))
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""source_key": "ask:trim tuesday""#)))
        XCTAssertFalse(RecCheckIn.isDue(try variant(#""source_key": "campaign:Tuesday:2026-09-01""#)))
        // Worse and flat results are asked about too.
        XCTAssertTrue(RecCheckIn.isDue(try variant(#""verdict": "worsened""#)))
        XCTAssertTrue(RecCheckIn.isDue(try variant(#""verdict": "no_clear_change""#)))
        XCTAssertEqual(RecCheckIn.answers.map(\.code), ["yes", "partly", "no"])
    }

    func testTheCheckInPostsTheKeyTheAnswerAndWhetherAnythingElseChanged() async throws {
        let captured = Box<[String: Any]?>(nil)
        let path = Box<String?>(nil)
        let client = EdgeHTTP.client { request in
            path.value = request.url?.path
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, """
                {"ok": true, "recorded": true, "checkin": {"did_it": "partly", "conditions_changed": true,
                 "note": null, "tracker_id": 7, "attribution": {"implemented": true, "confounded": true, "discount": true}}}
                """)
        }
        let r = try await client.checkIn(key: "trim_day:Tuesday", didIt: "partly", conditionsChanged: true,
                                         surface: "home")
        XCTAssertEqual(path.value, "/mobile/api/recs/checkin")
        XCTAssertEqual(captured.value?["key"] as? String, "trim_day:Tuesday")
        XCTAssertEqual(captured.value?["did_it"] as? String, "partly")
        XCTAssertEqual(captured.value?["conditions_changed"] as? Bool, true)
        XCTAssertEqual(captured.value?["surface"] as? String, "home")
        XCTAssertEqual(r.checkin?.trackerId, 7)
        XCTAssertEqual(r.checkin?.attribution?.discount, true)
        XCTAssertEqual(RecCheckInCard.thanks(didIt: "no", conditionsChanged: false),
                       "Noted \u{2014} this result no longer counts as one of Cavnar\u{2019}s.")
    }

    func testStopMeasuringPostsTheAbandonRoute() async throws {
        let line = Box<String>("")
        let client = EdgeHTTP.client { request in
            line.value = EdgeHTTP.line(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true}"#)
        }
        let r = try await client.abandonOutcome(id: 12)
        XCTAssertTrue(r.ok)
        XCTAssertEqual(line.value, "POST /mobile/api/outcomes/12/abandon")
    }

    // MARK: - What you followed (#13)

    static let summaryJSON = """
        {"ok": true, "days": 90, "since": "2026-06-25",
         "by_module": {
           "labor": {"shown": 14, "answered": 9, "accepted": 4, "completed": 2, "implemented": 1, "dismissed": 2,
                     "ignored": 3, "n": 12, "accept_rate": 0.583, "accept_rate_low": 0.36, "accept_rate_high": 0.77,
                     "enough": true},
           "reviews": {"shown": 4, "answered": 1, "accepted": 1, "completed": 0, "implemented": 0, "dismissed": 0,
                       "ignored": 1, "n": 2, "accept_rate": 0.5, "accept_rate_low": 0.1, "accept_rate_high": 0.9,
                       "enough": false},
           "intel": {"shown": 0, "answered": 0, "accepted": 0, "completed": 0, "implemented": 0, "dismissed": 0,
                     "ignored": 0, "n": 0, "accept_rate": null, "accept_rate_low": null, "accept_rate_high": null,
                     "enough": false}},
         "by_tag": [{"tag": "weekend", "label": "weekend staffing", "module": "labor", "measured": 5, "improved": 4,
                     "worsened": 1, "no_clear_change": 0, "unknown": 1, "success_rate": 0.8, "enough": true}],
         "most_effective": {"tag": "weekend", "label": "weekend staffing", "module": "labor",
                            "success_rate": 0.8, "measured": 5},
         "min_settled": 10, "min_measured": 5}
        """

    func testTheSummaryDecodesAndQuotesARateOnlyWhenThereIsEnough() throws {
        let s = try decode(RecSummary.self, Self.summaryJSON)
        XCTAssertEqual(s.days, 90)
        XCTAssertEqual(s.modulesInOrder.map(\.key), ["labor", "reviews"])     // nothing shown: left out
        let labor = try XCTUnwrap(s.byModule["labor"])
        XCTAssertEqual(labor.followed, 7)
        XCTAssertEqual(RecSummaryFormat.rate(labor), "58%")
        XCTAssertEqual(RecSummaryFormat.range(labor), "36%\u{2013}77% likely range")
        XCTAssertNil(RecSummaryFormat.notEnoughDetail(labor, minimum: s.minSettled))
        // Ignored is always counted — it is in the denominator.
        XCTAssertEqual(RecSummaryFormat.counts(labor), "14 shown · 7 followed · 2 said no · 3 ignored")

        let reviews = try XCTUnwrap(s.byModule["reviews"])
        XCTAssertEqual(RecSummaryFormat.rate(reviews), "Not enough yet")
        XCTAssertNil(RecSummaryFormat.range(reviews))                    // never a range without a rate
        XCTAssertEqual(RecSummaryFormat.notEnoughDetail(reviews, minimum: s.minSettled),
                       "2 of 10 settled \u{2014} a rate shows at 10")
        XCTAssertEqual(RecSummaryFormat.counts(reviews), "4 shown · 1 followed · 1 ignored")

        let e = try XCTUnwrap(s.mostEffective)
        XCTAssertEqual(RecSummaryFormat.mostEffectiveLine(e),
                       "Weekend staffing \u{2014} 4 of 5 measured changes improved")
    }

    func testASummaryWithNothingYetStillDecodes() throws {
        let s = try decode(RecSummary.self, """
            {"ok": true, "days": 30, "since": "2026-08-24", "by_module": {}, "by_tag": [], "most_effective": null,
             "min_settled": 10, "min_measured": 5}
            """)
        XCTAssertTrue(s.modulesInOrder.isEmpty)
        XCTAssertNil(s.mostEffective)
        XCTAssertEqual(RecSummaryFormat.moduleLabel("food"), "Food cost")
        XCTAssertEqual(RecSummaryFormat.moduleLabel("ops"), "Operations")
    }

    // MARK: - Timeline (#20)

    func testTheTimelineDecodesWithNullsAndSaysEachAnswerPlainly() throws {
        let page = try decode(RecTimelinePage.self, """
            {"ok": true, "next_before": null, "items": [
              {"key": "trim_day:Tuesday", "title": "Trim Tuesday lunch", "module": "labor", "tags": ["weekday", "lunch"],
               "first_shown_at": "2026-08-01 12:00:00", "surfaces": ["home", "brief_email"], "answer": "implemented",
               "answered_at": "2026-08-02 09:00:00", "reason_code": null, "reason": null,
               "implemented_at": "2026-08-05 10:00:00", "tracker_id": 7},
              {"key": "reprice:Burger", "title": "Raise the burger $1", "module": "food", "tags": [],
               "first_shown_at": "2026-08-03 12:00:00", "surfaces": [], "answer": "dismissed",
               "answered_at": "2026-08-03 13:00:00", "reason_code": "too_costly", "reason": "tried it in spring",
               "implemented_at": null, "tracker_id": null},
              {"key": "aiv_roadmap:gbp", "title": "Complete your Google Business Profile", "module": "intel",
               "tags": null, "first_shown_at": null, "surfaces": null, "answer": "expired", "answered_at": null,
               "reason_code": null, "reason": null, "implemented_at": null, "tracker_id": null}]}
            """)
        XCTAssertNil(page.nextBefore)
        XCTAssertEqual(page.items.count, 3)
        XCTAssertEqual(page.items[0].answerLabel, "Made the change")
        XCTAssertTrue(page.items[0].wasTaken)
        XCTAssertEqual(page.items[0].trackerId, 7)
        XCTAssertEqual(page.items[1].answerLabel, "Not for us")
        XCTAssertEqual(page.items[1].reasonLine, "Too costly \u{2014} tried it in spring")
        XCTAssertFalse(page.items[1].wasTaken)
        XCTAssertEqual(page.items[2].answerLabel, "Went unanswered")
        XCTAssertTrue(page.items[2].tags.isEmpty)
        XCTAssertNil(page.items[2].reasonLine)
        XCTAssertNotEqual(page.items[0].id, page.items[1].id)
    }

    @MainActor
    func testTheRecordJoinsTheTimelineToItsResultAndPages() async throws {
        let requests = Box<[String]>([])
        let client = EdgeHTTP.client { request in
            let path = request.url?.path ?? ""
            let query = request.url?.query ?? ""
            requests.value.append(path + (query.isEmpty ? "" : "?" + query))
            switch path {
            case "/mobile/api/recs/summary":
                return EdgeHTTP.reply(request, 200, RecOutcomeROITests.summaryJSON)
            case "/mobile/api/outcomes":
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "outcomes": [\#(RecOutcomeROITests.evaluatedRow)]}"#)
            case "/mobile/api/recs/timeline" where query.contains("before="):
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "next_before": null, "items": [
                      {"key": "old:1", "title": "Older one", "answer": "expired", "first_shown_at": "2026-06-01 10:00:00"}]}
                    """)
            case "/mobile/api/recs/timeline":
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "next_before": "2026-08-01 12:00:00|abc", "items": [
                      {"key": "trim_day:Tuesday", "title": "Trim Tuesday lunch", "module": "labor", "answer": "accepted",
                       "first_shown_at": "2026-08-01 12:00:00", "tracker_id": 7}]}
                    """)
            default:
                return EdgeHTTP.reply(request, 404, #"{"ok": false}"#)
            }
        }
        let vm = RecommendationHistoryViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.summary?.days, 90)
        XCTAssertEqual(vm.items.map(\.key), ["trim_day:Tuesday"])
        XCTAssertEqual(vm.outcome(for: vm.items[0])?.resultLine, "Labor % 31.2% → 29.8%, improved")
        XCTAssertTrue(requests.value.contains("/mobile/api/recs/summary?days=90"))
        XCTAssertNotNil(vm.nextBefore)

        await vm.loadMore()
        XCTAssertEqual(vm.items.map(\.key), ["trim_day:Tuesday", "old:1"])
        XCTAssertNil(vm.nextBefore)
        XCTAssertTrue(requests.value.contains { $0.contains("before=2026-08-01") })

        vm.window = .d180
        await vm.loadSummary()
        XCTAssertTrue(requests.value.contains("/mobile/api/recs/summary?days=180"))
    }

    // MARK: - What worked for you (#28)

    func testWhatWorkedIsHiddenUntilThereIsEnough() throws {
        let notYet = try decode(WhatWorked.self, """
            {"ok": true, "days": 90, "enough": false, "sentences": ["You followed 2 of 3."], "facts": {"n": 3}}
            """)
        XCTAssertFalse(notYet.isShown)
        let enough = try decode(WhatWorked.self, """
            {"ok": true, "days": 180, "enough": true,
             "sentences": ["Over the past 6 months you followed 83% of labor recommendations.", "  "],
             "facts": {"accept_rate": 0.83, "by_module": {"labor": {"n": 12}}}}
            """)
        XCTAssertTrue(enough.isShown)
        XCTAssertEqual(enough.sentences, ["Over the past 6 months you followed 83% of labor recommendations."])
        XCTAssertNil(enough.caveat)
        let caveated = try decode(WhatWorked.self, """
            {"ok": true, "days": 180, "enough": true, "sentences": ["s"],
             "facts": {"since": "2026-03-27", "caveat": "Measured before and after, not proven cause."}}
            """)
        XCTAssertEqual(caveated.caveat, "Measured before and after, not proven cause.")
        let empty = try decode(WhatWorked.self, #"{"ok": true, "enough": true, "sentences": []}"#)
        XCTAssertFalse(empty.isShown)
        let failed = try decode(WhatWorked.self, #"{"ok": false, "error": "nope"}"#)
        XCTAssertFalse(failed.isShown)
    }

    // MARK: - The value lines (#1, #14, #34)

    private func delivered(_ json: String) throws -> HomeFollowThroughViewModel.ValueSummary.Delivered {
        try decode(HomeFollowThroughViewModel.ValueSummary.self, #"{"ok": true, "delivered": \#(json)}"#).delivered!
    }

    func testNothingMeasuredIsNeverDrawnAsZeroDollars() throws {
        let d = try delivered("""
            {"monthly": 0, "annual": 0, "wins": 0, "evaluated": 0, "in_flight": 1,
             "worsened": {"count": 0, "monthly": 0.0}, "net_monthly": 0, "net_note": null,
             "validated_monthly": 0.0, "validated": 0, "faded": 0,
             "cumulative": {"total": null, "gained": 0, "lost": 0, "since": null, "until": null, "days": 0,
                            "measured_days": 0, "by_module": {}, "basis": "summed over days actually measured"}}
            """)
        XCTAssertNotNil(d.cumulative)
        XCTAssertNil(d.cumulative?.total)
        XCTAssertNil(RecValueFormat.cumulativeLine(d.cumulative))      // null total: no line at all
        XCTAssertNil(RecValueFormat.netLine(d))
        XCTAssertNil(RecValueFormat.countsLine(d))
        XCTAssertNil(RecValueFormat.cumulativeLine(nil))
    }

    func testAMeasuredZeroIsStillAMeasurement() throws {
        let d = try delivered("""
            {"monthly": 0, "cumulative": {"total": 0, "since": "2026-07-01", "days": 3}}
            """)
        XCTAssertEqual(RecValueFormat.cumulativeLine(d.cumulative),
                       "$0 measured since 7/1/26, over 3 days a change held \u{2014} net of what got worse.")
    }

    func testWorseResultsAreNettedBesideTheImprovements() throws {
        let d = try delivered("""
            {"monthly": 1240, "annual": 14880, "wins": 3, "evaluated": 5,
             "worsened": {"count": 1, "monthly": 260}, "net_monthly": 980,
             "net_note": "$1,240/month of improvements less $260/month from changes that got worse.",
             "validated_monthly": 420, "validated": 1, "faded": 1,
             "cumulative": {"total": 3420.5, "gained": 3900, "lost": 479.5, "since": "2026-07-01", "days": 41}}
            """)
        XCTAssertEqual(RecValueFormat.netLine(d),
                       "Net of 1 change that got worse: $980/month ($260/month worse).")
        XCTAssertNil(RecValueFormat.netNote(d))                          // only below zero
        XCTAssertEqual(RecValueFormat.countsLine(d), "1 validated at re-check ($420/month) · 1 got worse · 1 faded")
        XCTAssertEqual(RecValueFormat.cumulativeLine(d.cumulative),
                       "$3,421 measured since 7/1/26, over 41 days a change held \u{2014} net of what got worse.")
    }

    func testANegativeNetSaysSoInTheServersWords() throws {
        let d = try delivered("""
            {"monthly": 100, "worsened": {"count": 2, "monthly": 400}, "net_monthly": -300,
             "net_note": "More was measured getting worse than improving: $400/month worse against $100/month better.",
             "cumulative": {"total": -520, "since": "2026-08-01", "days": 12}}
            """)
        XCTAssertEqual(RecValueFormat.netLine(d),
                       "Net of 2 changes that got worse: \u{2212}$300/month ($400/month worse).")
        XCTAssertEqual(RecValueFormat.netNote(d),
                       "More was measured getting worse than improving: $400/month worse against $100/month better.")
        XCTAssertEqual(RecValueFormat.cumulativeLine(d.cumulative),
                       "\u{2212}$520 measured since 8/1/26, over 12 days a change held \u{2014} more got worse than improved.")
    }

    func testAnOlderValuePayloadStillDecodes() throws {
        let d = try delivered(#"{"monthly": 400, "annual": 4800, "wins": 1, "evaluated": 1, "in_flight": 0}"#)
        XCTAssertNil(d.worsened)
        XCTAssertNil(d.cumulative)
        XCTAssertNil(RecValueFormat.netLine(d))
        XCTAssertNil(RecValueFormat.cumulativeLine(d.cumulative))
    }

    // MARK: - Home: the one thing, its links, loss flags, check-ins, what worked

    @MainActor
    func testHomeLoadsTheOneThingLinksCheckInsAndWhatWorked() async throws {
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/outcomes":
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "caveat": "c", "outcomes": [\(RecOutcomeROITests.evaluatedRow),
                     {"id": 2, "status": "evaluated", "verdict": "improved", "source_key": "k2", "summary": "s",
                      "owner_checkin": {"did_it": "yes", "conditions_changed": false}}]}
                    """)
            case "/mobile/api/recs/what-worked":
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "days": 90, "enough": false, "sentences": []}"#)
            case "/mobile/api/cross-module":
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true,
                     "fix_first": {"key": "link:lean_friday:friday", "what": "Add one server on Friday dinner",
                                   "why": "complaints fall on the leanest Friday", "modules": ["reviews", "labor"],
                                   "evidence": ["3 complaints"], "dollars_monthly": null,
                                   "link_headline": "Friday complaints fall on your leanest Friday",
                                   "rec_key": "link:lean_friday:friday", "answerable": true},
                     "links": [{"headline": "Friday complaints fall on your leanest Friday", "modules": ["reviews", "labor"],
                                "rec_key": "link:lean_friday:friday", "answerable": true},
                               {"headline": "Waste rises on slow Mondays", "modules": ["food", "labor"],
                                "rec_key": "link:waste_monday:monday", "answerable": false}]}
                    """)
            case "/mobile/api/loss-signals":
                return EdgeHTTP.reply(request, 200, """
                    {"ok": true, "available": true, "week": ["2026-09-14", "9/14/26"],
                     "flagged": [{"type": "comp", "headline": "Comps ran 2x their baseline",
                                  "alternative": "a busy week", "key": "loss:2026-09-14:comp:spike",
                                  "rec_key": "loss:2026-09-14:comp:spike", "answerable": true}]}
                    """)
            default:
                return EdgeHTTP.reply(request, 404, #"{"ok": false}"#)
            }
        }
        let vm = HomeFollowThroughViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.fixFirst?.answerKey, "link:lean_friday:friday")
        XCTAssertEqual(vm.fixFirst?.answerable, true)
        // The link the one thing leads with isn't drawn twice.
        XCTAssertEqual(vm.links.map(\.headline), ["Waste rises on slow Mondays"])
        XCTAssertEqual(vm.links.first?.answerable, false)
        XCTAssertEqual(vm.lossFlags.first?.recKey, "loss:2026-09-14:comp:spike")
        XCTAssertEqual(vm.lossFlags.first?.answerable, true)
        // One landed result has no check-in; the other already has one.
        XCTAssertEqual(vm.checkInsDue.map(\.id), [7])
        XCTAssertEqual(vm.results.count, 2)
        XCTAssertEqual(vm.whatWorked?.isShown, false)
    }

    // MARK: - Daily report actions (#9)

    func testDSRActionsCarryTheirAnswerRowWiring() throws {
        let open = try decode(DSRAction.self, """
            {"text": "Cut a closer on Tuesday", "why": "labor ran 34%", "urgency": "next_schedule", "effort": "low",
             "kind": "control_hours", "dollars_monthly": 180, "key": "dsr_action:control_hours:labor/closer",
             "rec_key": "dsr_action:control_hours:labor/closer", "answered": false, "answerable": true}
            """)
        XCTAssertTrue(open.showsAnswers)
        XCTAssertEqual(open.answerKey, "dsr_action:control_hours:labor/closer")
        XCTAssertEqual(open.answerModule, "labor")
        // Track is offered on a labor action; a sales one answers for ops.
        XCTAssertEqual(RecAnswer.defaults(for: open.answerModule), RecAnswer.allCases)

        let answered = try decode(DSRAction.self, """
            {"text": "Reorder napkins", "key": "dsr_action:reorder:food", "rec_key": "dsr_action:reorder:food",
             "answered": true, "answerable": false}
            """)
        XCTAssertFalse(answered.showsAnswers)                 // the line stays, the controls go
        XCTAssertEqual(answered.answerModule, "food")

        let sales = try decode(DSRAction.self, #"{"text": "Push the patio", "key": "dsr_action:promote:sales"}"#)
        XCTAssertTrue(sales.showsAnswers)                     // older server: keyed and unanswered
        XCTAssertEqual(sales.answerModule, "ops")
        XCTAssertEqual(RecAnswer.defaults(for: sales.answerModule), [.completed, .notForUs])

        let unkeyed = try decode(DSRAction.self, #"{"text": "Check the walk-in"}"#)
        XCTAssertFalse(unkeyed.showsAnswers)
        XCTAssertNil(unkeyed.answerKey)
    }

    func testADSRAnswerIsRecordedOnTheDSRSurface() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
        }
        let action = try decode(DSRAction.self, #"{"text": "t", "key": "dsr_action:control_hours:labor"}"#)
        _ = try await client.answerRecommendation(key: try XCTUnwrap(action.answerKey), answer: .completed,
                                                  surface: "dsr", module: action.answerModule)
        XCTAssertEqual(captured.value?["surface"] as? String, "dsr")
        XCTAssertEqual(captured.value?["module"] as? String, "labor")
        XCTAssertEqual(captured.value?["key"] as? String, "dsr_action:control_hours:labor")
    }

    // MARK: - Coverage payloads

    func testTheLaborReadCarriesItsDiagnosisAndKeys() throws {
        let p = try decode(LaborInsightPayload.self, """
            {"ok": true, "insight": "raw", "insight_intro": "Labor ran hot.",
             "insight_recommendations": ["Cut a Tuesday closer", "Cap Sam at 38h"],
             "insight_rec_keys": ["insight_labor:aa", null], "insight_forecast": null,
             "rec_items": [{"index": 0, "key": "insight_labor:aa", "rec_key": "insight_labor:aa", "text": "Cut a Tuesday closer",
                            "answered": false, "answerable": true}],
             "diagnosis": {"available": true, "cause": "Tuesdays run 36% labor", "alternative_cause": null,
                           "what_would_confirm": "Compare the next two Tuesdays' schedules to their sales.",
                           "operational_evidence": [{"module": "labor", "metric": "labor % over the period", "value": "33% against a 30% target"}],
                           "confidence": "medium", "summary": "Labor ran 33% against 30% over 28 days.",
                           "driver": "weekday:tuesday", "rec_key": "diag_labor:weekday:tuesday",
                           "answered": false, "answerable": true}}
            """)
        XCTAssertEqual(p.insight.recKey(at: 0), "insight_labor:aa")
        XCTAssertNil(p.insight.recKey(at: 1))
        XCTAssertEqual(p.diagnosis?.recKey, "diag_labor:weekday:tuesday")
        XCTAssertEqual(p.diagnosis?.showsAnswers, true)
        XCTAssertEqual(p.diagnosis?.hasCause, true)

        let nothing = try decode(LaborInsightPayload.self, """
            {"insight_intro": "Fine.", "insight_recommendations": [],
             "diagnosis": {"available": true, "cause": null, "summary": "nothing over target", "confidence": "low"}}
            """)
        XCTAssertEqual(nothing.diagnosis?.hasCause, false)
        XCTAssertEqual(nothing.diagnosis?.showsAnswers, false)
        let none = try decode(LaborInsightPayload.self, #"{"insight_intro": "x", "insight_recommendations": []}"#)
        XCTAssertNil(none.diagnosis)
    }

    func testTheAIVisibilityRoadmapComesFromTheServer() throws {
        let r = try decode(AIVisibilityResult.self, """
            {"ok": true, "checklist": [], "roadmap": [
              {"key": "aiv_roadmap:gbp", "rec_key": "aiv_roadmap:gbp", "title": "Complete your Google Business Profile",
               "why": "w", "detail": "62% complete — 3 items left", "action": "Open GBP settings", "impact": "Fast win",
               "module": "account", "done": false, "answered": false, "answerable": true},
              {"key": "aiv_roadmap:reviews", "rec_key": "aiv_roadmap:reviews", "title": "Get more Google reviews",
               "why": null, "detail": null, "action": null, "impact": "Highest impact", "module": "reviews",
               "done": true, "answered": false, "answerable": false}]}
            """)
        XCTAssertEqual(r.roadmap?.map(\.key), ["aiv_roadmap:gbp", "aiv_roadmap:reviews"])
        XCTAssertEqual(r.roadmap?.first?.showsAnswers, true)
        XCTAssertEqual(r.roadmap?.last?.showsAnswers, false)        // done: not a recommendation
        let older = try decode(AIVisibilityResult.self, #"{"ok": true, "checklist": []}"#)
        XCTAssertNil(older.roadmap)
    }

    func testContentIdeasCarryTheirKeys() throws {
        let idea = try decode(ContentCalendarIdea.self, """
            {"day": "Friday", "date": "9/26", "platform": "Instagram", "angle": "Behind the bar on a Friday",
             "type": "post", "iso_date": "2026-09-26", "written": false,
             "rec_key": "content_idea:ab12", "answered": false, "answerable": true}
            """)
        XCTAssertEqual(idea.recKey, "content_idea:ab12")
        XCTAssertTrue(idea.showsAnswers)
        let cached = try decode(ContentCalendarIdea.self, """
            {"day": "Friday", "date": "9/26", "platform": "Instagram", "angle": "a", "type": "post"}
            """)
        XCTAssertFalse(cached.showsAnswers)
        // Round-trips through the on-device calendar cache.
        let again = try decode(ContentCalendarIdea.self, String(decoding: try JSONEncoder().encode(idea), as: UTF8.self))
        XCTAssertEqual(again.recKey, "content_idea:ab12")
    }

    func testAskAnswersCarryAMessageIdAndKeyedSuggestions() throws {
        let e = try decode(APIClient.SSEEvent.self, """
            {"type": "answer", "answer": "1. Cut one server Tuesday", "conversation_id": 4, "message_id": 88,
             "suggestions": [{"text": "Cut one server from Tuesday dinner", "rec_key": "ask_tip:9f", "answerable": true}],
             "modules_consulted": ["labor"], "confidence": "high", "unverified_figures": []}
            """)
        XCTAssertEqual(e.messageId, 88)
        XCTAssertEqual(e.suggestions?.first?.recKey, "ask_tip:9f")
        XCTAssertEqual(e.suggestions?.first?.showsAnswers, true)
        let progress = try decode(APIClient.SSEEvent.self, #"{"type": "progress", "label": "Reading labor"}"#)
        XCTAssertNil(progress.messageId)
        XCTAssertNil(progress.suggestions)
    }

    @MainActor
    func testWasThisUsefulPostsTheAnswersId() async throws {
        let captured = Box<[String: Any]?>(nil)
        let path = Box<String?>(nil)
        let client = EdgeHTTP.client { request in
            path.value = request.url?.path
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, """
                {"ok": true, "feedback": {"message_id": 88, "helpful": false}, "summary": {"rated": 1}}
                """)
        }
        let vm = AskCavnarViewModel(client: client)
        let message = ChatMessage(text: "answer", isUser: false, messageId: 88)
        vm.messages = [message]
        let ok = await vm.rate(message, helpful: false)
        XCTAssertTrue(ok)
        XCTAssertEqual(path.value, "/mobile/api/ask-cavnar/feedback")
        XCTAssertEqual(captured.value?["message_id"] as? Int, 88)
        XCTAssertEqual(captured.value?["helpful"] as? Bool, false)
        XCTAssertEqual(vm.messages.first?.rating, false)
        // No id, no request.
        let none = await vm.rate(ChatMessage(text: "x", isUser: false), helpful: true)
        XCTAssertFalse(none)
    }

    func testAScheduleDraftKeysItsStandbyAndOvertimeWithoutFailingOnAnUnnamedMove() throws {
        let s = try decode(GeneratedSchedule.self, """
            {"ok": true, "status": "done",
             "standby_days": [{"date": "2026-10-03", "day": "Saturday", "chance_of_a_no_show": 0.31,
                               "standby": {"employee": "Ana", "role": "Server"},
                               "rec_key": "standby:2026-10-03:ana", "answerable": false}],
             "overtime_forecast": [
               {"employee": "Sam", "hours": 44, "over": 4, "text": "Sam goes to 44h",
                "candidate": {"employee": "Kim", "role": "Server", "date": "2026-10-02", "shift_start": "16:00"},
                "rec_key": "overtime_move:Sam:2026-10-02", "answerable": true},
               {"employee": "Lee", "hours": 42, "over": 2, "text": "Lee goes to 42h, nobody has room"}]}
            """)
        XCTAssertEqual(s.standbyDays?.first?.recKey, "standby:2026-10-03:ana")
        XCTAssertEqual(s.standbyDays?.first?.answerable, false)
        let moves = (s.overtimeForecast ?? []).compactMap(\.move)
        XCTAssertEqual(moves.map(\.employee), ["Sam"])                 // Lee names nobody: no move
        XCTAssertEqual(moves.first?.recKey, "overtime_move:Sam:2026-10-02")
        XCTAssertEqual(moves.first?.showsNotForUs, true)
        // Round-trips through the on-device schedule cache.
        let again = try decode(GeneratedSchedule.self, String(decoding: try JSONEncoder().encode(s), as: UTF8.self))
        XCTAssertEqual(again.overtimeForecast?.count, 2)
    }

    // MARK: - Push opens (#7)

    func testAPushOpenForwardsItsSurface() throws {
        XCTAssertEqual(PushManager.surface(["surface": "brief_push"]), "brief_push")
        XCTAssertNil(PushManager.surface(["surface": ""]))
        XCTAssertNil(PushManager.surface([:]))
        let body = DeepLinkRouter.OpenedBody(type: "morning_brief", alert_id: 5, rec_key: "brief:1", surface: "brief_push")
        let json = try XCTUnwrap(try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any])
        XCTAssertEqual(json["surface"] as? String, "brief_push")
        // A tap from the in-app list has none, and sends none.
        let listed = DeepLinkRouter.OpenedBody(type: "1star", alert_id: nil, rec_key: nil)
        let listedJSON = try XCTUnwrap(try JSONSerialization.jsonObject(with: JSONEncoder().encode(listed)) as? [String: Any])
        XCTAssertNil(listedJSON["surface"])
    }

    // MARK: - Evidence viewed (#38)

    func testEvidenceViewedIsTheLedgersEvent() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
        }
        await client.recordEvidenceViewed(key: "aiv_roadmap:gbp", surface: "intel")
        XCTAssertEqual(captured.value?["event"] as? String, "evidence_viewed")
        XCTAssertEqual(captured.value?["key"] as? String, "aiv_roadmap:gbp")
        XCTAssertEqual(captured.value?["surface"] as? String, "intel")
        XCTAssertNil(captured.value?["kind"])
    }
}
