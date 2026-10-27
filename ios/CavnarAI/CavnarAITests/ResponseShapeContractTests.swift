import XCTest
@testable import CavnarAI

/// Every screen's main read, decoded exactly as the server sends it
/// (release-safety audit #25: A04-04, A06-03, A05-14). The fixtures are
/// CAPTURED, never written by hand: tests/test_ios_response_shapes.py calls
/// each real route on the fictional demo store (tests/ios_shape_capture.py),
/// fails when a payload changes shape, and re-captures these files
/// (CAVNAR_WRITE_IOS_FIXTURES=1). This test decodes each one with the app's
/// own model, so a model change that cannot read the server fails here —
/// TRUST-1 (10/10/26) was a hand-written fixture hiding exactly that.
///
/// `<route>.v1.json` is the shape the builds installed today read; the
/// server may only add to it until MOBILE_MIN_BUILD retires them. The
/// current build must still decode it too.
final class ResponseShapeContractTests: XCTestCase {

    private static func data(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle(for: ResponseShapeContractTests.self).url(forResource: name,
                                                                                  withExtension: "json"),
                                "Fixtures/\(name).json is not in the test bundle")
        return try Data(contentsOf: url)
    }

    private static func object(_ name: String, part: String? = nil) throws -> [String: Any] {
        let all = try XCTUnwrap(JSONSerialization.jsonObject(with: data(name)) as? [String: Any])
        guard let part else { return all }
        return try XCTUnwrap(all[part] as? [String: Any], "\(name).json has no \(part)")
    }

    /// `name` (and its .v1 copy) decoded as `T`, from `part` when the fixture
    /// holds several views.
    private static func decode<T: Decodable>(_ type: T.Type, _ name: String, part: String? = nil,
                                             file: StaticString = #filePath, line: UInt = #line) throws -> [T] {
        var out: [T] = []
        for fixture in [name, name + ".v1"] {
            let payload = try JSONSerialization.data(withJSONObject: object(fixture, part: part))
            do {
                out.append(try JSONDecoder.cavnar.decode(T.self, from: payload))
            } catch {
                let (path, why) = ClientErrorReport.describe(error)
                XCTFail("\(fixture)\(part.map { "[\($0)]" } ?? "") does not decode as \(T.self) at \(path): \(why)",
                        file: file, line: line)
                throw error
            }
        }
        return out
    }

    private static func count(_ raw: [String: Any], _ key: String) -> Int? {
        (raw[key] as? [Any])?.count
    }

    // MARK: - Sign-in and Account

    func testMe() throws {
        let raw = try Self.object("me")
        for me in try Self.decode(SessionStore.MeResponse.self, "me") {
            XCTAssertTrue(me.ok)
            XCTAssertFalse(me.user.username.isEmpty)
            XCTAssertEqual(me.timezone, raw["timezone"] as? String)
        }
    }

    func testAccount() throws {
        for account in try Self.decode(AccountSummary.self, "account") {
            XCTAssertTrue(account.ok)
        }
    }

    // MARK: - Home (the brief rides on it)

    func testHome() throws {
        let raw = try Self.object("home")
        for home in try Self.decode(HomeSummary.self, "home") {
            XCTAssertEqual(home.restaurantName, raw["restaurant_name"] as? String)
            XCTAssertEqual(home.modules.count, Self.count(raw, "modules"))
            XCTAssertEqual(home.needsAttention.count, Self.count(raw, "needs_attention"))
        }
    }

    func testActions() throws {
        let raw = try Self.object("actions")
        for waiting in try Self.decode(CommandWaitingResponse.self, "actions") {
            XCTAssertEqual(waiting.items?.count, Self.count(raw, "items"))
        }
        for actions in try Self.decode(HomeFollowThroughViewModel.ActionsResponse.self, "actions") {
            XCTAssertEqual(actions.items.count, Self.count(raw, "items"))
        }
        for widget in try Self.decode(WidgetSnapshotService.ActionsResponse.self, "actions") {
            XCTAssertEqual(widget.items?.count, Self.count(raw, "items"))
        }
    }

    func testNotifications() throws {
        let raw = try Self.object("notifications")
        for list in try Self.decode(NotificationsListViewModel.Response.self, "notifications") {
            XCTAssertTrue(list.ok)
            XCTAssertEqual(list.notifications.count, Self.count(raw, "notifications"))
        }
    }

    // MARK: - Modules

    func testReviews() throws {
        let raw = try Self.object("reviews")
        for page in try Self.decode(ReviewsListViewModel.ReviewsResponse.self, "reviews") {
            XCTAssertEqual(page.reviews.count, Self.count(raw, "reviews"))
            XCTAssertEqual(page.total, raw["total"] as? Int)
        }
    }

    func testLabor() throws {
        let raw = try Self.object("labor")
        for labor in try Self.decode(LaborStats.self, "labor") {
            XCTAssertTrue(labor.ok)
            XCTAssertEqual(labor.isLive, raw["is_live"] as? Bool)
            XCTAssertEqual(labor.roleSummary.count, Self.count(raw, "role_summary"))
        }
    }

    func testFoodCost() throws {
        let raw = try Self.object("food_cost")
        for food in try Self.decode(FoodCostAnalytics.self, "food_cost") {
            XCTAssertTrue(food.ok)
            XCTAssertEqual(food.reorderSoon.count, Self.count(raw, "reorder_soon"))
        }
    }

    func testFoodCostBar() throws {
        let raw = try Self.object("food_cost_bar")
        for bar in try Self.decode(BarPayload.self, "food_cost_bar") {
            XCTAssertEqual(bar.available, raw["available"] as? Bool)
            XCTAssertEqual(bar.insights.count, Self.count(raw, "insights"))
        }
    }

    func testMarketing() throws {
        let raw = try Self.object("marketing")
        for marketing in try Self.decode(MarketingViewModel.MarketingResponse.self, "marketing") {
            XCTAssertTrue(marketing.ok)
            XCTAssertEqual(marketing.calendar.count, Self.count(raw, "calendar"))
        }
    }

    func testIntel() throws {
        let raw = try Self.object("intel")
        for intel in try Self.decode(IntelSummary.self, "intel") {
            XCTAssertEqual(intel.hasData, raw["has_data"] as? Bool)
            XCTAssertEqual(intel.competitors.count, Self.count(raw, "competitors"))
        }
    }

    // MARK: - The Daily Sales Report, as the owner and as a manager

    func testTheNightsReport() throws {
        for view in ["owner", "manager"] {
            let raw = try Self.object("dsr_preview", part: view)
            for report in try Self.decode(DSRReport.self, "dsr_preview", part: view) {
                XCTAssertEqual(report.view, raw["view"] as? String)
                XCTAssertEqual(report.businessDate, raw["business_date"] as? String)
                XCTAssertFalse(report.orderedBlocks.isEmpty, view)
                XCTAssertEqual(report.narrative != nil, raw["narrative"] is [String: Any], view)
                // The narrative decodes leniently (a line it cannot read is
                // dropped, never the report): every line must survive.
                if let narrative = report.narrative, let lines = raw["narrative"] as? [String: Any] {
                    XCTAssertEqual(narrative.wentWell.count, Self.count(lines, "went_well") ?? 0, view)
                    XCTAssertEqual(narrative.needsAttention.count, Self.count(lines, "needs_attention") ?? 0, view)
                    XCTAssertEqual(narrative.actionsTomorrow.count, Self.count(lines, "actions_tomorrow") ?? 0, view)
                }
            }
        }
    }

    func testTheWeek() throws {
        for view in ["owner", "manager"] {
            let raw = try Self.object("dsr_week", part: view)
            let week = try XCTUnwrap(raw["week"] as? [String: Any])
            for response in try Self.decode(DSRWeekResponse.self, "dsr_week", part: view) {
                XCTAssertEqual(response.week.days.count, Self.count(week, "days"))
                XCTAssertEqual(response.week.categories.count, Self.count(week, "categories"))
            }
        }
    }

    // MARK: - The staff app

    func testStaffToday() throws {
        let raw = try Self.object("staff_shifts")
        for shifts in try Self.decode(StaffShiftsResponse.self, "staff_shifts") {
            XCTAssertTrue(shifts.ok)
            XCTAssertEqual(shifts.published, raw["published"] as? Bool)
            XCTAssertEqual(shifts.week?.count, Self.count(raw, "week"))
        }
    }

    func testStaffMe() throws {
        // Three screens read /staff/api/me, each with its own model.
        for profile in try Self.decode(StaffProfileResponse.self, "staff_me") {
            XCTAssertNotNil(profile.employee)
        }
        for account in try Self.decode(StaffAccountResponse.self, "staff_me") {
            XCTAssertNotNil(account.employee)
        }
        for me in try Self.decode(StaffMeEnvelope.self, "staff_me") {
            XCTAssertFalse(me.employee.name.isEmpty)
        }
    }

    // MARK: - The registry

    /// Every fixture tests/test_ios_response_shapes.py captures is decoded
    /// above — that test holds this list to its own.
    static let decodedFixtures = ["me", "account", "home", "actions", "notifications", "reviews", "labor",
                                  "food_cost", "food_cost_bar", "marketing", "intel", "dsr_preview", "dsr_week",
                                  "staff_shifts", "staff_me"]

    func testEveryCapturedFixtureIsInTheBundle() throws {
        for name in Self.decodedFixtures {
            XCTAssertNoThrow(try Self.data(name), name)
            XCTAssertNoThrow(try Self.data(name + ".v1"), name + ".v1")
        }
    }
}
