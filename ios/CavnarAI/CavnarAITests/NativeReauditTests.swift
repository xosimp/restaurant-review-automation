import XCTest
@testable import CavnarAI

/// Blind re-audit of the push / native work (10/8/26), the app half: Live
/// Activity tokens are filed per location (#4) with the service activity's
/// own restaurant (#10); widget and Live Activity links name their location
/// and the app honours it (#6); Siri Ask respects the app lock (#8); the
/// Share sheet names the location (#12); a platform page's bell row opens a
/// sheet that says something (#14); Home's read is stamped where it began
/// (Home #5).
final class NativeReauditTests: XCTestCase {

    private func json(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - #4 / #10: the token body and its scope

    func testAServiceUpdateTokenNamesItsActivitysRestaurant() throws {
        let body = LiveActivityTokenBody(activityType: "service", kind: "update", token: "ab12",
                                         environment: "production", activityKey: "2026-10-09", restaurantId: 7)
        let sent = try json(body)
        XCTAssertEqual(Set(sent.keys), ["activity_type", "kind", "token", "environment", "activity_key",
                                        "restaurant_id"])
        XCTAssertEqual(sent["restaurant_id"] as? Int, 7)
        // A start token says nothing about a restaurant: the session's.
        let start = try json(LiveActivityTokenBody(activityType: "service", kind: "start", token: "ab12",
                                                   environment: "production", activityKey: ""))
        XCTAssertNil(start["restaurant_id"])
    }

    func testWhatHasBeenFiledIsKeptPerSignInAndLocation() {
        XCTAssertNotEqual(LiveActivitySync.scope(bearer: "t", restaurantId: 1),
                          LiveActivitySync.scope(bearer: "t", restaurantId: 2))
        XCTAssertEqual(LiveActivitySync.scope(bearer: "t", restaurantId: 1),
                       LiveActivitySync.scope(bearer: "t", restaurantId: 1))
    }

    func testARefusalIsAnAnswerNotARetry() {
        XCTAssertTrue(LiveActivitySync.isFinalAnswer(403))
        XCTAssertTrue(LiveActivitySync.isFinalAnswer(404))
        XCTAssertFalse(LiveActivitySync.isFinalAnswer(500))
        XCTAssertFalse(LiveActivitySync.isFinalAnswer(0))
    }

    // MARK: - #6: links carry their location

    func testALinkNamesItsLocation() {
        XCTAssertEqual(CavnarLink.located("cavnarai://nav/dsr", 4), "cavnarai://nav/dsr?loc=4")
        XCTAssertEqual(CavnarLink.located("cavnarai://nav/x?a=1", 4), "cavnarai://nav/x?a=1&loc=4")
        XCTAssertEqual(CavnarLink.located("cavnarai://nav/dsr", nil), "cavnarai://nav/dsr")
        XCTAssertEqual(CavnarLink.located("cavnarai://nav/dsr", 0), "cavnarai://nav/dsr")
    }

    func testTheWidgetsLinksNameTheSnapshotsLocation() {
        var snap = WidgetSnapshot.empty
        snap.restaurantId = 5
        snap.nightDate = "2026-09-24"
        snap.updatedAt = Date()
        snap.waitingUpdatedAt = Date()
        XCTAssertEqual(snap.link(), "cavnarai://nav/dsr/night/2026-09-24?loc=5")
        XCTAssertEqual(snap.nightLink, "cavnarai://nav/dsr/night/2026-09-24?loc=5")
        snap.waitingCount = 3
        XCTAssertEqual(snap.link(), "cavnarai://command?loc=5")
    }

    func testTheLiveActivitiesLinkToTheirOwnLocation() throws {
        let send = PendingSendAttributes(actionId: 4, kind: "order_send", title: "Order", restaurantId: 9)
        XCTAssertEqual(send.link?.absoluteString, "cavnarai://nav/action/4?loc=9")
        let build = ScheduleBuildAttributes(jobId: "j1", weekLabel: "Next week", restaurantId: 9)
        XCTAssertEqual(build.link?.absoluteString, "cavnarai://nav/labor/schedule?loc=9")
        let service = ServiceAttributes(restaurantId: 3, restaurantName: "North", businessDate: "2026-10-09",
                                        dayLabel: "Fri 10/9/26")
        XCTAssertEqual(service.link?.absoluteString, "cavnarai://nav/labor?loc=3")
        // An activity pushed by an older server (no restaurantId) still decodes.
        let old = try JSONDecoder().decode(PendingSendAttributes.self,
                                           from: Data(#"{"actionId":4,"kind":"order_send","title":"Order"}"#.utf8))
        XCTAssertNil(old.restaurantId)
        XCTAssertEqual(old.link?.absoluteString, "cavnarai://nav/action/4")
    }

    func testTheAppReadsAWidgetLinksLocationAndKeepsItOutOfThePlace() throws {
        let url = try XCTUnwrap(URL(string: "cavnarai://nav/dsr/night/2026-09-24?loc=5"))
        XCTAssertEqual(SystemEntry.linkContext(for: url).location, 5)
        guard case .nav(let path)? = SystemEntry.destination(for: url) else { return XCTFail("no place") }
        XCTAssertEqual(path.raw, "dsr/night/2026-09-24")
        let kept = try XCTUnwrap(URL(string: "cavnarai://nav/reviews?filter=pending&loc=5"))
        guard case .nav(let filtered)? = SystemEntry.destination(for: kept) else { return XCTFail("no place") }
        XCTAssertEqual(filtered.raw, "reviews?filter=pending")
        // Only the location: a cavnarai link carries no recommendation.
        let rec = try XCTUnwrap(URL(string: "cavnarai://nav/home?rec=x&loc=2"))
        XCTAssertNil(SystemEntry.linkContext(for: rec).rec)
    }

    @MainActor
    func testWaitingAtAnotherLocationOpensThatLocationsHome() throws {
        let ctx = LinkContext(location: 5)
        guard case .link(let path, let context) = SystemEntry.located(.commandSheet, context: ctx,
                                                                      activeRestaurantId: 2) else {
            return XCTFail("expected the other location's Home")
        }
        XCTAssertEqual(path.raw, "home")
        XCTAssertEqual(context.location, 5)
        XCTAssertEqual(SystemEntry.located(.commandSheet, context: ctx, activeRestaurantId: 5), .commandSheet)
        XCTAssertEqual(SystemEntry.located(.commandSheet, context: LinkContext(), activeRestaurantId: 5),
                       .commandSheet)
    }

    // MARK: - #8: Siri Ask and the app lock

    func testTheAppLockCountsFaceIDOrAPasscode() {
        let key = "cavnar.biometric_lock_enabled"
        let saved = UserDefaults.standard.object(forKey: key)
        defer {
            if let saved { UserDefaults.standard.set(saved, forKey: key) } else { UserDefaults.standard.removeObject(forKey: key) }
        }
        UserDefaults.standard.set(true, forKey: key)
        XCTAssertTrue(SessionStore.appLockConfigured)
        UserDefaults.standard.removeObject(forKey: key)
        XCTAssertTrue(SessionStore.appLockConfigured, "Face ID is on until it is switched off")
        UserDefaults.standard.set(false, forKey: key)
        XCTAssertEqual(SessionStore.appLockConfigured, AppPasscode.isSet)
        XCTAssertFalse(SiriAsk.locked.ok, "a locked app answers nothing")
        XCTAssertEqual(SiriAsk.locked.proposals, 0)
    }

    // MARK: - #12: the Share sheet's location

    func testTheSharedSessionCarriesTheLocationWhenThereIsOne() throws {
        var session = Keychain.SharedSession(token: "t", baseURL: "https://dashboard.cavnar.ai")
        XCTAssertEqual(Set(try json(session).keys), ["token", "base_url"])
        session.restaurantId = 4
        session.locationName = "North Ave"
        let sent = try json(session)
        XCTAssertEqual(sent["restaurant_id"] as? Int, 4)
        XCTAssertEqual(sent["location_name"] as? String, "North Ave")
    }

    // MARK: - #14: a platform page's bell row

    func testAPlatformRowOpensASheetThatSaysWhatItWas() throws {
        let row = try JSONDecoder().decode(NotificationItem.self, from: Data(#"""
        {"type":"platform_alert","label":"Platform alert","fired_at":"2026-10-08 14:05:00",
         "review_id":null,"priority":1,"urgent":true,"module":"home","nav":"admin/platform","id":12,
         "snippet":"Scheduler stopped"}
        """#.utf8))
        let alert = try XCTUnwrap(PlatformAlert(row: row))
        XCTAssertEqual(alert.subject, "Scheduler stopped")
        XCTAssertNotNil(alert.alertAt)
        let other = try JSONDecoder().decode(NotificationItem.self, from: Data(#"""
        {"type":"1star","label":"1 star","fired_at":"2026-10-08 14:05:00","review_id":3,"module":"reviews"}
        """#.utf8))
        XCTAssertNil(PlatformAlert(row: other))
    }

    // MARK: - Home #5: a read is stamped where it began

    @MainActor
    func testHomesReadKeepsTheLocationItBeganAt() async {
        let share = HomeReadShare()
        let at = share.beginActions(restaurantId: 7)
        // The session moved on before the read landed: still 7's.
        share.finishActions([(key: "no_response", count: 2)], restaurantId: at)
        let lent = await share.recentActions(restaurantId: 7)
        XCTAssertEqual(lent?.count, 1)
        let elsewhere = await share.recentActions(restaurantId: 8)
        XCTAssertNil(elsewhere)
        let night = share.beginNight(restaurantId: 7)
        share.finishNight(.some(nil), restaurantId: night)
        let n = await share.recentNight(restaurantId: 8)
        XCTAssertNil(n)
    }
}
