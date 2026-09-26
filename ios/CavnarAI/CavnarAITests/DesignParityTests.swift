import XCTest
import SwiftUI
@testable import CavnarAI

/// Web/iOS design parity (DESIGN_SYSTEM.md): the house time form, the
/// neutral tone that is not ember, and the bell's urgent count.
final class DesignParityTests: XCTestCase {
    private var chicago: TimeZone { TimeZone(identifier: "America/Chicago")! }

    private func date(_ h: Int, _ m: Int) -> Date {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = chicago
        return c.date(from: DateComponents(year: 2026, month: 9, day: 21, hour: h, minute: m))!
    }

    func testTimeIsLowercaseAmPmWithNoLeadingZero() {
        XCTAssertEqual(CavnarDate.time(date(18, 45), in: chicago), "6:45pm")
        XCTAssertEqual(CavnarDate.time(date(9, 5), in: chicago), "9:05am")
        XCTAssertEqual(CavnarDate.time(date(0, 0), in: chicago), "12:00am")
        XCTAssertEqual(CavnarDate.time(date(12, 30), in: chicago), "12:30pm")
        XCTAssertEqual(CavnarDate.mdyTime(date(18, 45), in: chicago), "9/21/26 · 6:45pm")
    }

    func testNeutralToneIsNeverEmber() {
        XCTAssertNotEqual(CavnarTone.neutral.foreground, Color.cavnarEmber)
        XCTAssertEqual(CavnarTone.neutral.foreground, Color.cavnarInk2)
    }

    @MainActor
    func testBadgeStartsWithNothingUrgent() {
        let badge = NotificationsBadgeViewModel()
        XCTAssertEqual(badge.urgentCount, 0)
        XCTAssertEqual(badge.unreadCount, 0)
    }
}
