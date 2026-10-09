import XCTest
@testable import CavnarAI

/// The staff app's Today screen and frame (employee audit wave 2, I2): the
/// shift payload as the backend fixes send it (B3 legs / posted / request
/// state / week_hours / upcoming, B8 section, B5 running late and inbox
/// counts, B6 earnings / stats / recognition, B7 the personal brief), and
/// the pure rules the screen draws from.
final class StaffTodayTests: XCTestCase {

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    /// Fixed clock and zone: 10/2/26 on a UTC calendar.
    private var utc: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }

    private func at(_ hour: Int, _ minute: Int = 0, day: Int = 2) -> Date {
        utc.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
    }

    // A double today (11–3 and 5–10:30, the second with a live drop), the
    // shape staff_schedule.shifts_for_employee sends since C6.
    private let doubleWeek = """
        {"ok": true, "published": true, "week_start": "2026-10-02", "week_of": "2026-09-28",
         "week_hours": 15.5,
         "today": {"date": "2026-10-02", "day": "Friday", "role": "Server", "start": "11:00am", "end": "3:00pm",
                   "shift_start": "11:00am", "shift_end": "3:00pm", "hours": 4, "notes": "", "break": "",
                   "request": null, "actions": ["drop", "swap"],
                   "legs": [
                     {"date": "2026-10-02", "role": "Server", "start": "11:00am", "end": "3:00pm",
                      "shift_start": "11:00am", "shift_end": "3:00pm", "hours": 4, "notes": "", "break": "",
                      "request": null, "actions": ["drop", "swap"]},
                     {"date": "2026-10-02", "role": "Server", "start": "5:00pm", "end": "10:30pm",
                      "shift_start": "5:00pm", "shift_end": "10:30pm", "hours": 5.5, "notes": "Bring your wine key",
                      "break": "7:30pm–8:00pm", "section": "Patio", "station": "Bar",
                      "request": {"id": 41, "kind": "drop", "status": "pending"}, "actions": []}]},
         "week": [
           {"date": "2026-10-02", "weekday": "Friday", "is_today": true, "off": false, "posted": true,
            "shift": {"date": "2026-10-02", "start": "11:00am", "end": "3:00pm", "hours": 4, "actions": ["drop", "swap"]},
            "shifts": [
              {"date": "2026-10-02", "role": "Server", "start": "11:00am", "end": "3:00pm", "hours": 4,
               "actions": ["drop", "swap"]},
              {"date": "2026-10-02", "role": "Server", "start": "5:00pm", "end": "10:30pm", "hours": 5.5,
               "section": "Patio", "break": "7:30pm–8:00pm", "notes": "Bring your wine key",
               "request": {"id": 41, "kind": "drop", "status": "pending"}, "actions": []}]},
           {"date": "2026-10-03", "weekday": "Saturday", "is_today": false, "off": false, "posted": true,
            "shift": {"date": "2026-10-03", "start": "4:00pm", "end": "10:00pm", "hours": 6, "role": "Server",
                      "request": {"id": 52, "kind": "swap", "status": "open"}, "actions": []},
            "shifts": [{"date": "2026-10-03", "start": "4:00pm", "end": "10:00pm", "hours": 6, "role": "Server",
                        "request": {"id": 52, "kind": "swap", "status": "open"}, "actions": []}]},
           {"date": "2026-10-04", "weekday": "Sunday", "is_today": false, "off": true, "posted": true,
            "shift": null, "shifts": []},
           {"date": "2026-10-05", "weekday": "Monday", "is_today": false, "off": true, "posted": false,
            "shift": null, "shifts": []},
           {"date": "2026-10-06", "weekday": "Tuesday", "is_today": false, "off": true, "posted": false,
            "shift": null, "shifts": []},
           {"date": "2026-10-07", "weekday": "Wednesday", "is_today": false, "off": true, "posted": false,
            "shift": null, "shifts": []},
           {"date": "2026-10-08", "weekday": "Thursday", "is_today": false, "off": true, "posted": false,
            "shift": null, "shifts": []}],
         "upcoming": [
           {"date": "2026-10-02", "date_iso": "2026-10-02", "start": "11:00am", "end": "3:00pm", "hours": 4},
           {"date": "2026-10-10", "date_iso": "2026-10-10", "day": "Saturday", "start": "4:00pm", "end": "11:00pm",
            "hours": 7, "role": "Server"}]}
        """

    // MARK: Decoding — legs, posted, request state, section (C6, WF-19, V12)

    func testEveryLegOfADoubleDecodesWithItsOwnRequestAndActions() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        XCTAssertEqual(r.weekHours, 15.5)
        let today = try XCTUnwrap(r.today)
        XCTAssertEqual(today.allLegs.count, 2)
        let evening = today.allLegs[1]
        XCTAssertEqual(evening.shiftStart, "5:00pm")
        XCTAssertEqual(evening.timeRange, "5pm – 10:30pm")
        XCTAssertEqual(evening.hours, 5.5)
        XCTAssertEqual(evening.section, "Patio")
        XCTAssertEqual(evening.breakLine, "Break 7:30pm – 8pm")
        XCTAssertEqual(evening.noteLine, "Bring your wine key")
        XCTAssertEqual(evening.request, StaffShiftRequestState(id: 41, kind: "drop", status: "pending"))
        XCTAssertFalse(evening.canDrop)
        XCTAssertFalse(evening.canSwap)
        XCTAssertTrue(today.allLegs[0].canDrop)

        let friday = try XCTUnwrap(r.week?.first)
        XCTAssertEqual(friday.legs.count, 2, "a double shows both halves, not the first leg alone (UX-01)")
        XCTAssertEqual(friday.legs[1].detailLine, "Server \u{00B7} Patio section \u{00B7} 5.5 hrs")
    }

    func testANotPostedDayIsNotAnOffDay() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        let week = try XCTUnwrap(r.week)
        XCTAssertTrue(week[2].isPosted)
        XCTAssertFalse(week[3].isPosted)
        XCTAssertTrue(StaffTodayPlan.accessibilityLabel(week[3]).contains("Not posted yet"))
        XCTAssertTrue(StaffTodayPlan.accessibilityLabel(week[2]).contains("Off"))
    }

    func testAnOlderPayloadWithoutLegsOrPostedStillReads() throws {
        let day = try decode(StaffWeekDay.self, """
            {"date": "2026-10-03", "weekday": "Saturday", "is_today": false, "off": false,
             "shift": {"shift_start": "4pm", "shift_end": "10pm", "scheduled_hours": "6", "role": "Server"}}
            """)
        XCTAssertTrue(day.isPosted)
        XCTAssertEqual(day.legs.count, 1)
        XCTAssertEqual(day.legs[0].hours, 6)
        XCTAssertTrue(day.legs[0].canDrop, "no `actions` from an older server: both are offered, as before")
        XCTAssertNil(day.legs[0].request)
    }

    func testFocusingADayOnOneLegIsWhatTheChangeSheetActsOn() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        let friday = try XCTUnwrap(r.week?.first)
        let evening = friday.legs[1]
        let focused = friday.focused(on: evening)
        XCTAssertEqual(focused.shift?.shiftStart, "5:00pm")
        XCTAssertEqual(focused.legs.count, 1)
        XCTAssertEqual(focused.date, friday.date)
    }

    func testRequestStateChipsSayWhatEachStateMeans() {
        let drop = StaffShiftRequestState(id: 1, kind: "drop", status: "pending")
        XCTAssertEqual(drop.chipLabel, "Giving up")
        XCTAssertFalse(drop.isWatch)
        let swap = StaffShiftRequestState(id: 2, kind: "swap", status: "approved")
        XCTAssertEqual(swap.chipLabel, "Swap")
        XCTAssertTrue(swap.detail.contains("Waiting on your colleague"))
        let open = StaffShiftRequestState(id: 3, kind: "drop", status: "open")
        XCTAssertEqual(open.chipLabel, "Open")
        XCTAssertTrue(open.isWatch)
        XCTAssertTrue(open.detail.contains("still on it"), "an approved drop is still theirs (C8)")
        XCTAssertTrue(StaffShiftRequestState(id: 4, kind: "post", status: "open").detail.contains("posted"))
    }

    func testTheShiftsPayloadRoundTripsThroughTheDeviceCache() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        let data = try JSONEncoder().encode(r)
        let back = try JSONDecoder().decode(StaffShiftsResponse.self, from: data)
        XCTAssertEqual(back.week, r.week)
        XCTAssertEqual(back.today, r.today)
        XCTAssertEqual(back.weekHours, 15.5)
        XCTAssertEqual(back.upcoming?.last?.dateISO, "2026-10-10")
    }

    // MARK: The hero (V1, UX-09)

    func testTheHeroSkipsAnEndedLegAndShowsTheWholeDay() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        let hero = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(14, 45), calendar: utc))
        XCTAssertEqual(hero.day.date, "2026-10-02")
        XCTAssertEqual(hero.day.legs.count, 2, "every leg of the double is on the card")
        XCTAssertEqual(hero.nextLeg.shiftStart, "11:00am", "the lunch leg is still on until 3pm")
        XCTAssertEqual(hero.relative, "On now \u{00B7} until 3pm")
        XCTAssertTrue(hero.canRunLate)

        let afternoon = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(15, 30), calendar: utc))
        XCTAssertEqual(afternoon.nextLeg.shiftStart, "5:00pm")
        XCTAssertEqual(afternoon.title, "Tonight")
        XCTAssertEqual(afternoon.relative, "Starts in 1h 30m")
    }

    func testOnceTodayIsDoneTheHeroIsTomorrowAndRunningLateIsGone() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        let hero = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(23), calendar: utc))
        XCTAssertEqual(hero.day.date, "2026-10-03")
        XCTAssertEqual(hero.title, "Tomorrow")
        XCTAssertEqual(hero.relative, "",
                       "past 12 hours there is no relative line: the title, date and times say when (re-audit M6)")
        XCTAssertFalse(hero.canRunLate, "running late is for today's shift only (H1)")

        let morning = try XCTUnwrap(StaffTodayPlan.hero(week: Array(week.dropFirst()), now: at(2), calendar: utc))
        XCTAssertEqual(morning.relative, "")
        let within = try XCTUnwrap(StaffTodayPlan.hero(week: Array(week.dropFirst()), now: at(5, day: 3), calendar: utc))
        XCTAssertEqual(within.relative, "Starts in 11h")
    }

    func testNoShiftInTheWeekPointsPastIt() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        let offWeek = Array(r.week!.dropFirst(2))
        XCTAssertNil(StaffTodayPlan.hero(week: offWeek, now: at(9), calendar: utc))
        XCTAssertEqual(StaffTodayPlan.nextBeyondWeek(upcoming: r.upcoming!, week: r.week!), "Next on 10/10/26 at 4pm")
    }

    func testLaterRowsAreOnlyThePastTheSevenDays() throws {
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        let later = StaffTodayPlan.later(upcoming: r.upcoming ?? [], week: r.week ?? [])
        XCTAssertEqual(later.map(\.date), ["2026-10-10"])
        XCTAssertEqual(later.first?.legs.first?.timeRange, "4pm – 11pm")
    }

    func testTheOldClockFreeNextShiftStillReadsEveryLeg() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        let next = try XCTUnwrap(StaffPortalView.nextShift(week))
        XCTAssertEqual(next.when, "Today")
    }

    // MARK: Times

    func testTimesReadTheHouseWay() {
        XCTAssertEqual(StaffTime.label("4:00pm"), "4pm")
        XCTAssertEqual(StaffTime.label("16:30"), "4:30pm")
        XCTAssertEqual(StaffTime.label("12:00 AM"), "12am")
        XCTAssertEqual(StaffTime.label("11:15am"), "11:15am")
        XCTAssertEqual(StaffTime.label("soon"), "soon", "unreadable text is shown as given")
        XCTAssertEqual(StaffTime.minutes("10:30pm"), 22 * 60 + 30)
        XCTAssertNil(StaffTime.minutes("13pm"))
        XCTAssertEqual(StaffTime.hoursLabel(6), "6 hrs")
        XCTAssertEqual(StaffTime.hoursLabel(1), "1 hr")
        XCTAssertEqual(StaffTime.hoursLabel(6.25), "6.3 hrs")
        XCTAssertNil(StaffTime.hoursLabel(0), "unknown is never shown as zero")
        XCTAssertNil(StaffTime.hoursLabel(nil))
    }

    func testAShiftPastMidnightEndsTheNextDay() throws {
        let span = try XCTUnwrap(StaffTime.span(dayISO: "2026-10-02", start: "6pm", end: "1:00am", calendar: utc))
        XCTAssertEqual(span.end, at(1, day: 3))
    }

    // MARK: Waiting on you, inbox

    func testWaitingOnYouCountsAsksAndOffersAndSaysTheFirst() throws {
        let w = try decode(StaffWaitingResponse.self, """
            {"ok": true, "requests": [{"id": 9}], "open": [{"id": 3}],
             "asks": [{"id": 7, "kind": "swap", "date": "2026-10-01", "shift_start": "11:00am", "employee_name": "Ana R",
                       "target_name": "Jordan", "target_date": "2026-10-02", "target_start": "4:00pm",
                       "manager_approved": false}],
             "offers": [{"id": 12, "request_id": 30, "status": "offered", "date": "2026-10-04",
                         "shift_start": "5:00pm", "role": "Server", "employee_name": null, "for_coverage": true}]}
            """)
        XCTAssertEqual(w.count, 2, "open shifts and my own requests don't count — only what waits on me")
        XCTAssertEqual(StaffWaiting.sentence(w),
                       "Ana R asked to swap their Thu 10/1/26 11am for your Fri 10/2/26 4pm. And 1 more.")
        XCTAssertNil(StaffWaiting.sentence(StaffWaitingResponse(ok: true, asks: [], offers: [])))
    }

    func testTheInboxBadgeIsUnreadPlusUnreadMessages() throws {
        let b = try decode(StaffInboxBadge.self, #"{"ok": true, "announcements": [], "unread": 2, "unread_messages": 1}"#)
        XCTAssertEqual(b.total, 3)
        XCTAssertEqual(try decode(StaffInboxBadge.self, #"{"ok": true}"#).total, 0)
    }

    // MARK: Running late (H1), who's on (H7)

    func testRunningLateDecodesAndTheBodyUsesTheServersKeys() throws {
        let list = try decode(StaffRunningLateList.self, """
            {"ok": true, "eta_choices": [10, 20, 30, 45], "eta_max": 120,
             "reports": [{"id": 1, "date": "2026-10-02", "shift_start": "5:00pm", "role": "Server", "eta_minutes": 20,
                          "note": "bus", "reported_at": "2026-10-02T21:50:00Z", "updated_at": "2026-10-02T21:50:00Z",
                          "managers_told": true}]}
            """)
        XCTAssertEqual(list.etaChoices, [10, 20, 30, 45])
        XCTAssertEqual(list.reports?.first?.etaMinutes, 20)
        let body = try JSONSerialization.jsonObject(with: JSONEncoder().encode(
            StaffRunningLateBody(date: "2026-10-02", shiftStart: "5:00pm", etaMinutes: 20, note: nil))) as? [String: Any]
        XCTAssertEqual(body?["shift_start"] as? String, "5:00pm")
        XCTAssertEqual(body?["eta_minutes"] as? Int, 20)
        let result = try decode(StaffRunningLateResult.self, """
            {"ok": true, "created": false, "managers_told": false,
             "report": {"id": 1, "date": "2026-10-02", "shift_start": "5:00pm", "eta_minutes": 30}}
            """)
        XCTAssertEqual(result.created, false)
        XCTAssertEqual(result.report?.etaMinutes, 30)
    }

    func testWhosOnWithMeDecodes() throws {
        let r = try decode(StaffCoworkersResponse.self, """
            {"ok": true, "date": "2026-10-02", "posted": true, "colleagues": [],
             "coworkers": [{"name": "Ana R", "role": "Server", "station": null, "shift_start": "4:00pm",
                            "shift_end": "10:00pm"}]}
            """)
        XCTAssertEqual(r.coworkers?.first?.timeRange, "4pm – 10pm")
        XCTAssertEqual(r.posted, true)
    }

    // MARK: Earnings, stats, recognition (B6)

    func testEarningsShowTheNewestFinishedShiftAndNeverInventTips() throws {
        let e = try decode(StaffEarnings.self, """
            {"ok": true, "available": true, "reason": null, "message": null, "provider": "rpower", "days": 14,
             "as_of": "2026-10-01", "as_of_label": "10/1/26", "lag_note": "A day behind.", "tips_note": "POS tips.",
             "shifts": [{"business_date": "2026-10-02", "date_label": "10/2/26", "weekday": "Friday",
                         "still_open": true, "hours": 2, "overtime_hours": 0, "tips_total": 0, "tip_net": 0, "grats": 0},
                        {"business_date": "2026-10-01", "date_label": "10/1/26", "weekday": "Thursday",
                         "role": "Server", "still_open": false, "hours": 6.5, "overtime_hours": 0,
                         "tips_total": 186.5, "tip_net": 180, "grats": 0}],
             "totals": {"shifts": 2, "hours": 8.5, "tips_total": 186.5, "tip_net": 180, "grats": 0}}
            """)
        XCTAssertEqual(e.lastShift?.businessDate, "2026-10-01")
        XCTAssertEqual(StaffMoney.label(e.lastShift?.tipsTotal ?? 0), "$186.50")
        XCTAssertEqual(StaffMoney.label(186), "$186")
        let none = try decode(StaffEarnings.self, """
            {"ok": true, "available": false, "reason": "pos_not_connected",
             "message": "Your restaurant's POS isn't connected.", "shifts": []}
            """)
        XCTAssertNil(none.lastShift, "no POS is said, never shown as $0")
        XCTAssertNil(none.missingTipsLine, "no POS here: nothing to say on Today (re-audit M1)")
        XCTAssertNil(e.missingTipsLine, "a tile is showing")

        // Re-audit M2: a cook's punches carry 0 tips every day — no tile.
        let cook = try decode(StaffEarnings.self, """
            {"ok": true, "available": true, "lag_note": "A day behind.",
             "shifts": [{"business_date": "2026-10-01", "date_label": "10/1/26", "still_open": false, "tips_total": 0},
                        {"business_date": "2026-09-30", "date_label": "9/30/26", "still_open": false, "tips_total": 0}]}
            """)
        XCTAssertNil(cook.lastShift)
        XCTAssertNil(cook.missingTipsLine, "punches with no tips are not a POS lag")
        let nothingYet = try decode(StaffEarnings.self, """
            {"ok": true, "available": true, "lag_note": "A day behind.", "shifts": []}
            """)
        XCTAssertEqual(nothingYet.missingTipsLine, "A day behind.")
        let ambiguous = try decode(StaffEarnings.self, """
            {"ok": true, "available": false, "reason": "ambiguous", "message": "Two people have your name.",
             "lag_note": "A day behind.", "shifts": []}
            """)
        XCTAssertEqual(ambiguous.missingTipsLine, "Two people have your name.")
    }

    func testStatsAreHoursAndTheOvertimeHeadsUpOnlyWhenItApplies() throws {
        let s = try decode(StaffStats.self, """
            {"ok": true, "today": "2026-10-02", "week": {"start": "2026-09-28", "end": "2026-10-04", "label": "9/28/26 – 10/4/26"},
             "scheduled": {"hours": 32.5, "shifts": 5},
             "actual": {"available": true, "hours": 18.5, "as_of": "2026-10-01", "as_of_label": "10/1/26", "note": null},
             "overtime": {"applies": true, "line_hours": 40, "projected_hours": 36, "headroom_hours": 4, "over": false,
                          "message": "A pickup longer than 4h takes you past 40 hours this week."},
             "attendance": {"tracked": false}, "certifications": {"items": []}}
            """)
        XCTAssertEqual(StaffTodayPlan.weekHours(stats: s, shifts: nil)?.hours, 32.5)
        XCTAssertEqual(StaffTodayPlan.workedLine(s), "18.5 worked through 10/1/26")
        XCTAssertEqual(s.overtimeLine, "A pickup longer than 4h takes you past 40 hours this week.")
        let salaried = try decode(StaffStats.self, #"{"ok": true, "overtime": {"applies": false, "message": "x"}}"#)
        XCTAssertNil(salaried.overtimeLine)
        let shifts = try decode(StaffShiftsResponse.self, doubleWeek)
        XCTAssertEqual(StaffTodayPlan.weekHours(stats: nil, shifts: shifts)?.label, "Hours, next 7 days")
    }

    func testRecognitionDecodesWithAndWithoutAnExcerpt() throws {
        let r = try decode(StaffRecognition.self, """
            {"ok": true, "count": 2, "days": 365,
             "items": [{"id": 4, "date": "2026-09-20", "date_label": "9/20/26", "excerpt": "Jordan was wonderful."},
                       {"id": 3, "date": "2026-08-01", "date_label": "8/1/26", "excerpt": null}]}
            """)
        XCTAssertEqual(r.items?.first?.excerpt, "Jordan was wonderful.")
        XCTAssertNil(r.items?.last?.excerpt)
    }

    // MARK: The personal brief (B7, UX-36)

    func testTheBriefShowsOnlyOnAWorkingDayAndLeavesTheHerosLinesOut() throws {
        let on = try decode(StaffPersonalBrief.self, """
            {"ok": true, "day": "2026-10-02", "working": true, "published": true,
             "you": [{"kind": "you", "text": "You're on as Server, 5pm–10:30pm."}],
             "items": [{"kind": "you", "text": "You're on as Server, 5pm–10:30pm."},
                       {"kind": "station", "text": "Your station: Bar."},
                       {"kind": "rush", "text": "Busiest around 6–8pm on a usual Friday."}],
             "brief_text": null, "focus": {"item": "Short rib", "line": null}}
            """)
        XCTAssertTrue(on.shouldShow)
        XCTAssertEqual(on.dayItems.map(\.kind), ["rush"])
        let off = try decode(StaffPersonalBrief.self, """
            {"ok": true, "working": false, "items": [], "you": [], "brief_text": null, "focus": null,
             "message": "You're off today."}
            """)
        XCTAssertFalse(off.shouldShow, "never on a day off")
        let unknown = try decode(StaffPersonalBrief.self, """
            {"ok": true, "working": null, "items": [{"kind": "weather", "text": "Rain after 6pm."}]}
            """)
        XCTAssertTrue(unknown.shouldShow, "no published schedule: the server can't tell, the card shows")
    }

    // MARK: Freshness (H6) and the cache

    func testTheStampSaysUpdatedOrAsOf() {
        XCTAssertEqual(StaffFreshness.stamp(at: at(15, 42), fromCache: false, now: at(15, 50), calendar: utc),
                       "Updated 3:42pm")
        XCTAssertEqual(StaffFreshness.stamp(at: at(15), fromCache: true, now: at(18), calendar: utc),
                       "As of 3pm \u{00B7} saved on this phone")
        XCTAssertEqual(StaffFreshness.stamp(at: at(15, 42, day: 1), fromCache: true, now: at(9), calendar: utc),
                       "As of 10/1/26 \u{00B7} 3:42pm \u{00B7} saved on this phone")
        XCTAssertNil(StaffFreshness.stamp(at: nil, fromCache: false))
    }

    func testTheCacheKeepsOneEmployeesCopyApartFromAnothers() throws {
        let ana = StaffCache.memberScope(restaurant: "Test Bistro \(UUID())", name: "Ana R")
        let ben = StaffCache.memberScope(restaurant: "Test Bistro \(UUID())", name: "Ben T")
        XCTAssertNotEqual(ana, ben)
        let r = try decode(StaffShiftsResponse.self, doubleWeek)
        StaffCache.save(r, key: "shifts", scope: ana)
        XCTAssertEqual(StaffCache.load(StaffShiftsResponse.self, key: "shifts", scope: ana)?.value.weekHours, 15.5)
        XCTAssertNil(StaffCache.load(StaffShiftsResponse.self, key: "shifts", scope: ben), "never another person's week")

        let session = StaffCache.sessionScope(token: "tok-\(UUID())")
        XCTAssertNil(StaffCache.memberScope(forSession: session))
        StaffCache.link(session: session, to: ana)
        XCTAssertEqual(StaffCache.memberScope(forSession: session), ana)

        StaffCache.purge(scope: ana)
        XCTAssertNil(StaffCache.load(StaffShiftsResponse.self, key: "shifts", scope: ana))
        XCTAssertNil(StaffCache.memberScope(forSession: session))
    }

    // MARK: Sections never read a failure as empty (C7)

    func testASectionWithAFailureAndNoValueIsFailedNotEmpty() {
        var s = StaffSection<StaffShiftsResponse>()
        XCTAssertEqual(s.phase, .loading)
        s.error = "That didn't load."
        XCTAssertEqual(s.phase, .failed)
        XCTAssertFalse(s.isStale)
        s.value = StaffShiftsResponse(ok: true, published: true, today: nil, week: [])
        XCTAssertEqual(s.phase, .ready)
        XCTAssertTrue(s.isStale, "a refresh that failed over an older copy says so")
    }

    // MARK: The frame — deep links land on their tab

    @MainActor
    func testAnInboxLinkOpensTheInboxOverTodayAndATabLinkSelectsIt() throws {
        let store = StaffPortalStore()
        store.apply(try XCTUnwrap(StaffDeepLink.from(nav: "staff/inbox/5")))
        XCTAssertEqual(store.selectedTab, .today, "the inbox is a sheet, never a fifth tab")
        XCTAssertTrue(store.showingInbox)
        XCTAssertEqual(store.focus?.itemID, 5)

        store.showingInbox = false
        store.apply(try XCTUnwrap(StaffDeepLink.from(nav: "staff/requests/41")))
        XCTAssertEqual(store.selectedTab, .requests)
        XCTAssertFalse(store.showingInbox)
        store.consumeFocus()
        XCTAssertNil(store.focus)
        XCTAssertEqual(StaffTab.bar, [.today, .tasks, .requests, .me])
    }

    @MainActor
    func testTheBadgesCountWhatWaitsAndWhatIsUnread() {
        let store = StaffPortalStore()
        XCTAssertEqual(store.requestsBadge, 0)
        store.waiting.value = StaffWaitingResponse(ok: true, asks: [], offers: [])
        store.inbox.value = StaffInboxBadge(ok: true, unread: 1, unreadMessages: 2)
        XCTAssertEqual(store.requestsBadge, 0)
        XCTAssertEqual(store.inboxBadge, 3)
    }

    // MARK: iOS readability round (10/8/26) — the hero's one primary, the
    // week's one line, the manager strip, the folded checklist

    func testTheHerosPrimaryFollowsTheMomentInTheDay() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        // Two hours before the 11am leg: nothing is primary yet — Running
        // late waits for the hour before (re-audit H3), quiet till then.
        let early = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(9), calendar: utc))
        XCTAssertEqual(StaffTodayPlan.heroPrimary(early, now: at(9), hasTasks: true, calendar: utc), .none)
        XCTAssertTrue(StaffTodayPlan.offersRunningLate(early, now: at(9), calendar: utc))
        // Forty-five minutes before: Running late.
        let hourBefore = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(10, 15), calendar: utc))
        XCTAssertEqual(StaffTodayPlan.heroPrimary(hourBefore, now: at(10, 15), hasTasks: true, calendar: utc), .late)
        // Within 30 minutes of it: Tasks, when there are any…
        let soon = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(10, 45), calendar: utc))
        XCTAssertEqual(StaffTodayPlan.heroPrimary(soon, now: at(10, 45), hasTasks: true, calendar: utc), .tasks)
        // …else still Running late, since it hasn't started.
        XCTAssertEqual(StaffTodayPlan.heroPrimary(soon, now: at(10, 45), hasTasks: false, calendar: utc), .late)
        // On shift: Tasks; with none, nothing is primary.
        let on = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(12), calendar: utc))
        XCTAssertEqual(StaffTodayPlan.heroPrimary(on, now: at(12), hasTasks: true, calendar: utc), .tasks)
        XCTAssertEqual(StaffTodayPlan.heroPrimary(on, now: at(12), hasTasks: false, calendar: utc), .none)
        // Running late leaves the quiet row 15 minutes into the leg.
        let justStarted = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(11, 10), calendar: utc))
        XCTAssertTrue(StaffTodayPlan.offersRunningLate(justStarted, now: at(11, 10), calendar: utc))
        XCTAssertFalse(StaffTodayPlan.offersRunningLate(on, now: at(12), calendar: utc))
        // Not today: nothing is primary.
        let tomorrow = try XCTUnwrap(StaffTodayPlan.hero(week: week, now: at(23), calendar: utc))
        XCTAssertFalse(tomorrow.day.isToday)
        XCTAssertEqual(StaffTodayPlan.heroPrimary(tomorrow, now: at(23), hasTasks: true, calendar: utc), .none)
    }

    func testEachDayOfTheWeekIsOneLine() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        XCTAssertEqual(StaffTodayPlan.dayLine(week[0]), "11am – 3pm + 5pm – 10:30pm \u{00B7} Server")
        XCTAssertEqual(StaffTodayPlan.dayLine(week[1]), "4pm – 10pm \u{00B7} Server")
        XCTAssertEqual(StaffTodayPlan.dayLine(week[2]), "Off")
        XCTAssertEqual(StaffTodayPlan.dayLine(week[3]), "Not posted yet")
        XCTAssertTrue(StaffTodayPlan.dayHasDetail(week[1]), "the swap request opens on a tap")
        XCTAssertFalse(StaffTodayPlan.dayHasDetail(week[2]))
    }

    func testTheManagerStripIsOnlyUrgentNotesAndReplies() throws {
        let urgent = try decode(StaffInboxBadge.self, """
            {"ok": true, "unread": 2, "unread_messages": 0, "announcements": [
              {"id": 1, "title": "Walk-in is down", "priority": "urgent", "acked_at": null},
              {"id": 2, "title": "New menu", "priority": "normal", "acked_at": null},
              {"id": 3, "title": "Old", "priority": "urgent", "acked_at": "2026-10-01T09:00:00"}]}
            """)
        let u = try XCTUnwrap(StaffManagerStrip.content(urgent))
        XCTAssertEqual(u.text, "Urgent: Walk-in is down")
        XCTAssertTrue(u.urgent)
        XCTAssertFalse(u.opensThread)

        let replies = try decode(StaffInboxBadge.self, #"{"ok": true, "unread": 1, "unread_messages": 2, "announcements": [{"id": 2, "priority": "normal"}]}"#)
        let r = try XCTUnwrap(StaffManagerStrip.content(replies))
        XCTAssertEqual(r.text, "2 new replies from your manager")
        XCTAssertTrue(r.opensThread)

        // An ordinary unread announcement stays behind the tray.
        XCTAssertNil(StaffManagerStrip.content(try decode(StaffInboxBadge.self,
            #"{"ok": true, "unread": 1, "unread_messages": 0, "announcements": [{"id": 2, "priority": "normal"}]}"#)))
        // A list that won't read never costs the counts.
        let odd = try decode(StaffInboxBadge.self, #"{"ok": true, "unread": 4, "unread_messages": 1, "announcements": "x"}"#)
        XCTAssertEqual(odd.total, 5)
        XCTAssertTrue(odd.urgentUnread.isEmpty)
    }

    func testTheCachedWeekSaysOfflineAtTheTop() {
        XCTAssertEqual(StaffFreshness.warning(at: at(15, 42), offline: true, now: at(16), calendar: utc),
                       "As of 3:42pm \u{00B7} offline")
        XCTAssertEqual(StaffFreshness.warning(at: at(15), offline: false, now: at(16), calendar: utc),
                       "As of 3pm \u{00B7} couldn\u{2019}t refresh")
        XCTAssertNil(StaffFreshness.warning(at: nil, offline: true))
    }

    func testOverlappingCoworkersComeFirst() throws {
        let week = try XCTUnwrap(try decode(StaffShiftsResponse.self, doubleWeek).week)
        let people = try decode([StaffCoworker].self, """
            [{"name": "Ana", "shift_start": "6:00am", "shift_end": "10:00am"},
             {"name": "Bo", "shift_start": "2:00pm", "shift_end": "6:00pm"},
             {"name": "Cy"},
             {"name": "Di", "shift_start": "9:00pm", "shift_end": "1:00am"}]
            """)
        XCTAssertEqual(StaffTodayPlan.coworkersByOverlap(people, legs: week[0].legs).map(\.name), ["Bo", "Di", "Ana", "Cy"])
    }

    func testAChecklistFoldsItsDoneLinesBySection() throws {
        let sheet = try JSONDecoder().decode(StaffSheet.self, from: Data("""
            {"id": 9, "title": "Server opening", "shift_kind": "opening", "assignees": [], "unassigned": false,
             "status": "open", "done": 2, "total": 4, "lines": [
               {"line_id": 1, "label": "Roll silverware", "section": "Floor", "done": true},
               {"line_id": 2, "label": "Wipe menus", "section": "Floor", "done": false, "overdue": true},
               {"line_id": 3, "label": "Ice the well", "section": "Bar", "done": true},
               {"line_id": 4, "label": "Cut fruit", "section": "Bar", "done": false}]}
            """.utf8))
        let groups = StaffSheetProgress.groups(sheet.lines)
        XCTAssertEqual(groups.map(\.section), ["Floor", "Bar"])
        XCTAssertEqual(groups.map(\.doneCount), [1, 1])
        XCTAssertEqual(StaffSheetProgress.next(in: sheet)?.label, "Wipe menus")
        XCTAssertEqual(StaffSheetProgress.overdue([sheet]), 1)
        XCTAssertEqual(StaffSheetProgress.totals([sheet])?.done, 2)
        XCTAssertEqual(StaffSheetProgress.totals([sheet])?.total, 4)
        var closed = sheet
        closed.status = "closed"
        XCTAssertEqual(StaffSheetProgress.overdue([closed]), 0, "a closed sheet asks nothing of anyone")
        XCTAssertNil(StaffSheetProgress.totals([]))
    }

    func testAttendanceSaysItselfInOneLine() throws {
        let a = try decode(StaffAttendance.self, """
            {"tracked": true, "days": 30, "shifts_checked": 4, "recent": [
              {"date": "2026-10-01", "outcome": "on_time"}, {"date": "2026-09-30", "outcome": "late", "minutes_late": 12},
              {"date": "2026-09-29", "outcome": "on_time"}, {"date": "2026-09-28", "outcome": "on_time"}]}
            """)
        XCTAssertEqual(a.summaryLine, "4 shifts in 30 days \u{00B7} 3 on time \u{00B7} 1 late")
    }
}
