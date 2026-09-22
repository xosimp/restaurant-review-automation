import XCTest
@testable import CavnarAI

/// The generated-schedule payload gained a review layer (rules check,
/// per-assignment explanations, versions, the publish gate). Every new
/// key is optional so a payload cached on the device before the layer
/// existed still decodes — the on-device cache is the first thing the
/// Labor tab shows after a relaunch, and a decode failure there reads as
/// "the schedule keeps disappearing".
final class ScheduleReviewDecodingTests: XCTestCase {
    private static let fullJSON = """
    {"ok": true, "status": "done", "history_id": 91,
     "week_dates": ["2026-09-28", "2026-10-04"],
     "hours_scheduled": 212.5, "hours_budget": 220.0,
     "summary": ["+12h this week (200h → 212h).", "Saturday Server 4:00pm: Ana → Bob."],
     "narrative": "Kept Saturday night heavy for the two parties on the books.",
     "generation_seconds": 71.4, "chunked": true,
     "roster": ["Ana", "Bob", "Cara"], "roster_roles": {"Ana": "Server"},
     "pending_time_off": {"Cara": ["2026-10-02", "2026-10-03"]},
     "review": {"hard": 1, "soft": 2, "by_kind": {"time_off": 1, "rest": 2},
                "lines": ["⚠ Ana — Tuesday 4:00pm: approved time off", "Bob — 9h rest after Monday close"],
                "hard_rows": [0],
                "fixes": [{"index": 0, "from": "Ana", "to": "Cara", "kind": "time_off", "reason": "Cara is free and rated 4"}],
                "unfixed": [{"index": 3, "employee": "Bob", "reason": "nobody legal is free"}]},
     "rule_violations": [{"kind": "time_off", "index": 0, "employee": "Ana", "date": "2026-09-29",
                          "day": "Tuesday", "shift_start": "4:00pm", "role": "Server",
                          "detail": "approved time off", "hard": true, "no_show": false, "label": "Time off"}],
     "preview_rows": [{"date": "2026-09-29", "day": "Tuesday", "employee": "Ana", "role": "Server",
                       "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6",
                       "needs_review": true, "review_reason": "approved time off"},
                      {"date": "2026-09-29", "day": "Tuesday", "employee": "Bob", "role": "Bartender",
                       "shift_start": "10:00am", "shift_end": "3:00pm", "scheduled_hours": "5"}],
     "quality": {"checked": true, "score": 78, "band": "solid",
                 "shifts": [{"date": "2026-09-29", "day": "Tuesday", "daypart": "night", "scored": true,
                             "score": 74, "band": "solid",
                             "profile": {"key": "tue_dinner", "label": "Weeknight dinner", "demand": "normal"},
                             "assignments": [{"employee": "Ana", "role": "Server", "date": "2026-09-29",
                                              "day": "Tuesday", "daypart": "night",
                                              "why": "Level 4 server (strong); usually works Tuesday nights; 28h this week.",
                                              "facts": {"score": 4, "usual_nights": ["Tuesday", "Friday"],
                                                        "can_close": true, "hours_this_week": 28.5}}]}]}}
    """

    /// The shape a payload cached before the review layer existed has —
    /// none of the new keys, and it must still decode.
    private static let legacyJSON = """
    {"ok": true, "status": "done", "week_dates": ["2026-08-24", "2026-08-30"],
     "hours_scheduled": 1293.9, "summary": ["Added servers daily."],
     "preview_rows": [{"date": "2026-08-24", "day": "Monday", "employee": "Amy C.", "role": "Prep Cook",
                       "shift_start": "8:30am", "shift_end": "3:30pm", "scheduled_hours": "7"}]}
    """

