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
         "benchmark_label": "Near target", "benchmark_detail": "Near the 4–5% starting target — you're at 4.8%",
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
        XCTAssertEqual(analytics.benchmarkLabel, "Near target")
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

// MARK: - Food Cost audit regressions
//
// Each case here corresponds to a finding that was verified against the
// implementation before it was fixed. The comments say what the behaviour
// used to be, because in most of these the old code read as perfectly
// reasonable at the call site.

final class FoodCostAuditRegressionTests: XCTestCase {

    /// The totals the server computes over EVERY item, not the truncated
    /// lists it sends for display. The client summed the visible five
    /// overstock rows and presented that as the restaurant's tied-up
    /// capital — an undercount by construction past a sixth item.
    func testDecodesServerSideTotalsRatherThanSummingATruncatedList() throws {
        let json = """
        {"ok": true, "insight_recommendations": [],
         "waste_items": [], "overstock": [], "critical_low": [],
         "reorder_soon": [], "order_reduction": [], "price_watch": [],
         "overstock_total": 1875.25, "waste_items_total": 412.10}
        """
        let a = try JSONDecoder.cavnar.decode(FoodCostAnalytics.self, from: Data(json.utf8))
        XCTAssertEqual(a.overstockTotal, 1875.25)
        XCTAssertEqual(a.wasteItemsTotal, 412.10)
    }

    /// verify_figures was computed server-side and discarded; the client
    /// declared no such key, so figures the backend could not trace back to
    /// the data were rendered at full authority.
    func testUnverifiedFiguresSurfaceAsACaveatList() throws {
        let json = """
        {"ok": true, "insight_recommendations": [],
         "waste_items": [], "overstock": [], "critical_low": [],
         "reorder_soon": [], "order_reduction": [], "price_watch": [],
         "insight_unverified": "$340, 22%"}
        """
        let a = try JSONDecoder.cavnar.decode(FoodCostAnalytics.self, from: Data(json.utf8))
        XCTAssertTrue(a.hasUnverifiedFigures)
        XCTAssertEqual(a.unverifiedFigureList, ["$340", "22%"])
    }

    func testNoUnverifiedKeyMeansNoCaveat() throws {
        let json = """
        {"ok": true, "insight_recommendations": [],
         "waste_items": [], "overstock": [], "critical_low": [],
         "reorder_soon": [], "order_reduction": [], "price_watch": []}
        """
        let a = try JSONDecoder.cavnar.decode(FoodCostAnalytics.self, from: Data(json.utf8))
        XCTAssertFalse(a.hasUnverifiedFigures)
        XCTAssertTrue(a.unverifiedFigureList.isEmpty)
    }

    /// The chart used to back-solve its own target from this week's
    /// analytics and draw it across bars from a different endpoint. The
    /// server now sends the target with the series.
    func testTrendCarriesTheServerComputedTarget() throws {
        let json = """
        {"ok": true,
         "weeks": [{"label": "8/20", "start": "2026-08-14", "end": "2026-08-20", "waste": 347.0}],
         "target": {"pct": 4.5, "weekly": 312.75, "basis": "history"}}
        """
        let trend = try JSONDecoder.cavnar.decode(FoodCostTrend.self, from: Data(json.utf8))
        XCTAssertEqual(trend.target?.weekly, 312.75)
        XCTAssertEqual(trend.target?.pct, 4.5)
        XCTAssertEqual(trend.target?.basis, "history")
    }

    /// An older server sends no target; the chart must simply not draw the
    /// line rather than fall back to inventing one.
    func testTrendWithoutATargetDecodesCleanly() throws {
        let json = """
        {"ok": true, "weeks": []}
        """
        let trend = try JSONDecoder.cavnar.decode(FoodCostTrend.self, from: Data(json.utf8))
        XCTAssertNil(trend.target)
    }

