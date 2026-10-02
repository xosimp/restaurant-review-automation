import XCTest
@testable import CavnarAI

/// Employee audit wave 2, I3: the staff app's Requests, Me, Inbox and
/// thread payloads as the server on this branch sends them (B1–B8), and the
/// pure logic the screens rest on — labels, order, the availability draft's
/// checks and the bodies the routes read. Nothing here makes a request.
@MainActor
final class StaffRequestsMeInboxTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: Requests board (B3)

    private let boardJSON = """
    {"ok": true,
     "requests": [
       {"id": 1, "kind": "drop", "date": "2026-10-09", "shift_start": "4:00pm", "shift_end": "10:00pm",
        "role": "Server", "status": "open", "reason": "class", "employee_name": "Dana K", "target_name": null,
        "target_accepted": false, "replacement_name": null, "decision_note": "Thanks for the heads up",
        "decided_at": "2026-10-02 14:00:00", "created_at": "2026-10-01 10:00:00"},
       {"id": 2, "kind": "swap", "date": "2026-10-10", "shift_start": "11:00am", "shift_end": "4:00pm",
        "status": "pending", "employee_name": "Dana K", "target_name": "Ben T", "target_date": "2026-10-11",
        "target_start": "4:00pm", "target_end": "10:00pm", "target_accepted": true},
       {"id": 3, "kind": "drop", "date": "2026-09-20", "shift_start": "4:00pm", "status": "cancelled",
        "decision_note": "The shift changed hands before this was answered."},
       {"id": 4, "kind": "post", "date": "2026-10-12", "shift_start": "5:00pm", "status": "open"}],
     "asks": [{"id": 9, "kind": "swap", "date": "2026-10-13", "shift_start": "4:00pm", "shift_end": "10:00pm",
               "role": "Server", "status": "approved", "employee_name": "Ana R", "target_name": "Dana K",
               "target_date": "2026-10-14", "target_start": "11:00am", "target_end": "4:00pm", "manager_approved": true}],
     "offers": [{"id": 5, "request_id": 40, "status": "offered", "date": "2026-10-15", "shift_start": "5:00pm",
                 "shift_end": "11:00pm", "role": "Bartender", "employee_name": null, "note": "We need you",
                 "for_coverage": true, "created_at": "2026-10-02T10:00:00Z"},
                {"id": 6, "request_id": 41, "status": "accepted", "date": "2026-10-16", "shift_start": "5:00pm"}],
     "open": [{"id": 20, "kind": "drop", "date": "2026-10-17", "shift_start": "4:00pm", "shift_end": "10:00pm",
               "role": "Server", "status": "open", "employee_name": "Ben T", "posted_by_manager": false,
               "can_take": false, "why_not": "That would put you over 40 hours"},
              {"id": 21, "kind": "post", "date": "2026-10-18", "shift_start": "4:00pm", "status": "open",
               "employee_name": null, "posted_by_manager": true, "can_take": null, "why_not": null}]}
    """

    func testTheBoardDecodesEveryListAndItsNewStates() throws {
        let b = try decode(StaffRequestsBoard.self, boardJSON)
        XCTAssertEqual(b.requests.count, 4)
        XCTAssertEqual(b.asks.count, 1)
        XCTAssertEqual(b.offers.map(\.id), [5], "an answered offer is not waiting on anyone")
        XCTAssertEqual(b.open.count, 2)
        XCTAssertEqual(b.waitingCount, 2, "asks and offers wait on you; open shifts are a chance, not a wait")

        let open = b.requests[0]
        XCTAssertEqual(open.chip, StaffStatusChip(text: "Open — you're still on it", tone: .warning))
        XCTAssertTrue(open.detail?.contains("still on this shift until someone picks it up") == true)
        XCTAssertEqual(open.tag, "Giving up")
        XCTAssertTrue(open.canWithdraw, "approved and open drops can be withdrawn too")
        XCTAssertEqual(open.decisionNote, "Thanks for the heads up")

        let swap = b.requests[1]
        XCTAssertEqual(swap.tag, "Swap")
        XCTAssertEqual(swap.chip.text, "Ben T said yes")
        XCTAssertEqual(swap.detail, "Ben T said yes — waiting on your manager.")
        XCTAssertEqual(swap.targetLabel, "Sun 10/11/26 · 4pm – 10pm")

        XCTAssertEqual(b.requests[2].chip.text, "Cancelled")
        XCTAssertFalse(b.requests[2].canWithdraw)
        XCTAssertFalse(b.requests[3].canWithdraw, "a shift the manager posted open is theirs to take down")
    }

    func testAnOpenShiftSaysWhyItCantBeYoursAndAnUnjudgedOneStaysTakeable() throws {
        let b = try decode(StaffRequestsBoard.self, boardJSON)
        XCTAssertFalse(b.open[0].takeable)
        XCTAssertEqual(b.open[0].whyNotLine, "You can't take this one: that would put you over 40 hours")
        XCTAssertNil(b.open[1].canTake)
        XCTAssertTrue(b.open[1].takeable, "past the judging budget the claim itself still checks")
        XCTAssertNil(b.open[1].employeeName)
        XCTAssertTrue(b.open[1].postedByManager)
    }

    func testAcceptingASwapNamesBothShifts() throws {
        let ask = try decode(StaffRequestsBoard.self, boardJSON).asks[0]
        XCTAssertEqual(ask.theirShift, "Tue 10/13/26 · 4pm – 10pm")
        XCTAssertEqual(ask.yourShift, "Wed 10/14/26 · 11am – 4pm")
        XCTAssertTrue(ask.confirmMessage.hasPrefix("You'll work Ana R's Tue 10/13/26 · 4pm – 10pm; Ana R takes your Wed 10/14/26 · 11am – 4pm."))
        XCTAssertTrue(ask.confirmMessage.contains("already said yes"))
    }

    func testAnOfferWithNoHolderIsAnExtraShift() throws {
        let offer = try decode(StaffRequestsBoard.self, boardJSON).offers[0]
        XCTAssertEqual(offer.headline, "Your manager offered you an extra shift")
        XCTAssertTrue(offer.forCoverage)
        XCTAssertEqual(offer.note, "We need you")
    }

    func testARefusedBoardIsAFailureNotAnEmptyList() {
        XCTAssertThrowsError(try decode(StaffRequestsBoard.self, #"{"ok": false, "error": "No session"}"#))
        XCTAssertThrowsError(try decode(StaffTimeOffList.self, #"{"ok": false}"#))
        XCTAssertThrowsError(try decode(StaffInboxPayload.self, #"{"ok": false}"#))
    }

    func testTheAnswerBodiesAreJSONBooleans() throws {
        XCTAssertEqual(try object(StaffAcceptBody(accept: true))["accept"] as? Bool, true)
        XCTAssertTrue(try object(StaffEmptyBody()).isEmpty)
    }

    // MARK: Time off (B3 M6)

    func testTimeOffCarriesCancelAndTheShiftsStillScheduled() throws {
        let list = try decode(StaffTimeOffList.self, """
        {"ok": true, "requests": [
          {"id": 7, "employee_name": "Dana K", "start_date": "2026-10-20", "end_date": "2026-10-22",
           "reason": "wedding", "status": "approved", "decision_note": null, "can_cancel": true,
           "still_scheduled": [{"date": "2026-10-21", "day": "Wednesday", "shift_start": "4:00pm",
                                "shift_end": "10:00pm", "role": "Server"}]},
          {"id": 8, "start_date": "2026-09-01", "end_date": "2026-09-01", "status": "denied"}]}
        """)
        let t = list.requests[0]
        XCTAssertTrue(t.canCancel)
        XCTAssertEqual(t.stillScheduled.map(\.label), ["Wed 10/21/26 · 4pm – 10pm"])
        XCTAssertEqual(t.chip, StaffStatusChip(text: "Approved", tone: .good))
        XCTAssertEqual(t.rangeLabel, "10/20/26 – 10/22/26")
        XCTAssertFalse(list.requests[1].canCancel)
        XCTAssertEqual(list.requests[1].chip.tone, .bad)
    }

    func testYourRequestsPutTheLiveOnesFirstAndHideAWithdrawalInItsUndoWindow() throws {
        let b = try decode(StaffRequestsBoard.self, boardJSON)
        let offs = try decode(StaffTimeOffList.self, """
        {"ok": true, "requests": [{"id": 7, "start_date": "2026-10-20", "end_date": "2026-10-22", "status": "pending"},
                                  {"id": 8, "start_date": "2026-08-01", "end_date": "2026-08-02", "status": "withdrawn"}]}
        """).requests
        let merged = StaffYourRequest.merged(timeOff: offs, shifts: b.requests)
        XCTAssertEqual(merged.map(\.id), ["sr1", "sr2", "sr4", "to7", "sr3", "to8"])
        let hidden = StaffYourRequest.merged(timeOff: offs, shifts: b.requests, hiding: ["to7"])
        XCTAssertFalse(hidden.contains { $0.id == "to7" })
    }

    // MARK: Times and days

    func testTimesReadLikeTheRestOfTheApp() {
        XCTAssertEqual(StaffClock.display("17:00"), "5pm")
        XCTAssertEqual(StaffClock.display("4:00pm"), "4pm")
        XCTAssertEqual(StaffClock.display("4:30pm"), "4:30pm")
        XCTAssertEqual(StaffClock.display("00:30"), "12:30am")
        XCTAssertEqual(StaffClock.display("whenever"), "whenever")
        XCTAssertEqual(StaffClock.minutes("5:30 PM"), 17 * 60 + 30)
        XCTAssertEqual(StaffClock.minutes("12am"), 0)
        XCTAssertNil(StaffClock.minutes("25:00"))
        XCTAssertEqual(StaffClock.range("4:00pm", "10:00pm"), "4pm – 10pm")
        XCTAssertEqual(StaffClock.choices.count, 48)
        XCTAssertEqual(StaffClock.choices.first, "05:00")
        XCTAssertEqual(StaffClock.choices.last, "04:30")
        XCTAssertEqual(StaffDay.shortWeekday("2026-10-02"), "Fri")
        XCTAssertEqual(StaffDay.shift("2026-10-02", "16:00", "22:30"), "Fri 10/2/26 · 4pm – 10:30pm")
    }

    // MARK: Availability (B8 M5)

    private let availabilityJSON = """
    {"ok": true, "days": ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"],
     "week": [
       {"day": "Monday", "status": "any", "earliest": null, "latest": null, "from": null, "until": null},
       {"day": "Tuesday", "status": "window", "earliest": "17:00", "latest": null, "from": null, "until": "2026-12-15"},
       {"day": "Friday", "status": "off", "earliest": null, "latest": null, "from": "2026-10-10", "until": null}],
     "unavailable_days": ["Friday"], "notes": "school",
     "updated_at": "2026-10-02 14:03:11.123456",
     "time_off_hint": "Dates you're away go in a time-off request."}
    """

    func testAvailabilityIsAlwaysSevenDaysAndKeepsItsVersionExactly() throws {
        let r = try decode(StaffAvailabilityRecord.self, availabilityJSON)
        XCTAssertEqual(r.week.map(\.day), StaffAvailabilityRecord.weekdays)
        XCTAssertEqual(r.week[1].summary, "Not before 5pm · until 12/15/26")
        XCTAssertEqual(r.week[4].summary, "Can't work · from 10/10/26")
        XCTAssertEqual(r.week[6].status, .any, "a day the server left out is any time")
        XCTAssertEqual(r.updatedAt, "2026-10-02 14:03:11.123456")
        XCTAssertEqual(StaffAvailabilityDraft.rowSummary(r), "1 day off · 1 with hours")
    }

    func testAnAvailabilityAnswerWithoutTheWeekIsNeverAnEmptyForm() {
        XCTAssertThrowsError(try decode(StaffAvailabilityRecord.self, #"{"ok": true, "unavailable_days": []}"#))
    }

    func testTheSaveAlwaysSendsTheVersionEvenWhenThereIsNone() throws {
        var r = try decode(StaffAvailabilityRecord.self, availabilityJSON)
        var json = try object(StaffAvailabilityDraft(r).body())
        XCTAssertEqual(json["updated_at"] as? String, "2026-10-02 14:03:11.123456")
        XCTAssertEqual((json["week"] as? [[String: Any]])?.count, 7)

        r = StaffAvailabilityRecord(week: StaffAvailabilityRecord.weekdays.map { StaffAvailabilityDay(day: $0) })
        let data = try JSONEncoder.cavnar.encode(StaffAvailabilityDraft(r).body())
        json = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertTrue(json.keys.contains("updated_at"), "a missing key is itself a 409")
        XCTAssertTrue(json["updated_at"] is NSNull)
    }

    func testADaysEntryEncodesOnlyWhatItsStatusUses() throws {
        let any = try object(StaffAvailabilityDay(day: "Monday", status: .any, earliest: "17:00", until: "2026-12-01"))
        XCTAssertTrue(any["earliest"] is NSNull)
        XCTAssertTrue(any["until"] is NSNull)
        let off = try object(StaffAvailabilityDay(day: "Friday", status: .off, earliest: "17:00", until: "2026-12-01"))
        XCTAssertTrue(off["earliest"] is NSNull)
        XCTAssertEqual(off["until"] as? String, "2026-12-01")
        let window = try object(StaffAvailabilityDay(day: "Tuesday", status: .window, latest: "23:00"))
        XCTAssertEqual(window["status"] as? String, "window")
        XCTAssertEqual(window["latest"] as? String, "23:00")
    }

    func testTheDraftSaysWhatStopsTheSaveUnderTheDay() throws {
        var d = StaffAvailabilityDraft(try decode(StaffAvailabilityRecord.self, availabilityJSON))
        XCTAssertTrue(d.problems(today: "2026-10-02").isEmpty)
        d.days[2] = StaffAvailabilityDay(day: "Wednesday", status: .window)
        d.days[3] = StaffAvailabilityDay(day: "Thursday", status: .off, until: "2026-09-30")
        d.days[5] = StaffAvailabilityDay(day: "Saturday", status: .off, from: "2026-12-01", until: "2026-11-01")
        let p = d.problems(today: "2026-10-02")
        XCTAssertEqual(p["Wednesday"], "Pick the earliest start or the latest finish.")
        XCTAssertEqual(p["Thursday"], "That end date has passed.")
        XCTAssertEqual(p["Saturday"], "The end date is before the start date.")
        XCTAssertNil(p[""])

        d.days = StaffAvailabilityRecord.weekdays.map { StaffAvailabilityDay(day: $0, status: .off) }
        XCTAssertEqual(d.problems(today: "2026-10-02")[""], "Every day blocked — leave at least one you can work.")
    }

    func testASaveAnswersConflictsAndTheTimeOffHint() throws {
        let saved = try decode(StaffAvailabilitySaved.self, """
        {"ok": true, "days": [], "week": [{"day": "Friday", "status": "off"}], "unavailable_days": ["Friday"],
         "notes": "away Oct 3-6", "updated_at": "2026-10-02 15:00:00.000001",
         "conflicts": [{"date": "2026-10-09", "day": "Friday", "shift_start": "5:00pm", "shift_end": "11:00pm",
                        "role": "Server", "reason": "you marked Fridays unavailable"}],
         "conflicts_text": "You're still on Fri 10/9/26 5:00pm — ask to drop it.",
         "hint": {"kind": "time_off", "text": "Dates you're away go in a time-off request."}, "managers_told": 1}
        """)
        XCTAssertEqual(saved.record.updatedAt, "2026-10-02 15:00:00.000001")
        XCTAssertEqual(saved.conflicts.first?.label, "Fri 10/9/26 · 5pm – 11pm")
        XCTAssertEqual(saved.conflicts.first?.reason, "you marked Fridays unavailable")
        XCTAssertEqual(saved.conflictsText, "You're still on Fri 10/9/26 5:00pm — ask to drop it.")
        XCTAssertEqual(saved.hint?.kind, "time_off")
    }

    func testAStaleSaveCarriesTheRecordAsItIsNow() throws {
        let r = try decode(StaffAvailabilityRefusal.self, """
        {"ok": false, "stale": true, "error": "Your availability was changed somewhere else.",
         "availability": \(availabilityJSON)}
        """)
        XCTAssertTrue(r.stale)
        XCTAssertEqual(r.availability?.updatedAt, "2026-10-02 14:03:11.123456")
        let allOff = try decode(StaffAvailabilityRefusal.self,
                                #"{"ok": false, "error": "Every day blocked", "hint": {"kind": "time_off", "text": "Use time off."}}"#)
        XCTAssertFalse(allOff.stale)
        XCTAssertNil(allOff.availability)
        XCTAssertEqual(allOff.hint?.text, "Use time off.")
    }

    // MARK: Preferences and notices (B2)

    func testTheTextsSwitchKnowsWhetherTextsCanGoAndWhatItsConsentSays() throws {
        let p = try decode(StaffPreferencesState.self, """
        {"ok": true, "preferred_dayparts": ["night"], "desired_hours": "28", "schedule_texts": true,
         "sms_available": false, "schedule_texts_consent": "Text me about my schedule and my requests.",
         "schedule_texts_consent_version": 2, "schedule_texts_scope": ["schedule", "request"]}
        """)
        XCTAssertFalse(p.smsAvailable)
        XCTAssertEqual(p.desiredHours, 28)
        XCTAssertEqual(p.consentVersion, 2)
        XCTAssertEqual(p.scope, ["schedule", "request"])
        let body = try object(StaffTextsConsentBody(scheduleTexts: true, consentVersion: 2))
        XCTAssertEqual(body["schedule_texts"] as? Bool, true)
        XCTAssertEqual(body["consent_version"] as? Int, 2)
        XCTAssertEqual(Set(body.keys), ["schedule_texts", "consent_version"], "the switch alone, nothing else")
        let old = try decode(StaffPreferencesState.self, #"{"ok": true, "schedule_texts": false}"#)
        XCTAssertFalse(old.smsAvailable, "an older server hides the switch rather than promising texts")
    }

    func testTheRemindersSwitchAndQuietHours() throws {
        let n = try decode(StaffNotificationSettings.self, """
        {"ok": true, "push_registered": true, "reminders": false, "reminders_server_side": true,
         "quiet_hours": {"start": "22:00", "end": "08:00"}}
        """)
        XCTAssertTrue(n.pushRegistered)
        XCTAssertFalse(n.reminders)
        XCTAssertEqual(n.quietLine, "Quiet 10pm – 8am: notices arrive silently.")
    }

    func testMeCarriesTheMaskedPhoneEmailAndLocations() throws {
        let me = try decode(StaffMeEnvelope.self, """
        {"ok": true, "employee": {"name": "Maria Lopez", "role": "employee", "restaurant": "Alpha", "has_pin": true,
         "phone_masked": "(•••) •••-2233", "has_phone": true, "email": "maria@example.com",
         "notifications": {"push": true, "texts": false, "texts_consent": false, "email": true},
         "restaurant_id": 1, "membership_id": 12,
         "locations": [{"restaurant_id": 1, "restaurant": "Alpha", "membership_id": 12, "current": true},
                       {"restaurant_id": 2, "restaurant": "Bravo", "membership_id": 31, "current": false}]}}
        """).employee
        XCTAssertEqual(me.phoneMasked, "(•••) •••-2233")
        XCTAssertTrue(me.pushOn)
        XCTAssertEqual(me.locations.map(\.restaurant), ["Alpha", "Bravo"])
        XCTAssertTrue(me.locations[0].current)
        XCTAssertThrowsError(try decode(StaffMeEnvelope.self, #"{"ok": true}"#))
    }

    // MARK: Inbox and the thread (B5)

    func testTheInboxBadgeIsUnreadAnnouncementsPlusReplies() throws {
        let inbox = try decode(StaffInboxPayload.self, """
        {"ok": true, "announcements": [
           {"id": 3, "title": "New menu tomorrow", "body": "Tasting at 3pm.", "priority": "urgent",
            "created_at": "2026-10-01T22:05:00Z", "created_by_name": "Erik S", "expires_on": "2026-10-03", "acked_at": null},
           {"id": 2, "title": "Parking", "body": "", "priority": "normal", "created_at": "2026-09-30T12:00:00Z",
            "created_by_name": "", "expires_on": null, "acked_at": "2026-09-30T13:00:00Z"}],
         "unread": 1, "unread_messages": 2}
        """)
        XCTAssertEqual(inbox.badge, 3)
        XCTAssertTrue(inbox.announcements[0].isUrgent)
        XCTAssertFalse(inbox.announcements[0].isRead)
        XCTAssertTrue(inbox.announcements[0].byline.hasPrefix("Erik S · 10/"))
        let after = inbox.acking(3, at: "2026-10-02T09:00:00Z")
        XCTAssertTrue(after.announcements[0].isRead)
        XCTAssertEqual(after.unread, 0)
        XCTAssertEqual(after.acking(3, at: "x").unread, 0, "acking twice never goes below zero")
    }

    func testTheThreadMarksSeenOnTheLastMessageAManagerRead() throws {
        let t = try decode(StaffThreadPayload.self, """
        {"ok": true, "thread_id": 7, "unread": 0, "messages": [
          {"id": 1, "from": "staff", "sender_name": "Dana K", "body": "Can I come in at 5?", "shift_date": "2026-09-21",
           "request_id": null, "request_kind": null, "created_at": "2026-09-20T18:00:00Z", "read_at": "2026-09-20T18:05:00Z"},
          {"id": 2, "from": "manager", "sender_name": "Erik S", "body": "5 is fine.", "created_at": "2026-09-20T18:06:00Z",
           "read_at": null},
          {"id": 3, "from": "staff", "sender_name": "Dana K", "body": "Thanks", "created_at": "2026-09-20T18:07:00Z",
           "read_at": null}]}
        """)
        XCTAssertEqual(t.lastSeenMineId, 1)
        XCTAssertEqual(t.messages[0].contextLabel, "About Mon 9/21/26")
        XCTAssertTrue(t.messages[0].isMine)
        XCTAssertFalse(t.messages[1].isMine)
        let empty = try decode(StaffThreadPayload.self, #"{"ok": true, "thread_id": null, "messages": [], "unread": 0}"#)
        XCTAssertNil(empty.threadId)
        XCTAssertTrue(empty.messages.isEmpty)
        XCTAssertEqual(t.appending(t.messages[2]).messages.count, 3, "a message already shown is not added twice")
    }

    func testAMessageSendsItsContextOnlyWhenItHasOne() throws {
        let plain = try object(StaffMessageBody(body: "Hi", context: nil))
        XCTAssertEqual(Set(plain.keys), ["body"])
        let shift = try object(StaffMessageBody(body: "Late", context: .shift(date: "2026-10-02", start: "4:00pm")))
        XCTAssertEqual(shift["shift_date"] as? String, "2026-10-02")
        XCTAssertNil(shift["request_kind"], "no kind without a request id")
        let offs = try decode(StaffTimeOffList.self,
                              #"{"ok": true, "requests": [{"id": 7, "start_date": "2026-10-20", "end_date": "2026-10-22", "status": "pending"}]}"#)
        let req = try object(StaffMessageBody(body: "?", context: .timeOff(offs.requests[0])))
        XCTAssertEqual(req["request_id"] as? Int, 7)
        XCTAssertEqual(req["request_kind"] as? String, "time_off")
        XCTAssertEqual(StaffMessageContext.timeOff(offs.requests[0]).label, "Time off 10/20/26 – 10/22/26")
    }

    // MARK: Pulse, calendar, language, docs (B6, B7)

    func testThePulseIsDueOnceAndSendsAJSONInteger() throws {
        let s = try decode(StaffPulseState.self, """
        {"ok": true, "due": {"date": "2026-09-26", "date_label": "9/26/26", "start": "16:00", "role": "Server"},
         "recent": [{"date": "2026-09-25", "date_label": "9/25/26", "rating": 4, "note": null}]}
        """)
        XCTAssertEqual(s.due?.line, "Sat 9/26/26 · 4pm · Server")
        XCTAssertNil(try decode(StaffPulseState.self, #"{"ok": true, "due": null, "recent": []}"#).due)
        let body = try object(StaffPulseBody(date: "2026-09-26", rating: 4, note: nil))
        XCTAssertEqual(body["rating"] as? Int, 4)
        XCTAssertNil(body["note"])
        XCTAssertEqual(StaffPulseScale.ratings, [1, 2, 3, 4, 5])
    }

    func testTheCalendarLinkSubscribesThroughWebcal() throws {
        let link = try decode(StaffCalendarLink.self, """
        {"ok": true, "url": "https://app.example.com/staff/cal/abc.ics", "webcal_url": "webcal://app.example.com/staff/cal/abc.ics",
         "created_at": "2026-10-01 10:00:00", "last_fetched_at": null, "note": "Your published shifts."}
        """)
        XCTAssertEqual(link.webcalURL, "webcal://app.example.com/staff/cal/abc.ics")
        let derived = try decode(StaffCalendarLink.self, #"{"ok": true, "url": "https://x.test/staff/cal/t.ics"}"#)
        XCTAssertEqual(derived.webcalURL, "webcal://x.test/staff/cal/t.ics")
        XCTAssertThrowsError(try decode(StaffCalendarLink.self, #"{"ok": false, "error": "This isn't a staff account."}"#))
    }

    func testLanguagesDocsAndCitedAnswersDecode() throws {
        let langs = try decode(StaffLanguages.self, """
        {"ok": true, "language": "es", "languages": [{"code": "en", "name": "English"}, {"code": "es", "name": "Español"}]}
        """)
        XCTAssertEqual(langs.currentName, "Español")

        let docs = try decode(StaffDocsPayload.self, """
        {"ok": true, "house_rules": {"id": 1, "kind": "house_rules", "kind_label": "House rules", "title": "House rules",
                                     "body": "No phones on the floor.", "updated_at": "2026-09-01 10:00:00"},
         "docs": [{"id": 2, "kind": "allergens", "kind_label": "Allergens", "title": "Nut list", "body": "…", "updated_at": null}],
         "certifications": [{"cert": "food handler", "expires_on": "2026-10-20", "days_left": 18, "status": "expiring"}],
         "can_ask": true}
        """)
        XCTAssertEqual(docs.houseRules?.body, "No phones on the floor.")
        XCTAssertEqual(docs.certifications.first?.chip.tone, .warning)
        XCTAssertEqual(docs.certifications.first?.line, "Expires 10/20/26")
        XCTAssertTrue(docs.canAsk)

        let answer = try decode(StaffAskAnswer.self, """
        {"ok": true, "answered": true, "answer": "Phones stay in the back.", "reason": null, "suggest_message": false,
         "sources": [{"id": "S1", "source": "House rules", "kind": "house_rules", "line": "No phones on the floor."}]}
        """)
        XCTAssertTrue(answer.answered)
        XCTAssertEqual(answer.sources.first?.line, "No phones on the floor.")
        let refused = try decode(StaffAskAnswer.self, """
        {"ok": true, "answered": false, "answer": "Ask your manager — I can only answer from your restaurant's house rules and docs.",
         "reason": "pay", "suggest_message": true, "sources": []}
        """)
        XCTAssertTrue(refused.suggestMessage)
        let limited = try decode(StaffAskAnswer.self, #"{"ok": false, "error": "That's a lot of questions", "suggest_message": true}"#)
        XCTAssertTrue(limited.suggestMessage)
        XCTAssertEqual(limited.answer, "That's a lot of questions")
    }
}