    func testDecodesEveryNewKeyOnTheGeneratedSchedule() throws {
        let schedule = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.fullJSON.utf8))

        XCTAssertEqual(schedule.historyId, 91)
        XCTAssertEqual(schedule.narrative, "Kept Saturday night heavy for the two parties on the books.")
        XCTAssertEqual(schedule.generationSeconds, 71.4)
        XCTAssertEqual(schedule.chunked, true)
        XCTAssertEqual(schedule.roster, ["Ana", "Bob", "Cara"])
        XCTAssertEqual(schedule.pendingTimeOff?["Cara"], ["2026-10-02", "2026-10-03"])

        let review = try XCTUnwrap(schedule.review)
        XCTAssertEqual(review.hardCount, 1)
        XCTAssertEqual(review.softCount, 2)
        XCTAssertEqual(review.byKind?["rest"], 2)
        XCTAssertEqual(review.lines?.first, "⚠ Ana — Tuesday 4:00pm: approved time off")
        XCTAssertEqual(review.hardRows, [0])
        XCTAssertEqual(review.fixes?.first?.to, "Cara")
        XCTAssertEqual(review.unfixed?.first?.employee, "Bob")
        XCTAssertFalse(review.isClean)

        let violation = try XCTUnwrap(schedule.ruleViolations?.first)
        XCTAssertEqual(violation.kind, "time_off")
        XCTAssertTrue(violation.isHard)
        XCTAssertEqual(violation.noShow, false)

        let flagged = try XCTUnwrap(schedule.previewRows?.first)
        XCTAssertEqual(flagged.needsReview, true)
        XCTAssertEqual(flagged.reviewReason, "approved time off")
        XCTAssertNil(schedule.previewRows?.last?.reviewReason)
    }

    func testExplanationMatchesRowByDateEmployeeAndDaypart() throws {
        let schedule = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.fullJSON.utf8))
        let rows = try XCTUnwrap(schedule.previewRows)

        let ana = try XCTUnwrap(schedule.explanation(for: rows[0]))
        XCTAssertEqual(ana.employee, "Ana")
        XCTAssertEqual(ana.why, "Level 4 server (strong); usually works Tuesday nights; 28h this week.")
        // Facts render as chips in a stable order, each "label · value".
        XCTAssertEqual(ana.factChips, ["can close · yes", "hours this week · 28.5", "score · 4",
                                       "usual nights · Tuesday, Friday"])

        // Bob is on the same date but has no assignment in the payload.
        XCTAssertNil(schedule.explanation(for: rows[1]))

        XCTAssertEqual(GeneratedSchedule.daypart(of: "4:00pm"), "night")
        XCTAssertEqual(GeneratedSchedule.daypart(of: "10:00am"), "morning")
        XCTAssertEqual(GeneratedSchedule.daypart(of: "2:59pm"), "morning")
        XCTAssertEqual(GeneratedSchedule.daypart(of: nil), "night")
    }

    func testLegacyCachedPayloadStillDecodesWithEveryNewKeyNil() throws {
        let schedule = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.legacyJSON.utf8))

        XCTAssertNil(schedule.historyId)
        XCTAssertNil(schedule.review)
        XCTAssertNil(schedule.ruleViolations)
        XCTAssertNil(schedule.pendingTimeOff)
        XCTAssertNil(schedule.narrative)
        XCTAssertNil(schedule.generationSeconds)
        XCTAssertNil(schedule.chunked)
        XCTAssertNil(schedule.roster)
        XCTAssertNil(schedule.publishedAt)
        XCTAssertEqual(schedule.previewRows?.first?.employee, "Amy C.")
        XCTAssertNil(schedule.explanation(for: try XCTUnwrap(schedule.previewRows?.first)))
    }

    /// The full payload survives the on-device cache: encode, decode, and
    /// the review layer is intact on the other side.
    func testFullPayloadRoundTripsThroughCodable() throws {
        let decoded = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.fullJSON.utf8))
        let data = try JSONEncoder().encode(decoded)
        let again = try JSONDecoder().decode(GeneratedSchedule.self, from: data)

        XCTAssertEqual(again.review, decoded.review)
        XCTAssertEqual(again.ruleViolations, decoded.ruleViolations)
        XCTAssertEqual(again.quality?.shifts?.first?.assignments, decoded.quality?.shifts?.first?.assignments)
        XCTAssertEqual(again.previewRows?.first?.reviewReason, "approved time off")
    }

    func testHistoryDetailCarriesPublishedAtAndBy() throws {
        let json = """
        {"ok": true, "id": 79, "published_at": "2026-09-21 18:45:00", "published_by": "will",
         "generation_seconds": 40.2, "preview_rows": []}
        """
        let schedule = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(json.utf8))
        XCTAssertEqual(schedule.publishedAt, "2026-09-21 18:45:00")
        XCTAssertEqual(schedule.publishedBy, "will")
        XCTAssertEqual(CavnarDate.mdyTime("2026-09-21 18:45:00"), "9/21/26 · 6:45pm")
        XCTAssertEqual(CavnarDate.mdy("2026-09-05"), "9/5/26")
    }

    func testVersionsAndDraftVsPublishedDecode() throws {
        let json = """
        {"ok": true,
         "versions": [{"id": 1, "version": 1, "reason": "generated", "saved_by": null,
                       "created_at": "2026-09-20 09:00:00", "changes": 0, "lines": [], "score": 78},
                      {"id": 2, "version": 2, "reason": "edited", "saved_by": "will",
                       "created_at": "2026-09-20 09:30:00", "changes": 1,
                       "lines": ["Saturday Server 4:00pm: Ana → Bob."], "score": 80},
                      {"id": 3, "version": 3, "reason": "published", "saved_by": "will",
                       "created_at": "2026-09-21 18:45:00", "changes": 0, "lines": [], "score": 80}],
         "draft_vs_published": {"available": true, "changes": 1, "added": 0, "removed": 0, "moved": 1,
                                "retimed": 0, "hours_before": 212.0, "hours_after": 212.0,
                                "lines": ["Saturday Server 4:00pm: Ana → Bob."]}}
        """
        struct Envelope: Decodable {
            let versions: [ScheduleVersion]
            let draftVsPublished: DraftVsPublished
            enum CodingKeys: String, CodingKey {
                case versions
                case draftVsPublished = "draft_vs_published"
            }
        }
        let env = try JSONDecoder().decode(Envelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.versions.map(\.title), ["generated by Cavnar AI", "edited by will", "published by will"])
        XCTAssertTrue(env.versions.last!.isPublished)
        XCTAssertEqual(env.draftVsPublished.summaryLine, "1 change · 1 moved")
    }

    func testRosterPayloadDecodes() throws {
        let json = """
        {"ok": true, "can_edit": true,
         "roster": [{"name": "Ana", "role": "Server", "shifts": 42, "last_worked": "2026-09-20",
                     "is_manual": false, "active": true,
                     "settings": {"active": true, "employment_type": "part", "min_hours": 12, "max_hours": 30,
                                  "daypart_availability": {"Monday": "off", "Tuesday": "night"}, "is_minor": false},
                     "score": 4, "can_close": true,
                     "reliability": {"no_show_rate": 0.04, "short_rate": 0.0, "shifts": 42}},
                    {"name": "Dev", "role": "Busser", "shifts": 3, "active": false, "settings": {"active": false},
                     "score": null, "can_close": false, "reliability": null}],
         "pairs": [{"id": 7, "a": "Ana", "b": "Bob", "kind": "avoid", "note": "same section, argue"}],
         "choices": {"employment_type": ["full", "part"], "daypart": ["any", "morning", "night", "off"],
                     "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]}}
        """
        struct Envelope: Decodable {
            let roster: [RosterMember]
            let pairs: [StaffPair]
            let choices: RosterChoices
        }
        let env = try JSONDecoder().decode(Envelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.roster.first?.settings?.employmentType, "part")
        XCTAssertEqual(env.roster.first?.settings?.daypartAvailability?["Monday"], "off")
        XCTAssertEqual(env.roster.first?.reliability?.noShowLabel, "4% no-show")
        XCTAssertTrue(env.roster.first!.isActive)
        XCTAssertFalse(env.roster.last!.isActive)
        XCTAssertNil(env.roster.last?.reliability)
        XCTAssertFalse(env.pairs.first!.isPrefer)
        XCTAssertEqual(env.choices.days?.count, 7)
    }

    /// A settings patch sends only what changed — an unset field is absent
    /// from the body, never null, so the server does not read "clear it".
    func testStaffSettingsPatchEncodesOnlyTheChangedField() throws {
        let patch = ScheduleSetupViewModel.StaffSettingsPatch(employeeName: "Ana", maxHours: 32)
        let json = try XCTUnwrap(String(data: JSONEncoder().encode(patch), encoding: .utf8))
        XCTAssertTrue(json.contains("\"max_hours\":32"))
        XCTAssertTrue(json.contains("\"employee_name\":\"Ana\""))
        XCTAssertFalse(json.contains("min_hours"))
        XCTAssertFalse(json.contains("active"))
        XCTAssertFalse(json.contains("null"))
    }
}
