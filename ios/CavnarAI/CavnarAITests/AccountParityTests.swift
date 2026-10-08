import XCTest
@testable import CavnarAI

/// iOS parity audit 10/7/26 — Account, settings and auth. The request
/// bodies carry every key the server reads for their routes (the root cause
/// of the audit's Critical bugs), and the new payloads decode leniently.
@MainActor
final class AccountParityTests: XCTestCase {

    private func json<T: Encodable>(_ value: T) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, _ text: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(text.utf8))
    }

    // MARK: #2 Auto-approve

    /// client_api._do_auto_approve reads exactly these five keys; a body
    /// without include_4star switched the 4-star rule off on every save.
    func testAutoApproveBodyCarriesEveryKeyTheSharedBodyReads() throws {
        let body = AccountViewModel.AutoApproveBody(enabled: true, paused: false, dailyCap: 5, earned: true, include4star: true)
        let keys = Set(try json(body).keys)
        XCTAssertEqual(keys, ["enabled", "paused", "daily_cap", "earned", "include_4star"])
        XCTAssertEqual(try json(body)["include_4star"] as? Bool, true)
    }

    func testAutoApproveSettingsReadsTheFourStarRule() throws {
        let s = try decode(AutoApproveSettings.self, """
        {"auto_approve_5star": true, "auto_approve_4star": true, "auto_approve_daily_cap": 5,
         "auto_approve_paused": false, "auto_approved_today": 2, "auto_approve_earned": false}
        """)
        XCTAssertEqual(s.include4star, true)
        let old = try decode(AutoApproveSettings.self, """
        {"auto_approve_5star": true, "auto_approve_daily_cap": 5, "auto_approve_paused": false, "auto_approved_today": 0}
        """)
        XCTAssertNil(old.include4star)
    }

    // MARK: #7 / #88 Hours and closed dates

    func testSaveHoursSendsNoClosedDatesAndEachDateSavesOnItsOwn() throws {
        let hours = try json(AccountViewModel.HoursBody(open: ["Monday": "11:00am"], close: ["Monday": "9:00pm"]))
        XCTAssertEqual(Set(hours.keys), ["open", "close"])
        XCTAssertEqual(try json(AccountViewModel.ClosureChange(add: "2026-12-25")) as NSDictionary,
                       ["add": "2026-12-25"] as NSDictionary)
        XCTAssertEqual(try json(AccountViewModel.ClosureChange(remove: "2026-12-25")) as NSDictionary,
                       ["remove": "2026-12-25"] as NSDictionary)
    }

    func testFillFromGoogleSetsListedDaysAndClosesTheRest() {
        let days = ["Monday", "Tuesday", "Wednesday"]
        let start = HoursDayDraft.drafts(days: days, opens: [:], closes: [:])
        let filled = HoursDayDraft.filled(days: days, current: start,
                                          opens: ["Monday": "11:00am", "Tuesday": "4pm"],
                                          closes: ["Monday": "10:00pm", "Tuesday": "23:00"])
        let (open, close) = HoursDayDraft.payload(days: days, drafts: filled)
        XCTAssertEqual(open["Monday"], "11:00am")
        XCTAssertEqual(close["Tuesday"], "11:00pm")
        XCTAssertNil(open["Wednesday"], "a day Google lists no hours for is closed")
        XCTAssertTrue(filled["Wednesday"]?.closed == true)
    }

    func testCopyMondayPutsMondaysTimesOnEveryDay() {
        let days = ["Monday", "Tuesday", "Sunday"]
        let start = HoursDayDraft.drafts(days: days, opens: ["Monday": "7:30am"], closes: ["Monday": "3:00pm"])
        let copied = HoursDayDraft.copying(start["Monday"]!, to: days, current: start)
        let (open, close) = HoursDayDraft.payload(days: days, drafts: copied)
        XCTAssertEqual(open, ["Monday": "7:30am", "Tuesday": "7:30am", "Sunday": "7:30am"])
        XCTAssertEqual(close["Sunday"], "3:00pm")
    }

    // MARK: #86 Alert switches

    func testTheWebsAlertSwitchesDecodeAndAreSentOnlyWhenTheServerSentThem() throws {
        let base = """
        "alert_1star": true, "alert_2star": false, "alert_health": false, "alert_neg_spike": false,
        "alert_negative_trend": false, "alert_no_response": false, "alert_5star": false, "alert_labor_over": false,
        "urgent_via_sms": false, "urgent_via_email": true, "digest_enabled": true, "digest_day": "monday",
        "alert_quiet_start": null, "alert_quiet_end": null, "al_1star_push": true, "al_2star_push": true,
        "al_5star_push": true, "al_health_push": true, "al_spike_push": true, "al_unres_push": true,
        "alert_health_bypass_quiet": false, "alert_food_waste": true, "alert_ai_visibility_drop": false,
        "alert_competitor_move": true, "alert_extra_emails": "", "push_sound": true
        """
        let s = try decode(AlertSettings.self, "{\(base), \"alert_rating_threshold\": true, \"alert_rating_floor\": 4.6, \"alert_any_review\": false, \"alert_resp_approved\": true}")
        XCTAssertEqual(s.alertRatingFloor, 4.6)
        let sent = try json(AccountViewModel.alertSettingsBody(s, contacts: []))
        XCTAssertEqual(sent["alert_rating_threshold"] as? Bool, true)
        XCTAssertEqual(sent["alert_rating_floor"] as? Double, 4.6)
        XCTAssertEqual(sent["alert_resp_approved"] as? Bool, true)
        XCTAssertEqual(sent["alert_food_waste"] as? Bool, true)

        let older = try decode(AlertSettings.self, "{\(base)}")
        let sentOlder = try json(AccountViewModel.alertSettingsBody(older, contacts: []))
        for key in ["alert_rating_threshold", "alert_rating_floor", "alert_any_review", "alert_resp_approved"] {
            XCTAssertNil(sentOlder[key], "\(key) is left out when the server never sent it")
        }
    }

    // MARK: #70 Team, staff

    func testTeamAccessSendsOnlyTheKeyBeingChanged() throws {
        XCTAssertEqual(Set(try json(AccountViewModel.AccessBody(nightlyReport: false)).keys), ["nightly_report"])
        XCTAssertEqual(Set(try json(AccountViewModel.AccessBody(morningBrief: true)).keys), ["morning_brief"])
        let m = try decode(TeamMember.self, """
        {"id": 3, "username": "sam", "name": "Sam", "email": "s@x.test", "role": "manager",
         "created_at": "2026-09-01", "last_login": null, "is_you": false, "nightly_report": false}
        """)
        XCTAssertEqual(m.nightlyReport, false)
    }

    func testPinEventsDecode() throws {
        let e = try decode(AccountViewModel.PinEvent.self, #"{"event": "pin_locked", "name": "Ana", "created_at": "2026-10-07 23:10:00"}"#)
        XCTAssertTrue(e.isLockout)
        XCTAssertEqual(e.name, "Ana")
    }

    // MARK: #80 Connections

    func testConnectedAppsCountsRPowerAndWebsiteAnalytics() throws {
        let c = try decode(AccountConnections.self, """
        {"google_business": {"connected": true}, "instagram": {"connected": false},
         "toast": {"connected": false}, "square": {"connected": false}, "clover": {"connected": false},
         "rpower": {"connected": true}, "web_analytics": {"connected": true, "last_synced": null}}
        """)
        XCTAssertEqual(c.connectedCount, 3)
        XCTAssertEqual(c.all.count, 7)
        let older = try decode(AccountConnections.self, """
        {"google_business": {"connected": true}, "instagram": {"connected": false},
         "toast": {"connected": false}, "square": {"connected": false}, "clover": {"connected": false}}
        """)
        XCTAssertEqual(older.all.count, 5)
    }

    // MARK: #89 Health and the checkup

    func testAccountHealthDecodesTheServersScoreSentenceFixAndMeasuredLine() throws {
        let h = try decode(AccountHealth.self, """
        {"ok": true, "score": 72, "tone": "warn", "sub": "Nearly there",
         "say": {"lead": "Nearly set up.", "text": "Worth doing: connect your POS."},
         "fix": {"key": "integrations", "label": "Fix your data connection"},
         "items": [{"key": "profile", "label": "Restaurant profile", "state": "ok", "sub": "Complete", "say": "x"},
                   {"key": "people", "label": "People", "state": "bad", "sub": "Just you"}],
         "features": [{"key": "reviews", "label": "Reviews", "detail": "d", "on": true},
                      {"key": "intel", "label": "Intel", "detail": "d", "on": false}],
         "measured": {"line": "Nothing measured yet", "net_monthly": null, "measured": false},
         "connections": {"connected": 2, "total": 7}}
        """)
        XCTAssertEqual(h.score, 72)
        XCTAssertEqual(h.fix?.key, "integrations")
        XCTAssertEqual(h.items.map(\.key), ["profile", "people"])
        XCTAssertEqual(h.features.filter(\.on).map(\.key), ["reviews"])
        XCTAssertNil(h.measured?.netMonthly, "nothing measured is not $0")
        XCTAssertEqual(h.sentence, "Nearly set up. Worth doing: connect your POS.")
    }

    func testSecurityCheckupIsTheServersSixItemsWithTheirFixes() throws {
        let s = try decode(SecuritySummary.self, """
        {"ok": true, "two_fa_enabled": false, "checkup": {"score": 25, "max": 100, "items": [
          {"key": "two_fa", "title": "Two-factor authentication", "points": 30, "earned": false, "detail": "Off", "fix_label": "Turn on"},
          {"key": "login_notify", "title": "Sign-in notifications", "points": 10, "earned": true, "detail": "On", "fix_label": null},
          {"key": "recovery_email", "title": "Recovery email", "points": 15, "earned": true, "detail": "a@b.c"},
          {"key": "password", "title": "Password strength", "points": 15, "earned": false, "detail": "Rated good"},
          {"key": "backup_codes", "title": "Backup codes on hand", "points": 15, "earned": false, "detail": "Needs two-factor first"},
          {"key": "devices", "title": "No stale devices", "points": 15, "earned": false, "detail": "5 trusted, 1 signed in"}]}}
        """)
        XCTAssertEqual(s.checkup.score, 25)
        XCTAssertEqual(s.checkup.items.count, 6)
        for item in s.checkup.items {
            XCTAssertNotNil(AccountSecurityCheckupView.fix(for: item.key), item.key)
        }
    }

    // MARK: #27 Targets

    func testTargetsBodySavesOneFieldAndOneRoleAtATime() throws {
        let p = try decode(TargetsPayload.self, """
        {"labor_target_pct": 30, "food_cost_target": 28, "waste_target_pct": 4, "monthly_revenue_target": 120000,
         "weekly_revenue_target": 27692.31, "hourly_rate": 15, "week_start_day": 0,
         "role_rates": {"Server": 9, "Cook": 18}, "roles": ["Cook", "Server"],
         "people_by_role": {"Server": [{"name": "Ana", "pos_rate": null, "rate": 10}]},
         "role_pay": {"Cook": {"low": 17, "high": 21, "people": 3, "unrated": 0, "fallback": 18}},
         "salaried": [{"name": "Erik", "annual": 65000, "per_day": 208.33}], "salaried_names": ["Erik"],
         "set_notes": {"labor_target_pct": "Set by the owner on 9/12/26"},
         "labor": {"goal": {"label": "Your goal of 26% by 12/31/26"}}}
        """)
        XCTAssertNil(TargetsBody.forField("labor_target_pct", typed: "30", current: p), "unchanged sends nothing")
        XCTAssertEqual(try json(TargetsBody.forField("labor_target_pct", typed: "28", current: p)!) as NSDictionary,
                       ["labor_target_pct": 28] as NSDictionary)
        let role = try json(TargetsBody.forField("role:Server", typed: "11", current: p)!)
        XCTAssertEqual(role["role_rates"] as? [String: Double], ["Server": 11, "Cook": 18], "every other role keeps its rate")
        let waste = try json(TargetsBody.forField("waste_target_pct", typed: "", current: p)!)
        XCTAssertTrue(waste["waste_target_pct"] is NSNull)
        let person = try json(TargetsBody.forField("person:Ana", typed: "", current: p)!)
        let pr = try XCTUnwrap(person["person_rate"] as? [String: Any])
        XCTAssertEqual(pr["name"] as? String, "Ana")
        XCTAssertTrue(pr["rate"] is NSNull, "a blank person rate goes back to the role's")
        XCTAssertEqual(Set(try json(TargetsBody.forField("weekly_revenue_target", typed: "30000", current: p)!).keys),
                       ["weekly_revenue_target"], "only the box typed in is saved")
        XCTAssertEqual(p.noteFor("labor_target_pct"), "Your goal of 26% by 12/31/26 applies \u{00B7} Set by the owner on 9/12/26")
        XCTAssertEqual(p.perDay["Erik"], 208.33)
        XCTAssertEqual(p.rolePay["Cook"]?.rangeText, "$17\u{2013}$21")
        let add = try json(TargetsBody(salariedAdd: .init(name: "Gabe", annual: 52000)))
        XCTAssertEqual(Set(add.keys), ["salaried_add"])
    }

    // MARK: #44 Add someone to text

    func testAddSomeoneToTextSendsEveryKeyTheRouteReads() throws {
        let body = IssueTextContactSheet.ContactBody(role: "manager", name: "Ana", phone: "(312) 555-0100", consent: true)
        XCTAssertEqual(Set(try json(body).keys), ["role", "name", "phone", "consent"])
    }

    // MARK: #57 Passkeys

    func testPasskeyCredentialsAreTheShapesPyWebauthnReads() throws {
        let id = Data([1, 2, 250, 251, 252])
        let reg = try json(PasskeyCredentialJSON.registration(credentialID: id, clientDataJSON: Data("c".utf8),
                                                              attestationObject: Data("a".utf8)))
        XCTAssertEqual(reg["id"] as? String, Base64URL.encode(id))
        XCTAssertEqual(reg["rawId"] as? String, reg["id"] as? String)
        XCTAssertEqual(reg["type"] as? String, "public-key")
        let r = try XCTUnwrap(reg["response"] as? [String: Any])
        XCTAssertEqual(Set(r.keys), ["clientDataJSON", "attestationObject", "transports"])
        let asrt = try json(PasskeyCredentialJSON.assertion(credentialID: id, clientDataJSON: Data("c".utf8),
                                                            authenticatorData: Data("d".utf8), signature: Data("s".utf8),
                                                            userID: Data("u".utf8)))
        let ar = try XCTUnwrap(asrt["response"] as? [String: Any])
        XCTAssertEqual(Set(ar.keys), ["clientDataJSON", "authenticatorData", "signature", "userHandle"])
        XCTAssertFalse(Base64URL.encode(id).contains("="))
        XCTAssertEqual(Base64URL.decode(Base64URL.encode(id)), id)
        let verify = try json(SessionStore.PasskeyVerifyBody(credential: PasskeyCredentialJSON.assertion(
            credentialID: id, clientDataJSON: Data(), authenticatorData: Data(), signature: Data(), userID: nil)))
        XCTAssertEqual(Set(verify.keys), ["credential", "device_id"])
    }

    func testPasskeyOptionsDecodeFromTheServersJSON() throws {
        let o = try decode(PasskeyAssertionOptions.self, #"{"challenge": "AAEC", "timeout": 60000, "rpId": "dashboard.cavnar.ai", "allowCredentials": [], "userVerification": "required"}"#)
        XCTAssertEqual(o.rpId, "dashboard.cavnar.ai")
        let r = try decode(PasskeyRegistrationOptions.self, """
        {"rp": {"name": "Cavnar AI", "id": "dashboard.cavnar.ai"}, "user": {"id": "Y2F2bmFy", "name": "erik", "displayName": "erik"},
         "challenge": "AAEC", "pubKeyCredParams": [], "excludeCredentials": [{"id": "AQI", "type": "public-key"}],
         "authenticatorSelection": {"residentKey": "required", "userVerification": "required"}, "attestation": "none"}
        """)
        XCTAssertEqual(r.rp.id, "dashboard.cavnar.ai")
        XCTAssertEqual(r.excludeCredentials?.count, 1)
    }

    // MARK: #65 Memory

    func testMemoryFactsReadPinAndScope() throws {
        let f = try decode(MemoryFact.self, """
        {"id": 7, "fact": "Closed first Sunday", "kind": "constraint", "pinned": true, "can_pin": true,
         "scope": "org", "can_set_scope": true, "from_location": null, "can_forget": true}
        """)
        XCTAssertTrue(f.pinned && f.canPin && f.canSetScope)
        XCTAssertEqual(f.scope, "org")
        XCTAssertFalse(f.fromLocation)
        XCTAssertTrue(f.detailLine(viewerIsPrincipal: true).contains("Pinned"))
    }

    // MARK: #13 Billing

    func testBillingSaysWhereItIsManagedAsPlainText() {
        XCTAssertEqual(AccountBillingDetailView.manageNote, "Manage billing at dashboard.cavnar.ai")
        XCTAssertFalse(AccountBillingDetailView.manageNote.contains("://"))
        XCTAssertEqual(AccountBillingDetailView.invoiceStatus("open"), "Due")
    }
}
