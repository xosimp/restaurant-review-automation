import XCTest
@testable import CavnarAI

/// The memory round's iOS part B (UI-IB, 9/29/26): every new payload the
/// Labor, Reviews, Marketing, Food Cost, Intel and What Connects screens
/// read decodes leniently — a missing field is nil, an unknown kind is kept,
/// a malformed entry is skipped — and the lines the screens print say what
/// the server meant (M/D/YY, "Not watched yet", "—" below a floor).
final class MemoryRoundIBTests: XCTestCase {
    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: Scheduling notes

    func testStaffNotesDecodeDatedPartsAndCountTheStaleOnes() throws {
        let p = try decode(StaffNotesPayload.self, """
        {"ok": true, "stale_after_days": 90, "stale": 1, "can_edit": true,
         "notes": [{"id": 4, "employee_name": "Luis R.", "notes": "mornings only; no doubles",
                    "noted": "9/2/26", "noted_on": "2026-09-02", "stale": true, "expires_on": null,
                    "parts": [{"index": 0, "text": "mornings only", "noted": "5/2/26", "noted_on": "2026-05-02",
                               "expires": null, "expires_on": null, "ended": false, "stale": true},
                              {"index": 1, "text": "Out until 6/1", "noted": "5/20/26", "noted_on": "2026-05-20",
                               "expires": "6/1/26", "expires_on": "2026-06-01", "ended": true, "stale": false},
                              {"text": "no index — skipped"}]},
                   {"employee_name": "no id — skipped"}]}
        """)
        XCTAssertTrue(p.ok)
        XCTAssertTrue(p.canEdit)
        XCTAssertEqual(p.staleAfterDays, 90)
        XCTAssertEqual(p.notes.count, 1)
        let note = try XCTUnwrap(p.notes.first)
        XCTAssertEqual(note.parts.count, 2)
        XCTAssertEqual(note.parts[0].dateLine, "noted 5/2/26")
        XCTAssertEqual(note.parts[1].dateLine, "noted 5/20/26 \u{00B7} ended 6/1/26")
        XCTAssertEqual(note.staleParts.map(\.index), [0])
    }

