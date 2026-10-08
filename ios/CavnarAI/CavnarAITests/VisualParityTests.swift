import XCTest
import SwiftUI
@testable import CavnarAI

/// The visual round of the iOS parity audit (10/7/26 #82, #84, #85): the
/// Canvas charts speak their real figures to VoiceOver, a missing figure is
/// never spoken as a number, relative times read the web's "2h ago", the
/// reading surfaces follow the phone's text size past the app's cap, and
/// the series palette leads with the dark ember token.
final class VisualParityTests: XCTestCase {

    // MARK: - #82 chart summaries

    func testWeekRadarSaysEachDayAndNeverInventsAMissingOne() {
        let line = WeekRadarChart.spokenSummary(
            dowSummary: ["Monday": 28, "Saturday": 34.4], target: 30, positiveAllowed: true)
        XCTAssertTrue(line.contains("Target 30 percent"))
        XCTAssertTrue(line.contains("Monday 28 percent"))
        XCTAssertTrue(line.contains("Saturday 34 percent"))
        XCTAssertTrue(line.contains("Tuesday no data"), "a day with no figure is not drawn into one")
        XCTAssertTrue(line.contains("Highest Saturday, 4 points over target"))
        XCTAssertTrue(line.contains("1 of 2 days over"))
    }

    func testWeekRadarOnTargetHonoursThePositiveContract() {
        let ok = WeekRadarChart.spokenSummary(dowSummary: ["Monday": 25], target: 30, positiveAllowed: true)
        XCTAssertTrue(ok.contains("Every day on target"))
        let partial = WeekRadarChart.spokenSummary(dowSummary: ["Monday": 25], target: 30, positiveAllowed: false)
        XCTAssertTrue(partial.contains("data incomplete"))
        XCTAssertEqual(WeekRadarChart.spokenSummary(dowSummary: [:], target: 30, positiveAllowed: true),
                       "No weekday figures yet.")
    }

    func testLaborRibbonSaysRangeHighAndCountOver() {
        let pts = [LaborRibbonChart.Point(id: "a", label: "9/1", pct: 27),
                   LaborRibbonChart.Point(id: "b", label: "9/2", pct: 33),
                   LaborRibbonChart.Point(id: "c", label: "9/3", pct: 29)]
        let line = LaborRibbonChart.spokenSummary(points: pts, target: 30)
        XCTAssertTrue(line.contains("9/1 27 percent to 9/3 29 percent"))
        XCTAssertTrue(line.contains("Highest 9/2, 33 percent"))
        XCTAssertTrue(line.contains("1 of 3 over target"))
        XCTAssertEqual(LaborRibbonChart.spokenSummary(points: [], target: 30), "No labor figures yet.")
    }

    func testWasteLedgerReadsEveryBarWithItsReason() {
        let rows = [WasteLedgerChart.Row(id: "1", name: "Romaine", value: 412.4, detail: "24% waste"),
                    WasteLedgerChart.Row(id: "2", name: "Brie", value: 60, detail: nil)]
        let line = WasteLedgerChart.spokenSummary(headline: "This week · $472 flagged", rows: rows)
        XCTAssertEqual(line, "This week · $472 flagged. Romaine, $412, 24% waste. Brie, $60.")
    }

    func testVisibilityOrbitNeverSpeaksAMissingScoreAsZero() throws {
        XCTAssertEqual(VisibilityOrbitChart.spokenSummary(score: nil, runs: []), "Not measured.")
        let runs = try JSONDecoder().decode([AIVisibilityRun].self, from: Data("""
        [{"ai_score": 40, "created_at": "2026-09-01 10:00:00"},
         {"ai_score": 46, "created_at": "2026-09-08 10:00:00"}]
        """.utf8))
        let line = VisibilityOrbitChart.spokenSummary(score: 46, runs: runs)
        XCTAssertTrue(line.hasPrefix("46 out of 100"))
        XCTAssertTrue(line.contains("Up 6 since last run"))
        XCTAssertTrue(VisibilityOrbitChart.spokenSummary(score: 40, runs: Array(runs.prefix(1)))
            .contains("First check"))
    }

    // MARK: - #82 Dynamic Type past the cap

    func testReadingSizeFollowsThePhoneButStopsAtItsBound() {
        XCTAssertEqual(CavnarReadingSize.resolved(system: .accessibility5, upTo: .accessibility3), .accessibility3)
        XCTAssertEqual(CavnarReadingSize.resolved(system: .accessibility1, upTo: .accessibility3), .accessibility1)
        XCTAssertEqual(CavnarReadingSize.resolved(system: .large, upTo: .accessibility2), .large)
    }

    // MARK: - #84 relative time

    func testRelativeTimeIsTheWebsShortForm() {
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertEqual(NotificationItem.relative(now.addingTimeInterval(-20), now: now), "just now")
        let twoHours = NotificationItem.relative(now.addingTimeInterval(-7200), now: now)
        XCTAssertFalse(twoHours.contains("hr."), "was \"2 hr. ago\": \(twoHours)")
        XCTAssertTrue(twoHours.contains("2"))
    }

    // MARK: - #85 one token set

    func testSeriesPaletteLeadsWithTheDarkEmberToken() {
        XCTAssertEqual(Color.cavnarSeries.count, 12)
        XCTAssertEqual(Color.cavnarSeries.first, Color.cavnarEmber)
        XCTAssertEqual(RoleDonutChart.colors.count, Color.cavnarSeries.count)
    }

    func testKickerAndTagAreTheTwoUppercaseSizes() {
        XCTAssertEqual(CavnarType.kicker, 11.5)
        XCTAssertEqual(CavnarType.tag, 10)
    }
}
