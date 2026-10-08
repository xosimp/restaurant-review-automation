import XCTest
import SwiftUI
@testable import CavnarAI

/// The iPad layout (parity audit #99): the size class picks the shell, the
/// sidebar and the tab bar share one state, the keyboard's digits name the
/// tabs, and the Labor week offers its grid only where it fits.
@MainActor
final class IPadLayoutTests: XCTestCase {

    // MARK: The shell

    func testOnlyARegularWidthGetsTheSidebar() {
        XCTAssertTrue(CavnarLayout.usesSidebar(.regular))
        XCTAssertFalse(CavnarLayout.usesSidebar(.compact))
        // No size class yet is the phone shape, never a half-built sidebar.
        XCTAssertFalse(CavnarLayout.usesSidebar(nil))
        XCTAssertTrue(CavnarLayout.isWide(.regular))
        XCTAssertFalse(CavnarLayout.isWide(.compact))
    }

    func testFormSheetsAndTheReadableWidthAreTheIPadsOnly() {
        XCTAssertTrue(CavnarLayout.usesFormSheet(idiom: .pad))
        XCTAssertFalse(CavnarLayout.usesFormSheet(idiom: .phone))
        XCTAssertEqual(CavnarLayout.readableWidth, 720)
    }

    // MARK: Keyboard

    func testTheDigitsNameTheTabsInTheBarsOrder() {
        XCTAssertEqual(AppTab.forShortcut(1), .home)
        XCTAssertEqual(AppTab.forShortcut(2), .modules)
        XCTAssertEqual(AppTab.forShortcut(3), .ask)
        XCTAssertEqual(AppTab.forShortcut(4), .account)
        XCTAssertNil(AppTab.forShortcut(0))
        XCTAssertNil(AppTab.forShortcut(5))
        for tab in AppTab.allCases {
            XCTAssertEqual(AppTab.forShortcut(tab.shortcutDigit), tab)
        }
    }

    // MARK: The sidebar's selection

    func testTheSidebarHighlightsTheModuleWhoseScreenIsOpen() {
        XCTAssertEqual(SidebarItem.current(tab: .modules, moduleKey: "labor"), .module("labor"))
        XCTAssertEqual(SidebarItem.current(tab: .modules, moduleKey: nil), .tab(.modules))
        XCTAssertEqual(SidebarItem.current(tab: .modules, moduleKey: ""), .tab(.modules))
        // A key left over from the Modules tab never outranks another tab.
        XCTAssertEqual(SidebarItem.current(tab: .home, moduleKey: "labor"), .tab(.home))
        XCTAssertEqual(SidebarItem.current(tab: .ask, moduleKey: nil), .tab(.ask))
    }

    func testAModuleRowLivesInTheModulesTab() {
        XCTAssertEqual(SidebarItem.module("reviews").tab, .modules)
        XCTAssertEqual(SidebarItem.module("reviews").moduleKey, "reviews")
        XCTAssertEqual(SidebarItem.tab(.account).tab, .account)
        XCTAssertNil(SidebarItem.tab(.account).moduleKey)
    }

    func testTheSidebarListsOnlySwitchedOnModulesInTheServersOrder() {
        func m(_ key: String, _ status: String) -> ModuleSummary {
            ModuleSummary(key: key, label: key.capitalized, icon: key, status: status, kpi: nil)
        }
        let listed = SidebarItem.modules([m("reviews", "available"), m("waitlist", "coming_soon"),
                                          m("labor", "available")])
        XCTAssertEqual(listed.map(\.key), ["reviews", "labor"])
    }

    // MARK: Grids

    func testModuleTilesWidenOnAWideScreen() {
        XCTAssertEqual(HomeModuleGrid.minimumTileWidth(wide: false), 150)
        XCTAssertEqual(HomeModuleGrid.minimumTileWidth(wide: true), 200)
    }

    func testTheWeekGridIsOfferedOnlyOnAWideScreen() {
        typealias Pager = ScheduleWeekPager<EmptyView>
        XCTAssertEqual(Pager.modes(wide: false), [.day, .person])
        XCTAssertEqual(Pager.modes(wide: true), [.week, .day, .person])
        // The default: the grid on an iPad, the day pager on a phone.
        XCTAssertEqual(Pager.resolvedMode(nil, wide: true), .week)
        XCTAssertEqual(Pager.resolvedMode(nil, wide: false), .day)
        // Narrowed in Split View with the grid picked: back to the pager.
        XCTAssertEqual(Pager.resolvedMode(.week, wide: false), .day)
        XCTAssertEqual(Pager.resolvedMode(.person, wide: false), .person)
        XCTAssertEqual(Pager.resolvedMode(.day, wide: true), .day)
    }

    func testAGridCellHoldsThatPersonsShiftsOnThatDay() {
        let rows = [
            ScheduleRow(date: "2026-10-13", day: "Tuesday", employee: "Ana", role: "Server", shiftStart: "5:00pm",
                        shiftEnd: "10:00pm", scheduledHours: "5", notes: nil),
            ScheduleRow(date: "2026-10-12", day: "Monday", employee: "ana", role: "Bartender", shiftStart: "4:00pm",
                        shiftEnd: "11:00pm", scheduledHours: nil, notes: nil),
            // No day on the row: its date names it (10/12/26 is a Monday).
            ScheduleRow(date: "2026-10-12", day: nil, employee: "Ana", role: "Host", shiftStart: "9:00am",
                        shiftEnd: "1:00pm", scheduledHours: "4", notes: nil),
            ScheduleRow(date: "2026-10-12", day: "Monday", employee: "Bo", role: "Cook", shiftStart: "9:00am",
                        shiftEnd: "1:00pm", scheduledHours: "4", notes: nil),
        ]
        let people = ScheduleWeekMath.people(rows)
        let ana = try! XCTUnwrap(people.first { $0.name == "Ana" })
        XCTAssertEqual(ScheduleWeekMath.shifts(ana, on: "Monday").map(\.role), ["Host", "Bartender"])
        XCTAssertEqual(ScheduleWeekMath.shifts(ana, on: "Tuesday").map(\.role), ["Server"])
        XCTAssertTrue(ScheduleWeekMath.shifts(ana, on: "Sunday").isEmpty)
        XCTAssertEqual(ScheduleWeekMath.dayName(of: rows[2]), "Monday")
    }
}
