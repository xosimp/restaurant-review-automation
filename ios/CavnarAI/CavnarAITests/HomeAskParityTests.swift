import XCTest
@testable import CavnarAI

/// The iOS parity fix round (10/7/26), Home / Ask / issues: the decoding
/// and request shapes behind Still open, Ask's confirm, issues, the group
/// Home, missed goals, the hero date and Restaurant DNA.
@MainActor
final class HomeAskParityTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: #3 — Still open passes the server's body through, whole

    func testAStillOpenItemDecodesItsBodyConfirmAltAndNav() throws {
        let item = try decode(ActionItem.self, """
            {"key": "time_off:12", "kind": "time_off", "title": "Ana asked for time off from 10/9/26",
             "severity": "important", "nav": "request/time_off-12",
             "action": {"label": "Approve", "method": "POST",
                        "route": {"web": "/api/labor/time-off/12/decide", "mobile": "/mobile/api/labor/time-off/12/decide"},
                        "body": {"decision": "approve"},
                        "confirm": {"action": "decide_time_off", "args": {"request_id": 12, "decision": "approve"}},
                        "module": "labor", "nav": "request/time_off-12",
                        "alt": {"label": "Deny", "method": "POST",
                                "route": {"mobile": "/mobile/api/labor/time-off/12/decide"},
                                "body": {"decision": "deny"},
                                "confirm": {"action": "decide_time_off", "args": {"request_id": 12, "decision": "deny"}}}}}
            """)
        let action = try XCTUnwrap(item.action)
        XCTAssertEqual(action.body?["decision"], .string("approve"))
        XCTAssertEqual(action.confirm?.action, "decide_time_off")
        XCTAssertEqual(action.confirm?.args["request_id"], .int(12))
        XCTAssertEqual(action.alt?.label, "Deny")
        XCTAssertEqual(action.alt?.body?["decision"], .string("deny"))
        XCTAssertEqual(item.destination?.raw, "request/time_off-12")
        // An outward step opens its confirm card first, never a one-tap post.
        if case .confirm(let c) = action.step.kind { XCTAssertEqual(c.action, "decide_time_off") } else {
            XCTFail("Approve must open the confirm card")
        }
        XCTAssertEqual(HomeFollowThrough.altTint(try XCTUnwrap(action.alt)), .cavnarRed)
    }

    func testSendNowPostsTheScheduleIdItsRouteReads() throws {
        let item = try decode(ActionItem.self, """
            {"key": "schedule_unsent:88", "kind": "schedule", "title": "The week of 10/12/26 is built",
             "severity": "critical",
             "action": {"label": "Send now", "method": "POST", "history_id": 88,
                        "route": {"web": "/api/labor/publish-schedule", "mobile": "/mobile/api/labor/publish-schedule"},
                        "body": {"schedule_id": 88}, "nav": "schedule/88",
                        "alt": {"label": "Open it", "nav": "schedule/88"}}}
            """)
        let step = try XCTUnwrap(item.action?.step)
        // The body is the server's: it posted {} and got a 400 (re-audit).
        var body = try object(HomeFollowThroughViewModel.postBody(step))
        XCTAssertEqual(body["schedule_id"] as? Int, 88)
        // Sent past the publish gate: the keys shown, or true.
        body = try object(HomeFollowThroughViewModel.postBody(step, acknowledge: .array([.string("ot"), .string("mgr")])))
        XCTAssertEqual(body["acknowledge"] as? [String], ["ot", "mgr"])
        XCTAssertEqual(body["schedule_id"] as? Int, 88)
        if case .open = try XCTUnwrap(item.action?.alt).kind {} else { XCTFail("Open it opens the week") }
    }

    func testAnUnansweredAskProposalOpensAsAProposalNotTheAskModule() throws {
        let item = try decode(ActionItem.self, """
            {"key": "ask:41", "kind": "proposal", "title": "Email Fresh Co the order", "severity": "watch",
             "nav": "action/41",
             "action": {"label": "Open it", "module": "ask", "proposal_id": 41,
                        "ask": "Show me the proposal you made: Email Fresh Co the order", "nav": "action/41"}}
            """)
        XCTAssertEqual(item.proposalId, 41)
        XCTAssertEqual(item.destination?.raw, "proposal/41")
        XCTAssertEqual(item.destination(for: try XCTUnwrap(item.action?.step))?.raw, "proposal/41")
    }

    func testTheReviewersRepriceBodyStillRoundTrips() throws {
        let item = try decode(ActionItem.self, """
            {"key": "reprice:Burger", "kind": "reprice", "title": "Burger", "severity": "watch",
             "action": {"label": "Reprice to $16.50", "method": "POST",
                        "route": {"mobile": "/mobile/api/food-cost/reprice/apply"},
                        "body": {"dish": "Burger", "price": 16.5}}}
            """)
        let body = try object(HomeFollowThroughViewModel.postBody(try XCTUnwrap(item.action?.step)))
        XCTAssertEqual(body["dish"] as? String, "Burger")
        XCTAssertEqual(body["price"] as? Double, 16.5)
    }

    // MARK: #4 — Ask's confirm: one request, the right job, the warning

    func testTheConfirmNamesItsProposalAndConversation() {
        XCTAssertEqual(AskCavnarViewModel.confirmHeaders(proposalId: 7, conversationId: 3),
                       ["X-Cavnar-Proposal": "7", "X-Cavnar-Conversation": "3"])
        XCTAssertEqual(AskCavnarViewModel.confirmHeaders(proposalId: nil, conversationId: nil), [:])
    }

    func testAStartedJobIsPolledAtItsOwnStatusAddress() throws {
        let p = try decode(AskProposal.self, """
            {"action": "refresh_competitors", "summary": "Refresh competitor data", "proposal_id": 9,
             "route": {"web": "/api/refresh-competitor-intel", "mobile": "/mobile/api/intel/refresh-competitors",
                       "method": "POST",
                       "status": {"web": "/api/competitor-intel-status/", "mobile": "/mobile/api/intel/refresh-status/"}}}
            """)
        XCTAssertEqual(AskCavnarViewModel.statusPath(for: p), "/mobile/api/intel/refresh-status/")
        // An older server: only the schedule tool falls back to its poll.
        let old = try decode(AskProposal.self, """
            {"action": "refresh_competitors", "summary": "x", "route": {"mobile": "/m", "method": "POST"}}
            """)
        XCTAssertNil(AskCavnarViewModel.statusPath(for: old))
        XCTAssertEqual(AskCavnarViewModel.jobOutcome(ok: true, status: "pending", error: nil), nil)
        XCTAssertEqual(AskCavnarViewModel.jobOutcome(ok: true, status: "done", error: nil), .done)
        XCTAssertEqual(AskCavnarViewModel.jobOutcome(ok: false, status: "error", error: "No roster"), .failed("No roster"))
    }

    func testTheConfirmAnswerCarriesSettledWarningAndTheGate() throws {
        let r = try decode(AskCavnarViewModel.JobOrOK.self, """
            {"ok": true, "proposal_settled": true, "warning": "Ana is still on Friday's published week."}
            """)
        XCTAssertEqual(r.proposalSettled, true)
        XCTAssertEqual(r.warning, "Ana is still on Friday's published week.")
        let gate = try decode(AskBlockers.Gate.self, """
            {"ok": false, "needs_ack": true, "error": "This week has things to look at first.",
             "blockers": [{"key": "ot", "text": "Dana goes over 40h"}, {"key": "mgr", "text": "No manager Sunday 9am"}]}
            """)
        XCTAssertTrue(gate.needsAck)
        XCTAssertEqual(gate.texts, ["Dana goes over 40h", "No manager Sunday 9am"])
        XCTAssertEqual(gate.acknowledgement, .array([.string("ot"), .string("mgr")]))
        let plain = try decode(AskBlockers.Gate.self, """
            {"needs_ack": true, "blockers": ["One", "Two"]}
            """)
        XCTAssertEqual(plain.acknowledgement, .bool(true))
        XCTAssertTrue(AskBlockers.cardLine(error: nil, blockers: plain.texts).hasSuffix("open the week to send it knowingly."))
    }

    // MARK: #11 — issues

    func testAnIssueDecodesItsDetailAndNamesItsNextCover() throws {
        let issue = try decode(HomeDayViewModel.Issue.self, """
            {"id": 5, "title": "Bo hasn't clocked in", "severity": "high", "status": "open", "kind": "coverage",
             "detail": "5:00pm server shift", "created_at": "2026-10-07 21:02:00", "assignee_name": "Dana",
             "meta": {"covers": [{"name": "Lu Chen"}, {"name": "Pat Ray"}], "asked": [{"name": "Lu Chen"}]}}
            """)
        XCTAssertEqual(issue.detail, "5:00pm server shift")
        XCTAssertEqual(issue.nextCoverToAsk, "Pat Ray")
        XCTAssertEqual(issue.statusLine, "Dana \u{00B7} open")
        XCTAssertEqual(HomeDayViewModel.issuesShown, 4)
    }

    func testTheIssuePushOpensTheIssueOnHome() throws {
        let router = DeepLinkRouter()
        router.open(try XCTUnwrap(NavPath("issue/42")))
        XCTAssertEqual(router.pendingTab, .home)
        XCTAssertEqual(router.pendingIssueId, 42)
    }

    func testTheIssueCategoryActsInTheBackground() throws {
        let resolve = try XCTUnwrap(PushManager.backgroundAction(for: PushManager.resolveIssueAction,
                                                                 cavnar: ["issue_id": 42]))
        XCTAssertEqual(resolve.path, "/mobile/api/issues/42/resolve")
        let cover = try XCTUnwrap(PushManager.backgroundAction(for: PushManager.askCoverAction,
                                                               cavnar: ["issue_id": "42"]))
        XCTAssertEqual(cover.path, "/mobile/api/issues/42/ask-cover")
        XCTAssertEqual(cover.asksCoverForIssue, 42)
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.resolveIssueAction, cavnar: [:]))
        // A shift to cover asks and resolves; any other issue only resolves
        // (CAVNAR_COVERAGE / CAVNAR_ISSUE, parity follow-up 10/7/26).
        XCTAssertEqual(PushManager.coverageActions.map(\.title), ["Ask someone to cover", "Resolved"])
        XCTAssertEqual(PushManager.issueActions.map(\.title), ["Resolved"])
        XCTAssertTrue((PushManager.coverageActions + PushManager.issueActions).allSatisfy {
            $0.options.contains(.authenticationRequired) && !$0.options.contains(.foreground) })
    }

    // MARK: #19 / #90 — dates the owner reads

    func testTheLossWeekAndTheHeroDateAreMDY() {
        XCTAssertEqual(HomeFollowThrough.lossWeekLabel(["2026-09-21", "2026-09-27"]), "9/21/26 \u{2013} 9/27/26")
        XCTAssertNil(HomeFollowThrough.lossWeekLabel(["2026-09-21"]))
        // The restaurant's own day, whatever the phone's clock says.
        XCTAssertEqual(HomeView.heroDate(localNow: "2026-10-05T23:30:00-05:00"), "MONDAY \u{00B7} 10/5/26")
        XCTAssertEqual(HomeViewModel.homeQuery(fresh: true), ["fresh": "1"])
        XCTAssertEqual(HomeViewModel.homeQuery(fresh: false), [:])
    }

    func testRecordAndStreakMilestonesHaveTheirOwnKickers() {
        XCTAssertEqual(MilestoneMoment.kicker("record"), "A RECORD")
        XCTAssertEqual(MilestoneMoment.kicker("streak"), "ON A RUN")
        XCTAssertNotEqual(MilestoneMoment.glyph("record"), "sparkles")
    }

    // MARK: #66 — missed goals

    func testMissedGoalsDecodeBesideTheProposedOnes() throws {
        let r = try decode(HomeFollowThroughViewModel.GoalsResponse.self, """
            {"ok": true, "goals": [], "proposed": [],
             "missed": [{"id": 3, "summary": "Labor at 28% by 9/30/26", "metric": "labor_pct"}, {"oops": 1}],
             "can_confirm": true}
            """)
        XCTAssertEqual(r.missed?.items.map(\.id), [3])
        XCTAssertEqual(r.canConfirm, true)
    }

    // MARK: #32 — all locations

    func testTheGroupHomeReadsEveryLocationsFiguresAndSaysDashForMissing() throws {
        let g = try decode(LocationGroupBrief.self, """
            {"ok": true, "group_name": "EJ's", "headline": "1 location needs a look", "tone": "warn",
             "locations": [
               {"id": 5, "name": "Evanston", "health": "important", "attention": 2, "active": true,
                "issues": [{"text": "Labor 31.2% — 3.2 pts over target", "severity": "important"}],
                "reviews": {"rating_30d": 4.3, "rating_prev": 4.7, "urgent": 2, "awaiting": 5},
                "labor": {"pct": 31.2, "over": 3.2, "overtime": 1},
                "inventory": {"recoverable": 1240.0, "critical_low": 2},
                "last_active": "2026-10-07 10:00:00",
                "last_night": {"net": 8420.4, "vs_budget": -120.2, "business_date": "2026-10-06"}},
               {"id": 6, "name": "Skokie", "health": "healthy", "attention": 0,
                "reviews": {"rating_30d": null, "urgent": 0, "awaiting": 0}, "labor": null, "inventory": null,
                "last_night": {"status": "final", "net": null}}],
             "attention": [{"text": "a", "severity": "critical", "location": "Evanston", "restaurant_id": 5}, 7]}
            """)
        XCTAssertEqual(g.locations.count, 2)
        XCTAssertEqual(g.attention.count, 1)
        let a = g.locations[0], b = g.locations[1]
        XCTAssertEqual(LocationGroupFormat.lastNight(a.lastNight).figure, "$8,420")
        XCTAssertEqual(LocationGroupFormat.lastNight(a.lastNight).detail, "\u{2212}$120 vs budget")
        XCTAssertEqual(LocationGroupFormat.labor(a.labor).figure, "31.2%")
        XCTAssertEqual(LocationGroupFormat.reviews(a.reviews).figure, "4.3\u{2605} \u{2193}")
        XCTAssertEqual(LocationGroupFormat.reviews(a.reviews).detail, "2 urgent \u{00B7} 5 waiting")
        XCTAssertEqual(LocationGroupFormat.foodCost(a.inventory).figure, "$1,240/mo")
        XCTAssertEqual(LocationGroupFormat.foodCost(a.inventory).detail, "opportunity \u{00B7} 2 low")
        // Missing is "—", never 0.
        XCTAssertEqual(LocationGroupFormat.labor(b.labor).figure, "\u{2014}")
        XCTAssertEqual(LocationGroupFormat.foodCost(b.inventory).figure, "\u{2014}")
        XCTAssertEqual(LocationGroupFormat.reviews(b.reviews).figure, "\u{2014}")
        XCTAssertEqual(LocationGroupFormat.lastNight(b.lastNight).figure, "\u{2014}")
        XCTAssertEqual(LocationGroupFormat.lastNight(b.lastNight).detail, "not measured")
        XCTAssertEqual(LocationGroupFormat.lastActive(nil), "\u{2014}")
        let now = try XCTUnwrap(CavnarDate.timestamp("2026-10-07 12:00:00"))
        XCTAssertEqual(LocationGroupFormat.lastActive(a.lastActive, now: now), "2h ago")
    }

    // MARK: #98 — Restaurant DNA

    func testTheDNAProfileDecodesItsSeriesAndSaysDashBelowTheFloor() throws {
        let r = try decode(RestaurantDNA.self, """
            {"ok": true, "profile": {"available": true, "as_of": "10/6/26", "measured": 1, "of": 2, "coverage_pct": 50,
              "families": [{"key": "labor", "label": "Labor and staffing", "dimensions": [
                {"key": "labor_pct", "label": "Labor cost", "display": "30.0%", "unit": "pct", "better": "lower",
                 "measured": true, "basis": "28 days", "dormant": false,
                 "trend": {"direction": "down", "previous_display": "33.0%"},
                 "history": [{"week": "2026-W39", "value": 33.0}, {"week": "2026-W40", "value": 30.0}]},
                {"key": "overtime_share", "label": "Overtime share", "display": null, "measured": false,
                 "needs": "14 days of shifts", "dormant": false, "history": []}]}]}}
            """)
        let dims = try XCTUnwrap(r.profile?.families.first?.dimensions)
        XCTAssertEqual(dims[0].figure, "30.0%")
        XCTAssertEqual(dims[0].history.map(\.value), [33.0, 30.0])
        XCTAssertEqual(dims[0].trendLine, "Down from 33.0% four weeks ago")
        XCTAssertEqual(dims[0].trendTone, .cavnarGreen)
        XCTAssertEqual(dims[1].figure, "\u{2014}")
        XCTAssertEqual(dims[1].needsLine, "Needs 14 days of shifts")
        XCTAssertEqual(r.profile?.coverageLine, "1 of 2 measured \u{00B7} 50%")
        XCTAssertEqual(RestaurantDNA.weekStart("2026-W40"), "9/28/26")
    }

    // MARK: The email's "Ask about this" link

    func testAnAskLinkFillsInTheQuestion() throws {
        let url = try XCTUnwrap(URL(string: "https://dashboard.cavnar.ai/?ask=Why%20was%20Friday%20slow%3F"))
        let destination = try XCTUnwrap(SystemEntry.destination(for: url))
        guard case .link(let path, _) = SystemEntry.fromLink(destination) else {
            return XCTFail("an ask link opens Ask, as a link")
        }
        XCTAssertEqual(path.head, "ask")
        XCTAssertEqual(path.query["q"], "Why was Friday slow?")
    }
}
