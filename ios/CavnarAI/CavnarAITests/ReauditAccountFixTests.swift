import XCTest
@testable import CavnarAI

/// iOS blind re-audit, Account / Team / People fix round (10/8/26): the
/// pure halves of the fixes.
@MainActor
final class ReauditAccountFixTests: XCTestCase {

    private func profile(_ json: String) throws -> AccountProfile {
        try JSONDecoder().decode(AccountProfile.self, from: Data(json.utf8))
    }

    // A7: shortening retention is asked first; keeping more is not.
    func testOnlyAShorterRetentionIsConfirmed() {
        XCTAssertTrue(AccountExportDataView.shortens(from: 0, to: 12), "keep everything → 12 months erases")
        XCTAssertTrue(AccountExportDataView.shortens(from: 24, to: 6))
        XCTAssertFalse(AccountExportDataView.shortens(from: 6, to: 24))
        XCTAssertFalse(AccountExportDataView.shortens(from: 12, to: 0), "keep everything never erases")
    }

    // L9: the week in words, not a count of stored keys.
    func testHoursReadAsTheWeekRuns() throws {
        let p = try profile("""
        {"restaurant_name": "X", "timezone": "America/Chicago",
         "open_times_json": "{\\"Monday\\": \\"11am\\", \\"Tuesday\\": \\"11am\\", \\"Wednesday\\": \\"11am\\", \\"Thursday\\": \\"11am\\", \\"Friday\\": \\"11am\\", \\"Saturday\\": \\"11am\\"}",
         "close_times_json": "{\\"Monday\\": \\"10pm\\", \\"Tuesday\\": \\"10pm\\", \\"Wednesday\\": \\"10pm\\", \\"Thursday\\": \\"10pm\\", \\"Friday\\": \\"10pm\\", \\"Saturday\\": \\"10pm\\"}",
         "closures": ["2026-11-26", "2026-12-25", "2026-01-01"]}
        """)
        XCTAssertEqual(AccountProfileDetailView.hoursSummary(p, today: "2026-10-08"),
                       "Open 6 days \u{00B7} closes 10pm \u{00B7} 2 closed dates")
        let none = try profile(#"{"restaurant_name": "X", "timezone": "America/Chicago"}"#)
        XCTAssertEqual(AccountProfileDetailView.hoursSummary(none, today: "2026-10-08"), "Hours not set")
    }

    // M16
    func testStaffSignupsReadAsAShare() {
        XCTAssertEqual(AccountStaffDetailView.signedUpLine(signed: 12, notYet: 6), "12 of 18 signed up")
        XCTAssertEqual(AccountStaffDetailView.signedUpLine(signed: 3, notYet: 0), "3 signed up")
    }

    // L15: one PIN rule, the server's.
    func testAPinIsFourToEightDigits() {
        XCTAssertFalse(StaffPinRule.plausible("123"))
        XCTAssertTrue(StaffPinRule.plausible("4821"))
        XCTAssertTrue(StaffPinRule.plausible("48213579"))
        XCTAssertFalse(StaffPinRule.plausible("482135790"))
        XCTAssertFalse(StaffPinRule.plausible("48a1"))
    }

    // M7: "staff" opens Staff accounts, not Manage team.
    func testTheStaffLinkOpensStaffAccounts() {
        XCTAssertEqual(AccountLinkSection("staff"), .staff)
        XCTAssertEqual(AccountLinkSection("team"), .team)
    }

    // L5: a fact's known keys in owner words; an unknown key never raw.
    func testPersonFactsNeverShowARawKey() {
        let hours = JSONValue.object(["employment_type": .string("part"), "min_hours": .number(20),
                                      "max_hours": .number(32), "mystery_key": .number(9)])
        XCTAssertEqual(PersonRecord.describe(hours), "Part-time \u{00B7} at least 20 h \u{00B7} at most 32 h")
        XCTAssertNil(PersonRecord.describe(.object(["mystery_key": .number(9)])))
        XCTAssertNil(MemoryModule.label("mystery_module"))
        XCTAssertEqual(MemoryKind.singular("mystery_kind"), "Note")
    }

    // M21
    func testAFullLaneIsSaidInTheOwnersWords() {
        XCTAssertEqual(ArchivedFact.ownerWords("its lane was full"), "replaced by newer notes")
        XCTAssertEqual(ArchivedFact.ownerWords("its date passed"), "its date passed")
    }

    // L7
    func testViewAsHoursArePluralised() {
        XCTAssertEqual(ViewAsBanner.hoursPhrase(1), "1 hour")
        XCTAssertEqual(ViewAsBanner.hoursPhrase(2), "2 hours")
    }

    // A3: the server's reason follows "X is on the team, but".
    func testTheInviteNoticeReadsAsOneSentence() {
        XCTAssertEqual(AccountViewModel.lowerFirst("The invite email didn't go out."), "the invite email didn't go out.")
    }
}
