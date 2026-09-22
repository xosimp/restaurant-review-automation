import XCTest
@testable import CavnarAI

/// What these protect, on iOS: rows an owner can actually reach at large
/// Dynamic Type sizes, and the written M/D/YY date rule (CLAUDE.md, "Dates
/// read 9/21/26") on every owner-facing screen the audit found breaking it.
///
/// Both are layout/formatting facts with no view-model seam: rendering a
/// SwiftUI list at .accessibility3 needs a UI or snapshot test target, which
/// this project does not have. They are checked at the source instead, and
/// the manifest lists the on-device check as not automatable here.
/// XCTExpectFailure marks confirmed CLIENT-35 / CLIENT-45 defects.
final class EdgeAccessibilityThemingTests: XCTestCase {

    // MARK: CLIENT-35 — Dynamic Type clipping

    /// A list that cannot scroll, sized to a fixed height per row, cuts off
    /// any row that grows with Dynamic Type or a wrapped name.
    private func fixedHeightNonScrollingList(in source: String) -> Bool {
        source.contains(".scrollDisabled(true)")
            && source.range(of: #"\.frame\(height: CGFloat\([^)]*\.count\) \* \d+\)"#, options: .regularExpression) != nil
    }

    func testThePairsListDoesNotClipRowsAtLargeTextSizes() throws {
        let source = try EdgeSource.read("Features/Labor/RosterSection.swift")
        XCTExpectFailure("CLIENT-35: RosterSection's pairs list is .scrollDisabled with height = count * 56", strict: true) {
            XCTAssertFalse(fixedHeightNonScrollingList(in: source))
        }
    }

    func testTheDemandSignalsListDoesNotClipRowsAtLargeTextSizes() throws {
        let source = try EdgeSource.read("Features/Labor/DemandSignalsSection.swift")
        XCTExpectFailure("CLIENT-35: DemandSignalsSection's list is .scrollDisabled with height = count * 58", strict: true) {
            XCTAssertFalse(fixedHeightNonScrollingList(in: source))
        }
    }

    func testTheClippingCheckItselfRecognisesAMeasuredList() {
        // Guards the helper: a list without the fixed per-row height passes.
        XCTAssertFalse(fixedHeightNonScrollingList(in: "List { }.scrollDisabled(true)"))
        XCTAssertTrue(fixedHeightNonScrollingList(in: ".scrollDisabled(true)\n.frame(height: CGFloat(rows.count) * 44)"))
    }

    // MARK: CLIENT-45 — owner-facing dates

    /// Display formats that spell a month or weekday instead of M/D/YY.
    private func spelledOutDateFormats(in source: String) -> [String] {
        let pattern = #"dateFormat = "([^"]*)""#
        let regex = try! NSRegularExpression(pattern: pattern)
        let ns = source as NSString
        return regex.matches(in: source, range: NSRange(location: 0, length: ns.length)).compactMap { m in
            let format = ns.substring(with: m.range(at: 1))
            return (format.contains("MMM") || format.contains("EEE")) ? format : nil
        }
    }

    private func assertMDY(_ file: String, line: UInt = #line) throws {
        let source = try EdgeSource.read(file)
        let found = spelledOutDateFormats(in: source)
        XCTExpectFailure("CLIENT-45: \(file) formats owner-facing dates as \(found), not M/D/YY", strict: true) {
            XCTAssertEqual(found, [], file, line: line)
        }
    }

    func testScheduleHistoryDatesAreMDY() throws { try assertMDY("Features/ScheduleHistory/ScheduleHistoryView.swift") }
    func testLaborWeekLabelsAreMDY() throws { try assertMDY("Features/Labor/LaborView.swift") }
    func testGuestLastVisitDatesAreMDY() throws { try assertMDY("Features/Marketing/GuestTextClubView.swift") }
    func testScheduledPostDatesAreMDY() throws { try assertMDY("Features/Marketing/MarketingComposeModels.swift") }
    func testTheHomeDateKickerIsMDY() throws { try assertMDY("Features/Home/HomeView.swift") }

    func testTheInvoiceDateIsShownMDY() throws {
        let source = try EdgeSource.read("Features/FoodCost/InvoiceScanSheet.swift")
        XCTAssertTrue(source.contains("inv.invoiceDate"))
        XCTExpectFailure("CLIENT-45: the invoice card prints invoice_date as sent (2026-09-18)", strict: true) {
            XCTAssertTrue(source.contains("CavnarDate.mdy"))
        }
    }

    func testTheContentCalendarRangeIsMDY() throws {
        let source = try EdgeSource.read("Features/Marketing/MarketingView.swift")
        XCTExpectFailure("CLIENT-45: the content calendar's range is spelled with abbreviated month names", strict: true) {
            XCTAssertFalse(source.contains(".month(.abbreviated)"))
        }
    }

    func testTheDateCheckItselfAcceptsMachineFormats() {
        // Parsing formats are not owner-facing and must not trip the check.
        XCTAssertEqual(spelledOutDateFormats(in: #"f.dateFormat = "yyyy-MM-dd HH:mm:ss""#), [])
        XCTAssertEqual(spelledOutDateFormats(in: #"f.dateFormat = "MMM d""#), ["MMM d"])
    }
}
