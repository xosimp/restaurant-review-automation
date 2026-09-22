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

    // MARK: - Second batch

    private static let secondBatchJSON = """
    {"ok": true, "status": "done", "history_id": 92,
     "week_dates": ["2026-09-28", "2026-10-04"], "hours_scheduled": 200.0, "hours_budget": 205.0,
     "preview_rows": [{"date": "2026-09-28", "day": "Monday", "employee": "Ana", "role": "Server",
                       "shift_start": "4:00pm", "shift_end": "10:00pm", "scheduled_hours": "6"}],
     "trimmed": [{"date": "2026-09-29", "day": "Tuesday", "employee": "Dev", "role": "Busser",
                  "shift_start": "4:00pm", "shift_end": "9:00pm", "hours": 5.0,
                  "reason": "the lightest Tuesday night in eight weeks"}],
     "hours_trimmed": 5.0,
     "staggered": [{"date": "2026-10-02", "employee": "Bob", "role": "Bartender", "from": "4:00pm", "to": "5:00pm",
                    "reason": "sales do not pick up until five"}],
     "projected_cost": {"straight": 3400.0, "overtime_premium": 90.0, "overtime_hours": 3.0, "total": 3490.0, "multiplier": 1.5},
     "over_budget_dollars": 140.0,
     "projected_revenue_source": "median of the last 6 complete weeks",
     "hourly_profile_ready": false,
     "demand_data_through": {"date": "2026-09-05", "days_ago": 23, "blind": true},
     "reservation_feed": {"provider": "tock", "label": "Tock", "configured": true, "live": false,
                          "message": "Tock is keyed but this build cannot read it yet."},
     "holiday_lift": {"2026-10-03": {"name": "Founders Day", "lift_pct": 18, "based_on": "2025-10-03"},
                      "2026-10-04": {"name": "Quiet Sunday", "lift_pct": null, "based_on": null}},
     "could_hold": {"Cara": ["Bartender"]},
     "departments": ["Kitchen", "Front"],
     "regenerated_dates": ["2026-09-30"],
     "review": {"hard": 0, "soft": 1, "lines": ["Trimmed 5h to fit the budget"],
                "trimmed": [{"date": "2026-09-29", "employee": "Dev", "hours": 5.0}], "hours_trimmed": 5.0,
                "staggered": []},
     "rule_violations": [{"kind": "no_manager_on_duty", "index": 0, "employee": "Ana", "hard": true,
                          "label": "no manager or keyholder on the shift"},
                         {"kind": "before_arrival", "index": 0, "employee": "Ana", "hard": false,
                          "label": "starts before that role's arrival time"}],
     "quality": {"checked": true, "score": 81, "band": "solid",
                 "recommendations": ["Fill the gap on Friday night.", "Rate the three unrated servers."],
                 "suppressed_recommendation_kinds": ["hours", "fatigue"]}}
    """

    func testDecodesTheSecondBatchKeys() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.secondBatchJSON.utf8))

        XCTAssertEqual(s.trimmedShifts.count, 1)
        XCTAssertEqual(s.trimmedShifts.first?.reason, "the lightest Tuesday night in eight weeks")
        XCTAssertEqual(s.trimmedHours, 5.0)
        XCTAssertEqual(s.staggeredStarts.first?.from, "4:00pm")
        XCTAssertEqual(s.staggeredStarts.first?.to, "5:00pm")
        XCTAssertEqual(s.projectedCost?.total, 3490.0)
        XCTAssertEqual(s.projectedCost?.overtimeHours, 3.0)
        XCTAssertEqual(s.projectedCost?.overtimePremium, 90.0)
        XCTAssertEqual(s.overBudgetDollars, 140.0)
        XCTAssertEqual(s.projectedRevenueSource, "median of the last 6 complete weeks")
        XCTAssertEqual(s.hourlyProfileReady, false)
        XCTAssertEqual(s.demandDataThrough?.blind, true)
        XCTAssertEqual(s.demandDataThrough?.daysAgo, 23)
        XCTAssertEqual(s.reservationFeed?.provider, "tock")
        XCTAssertEqual(s.reservationFeed?.live, false)
        XCTAssertEqual(s.holiday(on: "2026-10-03")?.label, "Founders Day · +18%")
        XCTAssertEqual(s.holiday(on: "2026-10-04")?.label, "Quiet Sunday")
        XCTAssertNil(s.holiday(on: "2026-10-01"))
        XCTAssertEqual(s.couldHold?["Cara"], ["Bartender"])
        XCTAssertEqual(s.departments, ["Kitchen", "Front"])
        XCTAssertEqual(s.regeneratedDates, ["2026-09-30"])
        XCTAssertEqual(s.review?.hoursTrimmed, 5.0)
        XCTAssertEqual(s.review?.trimmed?.count, 1)
        XCTAssertEqual(s.ruleViolations?.first?.label, "no manager or keyholder on the shift")
        XCTAssertEqual(s.ruleViolations?.last?.kind, "before_arrival")
        XCTAssertEqual(s.quality?.suppressedRecommendationKinds, ["hours", "fatigue"])
        XCTAssertEqual(s.rowDates, ["2026-09-28"])
    }

    /// The trim list may live only under `review` on a re-scored payload.
    func testTrimFallsBackToTheReviewCopy() throws {
        let json = """
        {"ok": true, "review": {"hard": 0, "soft": 0, "trimmed": [{"employee": "Dev", "hours": 5.0}], "hours_trimmed": 5.0}}
        """
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(json.utf8))
        XCTAssertNil(s.trimmed)
        XCTAssertEqual(s.trimmedShifts.first?.employee, "Dev")
        XCTAssertEqual(s.trimmedHours, 5.0)
    }

    /// A payload cached before the second batch has none of its keys and
    /// must decode with every one of them nil — never a zero.
    func testLegacyPayloadLeavesTheSecondBatchNil() throws {
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: Data(Self.legacyJSON.utf8))
        XCTAssertNil(s.projectedCost)
        XCTAssertNil(s.overBudgetDollars)
        XCTAssertNil(s.hourlyProfileReady)
        XCTAssertNil(s.demandDataThrough)
        XCTAssertNil(s.holidayLift)
        XCTAssertNil(s.departments)
        XCTAssertNil(s.regeneratedDates)
        XCTAssertTrue(s.trimmedShifts.isEmpty)
        XCTAssertTrue(s.staggeredStarts.isEmpty)
        XCTAssertEqual(s.trimmedHours, 0)
        XCTAssertNil(s.storedWhatIf)
    }

    /// The 409 a save answers with when somebody saved the week first.
    func testSaveConflictDecodesThe409Shape() throws {
        let json = """
        {"ok": false, "conflict": true, "latest_version": 4, "saved_by": "maria",
         "lines": ["Saturday Server 4:00pm: Ana → Bob."],
         "error": "maria saved this week after you opened it. Reload to see their changes."}
        """
        let c = try JSONDecoder().decode(SaveConflict.self, from: Data(json.utf8))
        XCTAssertEqual(c.conflict, true)
        XCTAssertEqual(c.latestVersion, 4)
        XCTAssertEqual(c.savedBy, "maria")
        XCTAssertEqual(c.lines?.count, 1)
        XCTAssertTrue(c.error?.hasPrefix("maria saved") ?? false)
    }

    func testEditCostReadsAsHoursDollarsOvertime() throws {
        let json = """
        {"hours_before": 200.0, "hours_after": 206.0, "hours_delta": 6.0,
         "dollars_before": 3400.0, "dollars_after": 3490.0, "dollars_delta": 90.0, "overtime_hours_after": 2.0}
        """
        let cost = try JSONDecoder().decode(EditCostDelta.self, from: Data(json.utf8))
        XCTAssertEqual(cost.summary, "+6h · +$90 · 2h overtime")
        let flat = EditCostDelta(hoursBefore: 1, hoursAfter: 1, hoursDelta: 0, dollarsBefore: 1, dollarsAfter: 1,
                                 dollarsDelta: 0, overtimeHoursAfter: 0)
        XCTAssertNil(flat.summary)
        let down = EditCostDelta(hoursBefore: 10, hoursAfter: 6, hoursDelta: -4, dollarsBefore: 200, dollarsAfter: 140,
                                 dollarsDelta: -60, overtimeHoursAfter: 0)
        XCTAssertEqual(down.summary, "-4h · -$60")
    }

    /// The ledger's kind comes from the sentence's opening words — the
    /// same rule the server files them under.
    func testRecommendationKindByPrefix() {
        XCTAssertEqual(ScheduleQuality.recommendationKind("Fill the gap on Friday night."), "coverage")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Move somebody senior to Saturday."), "leadership")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Pair Ana with Bob on Tuesday."), "strength")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Trim about 6h from Monday."), "hours")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Give Cara a day off midweek."), "fatigue")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Rate the three unrated servers."), "ratings")
        XCTAssertEqual(ScheduleQuality.recommendationKind("Something else entirely."), "other")
    }

    func testRulesPayloadDecodesThePackAndFeed() throws {
        let json = """
        {"ok": true, "jurisdiction": "CA",
         "pack": {"code": "CA", "label": "California", "applied": {"daily_ot_hours": 8, "min_rest_hours": 10},
                  "notes": ["Daily overtime after 8 hours."]},
         "packs": [{"code": "CA", "label": "California"}, {"code": "NY", "label": "New York"}],
         "role_arrivals": {"Prep Cook": -30, "Server": 0},
         "role_requirements": {"Bartender": ["alcohol"]},
         "foh_roles": ["Server"], "patio_roles": ["Server", "Busser"], "trim_to_budget": true,
         "certifications": ["food handler", "alcohol"],
         "reservation_feed": {"provider": null, "configured": false, "live": false,
                              "message": "No reservation system connected — paste covers into Events & reservations."},
         "reservation_providers": [{"code": "tock", "label": "Tock", "live": false}]}
        """
        struct Envelope: Decodable {
            let jurisdiction: String?
            let pack: CompliancePack?
            let packs: [CodeLabel]
            let roleArrivals: [String: Int]
            let roleRequirements: [String: [String]]
            let fohRoles: [String]
            let trimToBudget: Bool
            let reservationFeed: ReservationFeedStatus
            let reservationProviders: [CodeLabel]
            enum CodingKeys: String, CodingKey {
                case jurisdiction, pack, packs
                case roleArrivals = "role_arrivals"
                case roleRequirements = "role_requirements"
                case fohRoles = "foh_roles"
                case trimToBudget = "trim_to_budget"
                case reservationFeed = "reservation_feed"
                case reservationProviders = "reservation_providers"
            }
        }
        let env = try JSONDecoder().decode(Envelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.jurisdiction, "CA")
        XCTAssertEqual(env.pack?.label, "California")
        XCTAssertEqual(env.pack?.applied?["daily_ot_hours"]?.display, "8")
        XCTAssertEqual(env.pack?.notes?.first, "Daily overtime after 8 hours.")
        XCTAssertEqual(env.packs.map(\.code), ["CA", "NY"])
        XCTAssertEqual(env.roleArrivals["Prep Cook"], -30)
        XCTAssertEqual(env.roleRequirements["Bartender"], ["alcohol"])
        XCTAssertEqual(env.fohRoles, ["Server"])
        XCTAssertTrue(env.trimToBudget)
        XCTAssertNil(env.reservationFeed.provider)
        XCTAssertEqual(env.reservationFeed.live, false)
        XCTAssertEqual(env.reservationProviders.first?.live, false)
    }

    /// A rules patch sends only the keys the sheet set. Clearing the
    /// jurisdiction goes over as null; an untouched key is absent.
    func testRulesPatchEncodesOnlyWhatWasSet() throws {
        var p = ScheduleSetupViewModel.RulesPatch(rules: ["manager_on_duty": .bool(true)], roleFloors: nil)
        p.jurisdiction = .some(nil)
        p.trimToBudget = false
        let json = try XCTUnwrap(String(data: JSONEncoder().encode(p), encoding: .utf8))
        XCTAssertTrue(json.contains("\"manager_on_duty\":true"))
        XCTAssertTrue(json.contains("\"jurisdiction\":null"))
        XCTAssertTrue(json.contains("\"trim_to_budget\":false"))
        XCTAssertFalse(json.contains("role_floors"))
        XCTAssertFalse(json.contains("reservation_provider"))
        XCTAssertFalse(json.contains("foh_roles"))
    }

    func testIntelPayloadDecodes() throws {
        let json = """
        {"ok": true,
         "outcomes": {"Friday": {"night": {"weeks": 6, "avg_hours": 48.0, "avg_sales": 5200.0, "splh": 108.0,
                                            "issues": 3, "troubled": true, "rating": 3.4}}},
         "ledger": {"Ana": {"weekend": 9, "closing": 4, "holiday": 1, "shifts": 30, "weeks": 8}},
         "behaviour": {"Bob": {"avoids": ["Sunday night"], "prefers": [], "drops": 3, "claims": 0}},
         "could_hold": {"Cara": ["Bartender"]}, "mentored": {},
         "suggested_pairs": [{"a": "Ana", "b": "Bob", "kind": "prefer", "shared": 12, "clean_rate": 0.92,
                              "evidence": "11 of 12 shared dayparts ran without a coverage or no-show issue"}],
         "splh": {"Friday": {"night": {"sales": 5200.0, "hours": 48.0, "splh": 108.0}}},
         "revenue": {"value": 31000.0, "source": "median of the last 6 complete weeks", "weeks": 6},
         "suppressed_recommendation_kinds": ["hours"]}
        """
        let intel = try JSONDecoder().decode(ScheduleIntel.self, from: Data(json.utf8))
        XCTAssertFalse(intel.isEmpty)
        XCTAssertEqual(intel.outcomes?["Friday"]?["night"]?.troubled, true)
        XCTAssertEqual(intel.outcomes?["Friday"]?["night"]?.splh, 108.0)
        XCTAssertEqual(intel.ledger?["Ana"]?.weekend, 9)
        XCTAssertEqual(intel.behaviour?["Bob"]?.avoids, ["Sunday night"])
        XCTAssertEqual(intel.couldHold?["Cara"], ["Bartender"])
        XCTAssertEqual(intel.suggestedPairs?.first?.id, "Ana|Bob")
        XCTAssertEqual(intel.suggestedPairs?.first?.cleanRate, 0.92)
        XCTAssertEqual(intel.revenue?.value, 31000.0)
        XCTAssertEqual(intel.suppressedRecommendationKinds, ["hours"])

        let empty = try JSONDecoder().decode(ScheduleIntel.self, from: Data("{\"ok\": true}".utf8))
        XCTAssertTrue(empty.isEmpty)
    }

    func testLearnedPatternsDecode() throws {
        let json = """
        {"ok": true, "can_edit": true,
         "patterns": [{"kind": "moved_off", "employee": "Ana", "day": "Sunday", "daypart": "night", "times": 3,
                       "text": "The manager has taken Ana off Sunday dinner/night 3 times recently — avoid scheduling them there.",
                       "key": "moved_off|ana|sunday|night", "active": true, "dismissed": false}]}
        """
        struct Envelope: Decodable { let patterns: [LearnedPattern] }
        let env = try JSONDecoder().decode(Envelope.self, from: Data(json.utf8))
        XCTAssertEqual(env.patterns.first?.id, "moved_off|ana|sunday|night")
        XCTAssertEqual(env.patterns.first?.active, true)
        XCTAssertEqual(env.patterns.first?.times, 3)
    }

    /// Swaps read "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm"; a drop has no
    /// swap label and keeps the replacement picker.
    func testShiftRequestSwapLabel() throws {
        let json = """
        [{"id": 1, "employee_name": "Ana", "date": "2026-09-28", "shift_start": "4:00pm", "shift_end": "10:00pm",
          "role": "Server", "status": "pending", "kind": "swap", "target_name": "Bob",
          "target_date": "2026-09-30", "target_start": "4:00pm", "target_end": "10:00pm"},
         {"id": 2, "employee_name": "Cara", "date": "2026-09-29", "shift_start": "10:00am", "status": "pending"}]
        """
        let rows = try JSONDecoder().decode([ShiftRequest].self, from: Data(json.utf8))
        XCTAssertTrue(rows[0].isSwap)
        XCTAssertEqual(rows[0].swapLabel, "Ana ↔ Bob: Mon 4:00pm for Wed 4:00pm")
        XCTAssertFalse(rows[1].isSwap)
        XCTAssertNil(rows[1].swapLabel)
        XCTAssertEqual(rows[1].kindLabel, "Drop")
    }

    func testRosterSettingsCarryWindowsCertificationsAndPreferences() throws {
        let json = """
        {"active": true, "time_windows": {"Monday": {"earliest": "10:00am", "latest": "9:00pm"}},
         "certifications": ["alcohol"], "preferred_dayparts": ["night"], "desired_hours": 28}
        """
        let s = try JSONDecoder().decode(RosterSettings.self, from: Data(json.utf8))
        XCTAssertEqual(s.timeWindows?["Monday"]?.earliest, "10:00am")
        XCTAssertEqual(s.timeWindows?["Monday"]?.latest, "9:00pm")
        XCTAssertEqual(s.certifications, ["alcohol"])
        XCTAssertEqual(s.preferredDayparts, ["night"])
        XCTAssertEqual(s.desiredHours, 28)

        let patch = ScheduleSetupViewModel.StaffSettingsPatch(
            employeeName: "Ana", timeWindows: ["Monday": TimeWindow(earliest: "10:00am", latest: nil)])
        let encoded = try XCTUnwrap(String(data: JSONEncoder().encode(patch), encoding: .utf8))
        XCTAssertTrue(encoded.contains("\"time_windows\""))
        XCTAssertTrue(encoded.contains("\"earliest\":\"10:00am\""))
        XCTAssertFalse(encoded.contains("certifications"))
        XCTAssertFalse(encoded.contains("preferred_dayparts"))
    }

    /// History detail: a save after sending, a draft replaced by a newer
    /// one, and the what-if stored as a JSON string in its column.
    func testHistoryDetailCarriesRepublishedSupersededAndStoredWhatIf() throws {
        // The column holds the what-if as a JSON string; built here with
        // JSONSerialization so the nesting is real, not hand-escaped.
        let inner = """
        {"ran": true, "evaluated": 40, "baseline_score": 78, "best_score": 81, "improvement": 3, "swaps": [],
         "verdict": "Three points available by moving one closer."}
        """
        let payload: [String: Any] = [
            "ok": true, "id": 80, "published_at": "2026-09-21 18:45:00", "republished_at": "2026-09-22 15:10:00",
            "superseded_by": 84, "quality_score": 78, "quality_band": "solid", "quality_confidence": 61,
            "what_if_json": inner, "preview_rows": [] as [Any],
        ]
        let data = try JSONSerialization.data(withJSONObject: payload)
        let s = try JSONDecoder().decode(GeneratedSchedule.self, from: data)
        XCTAssertEqual(s.republishedAt, "2026-09-22 15:10:00")
        XCTAssertEqual(CavnarDate.mdyTime("2026-09-22 15:10:00"), "9/22/26 · 3:10pm")
        XCTAssertEqual(s.supersededBy, 84)
        let whatIf = try XCTUnwrap(s.storedWhatIf)
        XCTAssertTrue(whatIf.ran)
        XCTAssertEqual(whatIf.bestScore, 81)
        XCTAssertEqual(whatIf.verdict, "Three points available by moving one closer.")
    }
}
