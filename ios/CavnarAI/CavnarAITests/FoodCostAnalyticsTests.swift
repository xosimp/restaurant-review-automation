import XCTest
@testable import CavnarAI

final class FoodCostAnalyticsTests: XCTestCase {
    func testDecodesFoodCostAnalytics() throws {
        let json = """
        {"ok": true, "insight": "Waste is trending down.",
         "insight_intro": "Waste is trending down.",
         "insight_recommendations": ["Cut romaine par by 10%."],
         "insight_forecast": "Should keep improving next week.",
         "waste_items": [{"item": "Lettuce", "waste_cost": 12.5, "waste_pct": 22.0,
                           "waste_last_week": 4.0, "unit": "lb"}],
         "overstock": [{"item": "Chicken", "overstock_cost": 40.0,
                         "current_stock": 60.0, "par_level": 40.0, "unit": "lb"}],
         "recoverable_monthly": 300.0}
        """
        let analytics = try JSONDecoder.cavnar.decode(FoodCostAnalytics.self, from: Data(json.utf8))
        XCTAssertTrue(analytics.ok)
        XCTAssertEqual(analytics.wasteItems.count, 1)
        XCTAssertEqual(analytics.wasteItems.first?.item, "Lettuce")
        XCTAssertEqual(analytics.overstock.first?.overstockCost, 40.0)
        XCTAssertEqual(analytics.recoverableMonthly, 300.0)
        XCTAssertEqual(analytics.insight?.intro, "Waste is trending down.")
        XCTAssertEqual(analytics.insight?.recommendations, ["Cut romaine par by 10%."])
        XCTAssertEqual(analytics.insight?.forecast, "Should keep improving next week.")
    }
}
