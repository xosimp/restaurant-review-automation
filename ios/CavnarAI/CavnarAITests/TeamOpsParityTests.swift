import XCTest
@testable import CavnarAI

/// Web vs iOS parity audit (10/7/26), owner-side team operations: the Team
/// inbox (#10), the lineup brief (#26), house rules / docs / certificates
/// (#62), the staff pulse (#67), team messages (#71) and the person
/// sheet's name and records (#87). Decoding and every request body's keys —
/// a body missing a key the route reads was half the Critical bugs.
@MainActor
final class TeamOpsParityTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: #10 — the Team inbox

    func testTheInboxDecodesThreadsUnreadAndWhoIsRunningLate() throws {
        let r = try decode(TeamInboxResponse.self, """
            {"ok": true, "unread": 3, "threads": [
               {"thread_id": 7, "membership_id": 4, "employee_name": "Dana Lee", "last_body": "Can I swap Friday?",
                "last_from": "staff", "last_shift_date": "2026-10-09", "last_at": "2026-10-07T21:10:00+00:00", "unread": 2},
               {"thread_id": 9, "employee_name": "Ben", "last_body": "See you at 4", "last_from": "manager", "unread": 0},
               {"no": "id"}],
             "late_today": [{"id": 1, "employee_name": "Ana", "shift_start": "4:00pm", "role": "Server",
                             "eta_minutes": 20, "note": "bus", "expected_at": "4:20pm"}]}
            """)
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.unread, 3)
        XCTAssertEqual(r.threads.map(\.threadId), [7, 9], "a row with no id is skipped, not fatal")
        XCTAssertEqual(r.threads[1].preview, "You: See you at 4")
        XCTAssertEqual(r.lateToday.first?.headline, "Ana is running about 20 minutes late")
        XCTAssertEqual(r.lateToday.first?.detail, "4:00pm Server \u{00B7} expect them around 4:20pm \u{00B7} \u{201C}bus\u{201D}")
    }

    func testSeenSitsUnderTheNewestManagerMessageTheEmployeeRead() {
        let msgs = [
            TeamThreadMessage(id: 1, from: "staff", senderName: "Dana", body: "hi"),
            TeamThreadMessage(id: 2, from: "manager", senderName: "Erik", body: "yes", readAt: "2026-10-07T20:00:00Z"),
            TeamThreadMessage(id: 3, from: "manager", senderName: "Erik", body: "and?", readAt: nil),
        ]
        XCTAssertEqual(TeamThreadView.lastSeenId(msgs), 2)
        XCTAssertNil(TeamThreadView.lastSeenId(Array(msgs.prefix(1))))
    }

    func testTheReplyAndAnnouncementBodiesCarryEveryKeyTheRoutesRead() throws {
        XCTAssertEqual(try object(TeamReplyBody(body: "On my way")) as NSDictionary, ["body": "On my way"])
        let all = try object(TeamAnnounceBody.make(title: " Closing at 9 ", body: "", urgent: false, audience: .all,
                                                   role: "Server", day: "2026-10-09", expires: ""))
        XCTAssertEqual(Set(all.keys), ["title", "body", "priority", "audience", "audience_value", "expires_on"])
        XCTAssertEqual(all["title"] as? String, "Closing at 9")
        XCTAssertEqual(all["priority"] as? String, "normal")
        XCTAssertTrue(all["audience_value"] is NSNull, "everyone names no role or day")
        XCTAssertTrue(all["expires_on"] is NSNull)
        let role = try object(TeamAnnounceBody.make(title: "Pre-shift at 3", body: "Bar", urgent: true, audience: .role,
                                                    role: "Bartender", day: "", expires: "2026-10-10"))
        XCTAssertEqual(role["audience"] as? String, "role")
        XCTAssertEqual(role["audience_value"] as? String, "Bartender")
        XCTAssertEqual(role["priority"] as? String, "urgent")
        XCTAssertEqual(role["expires_on"] as? String, "2026-10-10")
        let day = try object(TeamAnnounceBody.make(title: "x", body: "", urgent: false, audience: .shiftDate,
                                                   role: "Server", day: "2026-10-09", expires: ""))
        XCTAssertEqual(day["audience"] as? String, "shift_date")
        XCTAssertEqual(day["audience_value"] as? String, "2026-10-09")
    }

    func testAnAnnouncementReadsNOfMAndTheSentLine() throws {
        let r = try decode(TeamAnnounceResponse.self, """
            {"ok": true, "delivery": {"push": 4, "sms": 1, "email": 0, "none": 2},
             "announcement": {"id": 5, "title": "Storm", "body": "", "priority": "urgent", "audience": "all",
                              "audience_label": "Everyone", "expires_on": null, "expired": false, "withdrawn": false,
                              "created_at": "2026-10-07T18:00:00+00:00", "created_by_name": "Erik",
                              "recipients": 7, "read": 3, "read_line": "3 of 7 read",
                              "unread_names": ["Ana", "Ben"], "read_by": [{"name": "Dana", "acked_at": null}]}}
            """)
        let a = try XCTUnwrap(r.announcement)
        XCTAssertTrue(a.isUrgent && a.canWithdraw)
        XCTAssertEqual(a.readLine, "3 of 7 read")
        XCTAssertEqual(r.sentLine, "Sent to 7 people \u{00B7} 2 see it next time they open the app")
        XCTAssertFalse(TeamAnnouncementRow.metaLine(a).contains("2026-"), "owner-facing dates are M/D/YY")
    }

    func testAnInboxLinkOpensTheInboxOnItsThread() {
        XCTAssertEqual(LaborFocus(section: "inbox", item: "7"), .inbox(threadId: 7))
        XCTAssertEqual(LaborFocus(section: "inbox"), .inbox(threadId: nil))
        XCTAssertEqual(LaborFocus(section: "lineup"), .lineup)
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/inbox?thread=7")!)?.itemId, "7")
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/inbox/9")!)?.itemId, "9")
        XCTAssertEqual(ModuleRoute.from(NavPath("labor/inbox/9")!)?.section, "inbox")
        XCTAssertNil(ModuleRoute.from(NavPath("labor/requests")!)?.itemId)
    }

    // MARK: Lock-screen actions (#10, #71, #26)

    func testATypedReplyAnswersTheThreadOrTheTeammateInTheBackground() throws {
        let staff = try XCTUnwrap(PushManager.backgroundAction(
            for: PushManager.replyMessageAction, cavnar: ["alert_type": "employee_message", "thread_id": 7],
            userText: "  See you at 4  "))
        XCTAssertEqual(staff.path, "/mobile/api/labor/inbox/threads/7/reply")
        XCTAssertEqual(try object(XCTUnwrap(staff.payload)) as NSDictionary, ["body": "See you at 4"])
        let mate = try XCTUnwrap(PushManager.backgroundAction(
            for: PushManager.replyMessageAction, cavnar: ["alert_type": "team_message", "sender_id": "3"],
            userText: "Yes"))
        XCTAssertEqual(mate.path, "/mobile/api/team/messages")
        XCTAssertEqual(try object(XCTUnwrap(mate.payload)) as NSDictionary, ["recipient_id": 3, "body": "Yes"])
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.replyMessageAction,
                                                  cavnar: ["alert_type": "team_message", "sender_id": 3], userText: "  "),
                     "an empty reply sends nothing")
        XCTAssertTrue(PushManager.needsAppUnlock(mate, passcodeSet: true), "a reply is outward: the app passcode guards it")
    }

    func testApproveOnTheLineupPushApprovesTheDraftAsWritten() throws {
        let a = try XCTUnwrap(PushManager.backgroundAction(for: PushManager.approveLineupAction,
                                                           cavnar: ["alert_type": "lineup_brief_waiting", "day": "2026-10-02"]))
        XCTAssertEqual(a.path, "/mobile/api/staff-brief/approve")
        let body = try object(XCTUnwrap(a.payload))
        XCTAssertEqual(body["day"] as? String, "2026-10-02")
        XCTAssertTrue(body["text"] is NSNull, "null text approves the stored draft")
        XCTAssertNil(PushManager.backgroundAction(for: PushManager.approveLineupAction, cavnar: ["day": "tonight"]))
    }


    func testATeammatesPushOpensMessagesOnTheirThread() {
        let center = TeamMessagesCenter.shared
        center.reset()
        let router = DeepLinkRouter()
        router.open(NavPath("messages/5")!)
        XCTAssertTrue(center.showing)
        XCTAssertEqual(center.openWith, 5)
        center.reset()
        router.handleNotificationTap(alertType: "team_message", reviewId: nil)
        XCTAssertTrue(center.showing, "no nav from an older server still opens Messages")
        XCTAssertNil(center.openWith)
        center.reset()
        XCTAssertEqual(DeepLinkRouter.webModule(for: "lineup_brief_waiting"), "labor")
    }

    // MARK: #26 — the lineup brief

    func testTheBriefDecodesItsStateAndTheApproveBodyNamesTheDay() throws {
        let r = try decode(LineupBriefResponse.self, """
            {"ok": true, "can_edit": true, "brief": {"day": "2026-10-02", "weekday": "Friday",
              "items": [{"kind": "volume", "text": "Busy Friday."}, {"kind": "rush", "text": "Rush 6–8pm."}],
              "draft_text": "Busy one tonight.", "draft_status": "drafted", "draft_items_changed": false,
              "model_used_today": true, "approved_text": null, "edited": false, "focus": null,
              "suggestions": [{"item": "Old fashioned", "why": "Guests praised it."}]}}
            """)
        let b = try XCTUnwrap(r.brief)
        XCTAssertTrue(r.canEdit && b.isWaiting && !b.isApproved)
        XCTAssertFalse(b.canAskForDraft, "the day's one model call is spent")
        XCTAssertEqual(b.suggestions.first?.item, "Old fashioned")
        let approve = try object(LineupApproveBody(day: b.day, text: "Big night."))
        XCTAssertEqual(approve as NSDictionary, ["day": "2026-10-02", "text": "Big night."])
        XCTAssertEqual(try object(LineupFocusBody(day: b.day, item: "Old fashioned", line: "Push it")) as NSDictionary,
                       ["day": "2026-10-02", "item": "Old fashioned", "line": "Push it"])
    }

    // MARK: #67 — the staff pulse

    func testBelowTheFloorThePulseIsACountAndNeverAFigure() throws {
        let low = try decode(StaffPulseResponse.self, """
            {"ok": true, "days": 14, "responses": 2, "min_responses": 3, "enough": false, "average": null,
             "by_day": [], "notes": [], "message": "2 answers so far. Shown once 3 people have answered, so nobody's answer is singled out."}
            """)
        let s = try XCTUnwrap(low.summary)
        XCTAssertFalse(s.enough)
        XCTAssertEqual(s.averageText, "\u{2014}")
        XCTAssertTrue(s.floorLine.hasPrefix("2 answers so far"))
        let ok = try decode(StaffPulseResponse.self, """
            {"ok": true, "days": 14, "window": {"label": "9/24/26 – 10/7/26"}, "responses": 9, "min_responses": 3,
             "enough": true, "average": 4.2, "distribution": {"1": 0, "2": 1, "3": 1, "4": 3, "5": 4},
             "by_day": [{"date": "2026-10-06", "date_label": "10/6/26", "responses": 4, "average": 4.5}],
             "other_days_responses": 5, "notes": [{"text": "Smooth", "date_label": "10/6/26"}]}
            """)
        let t = try XCTUnwrap(ok.summary)
        XCTAssertTrue(t.enough)
        XCTAssertEqual(t.averageText, "4.2")
        XCTAssertEqual(t.distribution["5"], 4)
        XCTAssertEqual(t.byDay.first?.average, 4.5)
        let close = try decode(CloseOutStaffPulse.self, #"{"responses": 4, "average": 4.0, "line": "Staff rated tonight 4 out of 5 (4 answers)."}"#)
        XCTAssertEqual(close.responses, 4)
    }

    // MARK: #62 — house rules, docs, certificates

    func testTheCertificateAndDocBodiesSendEveryKeyTheUpsertReplaces() throws {
        let cert = try object(StaffCertSaveBody(employeeName: "Dana Lee", cert: "food handler", expiresOn: "2027-03-01",
                                                issuedOn: "", note: ""))
        XCTAssertEqual(Set(cert.keys), ["employee_name", "cert", "expires_on", "issued_on", "note"])
        XCTAssertEqual(cert["expires_on"] as? String, "2027-03-01")
        XCTAssertTrue(cert["issued_on"] is NSNull && cert["note"] is NSNull, "a blank clears, never left out")
        let doc = try object(StaffDocSaveBody(id: nil, kind: "menu_spec", title: "Fall menu", body: "…",
                                              roles: StaffDocSaveBody.roles(from: " Server, , Bartender ")))
        XCTAssertTrue(doc["id"] is NSNull, "a new doc says so")
        XCTAssertEqual(doc["roles"] as? [String], ["Server", "Bartender"])
        XCTAssertEqual(try object(HouseRulesBody(body: "Phones away.")) as NSDictionary,
                       ["title": "House rules", "body": "Phones away."])
    }

    func testCertificatesReadTheWebsWordsAndDates() throws {
        let r = try decode(StaffCertsResponse.self, """
            {"ok": true, "can_edit": true, "remind_days": 30, "roster": ["Dana Lee"], "certs": [
              {"id": 1, "employee_name": "Dana Lee", "cert": "food handler", "expires_on": "2026-09-30",
               "days_left": -7, "status": "expired"},
              {"id": 2, "employee_name": "Ben", "cert": "alcohol", "expires_on": null, "status": "no_expiry"}]}
            """)
        XCTAssertEqual(r.certs[0].expiryLine, "Expired 9/30/26")
        XCTAssertEqual(r.certs[1].expiryLine, "No expiry date")
        XCTAssertEqual(r.remindDays, 30)
        XCTAssertEqual(AccountLinkSection("people"), .people, "the expiry email's account/people opens People")
    }

    func testAScannedCardFillsTheDatesAndKindItCanRead() {
        let card = ["ServSafe Food Handler", "Certificate of Achievement", "Dana Lee",
                    "Date of Exam: 03/14/2025", "Expiration Date:", "03/14/2028"]
        let found = CertificateScanReader.parse(lines: card)
        XCTAssertEqual(found.expires, "2028-03-14")
        XCTAssertEqual(found.issued, "2025-03-14")
        XCTAssertEqual(found.kind, "food handler")
        XCTAssertEqual(CertificateScanReader.kind(in: "ServSafe Manager certification"), "manager")
        XCTAssertEqual(CertificateScanReader.kind(in: "BASSET seller-server"), "alcohol")
        XCTAssertEqual(CertificateScanReader.dates(in: "Valid thru Dec 31, 2027"), ["2027-12-31"])
        XCTAssertEqual(CertificateScanReader.parse(lines: ["No dates here"]).expires, nil)
    }

    // MARK: #71 — team messages

    func testTeamMessagesDecodeAndTheSendBodyNamesTheRecipient() throws {
        let inbox = try decode(TeamMessagesInboxResponse.self, """
            {"ok": true, "unread_total": 2, "teammates": [
              {"user_id": 3, "username": "jheflin", "name": "Jim Heflin", "role": "manager",
               "last_message": "Closing tonight?", "last_from_me": false, "last_at": "2026-10-07 21:00:00", "unread": 2},
              {"user_id": 4, "username": "erik", "name": "", "role": "client", "last_message": null,
               "last_from_me": false, "last_at": null, "unread": 0}]}
            """)
        XCTAssertEqual(inbox.unreadTotal, 2)
        XCTAssertEqual(inbox.teammates[0].initials, "JH")
        XCTAssertEqual(inbox.teammates[1].name, "erik", "the login when no person's name is on file")
        XCTAssertEqual(try object(TeamDirectSendBody(recipientId: 3, body: "Yes")) as NSDictionary,
                       ["recipient_id": 3, "body": "Yes"])
        let msgs = [TeamDirectMessage(id: 1, senderId: 1, recipientId: 3, body: "a"),
                    TeamDirectMessage(id: 2, senderId: 1, recipientId: 3, body: "b"),
                    TeamDirectMessage(id: 3, senderId: 3, recipientId: 1, body: "c")]
        XCTAssertFalse(TeamDirectThreadView.endsRun(msgs, at: 0, me: 1))
        XCTAssertTrue(TeamDirectThreadView.endsRun(msgs, at: 1, me: 1))
        XCTAssertTrue(TeamDirectThreadView.endsRun(msgs, at: 2, me: 1))
    }

    // MARK: #87 — name and records

    func testThePersonSheetKnowsWhoMayRenameMergeAndErase() throws {
        let p = try decode(PersonRecord.self, """
            {"key": "dana-lee", "name": "Dana Lee", "active": false, "has_login": false, "can_manage_login": true}
            """)
        XCTAssertTrue(p.canManageLogin && p.mayErase)
        let onRoster = try decode(PersonRecord.self, #"{"key": "ben", "name": "Ben", "active": true, "can_manage_login": true}"#)
        XCTAssertFalse(onRoster.mayErase, "erase waits until they are off the roster")
        let manager = try decode(PersonRecord.self, #"{"key": "ben", "name": "Ben", "active": false}"#)
        XCTAssertFalse(manager.canManageLogin || manager.mayErase, "the account holder's alone")
        XCTAssertTrue(PersonSheetViewModel.typedNameMatches("  dana   LEE ", "Dana Lee"))
        XCTAssertFalse(PersonSheetViewModel.typedNameMatches("Dana", "Dana Lee"))
        XCTAssertEqual(try object(PeopleMergeBody(from: "dana-l", into: "dana-lee")) as NSDictionary,
                       ["from": "dana-l", "into": "dana-lee"])
        XCTAssertEqual(try object(PersonEraseBody(confirm: "Dana Lee")) as NSDictionary, ["confirm": "Dana Lee"])
        XCTAssertEqual(PersonSheetViewModel.renamePath(for: "dana lee"), "/mobile/api/people/dana%20lee/rename")
        let merges = try decode(PeopleMergesResponse.self, """
            {"ok": true, "can_undo": true, "merges": [{"merge_id": 4, "from": "Dana L", "into": "Dana Lee",
              "merged_on": "10/2/26", "undo_until": "11/1/26", "undoable": true, "why_not": null, "undone": false}]}
            """)
        XCTAssertEqual(PersonSheet.mergeLine(merges.merges[0]), "Merged 10/2/26 \u{00B7} can be undone until 11/1/26")
    }
}
