import XCTest
@testable import CavnarAI

/// Memory round, UI wave — iOS part A: every field the memory workstreams
/// added to Home, the brief, the nightly report, Ask, Account, Data Health
/// and the K1 Why panel decodes from the server's shape, leniently (a new or
/// odd field is nil, never a screen that fails to decode), and each pure rule
/// the views draw from is pinned here.
@MainActor
final class MemoryRoundIATests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: - Home cards remember

    func testARecommendationCarriesWhatWasSaidBeforeAndTheConflict() throws {
        let rec = try decode(HomeRecommendation.self, #"""
        {"key": "trim_day:Tuesday", "title": "Trim Tuesday staffing",
         "previous_answer": {"answer": "dismissed", "answered_on": "3/12/26", "reason_label": "Too costly",
                             "text": "You passed on this on 3/12/26 ($120/mo then) — the figure has at least doubled since."},
         "delegate_answer": {"by": "Dana", "text": "Dana passed on this: already doing it (9/28/26)"},
         "retest": true, "caution": "Before cutting: 3 service complaints on Tuesdays.",
         "conflict": {"id": "trim_vs_fill:labor:day:tuesday", "rule": "trim_vs_fill", "label": "Trim vs fill",
                      "with": "Fill Tuesday with a text to regulars", "why": "Tuesday: they pull against each other.",
                      "choose": [{"signature": "labor:day:tuesday", "key": "trim_day:Tuesday", "label": "Trim Tuesday staffing"},
                                 {"signature": "guest_outreach:day:tuesday", "key": "fill:Tuesday", "label": "Fill Tuesday"}],
                      "route": {"web": "/api/recs/conflict", "mobile": "/mobile/api/recs/conflict"}}}
        """#)
        XCTAssertEqual(rec.previousAnswer?.answeredOn, "3/12/26")
        XCTAssertEqual(rec.delegateAnswer?.by, "Dana")
        XCTAssertEqual(rec.retest, true)
        XCTAssertEqual(rec.caution, "Before cutting: 3 service complaints on Tuesdays.")
        XCTAssertEqual(rec.conflict?.choose.count, 2)
        XCTAssertEqual(rec.conflict?.mobilePath, "/mobile/api/recs/conflict")
        XCTAssertTrue(rec.conflict?.isAnswerable ?? false)
    }

    func testAMalformedMemoryFieldNeverDropsTheCard() throws {
        let rec = try decode(HomeRecommendation.self, #"""
        {"key": "k", "title": "Do it", "previous_answer": {"text": ""}, "delegate_answer": 7,
         "conflict": {"why": "no id"}, "retest": "yes", "caution": "  "}
        """#)
        XCTAssertNil(rec.previousAnswer)
        XCTAssertNil(rec.delegateAnswer)
        XCTAssertNil(rec.conflict)
        XCTAssertNil(rec.retest)
        XCTAssertNil(rec.caution)
    }

    func testHistoryLinesDrawInOrderAndTheRetestOnlyWithoutAnEarlierAnswer() {
        let prev = RecPreviousAnswer(text: "You passed on this on 3/12/26.")
        let del = RecDelegateAnswer(text: "Dana passed on this.")
        XCTAssertEqual(RecMemoryNote.rows(previous: prev, delegate: del, retest: true).map(\.text),
                       ["You passed on this on 3/12/26.", "Dana passed on this."])
        XCTAssertEqual(RecMemoryNote.rows(previous: nil, delegate: nil, retest: true).map(\.text),
                       [RecMemoryLines.retestLine])
        XCTAssertTrue(RecMemoryNote.rows(previous: nil, delegate: nil, retest: false).isEmpty)
        XCTAssertEqual(RecMemoryLines.reviewOn("11/28/26"), "back for a re-test on 11/28/26")
        XCTAssertNil(RecMemoryLines.reviewOn(" "))
    }

    func testTheConflictPanelsWords() {
        let c = RecConflict(id: "reprice_vs_value:pricing:dish:salmon", with: nil, why: "Poor value.",
                            choose: [.init(signature: "pricing:dish:salmon", key: "reprice:Salmon", label: "Keep it"),
                                     .init(signature: "hold", label: "Hold it")])
        XCTAssertEqual(RecConflictPanel.kicker(c), "Pulls against other advice")
        XCTAssertEqual(RecConflictPanel.buttonLabel(c.choose[0]), "Keep it")
        XCTAssertEqual(RecConflictPanel.buttonLabel(.init(signature: "s", label: "Trim Tuesday staffing on the next schedule")),
                       "Keep \u{201C}Trim Tuesday staffing on the next schedule\u{201D}")
        let withCard = RecConflict(id: "x", with: "Fill Tuesday", choose: [])
        XCTAssertEqual(RecConflictPanel.kicker(withCard), "Pulls against: Fill Tuesday")
        XCTAssertFalse(withCard.isAnswerable)
    }

    func testSettlingAConflictPostsItsIdAndTheSignatureKept() async throws {
        let body = Box<[String: Any]?>(nil)
        let path = Box<String?>(nil)
        let client = EdgeHTTP.client { request in
            path.value = request.url?.path
            body.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "message": "Noted — Cavnar AI will settle this the same way next time"}"#)
        }
        let c = RecConflict(id: "trim_vs_fill:labor:day:tuesday", choose: [.init(signature: "labor:day:tuesday", label: "Keep")])
        let r = try await client.settleConflict(c, prefer: "labor:day:tuesday")
        XCTAssertTrue(r.ok)
        XCTAssertEqual(path.value, "/mobile/api/recs/conflict")
        XCTAssertEqual(body.value?["conflict"] as? String, "trim_vs_fill:labor:day:tuesday")
        XCTAssertEqual(body.value?["prefer"] as? String, "labor:day:tuesday")
    }

    func testAttentionItemsReadTheSameMemoryFields() throws {
        let item = try decode(NeedsAttentionItem.self, #"""
        {"type": "awaiting_approval", "module": "reviews", "title": "3 replies", "detail": "d",
         "previous_answer": {"text": "You said not for us to this on 9/1/26."}, "delegate_answer": null, "conflict": null}
        """#)
        XCTAssertEqual(item.previousAnswer?.text, "You said not for us to this on 9/1/26.")
        XCTAssertNil(item.delegateAnswer)
        XCTAssertEqual(HomeActionDeck.memoryLines(item), 1)
    }

    func testKindHoldsDecodeWithTheServersAnswerWords() throws {
        let hold = try decode(HomeKindHold.self, #"""
        {"key": "kind_hold:trim_day", "kind": "trim_day", "title": "Keep suggesting trim day?",
         "why": "Improved 2 of 6 measured here — no better than doing nothing (before and after, not proof).",
         "measured": 6, "improved": 2, "worsened": 3, "do_nothing_pct": 44,
         "answers": {"completed": "Keep suggesting it", "not_for_us": "Stop suggesting it"},
         "rec_key": "kind_hold:trim_day", "answerable": true}
        """#)
        XCTAssertEqual(hold.keepLabel, "Keep suggesting it")
        XCTAssertEqual(hold.stopLabel, "Stop suggesting it")
        XCTAssertEqual(hold.answerKey, "kind_hold:trim_day")
        XCTAssertTrue(hold.showsAnswers)
        let bars = try XCTUnwrap(HomeKindHoldBars.Model(hold))
        XCTAssertEqual(bars.improvedPct, 33)
        XCTAssertEqual(bars.doNothingPct, 44)
        XCTAssertNil(HomeKindHoldBars.Model(HomeKindHold(key: "k", title: "t")))
    }

    func testHomeDecodesKindHoldsQuieterDatesAndThePolicyNotice() throws {
        let summary = try decode(HomeSummary.self, Self.home(extra: #"""
        , "kind_holds": [{"key": "kind_hold:trim_day", "title": "Keep suggesting trim day?"}, {"nope": 1}],
          "quieter": [{"kind": "trim_day", "label": "Trim day", "review_on": "11/28/26"}],
          "policy_notice": {"key": "policy:2026-10-01", "updated_label": "10/1/26",
                            "text": "We updated our Privacy Policy and Terms on 10/1/26: how Cavnar AI’s benchmarks use pooled, de-identified figures.",
                            "link_label": "Read what changed", "url": "https://cavnar.ai/privacy",
                            "dismiss": {"web": "/api/account/policy-notice/dismiss",
                                        "mobile": "/mobile/api/account/policy-notice/dismiss"}}
        """#))
        XCTAssertEqual(summary.kindHolds?.items.map(\.key), ["kind_hold:trim_day"])
        XCTAssertEqual(summary.quieter?.first?.reviewOn, "11/28/26")
        let notice = try XCTUnwrap(summary.policyNotice)
        XCTAssertEqual(notice.linkText, "Read what changed \u{2192}")
        XCTAssertEqual(notice.mobileDismissPath, "/mobile/api/account/policy-notice/dismiss")
        XCTAssertEqual(notice.destination.absoluteString, "https://cavnar.ai/privacy")
    }

    func testABrokenPolicyNoticeIsNoNoticeNotABrokenHome() throws {
        let summary = try decode(HomeSummary.self, Self.home(extra: #", "policy_notice": {"text": 5}"#))
        XCTAssertNil(summary.policyNotice)
        let offSite = try decode(HomePolicyNotice.self, #"{"key": "k", "text": "t", "url": "https://example.com/x"}"#)
        XCTAssertEqual(offSite.destination.absoluteString, "https://cavnar.ai/privacy")
    }

    func testTheAnswerLineIsTheServersMessage() {
        XCTAssertEqual(HomeFollowThroughViewModel.answerLine(kind: "not_for_us",
                                                             message: "Noted — Cavnar AI will bring it back in 4 weeks",
                                                             trackerLine: nil, evaluateOn: nil),
                       "Noted — Cavnar AI will bring it back in 4 weeks")
        XCTAssertEqual(HomeFollowThroughViewModel.answerLine(kind: "done", message: "Done — hidden for 60 days",
                                                             trackerLine: "Measuring labor % until 10/21/26",
                                                             evaluateOn: nil),
                       "Done — hidden for 60 days. Measuring labor % until 10/21/26")
        XCTAssertEqual(HomeFollowThroughViewModel.answerLine(kind: "snooze", message: nil, trackerLine: nil, evaluateOn: nil),
                       "Not today \u{2014} it\u{2019}s back tomorrow")
        XCTAssertEqual(RecAnswer.notForUs.label, "Pass")
    }

    // MARK: - The brief

    func testTheTodayLineCarriesTheReportsCalls() throws {
        let line = try decode(HomeDayViewModel.BriefLine.self, #"""
        {"key": "today", "source": "dsr", "text": "Today, from last night's report: about $4,200.",
         "predictions": ["Sales above $4,000", "", "Rain holds covers down"], "confidence_pct": 72.4,
         "conflict": {"id": "trim_vs_fill:labor:day:friday", "choose": []}}
        """#)
        XCTAssertEqual(line.reportCalls, ["Sales above $4,000", "Rain holds covers down"])
        XCTAssertEqual(line.confidencePct, 72)
        XCTAssertEqual(line.conflict?.id, "trim_vs_fill:labor:day:friday")
        let notFromReport = try decode(HomeDayViewModel.BriefLine.self,
                                       #"{"key": "today", "text": "t", "predictions": ["x"]}"#)
        XCTAssertTrue(notFromReport.reportCalls.isEmpty)
    }

    func testHomeLeavesOutTheReportsOwnPriority() {
        let lines = [HomeDayViewModel.BriefLine(key: "dsr_action", text: "From last night's report: trim Tuesday.",
                                                rec: "dsr_action:x", source: "dsr"),
                     HomeDayViewModel.BriefLine(key: "memory:constraint", text: "Your note for today: close at 9."),
                     HomeDayViewModel.BriefLine(key: "today", text: "Today …", source: "dsr")]
        XCTAssertEqual(HomeBriefFilter.visible(lines, shown: []).map { $0.key ?? "" }, ["memory:constraint", "today"])
    }

    // MARK: - The nightly report

    func testAReportActionKeepsItsLineWhateverItsNewFieldsHold() throws {
        let a = try decode(DSRAction.self, #"""
        {"text": "Cut a closer on Tuesday", "caution": "Before cutting: 2 wait complaints.",
         "conflict": {"id": "c1", "choose": [{"signature": "a", "label": "Keep it"}, {"signature": "hold", "label": "Hold it"}]},
         "dollars_monthly": "not a number"}
        """#)
        XCTAssertEqual(a.caution, "Before cutting: 2 wait complaints.")
        XCTAssertEqual(a.conflict?.choose.count, 2)
        XCTAssertNil(a.dollarsMonthly)
        let broken = try decode([DSRAction].self, #"[{"text": "A", "conflict": {"nope": true}}, {"text": "B"}]"#)
        XCTAssertEqual(broken.map(\.text), ["A", "B"])
        XCTAssertNil(broken[0].conflict)
    }

    func testTheFooterSaysWhyAnActionWasLeftOut() throws {
        let v = try decode(DSRVerification.self, #"""
        {"checked": 9, "kept": 6, "dropped": [
          {"field": "actions_tomorrow[0]", "text": "x", "why": "a figure didn't trace"},
          {"field": "actions_tomorrow", "text": "y", "why": "the owner said not for us to this", "key": "k1"},
          {"field": "actions_tomorrow", "text": "", "why": "the owner chose the other advice when these conflicted", "key": "k2"},
          {"field": "actions_tomorrow", "text": "z", "why": "Not suggesting a Tuesday trim: the text to regulars fills it", "key": "k3"}]}
        """#)
        XCTAssertEqual(v.droppedAnswered, 1)
        XCTAssertEqual(v.leftOutWhys, ["the owner chose the other advice when these conflicted",
                                       "Not suggesting a Tuesday trim: the text to regulars fills it"])
        let footer = try XCTUnwrap(v.footer)
        XCTAssertTrue(footer.contains("1 dropped because it didn\u{2019}t pass the check"))
        XCTAssertTrue(footer.contains("1 left out: Not suggesting a Tuesday trim"))
    }

    func testTheSalesAndIntelBlocksReadTheirNewDetail() throws {
        let sales = DSRBlock(json: try decode(JSONValue.self, #"""
        {"status": "ready", "metrics": {"net": 4100},
         "detail": {"budget": {"net": 9000, "source": "goal", "label": "Your goal of $9,000 a night by 12/31/26"},
                    "baselines": {"forecast": {"net": null, "reason": "Fewer than 3 reports for Tuesdays on this basis"}}}}
        """#))
        XCTAssertEqual(sales.budgetGoalLabel, "Your goal of $9,000 a night by 12/31/26")
        XCTAssertNil(sales.forecastBasis)
        XCTAssertEqual(sales.forecastMissingReason, "Fewer than 3 reports for Tuesdays on this basis")
        let withForecast = DSRBlock(json: try decode(JSONValue.self, #"""
        {"status": "ready", "detail": {"budget": {"source": "budget", "label": "Budget"},
          "baselines": {"forecast": {"net": 3900, "basis": "median of the last 8 Tuesdays; Rain -12% (measured 5 times here)"}}}}
        """#))
        XCTAssertNil(withForecast.budgetGoalLabel)
        XCTAssertEqual(withForecast.forecastBasis, "median of the last 8 Tuesdays; Rain -12% (measured 5 times here)")
        let intel = DSRBlock(json: try decode(JSONValue.self, #"""
        {"status": "ready", "detail": {"weather": {"summary": "Forecast: rain",
          "observed": {"summary": "Light rain · high 71° · rain during service",
                       "basis": "observed by the nearest National Weather Service station"}}}}
        """#))
        XCTAssertEqual(intel.weatherObservedSummary, "Light rain · high 71° · rain during service")
    }

    // MARK: - Ask

    func testDeclinedRepeatsAreReadTopLevelOrUnderMeta() throws {
        let meta = try decode(AskMeta.self, #"{"declined_repeats": [{"text": "Cut a Friday closer", "declined_on": "8/12/26"}, 4]}"#)
        XCTAssertEqual(meta.declinedRepeats.map(\.declinedOn), ["8/12/26"])
        let ev = AskEvidence(declinedRepeats: meta.declinedRepeats)
        XCTAssertFalse(ev.isEmpty)
        XCTAssertEqual(ev.declinedLine,
                       "1 suggestion here is one you passed on, on 8/12/26 \u{2014} marked in the answer.")
        let two = AskEvidence(declinedRepeats: [.init(text: "a"), .init(text: "b")])
        XCTAssertEqual(two.declinedLine, "2 suggestions here are ones you passed on before \u{2014} each marked in the answer.")
        let event = try decode(APIClient.SSEEvent.self, #"""
        {"type": "answer", "answer": "…", "declined_repeats": [{"text": "Cut a Friday closer", "declined_on": "8/12/26"}]}
        """#)
        XCTAssertEqual(event.evidence.declinedRepeats.count, 1)
    }

    func testARatingsPreferenceIsSaidInTheOwnersWords() throws {
        let r = try decode(AskCavnarViewModel.FeedbackResponse.self,
                           #"{"ok": true, "preference": {"preference": "short", "fact": "Prefers short answers"}}"#)
        XCTAssertEqual(AskCavnarViewModel.preferenceNote(r.preference?.preference),
                       "Got it \u{2014} shorter answers for you (Account \u{2192} Memory)")
        XCTAssertNil(AskCavnarViewModel.preferenceNote(nil))
        let none = try decode(AskCavnarViewModel.FeedbackResponse.self, #"{"ok": true, "preference": null}"#)
        XCTAssertNil(none.preference)
    }

    func testAConfirmCardPostsItsListsIntact() throws {
        let p = try decode(AskProposal.self, #"""
        {"action": "set_staff_unavailable", "summary": "Mark Maria as not available on Sunday",
         "route": {"mobile": "/mobile/api/labor/availability", "method": "POST"},
         "body": {"employee_name": "Maria", "unavailable_days": ["Monday", "Sunday"], "notes": "school"},
         "fields_shown": [{"key": "employee_name", "label": "Who", "value": "Maria"},
                          {"key": "unavailable_days", "label": "Not available on", "value": "Monday, Sunday"},
                          {"key": "notes", "label": "Their notes (kept)", "value": "school"}]}
        """#)
        let posted = try JSONSerialization.jsonObject(with: JSONEncoder().encode(p.postedBody)) as? [String: Any]
        XCTAssertEqual(posted?["unavailable_days"] as? [String], ["Monday", "Sunday"],
                       "a list read as null saved the person as available every day")
        XCTAssertEqual(posted?["notes"] as? String, "school")
    }

    // MARK: - Account

    func testTheMemorySheetReadsFactsLanesAndTheArchive() throws {
        let m = try decode(AccountMemory.self, #"""
        {"facts": [{"id": 1, "fact": "Never cut the host", "kind": "constraint", "author": "Erik, owner",
                    "audience": "principals", "modules": ["labor"], "created_on": "9/21/26",
                    "valid_until_label": "12/31/26", "can_forget": true},
                   {"id": 2, "fact": "Call the linen company", "kind": "followup", "due_label": "10/3/26"},
                   {"fact": "no id"}],
         "lanes": [{"kind": "constraint", "count": 30, "cap": 30}, {"kind": "context", "count": 3, "cap": 30}],
         "archived": [{"id": 9, "fact": "Old rule", "reason_label": "its lane was full", "archived_on": "9/2/26",
                       "can_restore": true}]}
        """#)
        XCTAssertEqual(m.facts.map(\.id), [1, 2])
        XCTAssertEqual(m.facts[0].detailLine(viewerIsPrincipal: true),
                       "Erik, owner \u{00B7} Only owners \u{00B7} Labor \u{00B7} until 12/31/26 \u{00B7} added 9/21/26")
        XCTAssertFalse(m.facts[1].canForget)
        XCTAssertEqual(m.facts[1].detailLine(viewerIsPrincipal: false), "due 10/3/26")
        XCTAssertTrue(m.lanes[0].isFull)
        XCTAssertEqual(m.lanes[1].fraction, 0.1, accuracy: 0.001)
        XCTAssertEqual(m.archived.first?.detailLine, "Left 9/2/26 \u{2014} replaced by newer notes", "a full lane in the owner's words (re-audit M21)")
    }

    func testTheAddFormSendsEachFieldItNames() throws {
        var d = AccountMemoryViewModel.Draft()
        d.fact = "  We close early the first Sunday  "
        d.kind = "followup"
        d.modules = ["ops", "labor"]
        d.days = 7
        d.audience = "author"
        let today = try XCTUnwrap(Calendar.current.date(from: DateComponents(year: 2026, month: 9, day: 29)))
        let body = AccountMemoryViewModel.addBody(d, today: today)
        XCTAssertEqual(body.fact, "We close early the first Sunday")
        XCTAssertEqual(body.modules, ["labor", "ops"])
        XCTAssertEqual(body.dueOn, "2026-10-06")
        XCTAssertNil(body.validUntil)
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual(json?["due_on"] as? String, "2026-10-06")
        XCTAssertEqual(json?["audience"] as? String, "author")
        d.kind = "constraint"
        d.days = 0
        XCTAssertNil(AccountMemoryViewModel.addBody(d, today: today).validUntil)
    }

    func testProposedGoalsAndTheTrustRecord() throws {
        let g = try decode(ProposedGoal.self, #"{"id": 4, "summary": "Labor under 26% by 12/31/26", "proposed_by": "Your sales audit"}"#)
        XCTAssertEqual(g.byLine, "Proposed by your sales audit")
        let t = try decode(AccountAutomationViewModel.Trust.self, #"""
        {"ok": true, "auto_approve": {"bands": {"5": {"trusted": true, "earned_at": "2026-09-02 14:00:00", "weak_credit": 1.5},
                                                "4": {"trusted": false, "lapsed": {"at": "2026-09-20", "reason": "a retraction"}}}},
         "schedule": {"unedited_in_a_row": 1, "needed": 3, "undone_on": "9/20/26"},
         "suppliers": [{"name": "Sysco", "trusted": false, "orders": 2, "needed": 1,
                        "undone_at": "2026-09-18 08:30:00", "clean_since_undo": 1}],
         "invoices": [],
         "lapsed": [{"scope": "auto_approve", "subject": "4", "lapsed_on": "9/20/26",
                     "text": "4-star replies went back to you for approval on 9/20/26 (a retraction)."}]}
        """#)
        XCTAssertEqual(t.lapsed.first?.lapsedOn, "9/20/26")
        XCTAssertEqual(t.schedule?.undoneOn, "9/20/26")
        XCTAssertEqual(t.suppliers.first?.cleanSinceUndo, 1)
        let detail = try XCTUnwrap(t.bandsDetail)
        XCTAssertTrue(detail.hasPrefix("5\u{2605} since "))
        XCTAssertTrue(detail.contains("1.5 of the record is auto-posts left standing a week"))
    }

    func testTheChangeHistoryIsTheServersLine() throws {
        let c = try decode([AccountChange].self, #"""
        [{"kind": "target", "field": "labor_target_pct", "line": "Labor target: 30 → 28, by the owner on 9/12/26",
          "changed_at": "2026-09-12 15:00:00"}]
        """#)
        XCTAssertEqual(c.first?.line, "Labor target: 30 → 28, by the owner on 9/12/26")
        XCTAssertEqual(c.first?.symbol, "target")
    }

    func testMyNotificationsReadWhereEachSettingComesFrom() throws {
        let p = try decode(AccountPreferences.self, #"""
        {"ok": true, "mine": {"push_enabled": true, "push_muted_types": ["5star"], "quiet_start": "22:00",
                              "quiet_end": "07:00", "morning_brief": false},
         "location": {"alert_quiet_start": {"value": "23:00", "source": "this location"},
                      "alert_quiet_end": {"value": "07:00", "source": "all locations"},
                      "briefing_level": {"value": "normal", "source": "all locations"}},
         "can_apply_to_all": true, "locations": [{"id": 1, "name": "Main"}, {"id": 2, "name": "North"}],
         "unmutable_types": ["health", "issue"],
         "alert_types": [{"type": "5star", "label": "5★ review received"}, {"type": "health", "label": "Health"}],
         "never_opened": []}
        """#)
        XCTAssertTrue(p.mine.hasQuietHours)
        XCTAssertEqual(p.mine.morningBrief, false)
        XCTAssertTrue(p.showsSources)
        let quiet = try XCTUnwrap(p.sharedSettings.first { $0.id == "quiet" })
        XCTAssertEqual(quiet.source, "this location")
        XCTAssertEqual(quiet.keys, ["alert_quiet_start", "alert_quiet_end"])
        XCTAssertEqual(p.sharedSettings.first { $0.id == "level" }?.sourceLabel, "All locations")
        XCTAssertEqual(p.checklist().map(\.type), ["5star"], "health can never be muted")
        XCTAssertEqual(AccountPreferences.clock("22:00"), "10pm")
        XCTAssertEqual(AccountPreferences.clock("21:30"), "9:30pm")
    }

    func testClearingMyQuietHoursSendsNullAndNothingElse() throws {
        let clear = AccountPreferencesViewModel.MineBody(quietStart: .some(nil), quietEnd: .some(nil))
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(clear)) as? [String: Any]
        XCTAssertEqual(json?.keys.sorted(), ["quiet_end", "quiet_start"])
        XCTAssertTrue(json?["quiet_start"] is NSNull)
        let mute = AccountPreferencesViewModel.MineBody(mutedTypes: ["5star"])
        let m = try JSONSerialization.jsonObject(with: JSONEncoder().encode(mute)) as? [String: Any]
        XCTAssertEqual(m?.keys.sorted(), ["push_muted_types"])
    }

    // MARK: - Data health, the Why panel, the undo

    func testDistrustedSourcesDecodeWithTheirVerifyRoute() throws {
        let snap = try decode(DataHealthSnapshot.self, #"""
        {"ok": true, "sources": [], "not_connected": [], "modules": [],
         "distrusted": [{"source": "sales", "label": "Sales", "since": "9/20/26", "reports": 2,
                         "text": "You said you don't trust the sales data (9/20/26, 2 times).",
                         "verify": {"web": "/api/data-health/verify", "mobile": "/mobile/api/data-health/verify", "source": "sales"}},
                        {"label": "no source or text"}]}
        """#)
        XCTAssertEqual(snap.distrusted.map(\.source), ["sales"])
        XCTAssertEqual(snap.distrusted.first?.verifyPath, "/mobile/api/data-health/verify")
    }

    func testTheWhyPanelOffersTheProfileWhenItWouldUnlockAComparison() throws {
        let c = try decode(TrustConfidence.self, #"""
        {"pct": 58, "band": "medium", "dimensions": {
           "evidence": {"pct": 70, "basis": "12 reviews"},
           "accuracy": {"pct": null, "n": 2, "basis": "Not enough history yet — 2 measured, needs 5",
                        "source": "none", "prior_unlock": "confirm_profile", "prior_rung": null},
           "freshness": {"pct": 90}}}
        """#)
        XCTAssertEqual(c.dimensions?.accuracy?.priorUnlock, "confirm_profile")
        let d = ConfidenceDisplay(c)
        XCTAssertTrue(d.profileUnlock)
        XCTAssertEqual(d.rows[1].note, ConfidenceDisplay.profileUnlockNote)
        let measured = try decode(TrustConfidence.Dimension.self,
                                  #"{"pct": 70, "prior": {"source": "cohort", "rung": "concept"}}"#)
        XCTAssertEqual(measured.priorRung, "concept")
        XCTAssertFalse(measured.unlocksWithProfile)
    }

    func testAnUndoCarriesItsQuestion() throws {
        let r = try decode(UndoResponse.self, #"""
        {"ok": true, "message": "Undone — Cavnar AI will wait for a few clean runs before doing this on its own again",
         "ask_why": {"route": "/actions/12/why",
                     "options": [{"code": "not_ready", "label": "It wasn't ready yet"}, {"code": "bad_timing", "label": "Bad timing"}]}}
        """#)
        XCTAssertEqual(r.askWhy?.mobilePath, "/mobile/api/actions/12/why")
        XCTAssertEqual(r.askWhy?.options.map(\.code), ["not_ready", "bad_timing"])
        let plain = try decode(UndoResponse.self, #"{"ok": true}"#)
        XCTAssertNil(plain.askWhy)
    }

    // MARK: - Fixtures

    /// The smallest /mobile/api/home a HomeSummary decodes, plus `extra`.
    static func home(extra: String) -> String {
        #"{"restaurant_name": "R", "reviews_awaiting_approval": 0, "modules": [], "needs_attention": [], "#
            + #""total_value_delivered": 0, "value_history": [], "quiet_hours_active": false"# + extra + "}"
    }
}
