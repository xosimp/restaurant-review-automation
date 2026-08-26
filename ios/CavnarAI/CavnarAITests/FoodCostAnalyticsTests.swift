import XCTest
@testable import CavnarAI

final class FoodCostAnalyticsTests: XCTestCase {
    func testDecodesFoodCostAnalytics() throws {
        // Real shape mobile_food_cost_analytics returns (mobile_api.py) —
        // includes the Analytics-tab-redesign fields (critical_low/
        // reorder_soon/order_reduction and the annual/benchmark figures),
        // all sourced straight from analyse_inventory()'s own return dict.
        let json = """
        {"ok": true, "insight": "Waste is trending down.",
         "insight_intro": "Waste is trending down.",
         "insight_recommendations": ["Cut romaine par by 10%."],
         "insight_forecast": "Should keep improving next week.",
         "waste_items": [{"item": "Lettuce", "waste_cost": 12.5, "waste_pct": 22.0,
                           "waste_last_week": 4.0, "unit": "lb"}],
         "overstock": [{"item": "Chicken", "overstock_cost": 40.0,
                         "current_stock": 60.0, "par_level": 40.0, "unit": "lb"}],
         "critical_low": [{"item": "Shrimp", "unit": "lb", "days_remaining": 1.2,
                            "last_order_qty": 20.0, "suggested_order_qty": 30.0, "savings_vs_last": -18.4}],
         "reorder_soon": [{"item": "Chicken Breast", "unit": "lb", "days_remaining": 3.4,
                            "last_order_qty": 145.0, "suggested_order_qty": 150.0, "savings_vs_last": 12.2}],
         "order_reduction": [{"item": "Produce (misc)", "unit": "case",
                               "last_order_qty": 14.0, "suggested_order_qty": 9.0, "savings_vs_last": 34.0}],
         "price_watch": [{"item": "Parmesan Cheese", "kind": "trend", "change_pct": 18.1, "weeks": 3,
                           "old_price": 8.0, "new_price": 9.45, "is_big_8": true,
                           "action_hint": "Sustained rise — consider a menu price adjustment on dishes using this, or shop suppliers."}],
         "recoverable_monthly": 300.0, "annual_recoverable": 3600.0,
         "total_waste_cost_week": 347.0, "monthly_waste_projection": 1505.0,
         "annual_waste_projection": 18660.0, "waste_rate_pct": 4.8,
         "benchmark_label": "On Track", "benchmark_detail": "Near the 4-5% industry target",
         "total_stock_value": 8940.0, "total_items": 24,
         "week_start": "8/13/26", "week_end": "8/20/26", "last_updated": "8/20/26"}
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

        XCTAssertEqual(analytics.criticalLow.first?.item, "Shrimp")
        XCTAssertEqual(analytics.criticalLow.first?.suggestedOrderLabel, "30 lb")
        // 30 suggested vs 20 last order -> +10 more, shown in the caption
        // above the number instead of leaving the reader to subtract it.
        XCTAssertEqual(analytics.criticalLow.first?.orderCaption, "ORDER 10 MORE")
        XCTAssertEqual(analytics.reorderSoon.first?.item, "Chicken Breast")
        XCTAssertEqual(analytics.orderReduction.first?.item, "Produce (misc)")
        // 9 suggested vs 14 last order -> 5 less.
        XCTAssertEqual(analytics.orderReduction.first?.orderCaption, "ORDER 5 LESS")
        XCTAssertEqual(analytics.annualWasteProjection, 18660.0)
        XCTAssertEqual(analytics.annualRecoverable, 3600.0)
        XCTAssertEqual(analytics.wasteRatePct, 4.8)
        XCTAssertEqual(analytics.benchmarkLabel, "On Track")
        XCTAssertEqual(analytics.totalItems, 24)

        XCTAssertEqual(analytics.priceWatch.count, 1)
        let watch = analytics.priceWatch[0]
        XCTAssertEqual(watch.item, "Parmesan Cheese")
        XCTAssertTrue(watch.isTrend)
        XCTAssertEqual(watch.timeframeLabel, "3 weeks")
        XCTAssertTrue(watch.actionHint.contains("menu price"))
    }

    func testOrderCaptionFallsBackToPlainOrderWhenNoDeltaOrNoLastOrder() throws {
        let unchanged = try JSONDecoder.cavnar.decode(InventoryActionItem.self, from: Data("""
        {"item": "Butter", "unit": "lb", "last_order_qty": 10.0, "suggested_order_qty": 10.0}
        """.utf8))
        XCTAssertEqual(unchanged.orderCaption, "ORDER")

        let noHistory = try JSONDecoder.cavnar.decode(InventoryActionItem.self, from: Data("""
        {"item": "Butter", "unit": "lb", "suggested_order_qty": 10.0}
        """.utf8))
        XCTAssertEqual(noHistory.orderCaption, "ORDER")

        let skipped = try JSONDecoder.cavnar.decode(InventoryActionItem.self, from: Data("""
        {"item": "Butter", "unit": "lb", "last_order_qty": 10.0, "suggested_order_qty": 0}
        """.utf8))
        XCTAssertEqual(skipped.orderCaption, "ORDER")
    }

    func testInventoryActionItemSuggestsSkipWhenOrderQtyIsZero() throws {
        let json = """
        {"item": "Butter", "unit": "lb", "suggested_order_qty": 0}
        """
        let item = try JSONDecoder.cavnar.decode(InventoryActionItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.suggestedOrderLabel, "skip")
    }

    func testDecodesFoodCostTrend() throws {
        let json = """
        {"ok": true, "weeks": [
            {"label": "8/6", "start": "2026-07-31", "end": "2026-08-06", "waste": 280.0},
            {"label": "8/13", "start": "2026-08-07", "end": "2026-08-13", "waste": 347.0}
        ]}
        """
        let trend = try JSONDecoder.cavnar.decode(FoodCostTrend.self, from: Data(json.utf8))
        XCTAssertTrue(trend.ok)
        XCTAssertEqual(trend.weeks.count, 2)
        XCTAssertEqual(trend.weeks.last?.waste, 347.0)
    }
}
