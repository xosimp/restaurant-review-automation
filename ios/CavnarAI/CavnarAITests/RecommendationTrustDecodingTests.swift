import XCTest
@testable import CavnarAI

/// The recommendation-trust audit's payloads: every model-written
/// recommendation now carries a rec_ledger key the phone answers with.
/// Each shape here is what the server sends today, and each new field is
/// optional — the older payload beside it must still decode.
final class RecommendationTrustDecodingTests: XCTestCase {

    // MARK: - The answer itself

    func testAnswerRowPostsTheLedgerEvent() async throws {
        let captured = Box<[String: Any]?>(nil)
        let path = Box<String?>(nil)
        let client = EdgeHTTP.client { request in
            path.value = request.url?.path
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
        }
        let r = try await client.answerRecommendation(key: "insight_review:abc", answer: .notForUs, surface: "reviews")
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.recorded, true)
        XCTAssertEqual(path.value, "/mobile/api/recs/event")
        XCTAssertEqual(captured.value?["key"] as? String, "insight_review:abc")
        XCTAssertEqual(captured.value?["event"] as? String, "dismissed")
        XCTAssertEqual(captured.value?["kind"] as? String, "not_for_us")
        XCTAssertEqual(captured.value?["surface"] as? String, "reviews")
        XCTAssertEqual(captured.value?["module"] as? String, "reviews")
    }

    func testOnlyNotForUsCarriesAKind() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true}"#)
        }
        _ = try await client.answerRecommendation(key: "k", answer: .accepted, surface: "intel")
        XCTAssertEqual(captured.value?["event"] as? String, "accepted")
        XCTAssertNil(captured.value?["kind"])
        _ = try await client.answerRecommendation(key: "k", answer: .completed, surface: "food")
        XCTAssertEqual(captured.value?["event"] as? String, "completed")
        XCTAssertNil(captured.value?["kind"])
    }

    func testAnswerConfirmationsSayWhatHappensNext() {
        XCTAssertEqual(RecAnswer.completed.label, "Done")
        XCTAssertEqual(RecAnswer.notForUs.label, "Pass")
        XCTAssertEqual(RecAnswer.accepted.label, "Track")
        XCTAssertEqual(RecAnswer.completed.confirmation, "Done \u{2014} Cavnar AI won\u{2019}t suggest it again")
        XCTAssertEqual(RecAnswer.notForUs.confirmation, "Noted \u{2014} it won\u{2019}t come back")
        // Track's own promise now comes from the server (a tracker on a named
        // metric, or only hidden); the fallback claims nothing it can't keep.
        XCTAssertEqual(RecAnswer.accepted.confirmation, "Noted \u{2014} hidden for 14 days")
    }

    func testTrackIsOfferedOnlyWhereAMetricExists() {
        XCTAssertEqual(RecAnswer.defaults(for: "reviews"), RecAnswer.allCases)
        XCTAssertEqual(RecAnswer.defaults(for: "food"), RecAnswer.allCases)
        XCTAssertEqual(RecAnswer.defaults(for: "intel"), [.completed, .notForUs])
    }

    func testTheServersSentenceTravelsWithTheAnswer() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 200, #"{"ok": true, "recorded": true, "message": "Tracking — Cavnar AI will compare average rating over the next 30 days with the 30 before"}"#)
        }
        let r = try await client.answerRecommendation(key: "k", answer: .accepted, surface: "reviews")
        XCTAssertEqual(r.message, "Tracking \u{2014} Cavnar AI will compare average rating over the next 30 days with the 30 before")
    }

    // MARK: - Reviews

    @MainActor
    func testReviewsInsightDecodesRecsAndDiagnosisRecFields() async {
        let client = EdgeHTTP.client { request in
            switch request.url?.path {
            case "/mobile/api/reviews/insight":
                return EdgeHTTP.reply(request, 200, """
                {"ok": true,
                 "insight": "📊 This week: 12 reviews.\\n✅ Do today: Call back the two guests about cold food.",
                 "recs": [{"key": "insight_review:1a2b", "text": "Call back the two guests about cold food.", "kind": "do_today"}],
                 "diagnoses": [{"category": "food_quality", "mention_count": 6, "window_days": 30,
                                "cause": "The pass is backing up at 7pm.", "recommended_action": "Add a runner Fri-Sat.",
                                "evidence_review_ids": [41, 42], "operational_evidence": [],
                                "confidence": "medium", "rec_key": "diag_review:food_quality",
                                "answered": false, "as_of": "9/21/26",
                                "stale_note": "From a read on 9/14/26 — it has not been refreshed since."}],
                 "diagnosis": {"category": "food_quality", "mention_count": 6, "window_days": 30,
                               "cause": "The pass is backing up at 7pm.", "recommended_action": "Add a runner Fri-Sat.",
                               "evidence_review_ids": [41, 42], "operational_evidence": [],
                               "confidence": "medium", "rec_key": "diag_review:food_quality",
                               "answered": false, "as_of": "9/21/26",
                               "stale_note": "From a read on 9/14/26 — it has not been refreshed since."}}
                """)
            default:
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "data": null, "weeks": []}"#)
            }
        }
        let vm = ReviewsAnalyticsViewModel(client: client)
        await vm.load()
        XCTAssertEqual(vm.insightRecs, [ReviewInsightRec(key: "insight_review:1a2b",
                                                         text: "Call back the two guests about cold food.",
                                                         kind: "do_today")])
        XCTAssertEqual(vm.diagnosis?.recKey, "diag_review:food_quality")
        XCTAssertEqual(vm.diagnosis?.answered, false)
        XCTAssertEqual(vm.diagnosis?.asOf, "9/21/26")
        XCTAssertEqual(vm.diagnosis?.staleNote, "From a read on 9/14/26 — it has not been refreshed since.")
        XCTAssertEqual(vm.diagnosis?.evidenceReviewIds, [41, 42])

        // The answer row lands under the line it answers, and nowhere else.
        let lines = (vm.insight ?? "").split(separator: "\n").map(String.init)
        XCTAssertNil(ReviewsAnalyticsSection.rec(for: lines[0], in: vm.insightRecs))
        XCTAssertEqual(ReviewsAnalyticsSection.rec(for: lines[1], in: vm.insightRecs)?.key, "insight_review:1a2b")
        XCTAssertTrue(ReviewsAnalyticsSection.unplacedRecs(vm.insightRecs, lines: lines).isEmpty)
        XCTAssertEqual(ReviewsAnalyticsSection.unplacedRecs(vm.insightRecs, lines: [lines[0]]).count, 1)
    }

    func testOlderReviewDiagnosisStillDecodes() throws {
        let json = """
        {"category": "service", "mention_count": 3, "window_days": 30, "cause": "Short-staffed Sundays.",
         "evidence_review_ids": [7], "operational_evidence": []}
        """
        let d = try JSONDecoder.cavnar.decode(ReviewDiagnosis.self, from: Data(json.utf8))
        XCTAssertNil(d.recKey)
        XCTAssertNil(d.answered)
        XCTAssertNil(d.staleNote)
        XCTAssertNil(d.asOf)
    }

    // MARK: - Food cost

    func testFoodAnalyticsDecodesAlignedRecKeysIncludingANull() throws {
        let json = """
        {"ok": true, "insight_intro": "Waste is up.",
         "insight_recommendations": ["Cut romaine par by 10%.", "Re-cost the burger."],
         "insight_rec_keys": ["insight_food:aa11", null],
         "insight_forecast": null,
         "waste_items": [], "overstock": [], "critical_low": [], "reorder_soon": [],
         "order_reduction": [], "price_watch": []}
        """
        let a = try JSONDecoder.cavnar.decode(FoodCostAnalytics.self, from: Data(json.utf8))
        XCTAssertEqual(a.insightRecKeys, ["insight_food:aa11", nil])
        XCTAssertEqual(a.insight?.recKey(at: 0), "insight_food:aa11")
        XCTAssertNil(a.insight?.recKey(at: 1), "a null key means no controls for that line")
        XCTAssertNil(a.insight?.recKey(at: 2))
    }

    func testMisalignedRecKeysNeverKeyTheWrongLine() {
        let insight = AIInsight(intro: "x", recommendations: ["a", "b"], forecast: nil, recKeys: ["k1"])
        XCTAssertNil(insight.recKey(at: 0))
        XCTAssertNil(insight.recKey(at: 1))
    }

    func testAIInsightWithoutKeysStillDecodes() throws {
        // Labor's cached copy and older servers: no insight_rec_keys.
        let json = #"{"insight_intro": "Labor ran 2 pts hot.", "insight_recommendations": ["Trim Tuesday."], "insight_forecast": null}"#
        let insight = try JSONDecoder.cavnar.decode(AIInsight.self, from: Data(json.utf8))
        XCTAssertNil(insight.recKeys)
        XCTAssertNil(insight.recKey(at: 0))
        // And it round-trips through the cache encoder.
        let again = try JSONDecoder.cavnar.decode(AIInsight.self, from: try JSONEncoder.cavnar.encode(insight))
        XCTAssertEqual(again, insight)
    }

    func testFoodCFODiagnosisDecodesRecFields() throws {
        let json = """
        {"ok": true, "diagnosis": {"headline": "Protein is the driver.", "cause": "Chicken up 14%.",
          "recommended_action": "Re-bid chicken with a second supplier.", "confidence": "high",
          "rec_key": "diag_food:protein", "answered": true, "as_of": "9/20/26",
          "stale_note": "From a read on 9/13/26 — it has not been refreshed since."}}
        """
        let cfo = try JSONDecoder.cavnar.decode(FoodCostCFO.self, from: Data(json.utf8))
        XCTAssertEqual(cfo.diagnosis?.recKey, "diag_food:protein")
        XCTAssertEqual(cfo.diagnosis?.answered, true)
        XCTAssertEqual(cfo.diagnosis?.asOf, "9/20/26")
        XCTAssertNotNil(cfo.diagnosis?.staleNote)
    }

    func testRepriceSuggestionsDecode() throws {
        let json = """
        {"ok": true, "available": true,
         "suggestions": [
           {"dish": "Chicken Parm", "menu_item_id": 12, "sell_price": 18.0, "plate_cost": 5.9,
            "units_sold_30d": 210, "suggested_price": 19.25, "price_change": 1.25,
            "monthly_margin_lost": 262.5, "monthly_basis": "units sold over the last 28 days, scaled to 30",
            "increase_per_plate": 1.17, "food_cost_pct_before": 26.3, "food_cost_pct_now": 32.8,
            "drivers": [{"ingredient": "Chicken breast", "old_price": 3.1, "new_price": 3.55, "change_pct": 14.5, "per_plate": 0.9},
                        {"ingredient": "Mozzarella", "old_price": 4.0, "new_price": 4.3, "change_pct": 7.5, "per_plate": 0.27}],
            "rec_key": "reprice:chicken parm"},
           {"dish": "Side salad", "menu_item_id": 30, "sell_price": 6.0, "suggested_price": null,
            "price_change": null, "monthly_margin_lost": null,
            "monthly_basis": "no sales mix yet — per-plate figure only", "increase_per_plate": 0.4,
            "drivers": [], "rec_key": "reprice:side salad"}],
         "assumption": "Assumes your ingredient costs already reflect the new price."}
        """
        let r = try JSONDecoder.cavnar.decode(RepriceSuggestions.self, from: Data(json.utf8))
        XCTAssertEqual(r.suggestions?.count, 2)
        let first = try XCTUnwrap(r.suggestions?.first)
        XCTAssertEqual(first.suggestedPrice, 19.25)
        XCTAssertEqual(first.monthlyMarginLost, 262.5)
        XCTAssertEqual(first.recKey, "reprice:chicken parm")
        XCTAssertEqual(first.whyLine, "Chicken breast +15% and 1 more")
        let second = try XCTUnwrap(r.suggestions?.last)
        XCTAssertNil(second.suggestedPrice, "no computed price means no one-tap Set")
        XCTAssertNil(second.monthlyMarginLost, "no sales mix is unknown, never $0")
        XCTAssertNil(second.whyLine)
        XCTAssertEqual(r.assumption, "Assumes your ingredient costs already reflect the new price.")

        let unavailable = try JSONDecoder.cavnar.decode(
            RepriceSuggestions.self,
            from: Data(#"{"ok": true, "available": false, "reason": "no real inventory data"}"#.utf8))
        XCTAssertNil(unavailable.suggestions)
    }

    func testRepriceApplyResponseDecodes() throws {
        let json = """
        {"ok": true, "dish": "Chicken Parm", "menu_item_id": 12, "old_price": 18.0,
         "suggested_price": 19.25, "price": 19.25, "rec_key": "reprice:chicken parm", "tracked": true}
        """
        let r = try JSONDecoder.cavnar.decode(RepriceApplyResult.self, from: Data(json.utf8))
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.price, 19.25)
        XCTAssertEqual(r.oldPrice, 18.0)
        XCTAssertEqual(r.tracked, true)
    }

    @MainActor
    func testApplyRepriceSendsTheSuggestedPriceAndRecordsIt() async throws {
        let captured = Box<[String: Any]?>(nil)
        let client = EdgeHTTP.client { request in
            captured.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, """
            {"ok": true, "dish": "Chicken Parm", "menu_item_id": 12, "old_price": 18.0,
             "suggested_price": 19.25, "price": 19.25, "rec_key": "reprice:chicken parm", "tracked": true}
            """)
        }
        let s = try JSONDecoder.cavnar.decode(RepriceSuggestions.Suggestion.self, from: Data("""
        {"dish": "Chicken Parm", "menu_item_id": 12, "sell_price": 18.0, "suggested_price": 19.25}
        """.utf8))
        let vm = FoodCostAnalyticsViewModel(client: client)
        await vm.applyReprice(s)
        XCTAssertEqual(captured.value?["dish"] as? String, "Chicken Parm")
        XCTAssertEqual(captured.value?["price"] as? Double, 19.25)
        XCTAssertEqual(vm.repriceApplied["Chicken Parm"], 19.25)
        XCTAssertNil(vm.repriceErrors["Chicken Parm"])
    }

    @MainActor
    func testApplyRepriceShowsTheServersRefusal() async throws {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 409, #"{"ok": false, "error": "There's no price suggestion for that dish right now."}"#)
        }
        let s = try JSONDecoder.cavnar.decode(RepriceSuggestions.Suggestion.self, from: Data("""
        {"dish": "Chicken Parm", "suggested_price": 19.25}
        """.utf8))
        let vm = FoodCostAnalyticsViewModel(client: client)
        await vm.applyReprice(s)
        XCTAssertNil(vm.repriceApplied["Chicken Parm"])
        XCTAssertEqual(vm.repriceErrors["Chicken Parm"], "There's no price suggestion for that dish right now.")
    }

    // MARK: - Marketing

    func testMarketingInsightDecodesRecKeys() throws {
        let json = """
        {"ok": true, "insight_intro": "Reels outpull photos.",
         "insight_recommendations": ["Post a reel Thursday.", "Feature the special."],
         "insight_rec_keys": [null, "insight_marketing:77"]}
        """
        let insight = try JSONDecoder.cavnar.decode(AIInsight.self, from: Data(json.utf8))
        XCTAssertNil(insight.recKey(at: 0))
        XCTAssertEqual(insight.recKey(at: 1), "insight_marketing:77")
    }

    func testAttributionColoursFromTheVerdictNotTheSign() throws {
        let json = """
        {"ok": true, "posts": [
          {"topic": "Taco Tuesday", "platform": "instagram", "posted_at": "2026-09-01",
           "window_sales": 4100, "baseline_sales": 3980, "lift_pct": 3.0, "baseline_days": 3,
           "verdict": "no_clear_change", "noise_band_pct": 8.4},
          {"topic": "Brunch", "window_sales": 5200, "baseline_sales": 4400, "lift_pct": 18.2, "baseline_days": 3,
           "verdict": "lifted", "noise_band_pct": 6.0},
          {"topic": "Rainy day", "window_sales": 3000, "baseline_sales": 3600, "lift_pct": -16.7, "baseline_days": 3,
           "verdict": "dropped", "noise_band_pct": 7.1},
          {"topic": "Old server", "window_sales": 3000, "baseline_sales": 3100, "lift_pct": -3.2, "baseline_days": 3}
         ],
         "weakest": [{"topic": "Rainy day", "window_sales": 3000, "baseline_sales": 3600, "lift_pct": -16.7,
                      "baseline_days": 3, "verdict": "dropped", "noise_band_pct": 7.1}]}
        """
        let a = try JSONDecoder.cavnar.decode(MarketingAttribution.self, from: Data(json.utf8))
        XCTAssertEqual(a.posts[0].liftVerdict, .noClearChange, "+3% inside a ±8% band is not a lift")
        XCTAssertEqual(a.posts[0].noiseBandPct, 8.4)
        XCTAssertEqual(a.posts[1].liftVerdict, .lifted)
        XCTAssertEqual(a.posts[2].liftVerdict, .dropped)
        XCTAssertNil(a.posts[3].verdict)
        XCTAssertEqual(a.posts[3].liftVerdict, .dropped, "an older payload falls back to the sign")
        XCTAssertEqual(a.weakest?.first?.liftVerdict, .dropped)
    }

    func testWinbackSuggestionDecodes() throws {
        let json = """
        {"ok": true, "available": true, "draft": {
          "id": 9, "restaurant_id": 2, "kind": "winback", "segment": "lapsed_60",
          "segment_label": "Haven't been back in 60 days", "segment_size": 42,
          "message": "We miss you at Gia Mia — come see what's new this week. Reply STOP to opt out.",
          "rec_key": "winback:lapsed_60", "status": "pending",
          "return": {"measured": false, "campaigns": 0, "text": "No past win-back text has a measured return yet."},
          "max_chars": 320, "sms_window": "8:00 AM and 9:00 PM"}}
        """
        let w = try JSONDecoder.cavnar.decode(GuestWinback.self, from: Data(json.utf8))
        XCTAssertEqual(w.available, true)
        let d = try XCTUnwrap(w.draft)
        XCTAssertEqual(d.id, 9)
        XCTAssertEqual(d.segmentSize, 42)
        XCTAssertEqual(d.segmentLabel, "Haven't been back in 60 days")
        XCTAssertEqual(d.pastReturn?.measured, false)
        XCTAssertEqual(d.pastReturn?.text, "No past win-back text has a measured return yet.")
        XCTAssertEqual(d.maxChars, 320)
        XCTAssertEqual(d.recKey, "winback:lapsed_60")

        let none = try JSONDecoder.cavnar.decode(GuestWinback.self, from: Data("""
        {"ok": true, "available": false, "reason": "no lapsed segment has 10+ opted-in guests who can be texted"}
        """.utf8))
        XCTAssertNil(none.draft)
    }

    @MainActor
    func testWinbackSendRefusalShowsTheServersSentence() async {
        let client = EdgeHTTP.client { request in
            if request.url?.path == "/mobile/api/guest-winback" {
                return EdgeHTTP.reply(request, 200, """
                {"ok": true, "available": true, "draft": {"id": 9, "segment_size": 42, "message": "Come back!",
                 "max_chars": 320}}
                """)
            }
            return EdgeHTTP.reply(request, 400, #"{"ok": false, "error": "Texts only go out between 8:00 AM and 9:00 PM."}"#)
        }
        let vm = GuestTextClubViewModel(client: client)
        await vm.loadWinback()
        XCTAssertEqual(vm.winbackMessage, "Come back!")
        XCTAssertEqual(vm.winbackCharsLeft, 310)
        XCTAssertTrue(vm.canSendWinback)
        await vm.sendWinback()
        XCTAssertNil(vm.winbackSentTotal)
        XCTAssertEqual(vm.winbackError, "Texts only go out between 8:00 AM and 9:00 PM.")
    }

    // MARK: - Intel

    func testIntelDecodesRecommendationItemsAndCites() throws {
        let json = """
        {"ok": true, "has_data": true, "intro": "You lead on service.",
         "recommendations": ["Add a weekday lunch special."],
         "recommendation_items": [{"key": "insight_intel:9f", "text": "Add a weekday lunch special.",
           "cites": [{"ref": "R2", "competitor": "Luigi's", "rating": 2, "time": "3 weeks ago",
                      "text": "Lunch took 40 minutes."},
                     {"ref": "R5", "competitor": "Pasta Co", "rating": null, "time": null, "text": "Pricey."}]}],
         "recommendations_withheld": 0, "recommendations_unverified": null, "nothing_to_act_on": false,
         "sections": [], "competitors": []}
        """
        let s = try JSONDecoder.cavnar.decode(IntelSummary.self, from: Data(json.utf8))
        let item = try XCTUnwrap(s.displayRecommendations.first)
        XCTAssertEqual(item.key, "insight_intel:9f")
        XCTAssertEqual(item.cites?.count, 2)
        XCTAssertEqual(item.cites?.first?.competitor, "Luigi's")
        XCTAssertEqual(item.cites?.first?.rating, 2)
        XCTAssertEqual(item.cites?.first?.time, "3 weeks ago")
        XCTAssertNil(item.cites?.last?.rating)
        XCTAssertNil(s.emptyRecommendationsNote)
    }

    func testIntelExplainsAnEmptyRecommendationList() throws {
        let withheld = try JSONDecoder.cavnar.decode(IntelSummary.self, from: Data("""
        {"ok": true, "has_data": true, "recommendations": [], "recommendation_items": [],
         "recommendations_withheld": 3, "recommendations_unverified": "named a figure that wasn't in its data",
         "nothing_to_act_on": false, "sections": [], "competitors": []}
        """.utf8))
        XCTAssertEqual(withheld.emptyRecommendationsNote,
                       "3 recommendations held back: this read named a figure that wasn't in its data.")

        let quiet = try JSONDecoder.cavnar.decode(IntelSummary.self, from: Data("""
        {"ok": true, "has_data": true, "recommendations": [], "recommendation_items": [],
         "recommendations_withheld": 0, "nothing_to_act_on": true, "sections": [], "competitors": []}
        """.utf8))
        XCTAssertEqual(quiet.emptyRecommendationsNote, "Nothing worth acting on this week.")
    }

    func testOlderIntelPayloadFallsBackToPlainStrings() throws {
        let s = try JSONDecoder.cavnar.decode(IntelSummary.self, from: Data("""
        {"ok": true, "has_data": true, "recommendations": ["Answer every 1-star review."],
         "sections": [], "competitors": []}
        """.utf8))
        XCTAssertEqual(s.displayRecommendations.map(\.text), ["Answer every 1-star review."])
        XCTAssertNil(s.displayRecommendations.first?.key, "no key, no answer controls")
    }
}
