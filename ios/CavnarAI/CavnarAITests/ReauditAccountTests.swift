import XCTest
import AuthenticationServices
import UIKit
@testable import CavnarAI

/// Blind re-audit 10/8/26 — Account, auth, iPad and UI: the app halves.
@MainActor
final class ReauditAccountTests: XCTestCase {

    private func json<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(text.utf8))
    }

    private func makeClient(handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) -> APIClient {
        MockURLProtocol.requestHandler = handler
        return APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    nonisolated private static func respond(_ request: URLRequest, _ status: Int, _ body: String) -> (HTTPURLResponse, Data) {
        (HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, Data(body.utf8))
    }

    // MARK: #2 The iPad sidebar's modules

    /// Home reloads its summary on a switch only while it is built: always
    /// under the tab bar, only while selected beside the sidebar. Otherwise
    /// RootView reloads it, so the sidebar never lists the old location's.
    func testHomeIsMountedOnlyWhereItReloadsItself() {
        XCTAssertTrue(RootView.homeIsMounted(usesSidebar: false, selectedTab: .account))
        XCTAssertTrue(RootView.homeIsMounted(usesSidebar: true, selectedTab: .home))
        XCTAssertFalse(RootView.homeIsMounted(usesSidebar: true, selectedTab: .modules))
        XCTAssertFalse(RootView.homeIsMounted(usesSidebar: true, selectedTab: .account))
    }

    // MARK: #3 Adding someone to text keeps unsaved contact edits

    func testAddingATextContactMergesOnlyWhoIsNew() {
        let draft = [AlertContact(id: 4, name: "Jim (edited, unsaved)", phone: "(312) 555-0100", smsConsent: false)]
        let server = [AlertContact(id: 4, name: "Jim", phone: "+13125550100", smsConsent: true),
                      AlertContact(id: 9, name: "Danny", phone: "+13125550199", smsConsent: true)]
        let merged = AccountAlertsDetailView.mergeAddedContacts(draft: draft, before: [4], server: server)
        XCTAssertEqual(merged.map(\.id), [4, 9])
        XCTAssertEqual(merged[0].name, "Jim (edited, unsaved)", "the owner's typing is never replaced")
        XCTAssertEqual(merged[0].phone, "(312) 555-0100")
        XCTAssertTrue(merged[0].smsConsent, "a consent the server just recorded is taken")
        XCTAssertEqual(merged[1].name, "Danny")
    }

    func testABlankNewRowGivesWayToTheAddedContact() {
        let draft = [AlertContact(id: 4, name: "Jim", phone: "+13125550100", smsConsent: true),
                     AlertContact(id: -2, name: "", phone: "", smsConsent: false)]
        let server = [AlertContact(id: 4, name: "Jim", phone: "+13125550100", smsConsent: true),
                      AlertContact(id: 9, name: "Danny", phone: "+13125550199", smsConsent: true)]
        let merged = AccountAlertsDetailView.mergeAddedContacts(draft: draft, before: [4], server: server)
        XCTAssertEqual(merged.map(\.id), [4, 9])
        // A contact removed on screen and not saved stays removed.
        let removed = AccountAlertsDetailView.mergeAddedContacts(draft: [], before: [4], server: server)
        XCTAssertEqual(removed.map(\.id), [9])
    }

    // MARK: #4 A Targets figure survives a failed save

    nonisolated private static let targetsJSON = """
    {"ok": true, "can_edit": true, "sees_pay": true, "targets": {"labor_target_pct": 30, "food_cost_target": 28,
     "monthly_revenue_target": 120000, "weekly_revenue_target": 27692.31, "week_start_day": 0}}
    """

    func testATargetsFigureIsKeptWhenItsSaveFails() async {
        let client = makeClient { request in
            if request.httpMethod == "POST" {
                return Self.respond(request, 200, #"{"ok": false, "error": "That's above 100%."}"#)
            }
            return Self.respond(request, 200, Self.targetsJSON)
        }
        let model = AccountTargetsModel(client: client)
        await model.load()
        XCTAssertTrue(model.canEdit)
        model.binding(TargetsPayload.Field.monthly).wrappedValue = "150000"
        await model.commit(TargetsPayload.Field.monthly)
        XCTAssertEqual(model.error, "That's above 100%.")
        XCTAssertEqual(model.drafts[TargetsPayload.Field.monthly], "150000", "the typed figure stays")
        XCTAssertNotNil(model.drafts[TargetsPayload.Field.weekly], "its pair stays with it")
    }

    func testATargetsDraftClearsOnceSaved() async {
        let client = makeClient { request in Self.respond(request, 200, Self.targetsJSON) }
        let model = AccountTargetsModel(client: client)
        await model.load()
        model.binding(TargetsPayload.Field.labor).wrappedValue = "27"
        await model.commit(TargetsPayload.Field.labor)
        XCTAssertNil(model.error)
        XCTAssertNil(model.drafts[TargetsPayload.Field.labor])
    }

    // MARK: #5 / #7 Delete my login

    func testDeleteMyLoginIsOfferedOnTheServersWord() throws {
        XCTAssertTrue(AccountView.offersDeleteLogin(canDeleteLogin: true, isOwner: true), "a co-owner")
        XCTAssertFalse(AccountView.offersDeleteLogin(canDeleteLogin: false, isOwner: false))
        XCTAssertTrue(AccountView.offersDeleteLogin(canDeleteLogin: nil, isOwner: false), "an older server")
        XCTAssertFalse(AccountView.offersDeleteLogin(canDeleteLogin: nil, isOwner: true))
        let info = try decode(AccountInfo.self, """
        {"username": "co", "email": "co@x.test", "two_fa_enabled": false, "login_notify": false,
         "marketing_emails_opt_out": false, "can_delete_login": true}
        """)
        XCTAssertEqual(info.canDeleteLogin, true)
    }

    func testDeleteMyLoginSaysItEndsEverywhere() {
        let lines = AccountDeleteLoginView.whatHappens(restaurantName: "Simple EJ's").joined(separator: " ")
        XCTAssertTrue(lines.contains("every location you sign in to"))
        XCTAssertTrue(lines.contains("staff PIN"))
        XCTAssertTrue(lines.contains("two-factor"))
    }

    // MARK: #8 Replace the earlier note

    func testTheMemoryBodySendsReplacesOnlyOnTheSecondPost() throws {
        var d = AccountMemoryViewModel.Draft()
        d.fact = "We close at 10 on Sundays"
        let first = AccountMemoryViewModel.addBody(d)
        XCTAssertNil(try json(first)["replaces"])
        let pending = AccountMemoryViewModel.PendingReplace(
            similar: .init(id: 41, fact: "We close at 9 on Sundays"), body: first)
        let second = try json(AccountMemoryViewModel.replaceBody(pending))
        XCTAssertEqual(second["replaces"] as? Int, 41)
        XCTAssertEqual(second["fact"] as? String, "We close at 10 on Sundays")
    }

    func testTheMemoryAnswerReadsSimilarReplacedAndConfirmed() throws {
        let r = try decode(AccountMemoryViewModel.AddResponse.self, """
        {"ok": true, "similar": [{"id": 41, "fact": "We close at 9 on Sundays", "kind": "context"}],
         "replaced": null, "confirmed": false, "evicted": 0}
        """)
        XCTAssertEqual(r.similar.first?.id, 41)
        XCTAssertEqual(AccountMemoryViewModel.addedMessage(r), "Remembered")
        let replaced = try decode(AccountMemoryViewModel.AddResponse.self,
                                  #"{"ok": true, "replaced": "We close at 9 on Sundays"}"#)
        XCTAssertTrue(AccountMemoryViewModel.addedMessage(replaced).contains("it replaced"))
        let confirmed = try decode(AccountMemoryViewModel.AddResponse.self, #"{"ok": true, "confirmed": true}"#)
        XCTAssertTrue(AccountMemoryViewModel.addedMessage(confirmed).hasPrefix("Already remembered"))
    }

    // MARK: #9 / #10 Passkeys

    func testTheAutoFillChallengeIsRefreshedBeforeItExpires() {
        // passkeys.PASSKEY_CHALLENGE_MINUTES is 5.
        XCTAssertLessThan(LoginViewModel.autoFillRefreshSeconds, 300)
        XCTAssertGreaterThan(LoginViewModel.autoFillRefreshSeconds, 60)
    }

    func testAPasskeyCallbackOnlyAnswersItsOwnRequest() {
        // A controller needs at least one request (it throws on an empty list).
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: "dashboard.cavnar.ai")
        let current = ASAuthorizationController(authorizationRequests: [provider.createCredentialAssertionRequest(challenge: Data([1]))])
        let stale = ASAuthorizationController(authorizationRequests: [provider.createCredentialAssertionRequest(challenge: Data([2]))])
        XCTAssertTrue(PasskeyCoordinator.isCurrent(current, current: current))
        XCTAssertFalse(PasskeyCoordinator.isCurrent(stale, current: current))
        XCTAssertFalse(PasskeyCoordinator.isCurrent(stale, current: nil))
    }

    // MARK: #11 The passkey's device name

    func testThePasskeyIsNamedForItsDevice() throws {
        XCTAssertEqual(AccountPasskeysModel.deviceName(idiom: .pad, onMac: false), "iPad")
        XCTAssertEqual(AccountPasskeysModel.deviceName(idiom: .phone, onMac: false), "iPhone")
        XCTAssertEqual(AccountPasskeysModel.deviceName(idiom: .pad, onMac: true), "Mac")
        let cred = PasskeyCredentialJSON.registration(credentialID: Data([1]), clientDataJSON: Data([2]),
                                                      attestationObject: Data([3]))
        let body = try json(AccountPasskeysModel.RegisterBody(credential: cred, device: "iPad"))
        XCTAssertEqual(Set(body.keys), ["credential", "device"])
    }

    // MARK: #12 Error states

    func testAPasskeyListThatFailedIsNeverNoneYet() async {
        let client = makeClient { request in Self.respond(request, 404, #"{"ok": false, "error": "Not found"}"#) }
        let model = AccountPasskeysModel(client: client)
        await model.load()
        XCTAssertTrue(model.rows.isEmpty)
        XCTAssertNotNil(model.loadError)
    }

    func testASecurityCheckupThatFailedSaysSo() async {
        let client = makeClient { request in Self.respond(request, 404, #"{"ok": false, "error": "Not found"}"#) }
        let vm = AccountViewModel(client: client)
        await vm.loadSecuritySummary()
        XCTAssertNil(vm.securitySummary)
        XCTAssertNotNil(vm.securitySummaryError)
    }

    // MARK: #13 Auto-approve rolls back to what is saved

    func testAutoApproveRollsBackToTheSavedRule() throws {
        let saved = try decode(AutoApproveSettings.self, """
        {"auto_approve_5star": true, "auto_approve_daily_cap": 8, "auto_approve_paused": false,
         "auto_approved_today": 2, "auto_approve_earned": true, "auto_approve_4star": true}
        """)
        XCTAssertEqual(AccountProfileDetailView.savedAutoApprove(saved),
                       AccountViewModel.AutoApproveBody(enabled: true, paused: false, dailyCap: 8, earned: true,
                                                        include4star: true))
        XCTAssertEqual(AccountProfileDetailView.savedAutoApprove(nil).dailyCap, 5)
        XCTAssertFalse(AccountProfileDetailView.savedAutoApprove(nil).enabled)
    }

    // MARK: #15 Neutral billing words

    func testBillingWordsSendNobodyToPay() {
        let note = AccountBillingDetailView.manageNote
        XCTAssertEqual(note, "Billing is handled under your service agreement.")
        XCTAssertFalse(note.lowercased().contains("payment method"))
        XCTAssertFalse(note.contains("dashboard.cavnar.ai"))
    }

    // MARK: #17 ⌘R refreshes only the topmost screen

    func testAViewOutsideAWindowIsNeverTheTopmostScreen() {
        XCTAssertFalse(CavnarPresentation.isTopmost(nil))
        XCTAssertFalse(CavnarPresentation.isTopmost(UIView()))
        let root = UIViewController()
        XCTAssertTrue(CavnarPresentation.topmost(from: root) === root, "nothing presented: the root is on top")
    }
}
