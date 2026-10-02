import XCTest
@testable import CavnarAI

/// staff_routes.api_roster's answer. At file scope, not on the @MainActor
/// test class: the mock transport reads it from its own thread.
private let staffRosterJSON = #"{"ok": true, "restaurant": "Alpha", "roster": [{"membership_id": 12, "name": "Maria Lopez"}], "login_nonce": "n1"}"#

/// Employee audit wave 2, I1: sign-in, session, push and account on the staff
/// tier. The transport rules (C2), the sign-in nonce (C3), the device's
/// memory and idle lock (H10, M4, M12), the account calls (C10, H10) and the
/// staff push links (C4), against scripted server answers shaped exactly as
/// staff_routes.py / staff_account_routes.py send them.
@MainActor
final class StaffSignInTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite = ""

    override func setUp() {
        super.setUp()
        suite = "staff-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    private func store(token: String? = nil,
                       _ handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) -> StaffSessionStore {
        let client = EdgeHTTP.client(handler)
        return StaffSessionStore(client: client, storedToken: .some(token), defaults: defaults)
    }

    // MARK: - C2: the staff transport reads an ended session

    func testSessionExpiredFlagIsAnEndedSessionWhateverTheStatus() {
        let body = Data(#"{"ok": false, "error": "Your shift session ended — sign in again.", "session_expired": true}"#.utf8)
        XCTAssertEqual(APIClient.classifyAuthRefusal(status: 401, body: body, authenticated: true, expiresOn401: true),
                       .sessionEnded(message: "Your shift session ended — sign in again."))
        XCTAssertEqual(APIClient.classifyAuthRefusal(status: 403, body: body, authenticated: true, expiresOn401: false),
                       .sessionEnded(message: "Your shift session ended — sign in again."))
    }

    func testAny401OnStaffApiIsAnEndedSessionButChangePinsIsARefusal() {
        let body = Data(#"{"ok": false, "locked": false, "error": "Your current PIN didn't match."}"#.utf8)
        XCTAssertEqual(APIClient.classifyAuthRefusal(status: 401, body: body, authenticated: true, expiresOn401: true),
                       .sessionEnded(message: "Your current PIN didn't match."))
        XCTAssertEqual(APIClient.classifyAuthRefusal(status: 401, body: body, authenticated: true, expiresOn401: false),
                       .refused(message: "Your current PIN didn't match."))
    }

    func testAnUnauthenticated401IsNeverAnEndedSession() {
        // A wrong PIN at sign-in is a refusal: there is no session to end.
        XCTAssertEqual(APIClient.classifyAuthRefusal(status: 401, body: Data(), authenticated: false, expiresOn401: false),
                       .refused(message: "That didn't match. Try again."))
        XCTAssertNil(APIClient.classifyAuthRefusal(status: 200, body: Data(), authenticated: true, expiresOn401: true))
        XCTAssertNil(APIClient.classifyAuthRefusal(status: 409, body: Data(), authenticated: false, expiresOn401: false))
    }

    func testStaffApiPathsAreRecognised() {
        XCTAssertTrue(APIClient.isStaffAPI("/staff/api/me"))
        XCTAssertFalse(APIClient.isStaffAPI("/staff/r/abc/login"))
        XCTAssertFalse(APIClient.isStaffAPI("/mobile/api/home"))
        XCTAssertTrue(APIClient.staff401EndsSession("/staff/api/shifts"))
        XCTAssertFalse(APIClient.staff401EndsSession("/staff/api/pin"),
                       "Change PIN's 401 is a wrong current PIN, whoever calls it")
        XCTAssertFalse(APIClient.staff401EndsSession("/mobile/api/home"))
    }

    func testAStaff401SignsTheStaffStoreOutWithTheServersSentence() async {
        let staff = store(token: "tok") { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "Your shift session ended — sign in again.", "session_expired": true}"#)
        }
        XCTAssertTrue(staff.isAuthenticated)
        let shifts: StaffShiftsResponse? = try? await staff.authed("/staff/api/shifts")
        XCTAssertNil(shifts)
        XCTAssertFalse(staff.isAuthenticated, "an ended session signs the staff store out (C2)")
        XCTAssertEqual(staff.signInNotice, "Your shift session ended — sign in again.")
    }

    func testAStaff401NeverRunsTheOwnerSessionHandler() async {
        let client = EdgeHTTP.client { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "Your shift session ended — sign in again.", "session_expired": true}"#)
        }
        let fired = Box(false)
        await client.setSessionExpiredHandler { fired.value = true }
        do {
            let _: APIClient.OKResponse = try await client.sendWithBearer("/staff/api/me", bearer: "tok")
            XCTFail("expected an ended session")
        } catch let error as APIClient.SessionExpiredError {
            XCTAssertEqual(error.errorDescription, "Your shift session ended — sign in again.")
        } catch {
            XCTFail("expected SessionExpiredError, got \(error)")
        }
        XCTAssertFalse(fired.value)
    }

    func testSignOutEndsTheSessionOnTheServerAndForgetsTheTokenAtOnce() async throws {
        let seen = Box<[String]>([])
        let auth = Box<[String]>([])
        let staff = store(token: "tok") { request in
            seen.value.append(EdgeHTTP.line(request))
            auth.value.append(request.value(forHTTPHeaderField: "Authorization") ?? "")
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "signed_out": true}"#)
        }
        staff.portalToken = "ABC234"
        staff.signOut()
        XCTAssertFalse(staff.isAuthenticated, "signed out on the phone at once")
        XCTAssertEqual(staff.portalToken, "ABC234", "the phone keeps its restaurant")
        for _ in 0..<50 where !seen.value.contains("POST /staff/api/logout") {
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTAssertTrue(seen.value.contains("POST /staff/api/logout"))
        XCTAssertTrue(auth.value.allSatisfy { $0 == "Bearer tok" }, "the staff bearer, never the owner's")
    }

    func testOnlyAnExplicitSignOutRunsTheSignOutHook() async {
        let staff = store(token: "tok") { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "Your shift session ended — sign in again.", "session_expired": true}"#)
        }
        let purged = Box(0)
        staff.onExplicitSignOut = { purged.value += 1 }
        let _: StaffShiftsResponse? = try? await staff.authed("/staff/api/shifts")
        XCTAssertFalse(staff.isAuthenticated)
        XCTAssertEqual(purged.value, 0, "an ended session keeps the cached screens")
        staff.notMe()
        XCTAssertEqual(purged.value, 1, "\"Not you?\" takes them")
    }

    // MARK: - C3: one wrong PIN never breaks the next try

    func testAWrongPinsFreshNonceIsUsedByTheNextTry() async {
        let nonces = Box<[String]>([])
        let staff = store { request in
            if request.url?.path.hasPrefix("/staff/api/roster/") == true {
                return EdgeHTTP.reply(request, 200, staffRosterJSON)
            }
            let sent = EdgeHTTP.bodyJSON(request)?["nonce"] as? String ?? ""
            nonces.value.append(sent)
            if nonces.value.count == 1 {
                return EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "That PIN didn't match.", "locked": false, "login_nonce": "n2"}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "token": "session-1"}"#)
        }
        _ = try? await staff.roster(portal: "ABC234")
        let first = await staff.signIn(portal: "ABC234", membershipID: 12, pin: "4827")
        XCTAssertFalse(first)
        XCTAssertEqual(staff.lastError, "That PIN didn't match.")
        let second = await staff.signIn(portal: "ABC234", membershipID: 12, pin: "4829",
                                        name: "Maria Lopez", restaurant: "Alpha")
        XCTAssertTrue(second)
        XCTAssertEqual(nonces.value, ["n1", "n2"], "the 401's own nonce is spent by the next try")
        XCTAssertEqual(staff.token, "session-1")
        XCTAssertEqual(staff.lastPerson(for: "ABC234")?.membershipID, 12, "the phone remembers who signed in")
    }

    func testAStaleNonceIsRefreshedAndTheSamePinSentOnce() async {
        let lines = Box<[String]>([])
        let logins = Box(0)
        let staff = store { request in
            lines.value.append(EdgeHTTP.line(request))
            if request.url?.path.hasPrefix("/staff/api/roster/") == true {
                return EdgeHTTP.reply(request, 200, staffRosterJSON)
            }
            logins.value += 1
            if logins.value == 1 {
                return EdgeHTTP.reply(request, 409, #"{"ok": false, "error": "That sign-in expired — tap your name again.", "nonce_expired": true}"#)
            }
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "token": "session-2"}"#)
        }
        let ok = await staff.signIn(portal: "ABC234", membershipID: 12, pin: "4827")
        XCTAssertTrue(ok)
        XCTAssertEqual(lines.value, ["GET /staff/api/roster/ABC234", "POST /staff/r/ABC234/login",
                                     "GET /staff/api/roster/ABC234", "POST /staff/r/ABC234/login"])
    }

    func testAnInvalidCodeShowsTheServersSentence() async {
        let staff = store { request in
            EdgeHTTP.reply(request, 404, #"{"ok": false, "error": "That staff link isn't valid any more."}"#)
        }
        do {
            _ = try await staff.roster(portal: "NOPE12")
            XCTFail("expected a refusal")
        } catch let error as APIClient.APIError {
            XCTAssertEqual(error.status, 404)
            XCTAssertEqual(error.message, "That staff link isn't valid any more.")
        } catch {
            XCTFail("expected APIError, got \(error)")
        }
    }

    // MARK: - M4: the signup token rides in a header

    func testTheSignupTokenTravelsInAHeaderNotTheUrl() async throws {
        let captured = Box<URLRequest?>(nil)
        let staff = store { request in
            if request.url?.path == "/staff/api/signup/verify" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "signup_token": "signup-secret"}"#)
            }
            captured.value = request
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "restaurant": "Alpha", "names": [], "none_left": true}"#)
        }
        _ = try await staff.verifySignupCode(phone: "5550142233", code: "123456")
        _ = try await staff.claimableNames(joinCode: "ABC234")
        let request = try XCTUnwrap(captured.value)
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Signup-Token"), "signup-secret")
        XCTAssertNil(request.url?.query, "no token in the URL (SEC-14)")
    }

    func testAPhoneThatAlreadyHasALoginIsSentToForgotPin() async throws {
        let staff = store { request in
            if request.url?.path == "/staff/api/signup/verify" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "signup_token": "t"}"#)
            }
            return EdgeHTTP.reply(request, 409, #"{"ok": false, "has_account": true, "employee_name": "Maria Lopez", "error": "This phone already has an account here as Maria Lopez."}"#)
        }
        _ = try await staff.verifySignupCode(phone: "5550142233", code: "123456")
        let outcome = await staff.claim(joinCode: "ABC234", employeeName: "Maria Lopez", pin: "4827")
        XCTAssertEqual(outcome, .hasAccount(name: "Maria Lopez", message: "This phone already has an account here as Maria Lopez."))
        XCTAssertFalse(staff.isAuthenticated)
    }

    func testAnExpiredSignupOffersStartAgain() async throws {
        let staff = store { request in
            if request.url?.path == "/staff/api/signup/verify" {
                return EdgeHTTP.reply(request, 200, #"{"ok": true, "signup_token": "t"}"#)
            }
            return EdgeHTTP.reply(request, 400, #"{"ok": false, "error": "That signup expired. Start again."}"#)
        }
        _ = try await staff.verifySignupCode(phone: "5550142233", code: "123456")
        let outcome = await staff.claim(joinCode: "ABC234", employeeName: "Maria Lopez", pin: "4827")
        XCTAssertEqual(outcome, .expired("That signup expired. Start again."))
        // The token is spent: the list now says so too.
        do {
            _ = try await staff.claimableNames(joinCode: "ABC234")
            XCTFail("expected the expired signup")
        } catch {
            XCTAssertTrue(error is StaffSignupExpiredError)
        }
    }

    // MARK: - H10 / UX-23: Change PIN and Forgot PIN

    func testAWrongCurrentPinKeepsTheSession() async {
        let staff = store(token: "tok") { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "locked": false, "error": "Your current PIN didn't match."}"#)
        }
        let outcome = await staff.changePin(current: "1111", new: "4827")
        XCTAssertEqual(outcome, .wrongCurrent("Your current PIN didn't match."))
        XCTAssertTrue(staff.isAuthenticated)
    }

    func testTheThirdWrongCurrentPinSignsOutWithTheServersSentence() async {
        let staff = store(token: "tok") { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "locked": true, "signed_out": true, "error": "Too many wrong PINs — you've been signed out. Sign in again to change it."}"#)
        }
        let outcome = await staff.changePin(current: "1111", new: "4827")
        XCTAssertEqual(outcome, .signedOut("Too many wrong PINs — you've been signed out. Sign in again to change it."))
        XCTAssertFalse(staff.isAuthenticated)
        XCTAssertEqual(staff.signInNotice, "Too many wrong PINs — you've been signed out. Sign in again to change it.")
    }

    func testAChangedPinLandsOnThisPersonsPadWithTheRightWords() async {
        let staff = store(token: "tok") { request in
            EdgeHTTP.reply(request, 200, #"{"ok": true, "signed_out": true}"#)
        }
        staff.remember(code: "ABC234", restaurant: "Alpha", membershipID: 12, employeeName: "Maria Lopez")
        staff.portalToken = "ABC234"
        let outcome = await staff.changePin(current: "1111", new: "4827")
        XCTAssertEqual(outcome, .changed)
        XCTAssertFalse(staff.isAuthenticated)
        XCTAssertEqual(staff.signInNotice, "PIN changed — sign in with your new PIN.")
        XCTAssertEqual(staff.lastPerson(for: "ABC234")?.name, "Maria Lopez", "the pad opens on this person")
    }

    func testForgotPinSignsInAtTheRestaurantItNames() async {
        let device = Box<String?>(nil)
        let staff = store { request in
            device.value = EdgeHTTP.bodyJSON(request)?["device_id"] as? String
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "signed_in": true, "employee_name": "Maria Lopez", "membership_id": 12, "restaurant": "Alpha", "portal_token": "long-portal-token", "token": "session-3"}"#)
        }
        let outcome = await staff.forgotSet(resetToken: "reset", pin: "4827", fallbackCode: "ABC234")
        XCTAssertEqual(outcome, .signedIn)
        XCTAssertEqual(staff.token, "session-3")
        XCTAssertEqual(staff.portalToken, "long-portal-token")
        XCTAssertEqual(staff.lastPerson(for: "long-portal-token")?.membershipID, 12)
        XCTAssertNotNil(device.value, "device_id is what returns the bearer")
    }

    func testAnExpiredResetGoesBackToTheStart() async {
        let staff = store { request in
            EdgeHTTP.reply(request, 400, #"{"ok": false, "reset_expired": true, "error": "That reset expired. Start again."}"#)
        }
        let outcome = await staff.forgotSet(resetToken: "reset", pin: "4827", fallbackCode: "ABC234")
        XCTAssertEqual(outcome, .expired("That reset expired. Start again."))
        XCTAssertFalse(staff.isAuthenticated)
    }

    // MARK: - C10: delete my account

    func testDeletingTheAccountSignsOutAndForgetsThePerson() async {
        let body = Box<[String: Any]?>(nil)
        let staff = store(token: "tok") { request in
            body.value = EdgeHTTP.bodyJSON(request)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "deleted": true, "signed_out": true}"#)
        }
        staff.remember(code: "ABC234", restaurant: "Alpha", membershipID: 12, employeeName: "Maria Lopez")
        staff.portalToken = "ABC234"
        let failure = await staff.deleteAccount()
        XCTAssertNil(failure)
        XCTAssertEqual(body.value?["confirm"] as? Bool, true, "only the JSON boolean true is accepted")
        XCTAssertFalse(staff.isAuthenticated)
        XCTAssertNil(staff.lastPerson(for: "ABC234"))
    }

    // MARK: - H10 / M12: the device's memory

    func testOneEntryPerRestaurantMostRecentFirst() {
        var list = StaffSessionStore.merged([], code: "ABC234", restaurant: "Alpha", restaurantID: nil,
                                            membershipID: 12, employeeName: "Maria Lopez")
        list = StaffSessionStore.merged(list, code: "XYZ789", restaurant: "Bravo", restaurantID: 2,
                                        membershipID: 31, employeeName: "Maria Lopez")
        // The same restaurant through its long token: one entry, now first.
        list = StaffSessionStore.merged(list, code: "long-alpha-token", restaurant: "alpha", restaurantID: 1,
                                        membershipID: nil, employeeName: nil)
        XCTAssertEqual(list.map(\.code), ["long-alpha-token", "XYZ789"])
        XCTAssertEqual(list.first?.membershipID, 12, "who signed in there is kept")
        XCTAssertEqual(list.first?.restaurantID, 1)
    }

    func testANotRecognisedCodeLeavesThePhone() {
        let staff = store { request in EdgeHTTP.reply(request, 200, "{}") }
        staff.remember(code: "OLD123", restaurant: "Alpha")
        staff.remember(code: "XYZ789", restaurant: "Bravo")
        staff.portalToken = "OLD123"
        staff.forgetCode("OLD123")
        XCTAssertEqual(staff.savedLocations.map(\.code), ["XYZ789"])
        XCTAssertEqual(staff.portalToken, "XYZ789")
    }

    func testFifteenMinutesAwayLocksTheStaffSession() {
        let staff = store(token: "tok") { request in EdgeHTTP.reply(request, 200, "{}") }
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        staff.noteBackgrounded(now: t0)
        staff.lockIfIdle(now: t0.addingTimeInterval(5 * 60))
        XCTAssertFalse(staff.isLocked, "five minutes is not idle")
        staff.noteBackgrounded(now: t0)
        staff.lockIfIdle(now: t0.addingTimeInterval(16 * 60))
        XCTAssertTrue(staff.isLocked)
        XCTAssertTrue(staff.isAuthenticated, "locked, not signed out: pushes keep coming")
        XCTAssertEqual(staff.signInNotice, StaffSessionStore.idleLockNotice)
    }

    func testAFreshInstallIsMarkedOnce() {
        StaffSessionStore.clearInheritedTokenOnFreshInstall(defaults)
        XCTAssertTrue(defaults.bool(forKey: "cavnar.staff.installed"))
    }

    // MARK: - Account payloads (fix_B1's /staff/api/me)

    func testMeDecodesLocationsAndSkipsAnOddOne() throws {
        let json = #"""
        {"ok": true, "employee": {
          "name": "Maria Lopez", "role": "employee", "restaurant": "Alpha", "has_pin": true,
          "phone_masked": "(•••) •••-2233", "has_phone": true, "email": "maria@example.com",
          "notifications": {"push": true, "texts": false, "texts_consent": false, "email": true},
          "restaurant_id": 1, "membership_id": 12,
          "locations": [
            {"restaurant_id": 1, "restaurant": "Alpha", "membership_id": 12, "employee_name": "Maria Lopez",
             "portal_token": "tok-a", "join_code": "ABC234", "current": true},
            {"restaurant": "Broken"},
            {"restaurant_id": 2, "restaurant": "Bravo", "membership_id": 31, "employee_name": "Maria Lopez",
             "portal_token": "tok-b", "join_code": "XYZ789", "current": false}]}}
        """#
        let resp = try JSONDecoder.cavnar.decode(StaffAccountResponse.self, from: Data(json.utf8))
        let me = try XCTUnwrap(resp.employee)
        XCTAssertEqual(me.membershipID, 12)
        XCTAssertEqual(me.email, "maria@example.com")
        XCTAssertEqual(me.notifications?.push, true)
        XCTAssertEqual(me.locations.map(\.restaurantID), [1, 2])
        XCTAssertTrue(me.hasOtherLocations)
    }

    func testAnOlderMeStillDecodes() throws {
        let json = #"{"ok": true, "employee": {"name": "Maria Lopez", "role": "employee", "restaurant": "Alpha", "has_pin": true}}"#
        let me = try XCTUnwrap(try JSONDecoder.cavnar.decode(StaffAccountResponse.self, from: Data(json.utf8)).employee)
        XCTAssertEqual(me.locations, [])
        XCTAssertEqual(me.email, "")
        XCTAssertFalse(me.hasOtherLocations)
    }

    func testTheSwitchAnswerDecodes() throws {
        let json = #"{"ok": true, "requires_pin": true, "current": false, "restaurant_id": 2, "restaurant": "Bravo", "membership_id": 31, "employee_name": "Maria Lopez", "portal_token": "tok-b", "join_code": "XYZ789", "login_nonce": "n9"}"#
        let resp = try JSONDecoder.cavnar.decode(StaffSwitchResponse.self, from: Data(json.utf8))
        XCTAssertEqual(resp.membershipID, 31)
        XCTAssertEqual(resp.portalToken, "tok-b")
        XCTAssertEqual(resp.loginNonce, "n9")
        XCTAssertEqual(resp.requiresPin, true)
    }

    // MARK: - UX-21: name search

    func testNameSearchMatchesEveryWordIgnoringCaseAndAccents() {
        let roster = [StaffRosterEntry(membershipID: 1, name: "María López"),
                      StaffRosterEntry(membershipID: 2, name: "Jordan Park")]
        XCTAssertEqual(StaffNameSearch.filter(roster, by: "lop mar", name: \.name).map(\.membershipID), [1])
        XCTAssertEqual(StaffNameSearch.filter(roster, by: "  ", name: \.name).count, 2)
        XCTAssertEqual(StaffNameSearch.filter(roster, by: "zed", name: \.name).count, 0)
    }

    // MARK: - C4: staff push links

    func testAStaffNavOpensItsTabAndItem() {
        let link = StaffDeepLink.from(cavnar: ["alert_type": "staff_request", "nav": "staff/requests/41",
                                               "tab": "requests", "request_id": 41, "request_kind": "swap"],
                                      alertType: "staff_request")
        XCTAssertEqual(link?.tab, .requests)
        XCTAssertEqual(link?.itemID, 41)
        XCTAssertEqual(link?.requestKind, "swap")
    }

    func testATabWithAnIdButNoNavStillOpensTheItem() {
        let link = StaffDeepLink.from(cavnar: ["tab": "messages", "thread_id": "5"], alertType: "staff_message")
        XCTAssertEqual(link?.tab, .inbox, "messages is the inbox")
        XCTAssertEqual(link?.itemID, 5)
    }

    func testATypeAloneOpensItsDefaultTab() {
        XCTAssertEqual(StaffDeepLink.from(cavnar: [:], alertType: "staff_schedule")?.tab, .today)
        XCTAssertEqual(StaffDeepLink.from(cavnar: [:], alertType: "staff_urgent")?.tab, .inbox)
        XCTAssertNil(StaffDeepLink.from(cavnar: [:], alertType: "1star"))
    }

    func testOwnerNoticesAboutStaffAreNotStaffNotices() {
        XCTAssertFalse(StaffDeepLink.isStaffNotice(alertType: "staff_signin", module: "account"))
        XCTAssertFalse(StaffDeepLink.isStaffNotice(alertType: "staff_claim", module: nil))
        XCTAssertTrue(StaffDeepLink.isStaffNotice(alertType: "staff_reminder", module: nil))
        XCTAssertTrue(StaffDeepLink.isStaffNotice(alertType: "", module: "staff"))
    }

    func testTheCenterHandsALinkOutOnce() {
        let center = StaffDeepLinkCenter()
        center.post(StaffDeepLink(tab: .tasks, itemID: 9, alertType: "staff_reminder",
                                  kind: "reminder", event: "task_due", requestKind: nil))
        XCTAssertEqual(center.consume()?.itemID, 9)
        XCTAssertNil(center.consume())
    }

    // MARK: - Owner app: staff notices and employee messages

    func testAStaffNoticeOnAConsolePhoneOpensLabor() {
        let router = DeepLinkRouter()
        router.open(NavPath("staff/requests/41")!)
        XCTAssertEqual(router.pendingTab, .modules)
        XCTAssertEqual(router.pendingModuleKey, "labor")
        // alert_type left empty on purpose: a non-empty one records the open
        // through APIClient.shared, a real request.
        let tapped = DeepLinkRouter()
        tapped.handleNotificationTap(alertType: "", reviewId: nil, module: "staff", nav: "staff/today")
        XCTAssertEqual(tapped.pendingModuleKey, "labor")
    }

    func testAnEmployeeMessageOpensTheLaborInbox() {
        XCTAssertEqual(DeepLinkRouter.webModule(for: "employee_message"), "labor")
        let router = DeepLinkRouter()
        router.open(NavPath("labor/inbox/5")!)
        XCTAssertEqual(router.pendingModuleKey, "labor")
        XCTAssertEqual(router.pendingModuleRoute?.section, "inbox")
    }
}