    func testANoteFromBeforePartsIsOneUndatedConstraint() throws {
        let note = try decode(StaffNote.self, #"{"id": 1, "employee_name": "Kim", "notes": "no closes"}"#)
        XCTAssertEqual(note.parts.map(\.text), ["no closes"])
        XCTAssertNil(note.parts[0].dateLine)
    }

    func testTheNudgeSaysWhatWaitsAndIsNilWhenNothingDoes() {
        XCTAssertNil(TeamMemoryViewModel.nudgeLine(stale: 0, days: 90, questions: 0, mentions: 0))
        XCTAssertEqual(TeamMemoryViewModel.nudgeLine(stale: 2, days: 90, questions: 1, mentions: 0),
                       "2 scheduling notes over 90 days old \u{2014} still true? \u{00B7} 1 person may be listed twice")
    }

    // MARK: Who is who, and guests naming the team

    func testIdentityQuestionsAskInTheOwnersWords() throws {
        let p = try decode(IdentityPayload.self, """
        {"ok": true, "can_answer": true, "questions": [
          {"id": 3, "kind": "similar_name", "reason": "Kim T. and Kim Tran share a first name and last initial",
           "a": {"person_id": 1, "name": "Kim T.", "key": "kim-t", "rated": true, "sources": ["upload"]},
           "b": {"person_id": 2, "name": "Kim Tran", "key": "kim-tran", "rated": false, "sources": ["toast"]},
           "asked": "9/28/26"},
          {"id": 4, "a": {"name": "no b"}}]}
        """)
        XCTAssertTrue(p.canAnswer)
        XCTAssertEqual(p.questions.count, 1)
        XCTAssertEqual(p.questions[0].question, "Is Kim T. the same person as Kim Tran?")
        XCTAssertEqual(p.questions[0].a.detail, "rated \u{00B7} from an upload")
        XCTAssertEqual(p.questions[0].b.detail, "from Toast")
    }

    func testMentionPolarityIsTheServersIntegerOrAWord() throws {
        let p = try decode(MentionsPayload.self, """
        {"ok": true, "can_confirm": true, "mentions": [
          {"id": 9, "name": "Maria Garcia", "key": "maria-garcia", "date": "9/20/26", "date_iso": "2026-09-20",
           "polarity": 1, "review_id": "412", "snippet": "Maria was amazing", "status": "proposed"},
          {"id": 10, "name": "Will Stone", "date_iso": "2026-09-21", "polarity": -1},
          {"id": 11, "name": "Old", "polarity": "negative"}]}
        """)
        XCTAssertEqual(p.mentions.map(\.isPraise), [true, false, false])
        XCTAssertEqual(p.mentions.map(\.isComplaint), [false, true, true])
        XCTAssertEqual(p.mentions[1].dateLabel, "9/21/26")
        XCTAssertEqual(p.mentions[0].reviewId, "412")
    }

    // MARK: The person record

    func testThePersonRecordSaysUnwatchedAttendanceIsUnknown() throws {
        let p = try decode(PersonRecord.self, """
        {"key": "ana-b", "name": "Ana B.", "role": "Server",
         "roles_held": [{"role": "bartender", "since": "2026-09-01", "primary": 1}, {"since": "no role"}],
         "covers": {"taken": 3, "declined": 1, "days": 180},
         "guest_mentions": [], "attendance": {"known": false}}
        """)
        XCTAssertTrue(p.hasMemory)
        XCTAssertEqual(p.attendance?.line, "Not watched yet")
        XCTAssertEqual(p.covers?.line, "Took 3 of 4 covers asked \u{00B7} 180 days")
        XCTAssertEqual(p.rolesHeld.map(\.line), ["Bartender \u{00B7} since 9/1/26 \u{00B7} their role on the roster"])
        let old = try decode(PersonRecord.self, #"{"key": "x", "name": "X"}"#)
        XCTAssertFalse(old.hasMemory, "an older server sends none — the section is left off")
    }

    func testWatchedAttendanceReadsItsMisses() {
        let a = PersonAttendance(known: true, shifts: 24, missed: 1, late: 2, lastMiss: "2026-09-03")
        XCTAssertEqual(a.line, "Missed 1 of 24 watched shifts \u{00B7} late 2 \u{00B7} last miss 9/3/26")
        XCTAssertEqual(PersonCovers(taken: 0, declined: 0, days: 180).line, "No covers asked yet \u{00B7} 180 days")
    }

    // MARK: Standing patterns and the drafted week

    func testStandingPatternsSayWhoTaughtThemAndConflictsWhoPulls() throws {
        let s = try decode(StandingPattern.self, """
        {"key": "p:dana:friday:night", "kind": "moved_off", "employee": "Dana", "day": "Friday", "daypart": "night",
         "text": "Dana is kept off Friday nights", "editors": {"Sam": 3, "Lee": 1},
         "first_learned": "9/7/26", "last_confirmed": "9/21/26", "times_applied": 4, "times_overridden": 0,
         "status": "active", "can_be_rule": true, "rule": null}
        """)
        XCTAssertEqual(s.historyLine, "learned 9/7/26 \u{00B7} last kept 9/21/26 \u{00B7} taught by Lee, Sam")
        XCTAssertEqual(s.statusLabel, "in use")
        XCTAssertTrue(s.canBeRule)
        let odd = try decode(StandingPattern.self, #"{"key": "k", "text": "t", "status": "paused_by_owner"}"#)
        XCTAssertEqual(odd.statusLabel, "paused by owner", "an unknown status reads as the server wrote it")
        let c = try decode(PatternConflict.self,
                           #"{"employee": "Dana", "day": "Friday", "daypart": "night", "off_by": ["Sam"], "on_by": ["Lee"]}"#)
        XCTAssertEqual(c.line, "Dana, Friday night: Sam takes them off, Lee puts them on")
    }

    func testTheDraftCarriesSoftRequirementsConflictsAndItsTarget() throws {
        let s = try decode(GeneratedSchedule.self, """
        {"ok": true, "labor_target_label": "your goal of 26% by 12/31/26", "labor_target_source": "goal",
         "soft_requirements": [{"source": "reviews", "day": "Friday", "date": "2026-10-02", "daypart": "night",
                                "role": "server", "text": "+1 server Friday dinner", "confirm": "wait times",
                                "applied": true, "scheduled": 5},
                               {"source": "dsr", "text": "Add a dishwasher Saturday", "applied": false,
                                "expires": "2026-10-10"},
                               {"source": "dsr"}],
         "pattern_conflicts": [{"employee": "Dana", "off_by": ["Sam"], "on_by": ["Lee"]}],
         "detail_thinned_at": "2026-10-30 04:00:00"}
        """)
        let reqs = try XCTUnwrap(s.softRequirements?.items)
        XCTAssertEqual(reqs.count, 2, "a requirement with no text is skipped, never a failed week")
        XCTAssertEqual(reqs.map(\.appliedLabel), ["Applied", "Not applied"])
        XCTAssertEqual(reqs.map(\.sourceLabel), ["Reviews", "Nightly report"])
        XCTAssertEqual(s.patternConflicts?.items.count, 1)
        XCTAssertEqual(ScheduleWeekNotes.targetLine(label: s.laborTargetLabel?.value, source: s.laborTargetSource?.value),
                       "Labor target: Your goal of 26% by 12/31/26.")
        XCTAssertEqual(s.detailThinnedAt?.value, "2026-10-30 04:00:00")
        let old = try decode(GeneratedSchedule.self, #"{"ok": true}"#)
        XCTAssertNil(old.softRequirements)
    }

    func testQuietKindsSayWhyAndWhenTheyAreRetested() throws {
        let m = try decode(SuppressionMap.self, """
        {"trim_day": {"state": "suppressed", "reason": "the last four went unanswered", "since": "2026-09-01 10:00:00",
                      "review_on": "10/1/26", "retests": 0}}
        """)
        XCTAssertEqual(m.items.first?.line,
                       "Trim Day \u{2014} quiet since 9/1/26: the last four went unanswered. Re-tested on 10/1/26.")
        XCTAssertTrue(try decode(SuppressionMap.self, "[1, 2]").items.isEmpty)
    }

    // MARK: Events, reviews, marketing

    func testAListedEventCarriesItsMeasuredRecord() throws {
        let e = try decode(EventMeasured.self, #"{"median_lift_pct": 14.2, "n": 4, "applies": true}"#)
        XCTAssertEqual(e.line, "Nights like this ran a median 14% above a typical same weekday (measured 4 times) "
                       + "\u{2014} before and after, not proof")
        let lesson = try decode(NightLesson.self, #"{"label": "game day", "n": 3, "applies": true, "text": "Game day: …"}"#)
        XCTAssertTrue(lesson.applies)
    }

    func testRequestConversionIsADashBelowItsFloor() throws {
        let c = try decode(RequestConversion.self, #"{"asked": 4, "reviewed": 1, "pct": null, "window_days": 14}"#)
        XCTAssertEqual(c.line, "1 of 4 guests you asked left a review within 14 days (\u{2014})")
        let d = try decode(RequestConversion.self, #"{"asked": 10, "reviewed": 3, "pct": 30.0, "window_days": 14}"#)
        XCTAssertEqual(d.line, "3 of 10 guests you asked left a review within 14 days (30%)")
    }

    func testReturnsBySegmentListBestFirst() throws {
        let map = try decode([String: SegmentReturn].self, """
        {"lapsed_60": {"label": "Haven't been in 60+ days", "back_per_100": 9, "campaigns": 2, "sent": 200, "came_back": 18},
         "all": {"label": "Everyone", "back_per_100": 1.5, "campaigns": 3}}
        """)
        let list = SegmentReturn.list(from: map)
        XCTAssertEqual(list.map(\.segment), ["lapsed_60", "all"])
        XCTAssertEqual(list[0].line, "Haven't been in 60+ days: 9 came back per 100 texted (2 campaigns)")
    }

    func testRetagVocabularyIsTheAnalysersWords() {
        XCTAssertEqual(ReviewTagVocabulary.categoryLabel("value"), "value for money")
        XCTAssertEqual(ReviewTagVocabulary.categoryLabel("brand_new_topic"), "brand new topic")
        XCTAssertEqual(ReviewTagVocabulary.severities.map(\.key), ["safety", "legal", "operational", "service", "minor"])
    }

    // MARK: Food Cost

    func testFoodCostCorrectionsDecodeAndSayWhy() throws {
        let item = try decode(SupplierOrderItem.self, """
        {"item": "Salmon", "unit": "lb", "qty": 8, "base_qty": 10,
         "owner_adjusted": {"factor": 0.8, "orders": 5, "basis": "your last 5 orders sent about 80% of what the draft suggested"}}
        """)
        XCTAssertEqual(item.adjustmentLine,
                       "Adjusted to how you order: your last 5 orders sent about 80% of what the draft suggested")
        let par = try decode(ParSuggestion.self, """
        {"ingredient_id": 7, "name": "Burrata", "par": 6, "suggested_par": 9, "times": 3, "last": "2026-09-26",
         "key": "raise_par:burrata"}
        """)
        XCTAssertEqual(par.title, "Raise the par on Burrata from 6 to 9")
        XCTAssertEqual(par.why, "Ran out 3 times in 4 weeks, last 9/26/26")
        let fix = try decode(ProjectionCorrection.self, #"{"factor": 0.89, "bias_pct": 12.0, "note": null}"#)
        XCTAssertEqual(fix.line, "already corrected: earlier projections ran 12% high")
    }

    func testARepriceKeepsItsGuardAndTheOwnersUsualChoice() throws {
        let r = try decode(RepriceSuggestions.self, """
        {"ok": true, "available": true, "suggestions": [
          {"dish": "Margherita", "sell_price": 18.0, "suggested_price": 22.0, "monthly_margin_lost": 300,
           "owner_ratio": {"ratio": 0.5, "decisions": 4, "basis": "…"}, "typical_price": 20.0, "typical_monthly": 150,
           "guard": {"kind": "reviews_x_menu", "text": "Guests are naming it in complaints — fix the plate first."},
           "value_note": "Guests called it poor value in 2 reviews in the last 90 days"},
          {"dish": "Carbonara", "guard": {"kind": "no text"}}]}
        """)
        let s = try XCTUnwrap(r.suggestions)
        XCTAssertEqual(s.count, 2, "an odd guard never drops the suggestion")
        XCTAssertEqual(s[0].typicalLine, "You usually raise about half of the suggested rise \u{2014} $20.00 would recover $150/mo")
        XCTAssertNotNil(s[0].repriceGuard)
        XCTAssertNil(s[1].repriceGuard)
    }

    func testAnInvoiceLineMatchedTheOwnersWaySaysSo() throws {
        let line = try decode(InvoiceLine.self, """
        {"index": 0, "description": "SALMON FILLET 10LB", "selected": true, "matched_by": "your_match"}
        """)
        XCTAssertEqual(line.matchNote, "Matched as you did last time")
    }

    // MARK: Intel and What Connects

    func testTheMarketsHistoryAndTheOwnRatingDecode() throws {
        let m = try decode(IntelMovement.self, """
        {"ok": true, "significant": [], "arrived": [], "gone": [],
         "market_history": [{"place_id": "a", "name": "Bella's", "kind": "arrived", "to_rating": 4.5, "observed_on": "2026-03-14"},
                            {"name": "Old Place", "kind": "rating_down", "from_rating": 4.4, "to_rating": 4.1,
                             "observed_on": "2026-05-02"},
                            {"name": "Mystery", "kind": "renamed"}],
         "own_rating_history": {"available": true, "change": 0.3, "weeks": 30,
                                "first": {"week": "2026-W10", "rating": 4.3}, "latest": {"week": "2026-W39", "rating": 4.6},
                                "series": [{"week": "2026-W10", "rating": 4.3}, {"week": "2026-W39", "rating": 4.6}]}}
        """)
        XCTAssertEqual(m.marketHistory.map(\.what), ["Arrived nearby at 4.5\u{2605}", "4.4\u{2605} \u{2192} 4.1\u{2605}", "Renamed"])
        XCTAssertEqual(m.marketHistory[0].dateLabel, "3/14/26")
        XCTAssertEqual(m.ownRatingHistory?.line, "4.3\u{2605} the week of 3/2/26 \u{2192} 4.6\u{2605} now (+0.3)")
        let older = try decode(IntelMovement.self, #"{"ok": true}"#)
        XCTAssertTrue(older.marketHistory.isEmpty)
        XCTAssertNil(older.ownRatingHistory)
    }

    func testALinksMemorySaysHowLongItHasStood() throws {
        let x = try decode(HomeFollowThroughViewModel.CrossModule.self, """
        {"ok": true, "links": [
          {"kind": "dsr_x_reviews", "headline": "Complaints land on the nights the report logs no-shows",
           "memory": {"first_seen": "2026-09-07", "weeks_running": 3, "recurring": true, "came_back": null,
                      "label": "Found 3 weeks running, since 9/7/26"}},
          {"kind": "a_kind_this_build_never_heard_of", "headline": "Still decodes",
           "memory": {"recurring": true, "came_back": {"on": "2026-09-20", "after": "done"},
                      "label": "Still found after you marked it done on 9/14/26"}}],
         "fix_first": {"key": "link:x", "what": "Staff Friday", "recurring": true,
                       "link_memory": {"weeks_running": 4, "recurring": true}}}
        """)
        let links = try XCTUnwrap(x.links)
        XCTAssertEqual(links.count, 2)
        XCTAssertEqual(links[0].memory?.line, "Found 3 weeks running, since 9/7/26")
        XCTAssertEqual(links[0].memory?.badge, "Recurring")
        XCTAssertEqual(links[1].memory?.badge, "Came back")
        XCTAssertEqual(x.fixFirst?.recurring, true)
    }

    func testAnISOWeekReadsAsTheMondayItStarts() {
        XCTAssertEqual(CavnarDate.isoWeekStart("2026-W10"), "3/2/26")
        XCTAssertEqual(CavnarDate.isoWeekStart("not a week"), "not a week")
    }
}
