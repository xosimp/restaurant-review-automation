import XCTest
@testable import CavnarAI

/// iOS blind re-audit, Home + Ask (10/8/26): the pure rules behind the
/// fixes — the Ask scaffolding never drawn as text (H1), the badge-led
/// scroll (H8), the replies tile (L6), the daily launch intro (M12) and
/// the group food-cost figure (M14).
final class ReauditHomeAskTests: XCTestCase {

    // MARK: H1 — no "---" and no "Follow-ups:" line on the phone

    func testScaffoldingIsStrippedAndTheFollowUpsLifted() {
        let raw = """
            Reviews are steady at 4.6 stars.
            Two 2-star reviews are unanswered.
            Follow-ups: Which reviews mention wait times? | How did last month compare?
            ---
            The detail.
            """
        let out = AskAnswerText.scaffoldingStripped(raw)
        XCTAssertFalse(out.text.contains("---"))
        XCTAssertFalse(out.text.contains("Follow-ups"))
        XCTAssertTrue(out.text.hasSuffix("The detail."))
        XCTAssertEqual(out.followUps, ["Which reviews mention wait times?", "How did last month compare?"])
        // A markdown table's rule row is not scaffolding.
        let table = "| a | b |\n|---|---|\n| 1 | 2 |"
        XCTAssertEqual(AskAnswerText.scaffoldingStripped(table).text, table)
    }

    // MARK: H8 — the badge-led scroll happens once per count

    func testTheBadgeScrollsOnlyWhenTheCountChanged() {
        XCTAssertTrue(HomeView.scrollsToNeedsYou(count: 3, lastScrolledFor: nil))
        XCTAssertFalse(HomeView.scrollsToNeedsYou(count: 3, lastScrolledFor: 3))
        XCTAssertTrue(HomeView.scrollsToNeedsYou(count: 4, lastScrolledFor: 3))
        XCTAssertFalse(HomeView.scrollsToNeedsYou(count: 0, lastScrolledFor: nil))
    }

    // MARK: L6 — the replies tile says the rate and what is left

    func testTheRepliesTileReadsAsARateAndWhatIsWaiting() {
        let r = HomeKPIRow.repliesFigure("45/52")
        XCTAssertEqual(r?.value, "87%")
        XCTAssertEqual(r?.detail, "7 waiting")
        XCTAssertEqual(HomeKPIRow.repliesFigure("52/52")?.detail, "None waiting")
        XCTAssertNil(HomeKPIRow.repliesFigure("87%"))
        XCTAssertNil(HomeKPIRow.repliesFigure("3/0"))
    }

    // MARK: M12 — the full intro once a day

    func testTheFullIntroPlaysOncePerDay() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "reaudit.launch.\(UUID().uuidString)"))
        // Noon on the phone's own calendar, so +1h never crosses midnight.
        let day = try XCTUnwrap(Calendar.current.date(from: DateComponents(year: 2026, month: 10, day: 3, hour: 12)))
        XCTAssertTrue(LaunchIntroDay.claim(defaults, now: day))
        XCTAssertFalse(LaunchIntroDay.claim(defaults, now: day.addingTimeInterval(3600)))
        XCTAssertTrue(LaunchIntroDay.claim(defaults, now: day.addingTimeInterval(86_400 * 2)))
    }

    // MARK: M14 — a location's food cost figure is the measured %

    func testAGroupFoodCostFigureIsNeverTheOpportunity() throws {
        let data = Data(#"{"recoverable": 1240.0, "critical_low": 1}"#.utf8)
        let inv = try JSONDecoder().decode(LocationGroupBrief.Inventory.self, from: data)
        XCTAssertEqual(LocationGroupFormat.foodCost(inv).figure, "\u{2014}")
        XCTAssertEqual(LocationGroupFormat.foodCost(inv).detail, "$1,240/mo could recover \u{00B7} 1 low")
    }
}
