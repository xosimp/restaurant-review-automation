import XCTest
@testable import CavnarAI

/// Seven settings only the web could edit (tests/test_ios_request_body_parity.py
/// listed them as web-only): the closed weekdays, the dining-section count,
/// the kitchen stations, the floor-section names, a role's start date, and
/// a task sheet's job code and removal. Each body carries the key its route
/// reads, by the name the route reads it.
@MainActor
final class WebOnlySettingsParityTests: XCTestCase {

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder.cavnar.encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder.cavnar.decode(T.self, from: Data(json.utf8))
    }

    // MARK: 1 — closed weekdays (POST labor/rules)

    func testClosedWeekdaysSendTheServerListWithOneDayChanged() throws {
        let on = TeamSetupStore.closedWeekdays(["Monday"], toggling: "Sunday")
        XCTAssertEqual(on, ["Monday", "Sunday"])
        XCTAssertEqual(TeamSetupStore.closedWeekdays(["Sunday", "Monday"], toggling: "Tuesday"),
                       ["Monday", "Tuesday", "Sunday"], "in week order, as the server stores them")
        XCTAssertEqual(TeamSetupStore.closedWeekdays(["Monday", "Sunday"], toggling: "Monday"), ["Sunday"])
        let j = try object(ClosedWeekdaysBody(closedWeekdays: on))
        XCTAssertEqual(Set(j.keys), ["closed_weekdays"])
        XCTAssertEqual(j["closed_weekdays"] as? [String], ["Monday", "Sunday"])
    }

    func testTheRulesReadCarriesClosuresAndStations() throws {
        let f = try decode(RulesSetupFields.self, """
            {"section_count": 6,
             "closures": {"closed_weekdays": ["Monday"], "closed_dates": ["2026-12-25"]},
             "kitchen_stations": {"roles": ["Cook"], "stations": ["Grill", "Fry"],
               "needs": [{"station": "Grill", "daypart": "night", "days": ["Friday", "Saturday"], "count": 2}, {"x": 1}],
               "skills": {"Jesus Hernandez": ["Grill"]}, "active": true,
               "roster_roles": ["Cook", "Server"], "kitchen_people": ["Jesus Hernandez", "Ana"]}}
            """)
        XCTAssertEqual(f.closures?.closedWeekdays, ["Monday"])
        XCTAssertEqual(f.closures?.closedDates, ["2026-12-25"])
        let ks = try XCTUnwrap(f.kitchenStations)
        XCTAssertEqual(ks.stations, ["Grill", "Fry"])
        XCTAssertEqual(ks.needs.count, 1, "a need with no station is skipped")
        XCTAssertEqual(ks.needs.first?.line, "Nights (after 3pm) \u{00B7} Friday, Saturday \u{00B7} 2 cooks")
        XCTAssertEqual(ks.skills(of: "jesus hernandez"), ["Grill"], "skills match without regard to case")
        XCTAssertEqual(ks.roleChoices, ["Cook", "Server"])
        let store = TeamSetupStore()
        store.apply(f)
        XCTAssertEqual(store.closedWeekdays, ["Monday"])
        XCTAssertEqual(store.kitchenStations.kitchenPeople, ["Jesus Hernandez", "Ana"])
        XCTAssertEqual(store.sectionCount, 6)
    }

    // MARK: 2 — dining section count

    func testSectionCountAlwaysSendsTheKeyAndNullIsNoCap() throws {
        XCTAssertEqual(try object(SectionCountBody(sectionCount: 8))["section_count"] as? Int, 8)
        let cleared = try object(SectionCountBody(sectionCount: nil))
        XCTAssertEqual(Set(cleared.keys), ["section_count"], "a cleared cap is sent as null, never left out")
        XCTAssertTrue(cleared["section_count"] is NSNull)
        let r = try decode(QuickRulesResponse.self, """
            {"ok": true, "section_count": null, "floor_cap_conflicts": []}
            """)
        XCTAssertTrue(r.ok)
        XCTAssertNil(r.sectionCount)
        XCTAssertEqual(r.floorCapConflicts?.count, 0)
    }

    // MARK: 3 — kitchen stations: one edit per request

    func testEveryStationEditCarriesItsOpAndFields() throws {
        func edit(_ e: StationEdit) throws -> [String: Any] {
            let j = try object(StationEditBody(stationEdit: e))
            XCTAssertEqual(Set(j.keys), ["station_edit"])
            return try XCTUnwrap(j["station_edit"] as? [String: Any])
        }
        var e = try edit(.roles(["Cook", "Prep"]))
        XCTAssertEqual(e["op"] as? String, "roles")
        XCTAssertEqual(e["roles"] as? [String], ["Cook", "Prep"])
        e = try edit(.addStation("Grill"))
        XCTAssertEqual(e["op"] as? String, "add_station"); XCTAssertEqual(e["name"] as? String, "Grill")
        e = try edit(.removeStation("Fry"))
        XCTAssertEqual(e["op"] as? String, "remove_station"); XCTAssertEqual(e["name"] as? String, "Fry")
        e = try edit(.addNeed(station: "Grill", daypart: "night", days: ["Friday"], count: 2))
        XCTAssertEqual(Set(e.keys), ["op", "station", "daypart", "days", "count"])
        XCTAssertEqual(e["op"] as? String, "add_need"); XCTAssertEqual(e["count"] as? Int, 2)
        XCTAssertEqual(e["days"] as? [String], ["Friday"])
        e = try edit(.removeNeed(station: "Grill", daypart: "all", days: []))
        XCTAssertEqual(Set(e.keys), ["op", "station", "daypart", "days"])
        XCTAssertEqual(e["op"] as? String, "remove_need")
        e = try edit(.skill(person: "Ana", station: "Grill", on: true))
        XCTAssertEqual(e["op"] as? String, "skill"); XCTAssertEqual(e["person"] as? String, "Ana")
        XCTAssertEqual(e["on"] as? Bool, true)
    }

    // MARK: 4 — floor section names (whole list; server list + one change)

    func testSectionNamesAddAndRemoveOneAgainstTheServerList() throws {
        let added = FloorSectionsStore.adding("  Back   patio ", to: ["Bar"])
        XCTAssertEqual(added.list, ["Bar", "Back patio"])
        XCTAssertNil(added.refusal)
        XCTAssertEqual(FloorSectionsStore.adding("bar", to: ["Bar"]).refusal, "Bar is already a section.")
        XCTAssertEqual(FloorSectionsStore.adding(" ", to: []).refusal, "Name the section.")
        XCTAssertEqual(FloorSectionsStore.adding("X", to: (1...30).map { "S\($0)" }).refusal,
                       "You can name up to 30 sections.")
        XCTAssertEqual(FloorSectionsStore.clean(String(repeating: "a", count: 50)).count, 40)
        XCTAssertEqual(FloorSectionsStore.removing("patio", from: ["Bar", "Patio"]), ["Bar"])
        let j = try object(FloorSectionsBody(sections: ["Bar", "Patio"]))
        XCTAssertEqual(Set(j.keys), ["sections"])
        XCTAssertEqual(j["sections"] as? [String], ["Bar", "Patio"])
    }

    func testTheSectionsReadSaysWhetherThisLoginMayEditThem() throws {
        let r = try decode(ScheduleSections.self, """
            {"ok": true, "sections": ["Patio"], "assigned": [], "usual": [], "foh_roles": ["server"], "can_edit": false}
            """)
        XCTAssertEqual(r.canEdit, false)
        XCTAssertNil(try decode(ScheduleSections.self, #"{"sections": []}"#).canEdit, "an older server says nothing")
    }

    // MARK: 5 — a role's start date

    func testARoleCarriesItsStartDate() throws {
        let j = try object(PersonSheetViewModel.RoleBody(role: "Bartender", since: "2026-09-01", primary: false, remove: nil))
        XCTAssertEqual(j["role"] as? String, "Bartender")
        XCTAssertEqual(j["since"] as? String, "2026-09-01")
        XCTAssertEqual(j["primary"] as? Bool, false)
        XCTAssertNil(j["remove"])
        let removal = try object(PersonSheetViewModel.RoleBody(role: "Bartender", primary: nil, remove: true))
        XCTAssertNil(removal["since"], "a removal sends no date")
        XCTAssertEqual(removal["remove"] as? Bool, true)
    }

    // MARK: 6, 7 — a task sheet's job code, and removing the sheet

    func testSheetSettingsCarryTheJobCode() throws {
        let j = try object(TSSheetSettingsBody(job_code: "Line Cook", shift_kind: "closing", days_of_week: [4, 5],
                                               requires_signoff: true))
        XCTAssertEqual(Set(j.keys), ["job_code", "shift_kind", "days_of_week", "requires_signoff"])
        XCTAssertEqual(j["job_code"] as? String, "Line Cook")
    }

    func testRemovingASheetSendsActiveFalseOnly() throws {
        let j = try object(TSSheetRemoveBody())
        XCTAssertEqual(Set(j.keys), ["active"])
        XCTAssertEqual(j["active"] as? Bool, false)
    }
}