    /// A blank price field was coerced to 0 and stored as this week's price
    /// of record, becoming next week's baseline and silently suppressing
    /// that ingredient's drift alert.
    @MainActor
    func testABlankPriceIsNotSubmittable() {
        let vm = FoodCostQuickEntryViewModel()
        vm.items = [FoodCostItem(name: "Romaine", unit: "lb")]
        XCTAssertFalse(vm.canSubmit, "a row with no price must not be submittable")
        XCTAssertEqual(vm.rowsMissingAPrice, ["Romaine"])
    }

    @MainActor
    func testARowWithAPriceIsSubmittable() {
        let vm = FoodCostQuickEntryViewModel()
        var item = FoodCostItem(name: "Romaine", unit: "lb")
        item.priceText = "2.50"
        vm.items = [item]
        XCTAssertTrue(vm.canSubmit)
        XCTAssertTrue(vm.rowsMissingAPrice.isEmpty)
    }

    /// Double("3,50") is nil in every comma-decimal locale, and the nil fell
    /// back to a confident $0.00.
    func testPriceParsingHandlesAPlainDecimal() {
        XCTAssertEqual(FoodCostQuickEntryViewModel.parsedPrice("3.50"), 3.50)
        XCTAssertNil(FoodCostQuickEntryViewModel.parsedPrice(""))
        XCTAssertNil(FoodCostQuickEntryViewModel.parsedPrice("   "))
        XCTAssertNil(FoodCostQuickEntryViewModel.parsedPrice("abc"))
    }

    /// Dishes whose ingredients aren't all priced are their own group. They
    /// used to be costed with COALESCE(unit_cost, 0), so a dish whose main
    /// protein had never been priced showed an excellent margin.
    func testMenuProfitabilitySeparatesDishesWithUncostedIngredients() throws {
        let json = """
        {"ok": true, "priced": [], "unpriced": [], "unmapped": [],
         "uncosted": [{"id": 3, "name": "Burger", "sell_price": 15.0,
                        "ingredient_count": 2, "uncosted_ingredients": 1}],
         "average_food_cost_pct": null, "average_basis": null,
         "has_sales_data": false, "highest_food_cost": null}
        """
        let p = try JSONDecoder.cavnar.decode(MenuProfitability.self, from: Data(json.utf8))
        XCTAssertEqual(p.uncosted.count, 1)
        XCTAssertEqual(p.uncosted.first?.uncostedIngredients, 1)
        XCTAssertFalse(p.isEmpty, "a menu with only uncosted dishes is not an empty menu")
    }

    /// "Best" means the biggest contributor, not the lowest food cost
    /// percentage — a $3 soda used to outrank a $36 steak carrying $27 of
    /// gross margin.
    func testMenuProfitabilityCarriesContributionAndPopularity() throws {
        let json = """
        {"ok": true, "unpriced": [], "unmapped": [], "uncosted": [],
         "priced": [{"id": 1, "name": "Ribeye", "sell_price": 36.0, "plate_cost": 9.0,
                      "margin": 27.0, "food_cost_pct": 25.0, "margin_pct": 75.0,
                      "units_sold": 100.0, "total_contribution": 2700.0, "ingredient_count": 3}],
         "average_food_cost_pct": 23.8,
         "average_basis": "weighted by units sold over the last 28 days",
         "has_sales_data": true,
         "best": {"id": 1, "name": "Ribeye", "sell_price": 36.0, "plate_cost": 9.0,
                   "margin": 27.0, "food_cost_pct": 25.0, "margin_pct": 75.0,
                   "units_sold": 100.0, "total_contribution": 2700.0, "ingredient_count": 3},
         "highest_food_cost": null}
        """
        let p = try JSONDecoder.cavnar.decode(MenuProfitability.self, from: Data(json.utf8))
        XCTAssertEqual(p.best?.name, "Ribeye")
        XCTAssertEqual(p.priced.first?.totalContribution, 2700.0)
        XCTAssertEqual(p.priced.first?.unitsSold, 100.0)
        XCTAssertEqual(p.hasSalesData, true)
        XCTAssertTrue((p.averageBasis ?? "").contains("weighted"))
    }
}
