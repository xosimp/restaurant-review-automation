import XCTest
@testable import CavnarAI

/// The native layer round (iOS parity audit 10/7/26 #18, #31, #38, #58, #59,
/// #61, #94, #95, #96, #97): the request bodies the phone sends carry every
/// key the server reads, the Live Activity states decode from exactly what
/// the server pushes, and the widget's new figures never read a missing
/// measurement as 0.
final class NativeLayerTests: XCTestCase {

    private func json(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Bodies (every key the route reads)

    func testLiveActivityTokenBodyCarriesEveryKeyTheRouteReads() throws {
        let body = LiveActivityTokenBody(activityType: "service", kind: "start", token: "ab12",
                                         environment: "sandbox", activityKey: "")
        let sent = try json(body)
        XCTAssertEqual(Set(sent.keys), ["activity_type", "kind", "token", "environment", "activity_key"])
        XCTAssertEqual(sent["activity_type"] as? String, "service")
        XCTAssertEqual(sent["environment"] as? String, "sandbox")
    }

    func testTurningServiceOffSendsTheTypeAndKind() throws {
        let sent = try json(LiveActivityTokenRemoval(activityType: "service", kind: "start"))
        XCTAssertEqual(sent["activity_type"] as? String, "service")
        XCTAssertEqual(sent["kind"] as? String, "start")
        XCTAssertNil(try json(LiveActivityTokenRemoval(activityType: "service", kind: nil))["kind"])
    }

    func testSiriAskStartsANewConversationWithNoHistory() throws {
        let sent = try json(SiriAskBody(question: "How did labor look last week?"))
        XCTAssertEqual(Set(sent.keys), ["question", "history", "conversation_id", "new_conversation", "screen"])
        XCTAssertEqual(sent["question"] as? String, "How did labor look last week?")
        XCTAssertEqual((sent["history"] as? [Any])?.count, 0)
        XCTAssertEqual(sent["new_conversation"] as? Bool, true)
        XCTAssertTrue(sent["conversation_id"] is NSNull)
        XCTAssertTrue(sent["screen"] is NSNull)
    }

    // MARK: - Siri's answer

    func testSiriReadsPlainSentencesAndNeverActsOnAProposal() {
        let plain = SiriAsk.plain("## Labor\n- **Labor** ran 31% last week\n* Cut one server on [Tuesday](x)\n")
        XCTAssertEqual(plain, "Labor. Labor ran 31% last week. Cut one server on Tuesday.")
        let out = SiriAsk.outcome(answer: "Send the Sysco order now.", proposals: 1)
        XCTAssertTrue(out.ok)
        XCTAssertEqual(out.proposals, 1)
        XCTAssertTrue(out.spoken.contains("waiting in Ask"))
        XCTAssertTrue(out.spoken.contains("nothing was done"))
    }

    func testALongAnswerIsCutAtASentence() {
        let long = String(repeating: "Labor ran high on Friday. ", count: 60)
        let out = SiriAsk.outcome(answer: long, proposals: 0)
        XCTAssertLessThanOrEqual(out.spoken.count, SiriAsk.spokenLimit)
        XCTAssertTrue(out.spoken.hasSuffix("."))
        XCTAssertEqual(out.text, SiriAsk.plain(long))
    }

    // MARK: - Live Activity states, as the server pushes them

    func testServiceRouteDecodesStraightIntoTheActivity() throws {
        let raw = """
        {"ok": true, "in_service": true, "business_date": "2026-10-09", "closes_at_unix": 1791601200,
         "attributes": {"restaurantId": 5, "restaurantName": "Simple EJ's", "businessDate": "2026-10-09",
                        "dayLabel": "Fri 10/9/26"},
         "content_state": {"status": "open", "pulseLine": "\\u25B2 8% vs a typical Fri", "pulseUp": true,
                           "missing": ["Dana", "Luis", "Kim"], "missingCount": 3,
                           "closesAt": 813294000.0, "updatedAt": 813280000.0}}
        """
        let r = try JSONDecoder().decode(ServiceTonightResponse.self, from: Data(raw.utf8))
        XCTAssertTrue(r.ok && r.inService)
        XCTAssertEqual(r.attributes?.dayLabel, "Fri 10/9/26")
        let state = try XCTUnwrap(r.contentState)
        XCTAssertEqual(state.pulseLine, "\u{25B2} 8% vs a typical Fri")
        // Dates in content-state count from 2001 (push.apple_date).
        XCTAssertEqual(state.closesAt, Date(timeIntervalSinceReferenceDate: 813294000))
        XCTAssertEqual(state.coverageLine, "Dana and Luis +1 haven't clocked in")
    }

    func testAServiceStateWithNobodyMissingSaysSo() throws {
        let raw = #"{"status": "open", "pulseNote": "Nothing captured", "coverageNote": "Not checked yet"}"#
        let state = try JSONDecoder().decode(ServiceAttributes.ContentState.self, from: Data(raw.utf8))
        XCTAssertEqual(state.missing, [])
        XCTAssertNil(state.pulseLine)
        XCTAssertEqual(state.coverageLine, "Not checked yet")
        let one = ServiceAttributes.ContentState(status: "open", missing: ["Dana"], missingCount: 1)
        XCTAssertEqual(one.coverageLine, "Dana hasn't clocked in")
    }

    func testTheGenerationsPushedStateDecodes() throws {
        let raw = #"{"daysDrafted": 3, "daysTotal": 7, "status": "building", "estimatedEnd": 813294000.0}"#
        let state = try JSONDecoder().decode(ScheduleBuildAttributes.ContentState.self, from: Data(raw.utf8))
        XCTAssertEqual(state.progressLine, "3 of 7 days drafted")
        XCTAssertNil(ScheduleBuildAttributes.ContentState(daysTotal: 7).progressLine)
        let ahead = ScheduleBuildAttributes.ContentState(estimatedEnd: Date().addingTimeInterval(600))
        XCTAssertNotNil(ahead.timerRange())
        XCTAssertNil(ScheduleBuildAttributes.ContentState(status: "done", estimatedEnd: Date().addingTimeInterval(600))
            .timerRange())
        XCTAssertNil(ScheduleBuildAttributes.ContentState().timerRange(), "no measured typical, no timer")
    }

    func testTheGenerationsWeekLabel() {
        XCTAssertEqual(ScheduleBuildActivities.weekLabel(pickerLabel: "Next week", redoCount: 0), "Next week")
        XCTAssertEqual(ScheduleBuildActivities.weekLabel(pickerLabel: "10/12/26", redoCount: 0), "Week of 10/12/26")
        XCTAssertEqual(ScheduleBuildActivities.weekLabel(pickerLabel: "Next week", redoCount: 2), "Redoing 2 days")
    }

    func testThePendingSendStateTheServerPushesDecodes() throws {
        let raw = #"{"fireAt": 813294000.0, "status": "sent"}"#
        let state = try JSONDecoder().decode(PendingSendAttributes.ContentState.self, from: Data(raw.utf8))
        XCTAssertEqual(state.status, "sent")
        XCTAssertFalse(state.offersUndo(isStale: false))
    }

    // MARK: - The costs widget (#59)

    func testCostsComeFromTheReportsOwnKPIText() throws {
        let raw = """
        [{"key": "labor_pct", "label": "Labor", "value_text": "28.4%", "spark": [], "estimate": false,
          "target": {"label": "Target", "value_text": "30%"}},
         {"key": "food_pct", "label": "Food cost", "value_text": "31.2%", "spark": [], "estimate": true}]
        """
        let kpis = try JSONDecoder().decode([DSRKPI].self, from: Data(raw.utf8))
        var part = WidgetSnapshotService.NightPart()
        part.setCosts(kpis)
        XCTAssertEqual(part.laborLabel, "28.4%")
        XCTAssertEqual(part.laborPct, 28.4)
        XCTAssertEqual(part.laborTarget, 30)
        XCTAssertEqual(part.foodPct, 31.2)
        XCTAssertNil(part.foodTarget)
        let snap = try XCTUnwrap(WidgetSnapshotService.merge(previous: nil, restaurantId: 4, restaurantName: nil,
                                                             waiting: nil, night: part, now: Date()))
        XCTAssertEqual(snap.costsLine, "Labor 28.4% \u{00B7} Food 31.2%")
    }

    func testAMissingCostIsADashNeverZero() throws {
        var part = WidgetSnapshotService.NightPart()
        part.setCosts([])
        XCTAssertNil(part.laborPct)
        XCTAssertNil(WidgetSnapshotService.percent("—"))
        let snap = try XCTUnwrap(WidgetSnapshotService.merge(previous: nil, restaurantId: 4, restaurantName: nil,
                                                             waiting: nil, night: part, now: Date()))
        XCTAssertEqual(snap.costsLine, "Labor \u{2014} \u{00B7} Food \u{2014}")
    }

    func testAnOlderSnapshotStillDecodes() throws {
        let raw = #"{"waitingCount": 2, "pendingReplies": 1, "updatedAt": 0}"#
        let snap = try JSONDecoder().decode(WidgetSnapshot.self, from: Data(raw.utf8))
        XCTAssertNil(snap.laborLabel)
        XCTAssertEqual(snap.waitingCount, 2)
    }

    // MARK: - Background refresh (#31)

    func testOnlyTheServersSilentPushesRefresh() {
        XCTAssertTrue(BackgroundRefresh.isSilentRefresh(["aps": ["content-available": 1],
                                                         "cavnar": ["silent": "dsr", "restaurant_id": 4]]))
        XCTAssertFalse(BackgroundRefresh.isSilentRefresh(["aps": ["alert": ["title": "x"]],
                                                          "cavnar": ["alert_type": "1star"]]))
        XCTAssertFalse(BackgroundRefresh.isSilentRefresh(["aps": ["content-available": 1],
                                                          "cavnar": ["silent": "anything"]]))
    }

    // MARK: - The widget-read dedupe

    @MainActor
    func testHomesReadIsLentOnlyForItsOwnLocation() async {
        let share = HomeReadShare()
        share.beginActions()
        share.finishActions([(key: "no_response", count: 3), (key: "issue:4", count: nil)], restaurantId: 7)
        let lent = await share.recentActions(restaurantId: 7)
        XCTAssertEqual(lent?.count, 2)
        XCTAssertEqual(lent?.replies, 3)
        let other = await share.recentActions(restaurantId: 8)
        XCTAssertNil(other)
        share.finishNight(.some(nil), restaurantId: 7)
        let night = await share.recentNight(restaurantId: 7)
        XCTAssertNotNil(night)
        XCTAssertNil(night?.latest, "no report is a real answer")
        share.reset()
        let afterReset = await share.recentActions(restaurantId: 7)
        XCTAssertNil(afterReset)
    }

    // MARK: - Handoff (#97)

    func testHandoffOffersTheSameWebPage() {
        XCTAssertEqual(CavnarHandoff.webpageURL(for: "labor/schedule")?.absoluteString,
                       "https://dashboard.cavnar.ai/?nav=labor/schedule")
        XCTAssertNil(CavnarHandoff.webpageURL(for: ""))
    }

    // MARK: - The Share extension's session copy (#95)

    func testTheSharedSessionCopyNamesItsServer() throws {
        let sent = try json(Keychain.SharedSession(token: "t", baseURL: "https://dashboard.cavnar.ai"))
        XCTAssertEqual(Set(sent.keys), ["token", "base_url"])
    }
}
