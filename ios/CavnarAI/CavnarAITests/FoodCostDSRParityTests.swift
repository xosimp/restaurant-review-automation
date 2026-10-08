import XCTest
@testable import CavnarAI
import UniformTypeIdentifiers

/// The iOS parity round (10/7/26), Food Cost and the nightly report: every
/// payload the new screens decode, and every body they post, in the
/// server's own key names.
final class FoodCostDSRParityTests: XCTestCase {
    private func json(_ s: String) -> Data { Data(s.utf8) }

    private func block(_ s: String) throws -> DSRBlock {
        DSRBlock(json: try JSONDecoder.cavnar.decode(JSONValue.self, from: json(s)))
    }

    private func object(_ e: some Encodable) throws -> [String: Any] {
        try JSONSerialization.jsonObject(with: JSONEncoder().encode(e)) as! [String: Any]
    }

    // MARK: #14 — the night in detail

    func testTheServiceBlockReadsInOrderRightAfterLabor() {
        XCTAssertEqual(DSRBlock.order, ["sales", "labor", "service", "food", "reviews", "intel", "marketing", "closeout"])
        XCTAssertEqual(DSRBlock.titles["service"], "The night in detail")
    }

    private let service = """
    {"status": "ready", "source": "rpower",
     "metrics": {"checks": 112, "servers_measured": 2, "drinks_per_guest": 1.4, "server_floor_per_guest": 38.5},
     "detail": {
       "dayparts": [{"name": "Dinner", "net": 5200, "guests": 140, "checks": 80, "per_guest": 37.14, "share_pct": 74.6},
                    {"name": "Lunch", "net": 1775, "guests": 60, "checks": 32, "per_guest": 29.58, "share_pct": 25.4}],
       "rooms": [{"name": "Bar", "net": 2100, "guests": 50, "checks": 40, "per_guest": 42.0, "share_pct": 30.1}],
       "servers": [{"name": "Dana", "checks": 30, "guests": 70, "net": 2800, "per_guest": 40.0, "drinks_per_guest": 1.6,
                    "tip_pct": 19.5, "hours": 7.5, "net_per_hour": 373.33, "vs_floor": 1.5}],
       "servers_below_floor": 3, "servers_min_checks": 8, "servers_basis": "people who carried at least 8 checks",
       "loss": {"given": {"total": 210.5, "lines": 9, "pct_of_gross": 2.6,
                          "by_reason": [{"kind": "comp", "reason": "Long wait", "lines": 2, "amount": 64.0}],
                          "by_approver": [{"approver": "Erik", "lines": 9, "amount": 210.5}]},
                "voids": {"total": 40.0, "lines": 2, "by_reason": [{"kind": "void", "reason": "Rung twice", "lines": 2, "amount": 40.0}],
                          "by_approver": []}},
       "register": {"tenders": [{"method": "Visa", "kind": "card", "payments": 60, "amount": 4100.0, "tips": 700.0, "tip_fees": 12.0}],
                    "totals": {"card": 4100.0, "cash": 900.0, "other": 0, "all": 5000.0, "card_tips": 700.0,
                               "card_tip_fees": 12.0, "payouts": 45.0, "payins": 0},
                    "payouts": [{"category": "Petty cash", "type": "payout", "amount": 45.0, "approved_by": "Office Drawer",
                                 "at": "2026-10-05T18:02", "reference": null}],
                    "payouts_note": null},
       "timeclock_edits": [{"employee": "Sam", "role": "Cook", "clock_in": "2026-10-05T15:58", "clock_out": "2026-10-05T23:10",
                            "hours": 7.2, "edited_by": "Erik", "edited_at": "2026-10-05T23:40", "code": "3"}]
     }}
    """

    func testTheServiceBlockDecodesEveryPartTheWebDraws() throws {
        let b = try block(service)
        XCTAssertTrue(b.isReady)
        XCTAssertEqual(b.dayparts.map(\.name), ["Dinner", "Lunch"])
        XCTAssertTrue(b.showsDayparts)
        XCTAssertEqual(b.rooms.first?.sharePct, 30.1)
        XCTAssertEqual(b.servers.first?.name, "Dana")
        XCTAssertEqual(b.servers.first?.vsFloor, 1.5)
        XCTAssertEqual(b.serversBelowFloor, 3)
        XCTAssertEqual(b.lossGiven?.total, 210.5)
        XCTAssertEqual(b.lossGiven?.byReason.first?.label, "Comp \u{00B7} Long wait")
        XCTAssertEqual(b.lossGiven?.byApprover.first?.label, "Erik")
        XCTAssertEqual(b.lossVoids?.byReason.first?.label, "Rung twice")
        XCTAssertEqual(b.register?.all, 5000)
        XCTAssertEqual(b.register?.payouts.first?.approvedBy, "Office Drawer")
        XCTAssertEqual(b.punchEdits?.count, 1)
        XCTAssertEqual(DSRPunchEdits.line(b.punchEdits![0]),
                       "3:58pm\u{2013}11:10pm \u{00B7} 7.20h \u{00B7} edited by Erik at 11:40pm \u{00B7} RPOWER code 3")
        XCTAssertEqual(DSRHeadline.line(for: "service", b), "112 checks \u{00B7} 2 servers \u{00B7} $210.50 given away")
    }

    /// What dsr/access withholds is absent from the payload, so it is not
    /// drawn: no loss without the comps-and-voids grant, no punch edits
    /// outside the owner view — "no list" is not "no edits".
    func testAWithheldPartIsAbsentNotEmpty() throws {
        let b = try block("""
        {"status": "ready", "metrics": {}, "detail": {"servers": []}}
        """)
        XCTAssertNil(b.lossGiven)
        XCTAssertNil(b.punchEdits)
        XCTAssertNil(b.register)
        let none = try block("""
        {"status": "ready", "metrics": {}, "detail": {"timeclock_edits": []}}
        """)
        XCTAssertEqual(none.punchEdits?.count, 0)
    }

    func testLaborDepartmentsAddTheSalariesForTheOwnerOnly() throws {
        let owner = try block("""
        {"status": "ready", "metrics": {"salaried_cost": 300},
         "detail": {"departments": [{"department": "FOH", "hours": 40, "cost": 600, "pct_of_sales": 8.6, "roles": ["Server"]}]}}
        """)
        XCTAssertEqual(owner.departments.map(\.name), ["FOH", "Salaried"])
        XCTAssertTrue(owner.departments.last!.salaried)
        let manager = try block("""
        {"status": "ready", "metrics": {},
         "detail": {"departments": [{"department": "FOH", "hours": 40, "cost": 600}]}}
        """)
        XCTAssertEqual(manager.departments.map(\.name), ["FOH"])
    }

    private func hours(_ nets: [Double], usual: [Double]? = nil, people: [Double]? = nil) throws -> DSRHourStory? {
        let hs = nets.enumerated().map { "{\"hour\": \(16 + $0.offset), \"net\": \($0.element)}" }.joined(separator: ",")
        var typical = ""
        if let usual {
            let u = usual.enumerated().map { "\"\(16 + $0.offset)\": \($0.element)" }.joined(separator: ",")
            typical = ", \"hourly_typical\": {\"hours\": {\(u)}, \"nights\": 4}"
        }
        let sales = try block("{\"status\": \"ready\", \"metrics\": {}, \"detail\": {\"hourly\": [\(hs)]\(typical)}}")
        var labor: DSRBlock?
        if let people {
            let p = people.enumerated().map { "\"\(16 + $0.offset)\": \($0.element)" }.joined(separator: ",")
            labor = try block("{\"status\": \"ready\", \"metrics\": {}, \"detail\": {\"hourly_hours\": {\(p)}}}")
        }
        return DSRHourStory(sales: sales, labor: labor)
    }

    func testTheHourStoryNamesTheRushThePeakAndAMissedHour() throws {
        let story = try XCTUnwrap(try hours([200, 300, 900, 1400, 1100, 400],
                                            usual: [220, 320, 850, 1300, 1050, 700]))
        XCTAssertEqual(story.peak.hour, 19)
        XCTAssertEqual(DSRHourStory.hourLong(19), "7pm")
        let kinds = story.callouts.map(\.kind)
        XCTAssertTrue(kinds.contains("rush"))
        XCTAssertTrue(kinds.contains("missed"), "\(kinds)")
        XCTAssertEqual(story.story(weekday: "Saturday"),
                       "The rush began at 6pm and peaked at 7pm with $1,400, 8% above a usual Saturday. 9pm came in below a usual Saturday.")
    }

    func testTheHourStoryNeedsThreeHoursOfAReadySalesBlock() throws {
        XCTAssertNil(try hours([200, 300]))
        XCTAssertNil(DSRHourStory(sales: nil, labor: nil))
    }

    func testStaffingAheadOfDemandIsCalledOut() throws {
        let story = try XCTUnwrap(try hours([600, 1200, 1400, 1300, 200], people: [4, 6, 7, 6, 6]))
        XCTAssertTrue(story.hasLabor)
        XCTAssertTrue(story.callouts.contains { $0.kind == "labor" }, "\(story.callouts.map(\.kind))")
    }

    // MARK: #33 — the day after, the game, slowest items, unmapped departments

    func testTheDayAftersLaborAndOvertimeDecodeAndSayWhoseTargetItIs() throws {
        let t = try JSONDecoder.cavnar.decode(DSRTomorrow.self, from: json("""
        {"date": "2026-10-06", "weekday": "Tuesday", "items": [],
         "labor": {"hours": 64, "hourly_cost": 1100, "hourly_pct": 24.4, "forecast_net": 4500, "overtime_hours": 2,
                   "salaried_total_cost": 1400, "salaried_total_pct": 31.1, "target_pct": 30, "target_source": "default",
                   "basis": "tomorrow's published schedule"},
         "overtime": {"people": [{"employee": "Sam", "role": "Cook", "projected_hours": 44, "overtime_hours": 4,
                                  "extra_cost": 36, "room": ["Lee"]}], "over_count": 1, "extra_cost": 36, "basis": "x"}}
        """))
        let l = try XCTUnwrap(t.labor)
        XCTAssertEqual(l.gap(pct: l.salariedTotalPct), 1.1)
        XCTAssertEqual(l.targetLine(pct: l.salariedTotalPct), "1.1 pts over Cavnar AI\u{2019}s starting 30%")
        XCTAssertEqual(l.targetLine(pct: 30), "On Cavnar AI\u{2019}s starting 30%")
        XCTAssertEqual(t.overtime?.people.first?.room, ["Lee"])
        XCTAssertEqual(t.overtime?.overCount, 1)
    }

    func testSlowestItemsAndUnmappedDepartmentsRead() throws {
        let b = try block("""
        {"status": "ready", "metrics": {"net": 100},
         "detail": {"bottom_items": [{"name": "Kale salad", "qty": 1, "net": 12}],
                    "unmapped": [{"department": "Pool", "net": 80, "new_in": "Other", "mapped_to": "Darts"},
                                 {"department": "Merch", "net": 20}],
                    "unallocated": 4.5}}
        """)
        XCTAssertEqual(b.slowestItems.first?.name, "Kale salad")
        XCTAssertEqual(b.unmappedDepartments.map(\.department), ["Pool", "Merch"])
        XCTAssertEqual(DSRCategoryMapSheet.why(b.unmappedDepartments[0]),
                       "New inside Other, which counts toward Darts \u{2014} not counted there until you pick")
        XCTAssertEqual(b.unallocated, 4.5)
    }

    func testTheGameCardNamesTheOtherGamesFromAnOlderReportsText() throws {
        let g = try JSONDecoder.cavnar.decode(JSONValue.self, from: json("""
        {"text": "Cubs at home. Also tonight: Bears at Packers.", "tonight": {"net": 9000}}
        """))
        XCTAssertEqual(DSRGameCard.alsoTonight(g), "Bears at Packers")
    }

    func testMappingADepartmentPostsTheKeysTheRouteReads() throws {
        let o = try object(DSRCategoryBody(posName: "Pool", category: "Darts"))
        XCTAssertEqual(o["pos_name"] as? String, "Pool")
        XCTAssertEqual(o["category"] as? String, "Darts")
    }

    // MARK: #63 — the budget

    @MainActor
    func testABudgetSaveSendsEveryNightWithExplicitBlanks() throws {
        let body = DSRBudgetBody(days: [.init(date: "2026-10-07", gross: 5000, net: nil)])
        let o = try object(body)
        let day = (o["days"] as! [[String: Any]])[0]
        XCTAssertEqual(day["date"] as? String, "2026-10-07")
        XCTAssertEqual(day["gross"] as? Double, 5000)
        XCTAssertTrue(day.keys.contains("net"), "a blank clears the night's net — sent as null")
        XCTAssertTrue(day["net"] is NSNull)
        XCTAssertEqual(DSRBudgetViewModel.parse("$1,250").value, 1250)
        XCTAssertNil(DSRBudgetViewModel.parse("").value)
        XCTAssertTrue(DSRBudgetViewModel.parse("-4").bad)
    }

    func testThePrefillDecodes() throws {
        let p = try JSONDecoder.cavnar.decode(DSRBudgetPrefill.self, from: json("""
        {"ok": true, "days": [{"date": "2026-10-07", "gross": 5100.4, "net": 4600, "from": "last week's budget"}],
         "missing": ["2026-10-08"], "basis": "Last week's budget."}
        """))
        XCTAssertEqual(p.days.first?.net, 4600)
        XCTAssertEqual(p.missing, ["2026-10-08"])
    }

    // MARK: #64 — settings and the off switch

    func testASettingSavesOneFieldAndClearsWithAnExplicitNull() throws {
        XCTAssertEqual(try object(DSRSettingBody(field: "dsr_notify", value: .bool(true)))["dsr_notify"] as? Bool, true)
        XCTAssertEqual(try object(DSRSettingBody(field: "dsr_deadline_hour", value: .int(5)))["dsr_deadline_hour"] as? Int, 5)
        let cleared = try object(DSRSettingBody(field: "dsr_late_night_hour", value: .null))
        XCTAssertTrue(cleared["dsr_late_night_hour"] is NSNull)
    }

    func testTheSettingsPayloadDecodes() throws {
        let s = try JSONDecoder.cavnar.decode(DSRSettingsPayload.self, from: json("""
        {"ok": true, "can_edit": true,
         "settings": {"dsr_enabled": false, "dsr_notify": true, "dsr_gross_basis": "all", "dsr_deadline_hour": 5,
                      "dsr_late_night_hour": null, "calendar_label": "Period 10 · Week 2", "fiscal_year_start": null},
         "categories": ["Food", "Liquor"], "category_map": [{"pos_name": "Other", "category": "Darts"}],
         "held": {"Other": ["Darts", "Pool"]}, "contents": {}, "unmapped": [{"department": "Merch", "net": 20}],
         "unmapped_as_of": "10/5/26"}
        """))
        XCTAssertEqual(s.settings?.dsrEnabled, false)
        XCTAssertEqual(s.settings?.dsrGrossBasis, "all")
        XCTAssertNil(s.settings?.dsrLateNightHour)
        XCTAssertEqual(s.categoryMap.first?.posName, "Other")
        XCTAssertEqual(s.holds("Other"), "holds Darts, Pool")
        XCTAssertEqual(s.unmapped.first?.asUnmapped.department, "Merch")
    }

    func testTheListCarriesTheSwitchAndTheAppRemembersIt() throws {
        let off = try JSONDecoder.cavnar.decode(DSRListResponse.self, from: json("""
        {"ok": true, "view": "owner", "enabled": false, "reports": []}
        """))
        XCTAssertEqual(off.enabled, false)
        let older = try JSONDecoder.cavnar.decode(DSRListResponse.self, from: json("""
        {"ok": true, "reports": []}
        """))
        XCTAssertNil(older.enabled)
        let saved = UserDefaults.standard.object(forKey: DSRAvailability.key)
        defer { UserDefaults.standard.set(saved, forKey: DSRAvailability.key) }
        DSRAvailability.record(false)
        XCTAssertFalse(DSRAvailability.isEnabled)
        XCTAssertFalse(CommandMatch.fallbackPlaces.contains { $0.nav == "dsr" })
        DSRAvailability.record(nil)
        XCTAssertFalse(DSRAvailability.isEnabled, "an older server's silence changes nothing")
        DSRAvailability.record(true)
        XCTAssertTrue(CommandMatch.fallbackPlaces.contains { $0.nav == "dsr" })
    }

    func testTheWeeksWorkbookIsNamedWithoutSlashes() {
        XCTAssertEqual(DSRWeekWorkbook.filename(start: "2026-09-16"), "Daily sales - week of 9-16-26.xlsx")
    }

    // MARK: #8 — synced inventory

    func testTheSyncSourceSaysWhereCountsComeFrom() throws {
        let s = try JSONDecoder.cavnar.decode(InventorySyncSource.self, from: json("""
        {"provider": "marketman", "label": "MarketMan", "connected": true, "synced": true,
         "synced_at": "2026-10-06T05:00:00", "error": null}
        """))
        XCTAssertTrue(s.synced)
        XCTAssertEqual(s.line("Counts"), "Counts from MarketMan \u{00B7} synced 10/6/26")
        XCTAssertEqual(InventorySyncSource(synced: true).line("Prices"), "Prices from your inventory system")
    }

    // MARK: #23 / #41 — the count, waste and receiving

    @MainActor
    func testTheCountSheetCountsChangesAndStepsNeverBelowZero() throws {
        let vm = CountSheetViewModel(persistsDraft: false)
        vm.items = try JSONDecoder.cavnar.decode([CountSheetItem].self, from: json("""
        [{"ingredient_id": 1, "name": "Basil", "expected": 2},
         {"ingredient_id": 2, "name": "Dough", "expected": 10, "supplier_name": "Sysco", "supplier_email": "s@x.com"}]
        """))
        vm.counts = [1: "2", 2: "10"]
        XCTAssertFalse(vm.hasUnsaved)
        vm.step(1, by: -1)
        vm.step(1, by: -1)
        vm.step(1, by: -1)
        XCTAssertEqual(vm.counts[1], "0")
        XCTAssertEqual(vm.progressLine, "1 of 2 changed")
        XCTAssertTrue(vm.hasUnsaved)
        vm.source = InventorySyncSource(synced: true)
        XCTAssertFalse(vm.hasUnsaved, "a synced sheet has nothing of its own to lose")
    }

    @MainActor
    func testQueuedWasteCarriesEveryKeyAndItsIdempotencyKey() throws {
        let body = WasteLogViewModel.Body(ingredientId: 7, qty: 2.5, reason: "spoiled", idempotencyKey: "waste:abc")
        let write = try XCTUnwrap(QueuedWrite.waste(body))
        XCTAssertEqual(write.path, "/mobile/api/food-cost/waste")
        let o = try JSONSerialization.jsonObject(with: write.bodyJSON!) as! [String: Any]
        XCTAssertEqual(o["ingredient_id"] as? Int, 7)
        XCTAssertEqual(o["qty"] as? Double, 2.5)
        XCTAssertEqual(o["reason"] as? String, "spoiled")
        XCTAssertEqual(o["idempotency_key"] as? String, "waste:abc")
        XCTAssertTrue(QueuedWrite.standsAlone(path: write.path))
        XCTAssertNil(QueuedWrite.waste(WasteLogViewModel.Body(ingredientId: 7, qty: 1, reason: "other")),
                     "a waste line is only parked with a key")
    }

    @MainActor
    func testAQueuedReceiveCarriesItsLinesAndKey() throws {
        let body = DeliveriesViewModel.ReceiveBody(lines: [.init(ingredientId: 3, item: nil, qty: 0)], idempotencyKey: "po:9:x")
        let write = try XCTUnwrap(QueuedWrite.receive(poId: 9, poNumber: "PO-0009", body))
        XCTAssertEqual(write.path, "/mobile/api/food-cost/purchase-orders/9/received")
        XCTAssertEqual(write.label, "Receive PO-0009")
        let o = try JSONSerialization.jsonObject(with: write.bodyJSON!) as! [String: Any]
        XCTAssertEqual(o["idempotency_key"] as? String, "po:9:x")
        XCTAssertEqual(((o["lines"] as? [[String: Any]])?.first?["ingredient_id"]) as? Int, 3)
    }

    func testAKeyedWriteStillInFlightWaitsInsteadOfDropping() {
        let busy = APIClient.APIError(message: "busy", status: 409,
                                      body: Data(#"{"ok": false, "in_progress": true}"#.utf8))
        XCTAssertTrue(PendingWriteQueue.isInFlight(busy))
        let refused = APIClient.APIError(message: "no", status: 409, body: Data(#"{"ok": false}"#.utf8))
        XCTAssertFalse(PendingWriteQueue.isInFlight(refused))
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 409))
    }

    // MARK: #76 — driver actions

    func testADriversActionDecodesAndOpensItsPlace() throws {
        let d = try JSONDecoder.cavnar.decode(FoodCostCFO.Driver.self, from: json("""
        {"kind": "portion", "label": "Salmon plate", "dollars_monthly": 120, "difficulty": "low",
         "evidence": "x", "if_ignored": "y", "rec_key": null,
         "act": {"label": "Look at Salmon's price", "nav": "inventory/menu?dish=Salmon"}}
        """))
        XCTAssertEqual(d.act?.label, "Look at Salmon's price")
        let path = try XCTUnwrap(NavPath(d.act!.nav))
        XCTAssertEqual(FoodCostAction(path: path), .menu(dish: "Salmon"))
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/pars")!), .pars)
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/count?day=2026-10-05")!), .count)
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/dishes")!), .dishes)
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/suppliers")!), .suppliers)
    }

    // MARK: #77 — the waste trend

    func testTheWasteTrendCardDecodesAndTheOlderBodyStillDoes() throws {
        let t = try JSONDecoder.cavnar.decode(FoodCostWasteTrend.self, from: json("""
        {"ok": true, "range": "13w", "ranges": ["8w", "13w"], "weeks_total": 10,
         "target": {"pct": 4.5, "weekly": 135.0, "basis": "live", "label": "Cavnar AI's starting target"},
         "weeks": [{"week_end": "2026-09-16", "start": "2026-09-10", "label": "9/16", "waste": 150.0,
                    "flags": {"over_target": true}}],
         "observations": [{"text": "Waste fell $50 this week compared to last week.", "tone": "good"}],
         "empty": null}
        """))
        XCTAssertEqual(t.range, "13w")
        XCTAssertEqual(t.ranges, ["8w", "13w"])
        XCTAssertEqual(t.weeks.first?.end, "2026-09-16")
        XCTAssertEqual(t.weeks.first?.overTarget, true)
        XCTAssertEqual(t.observations.first?.tone, "good")
        XCTAssertEqual(FoodCostWasteTrend.label("13w"), "13 weeks")
        XCTAssertEqual(FoodCostWasteTrend.label("all"), "All")
        let old = try JSONDecoder.cavnar.decode(FoodCostWasteTrend.self, from: json("""
        {"ok": true, "weeks": [{"label": "8/6", "start": "2026-07-31", "end": "2026-08-06", "waste": 280.0}]}
        """))
        XCTAssertEqual(old.range, "8w")
        XCTAssertEqual(old.weeks.first?.end, "2026-08-06")
    }

    // MARK: #42 — your dishes

    func testTheScorecardGroupsByMoveAndHoldsAMoveOnTheUnits() throws {
        let card = try JSONDecoder.cavnar.decode(DishScorecard.self, from: json("""
        {"ok": true, "available": true, "dishes": [
          {"id": 1, "name": "Burger", "sell_price": 16, "food_cost_pct": 31.2, "units_sold": 80,
           "positive_mentions": 3, "negative_mentions": 0, "action": "reprice", "why": "Sells well, earns little"},
          {"id": 2, "name": "Salmon", "sell_price": 28, "food_cost_pct": 140.0, "units_sold": 5,
           "positive_mentions": 0, "negative_mentions": 0, "action": "cut", "why": "x",
           "unit_warning": "The plate costs more than the menu price — check the recipe's units."},
          {"id": 3, "name": "Fries", "sell_price": 6, "food_cost_pct": 20.0, "action": null}]}
        """))
        XCTAssertEqual(card.groups.map(\.title), ["Check units first", "Reprice", "No move needed"])
        XCTAssertTrue(card.dishes[1].waitsOnUnits)
        XCTAssertEqual(card.dishes[1].moveLabel, "Check units first")
        XCTAssertEqual(card.dishes[0].moveLabel, "Reprice")
        XCTAssertEqual(card.dishes[0].menuItemId, 1)
        let price = try object(DishPriceBody(menuItemId: 1, sellPrice: 17.5))
        XCTAssertEqual(price["menu_item_id"] as? Int, 1)
        XCTAssertEqual(price["sell_price"] as? Double, 17.5)
        let apply = try object(DishRepriceBody(dish: "Burger", price: 17.5, menuItemId: 1))
        XCTAssertEqual(Set(apply.keys), ["dish", "price", "menu_item_id"])
    }

    // MARK: #78 — suppliers, game week, synced recipes

    @MainActor
    func testTheSupplierOverviewGroupsByEmailAndFindsTheUnassigned() throws {
        let items = try JSONDecoder.cavnar.decode([CountSheetItem].self, from: json("""
        [{"ingredient_id": 1, "name": "Basil", "supplier_name": "Sysco", "supplier_email": "S@x.com"},
         {"ingredient_id": 2, "name": "Dough", "supplier_name": "", "supplier_email": "s@x.com"},
         {"ingredient_id": 3, "name": "Kale", "supplier_email": ""}]
        """))
        let sups = SupplierOverviewViewModel.suppliers(items)
        XCTAssertEqual(sups.count, 1)
        XCTAssertEqual(sups[0].count, 2)
        XCTAssertEqual(SupplierOverviewViewModel.unassigned(items).map(\.name), ["Kale"])
        let body = try object(IngredientSupplierBody(name: "Kale", supplierName: "Sysco", supplierEmail: "s@x.com"))
        XCTAssertEqual(Set(body.keys), ["name", "supplier_name", "supplier_email"])
    }

    func testTheGameWeekAndSyncedRecipesDecode() throws {
        let g = try JSONDecoder.cavnar.decode(FoodCostGameWeek.self, from: json("""
        {"ok": true, "game": {"describe": "Cubs vs Cardinals, Sat 7:05pm", "text": "Last one sold 40 more burgers.",
                              "order": {"lines": [{"ingredient": "Buns", "extra": 40, "unit": "each"}]}, "basis": "one game"}}
        """))
        XCTAssertEqual(g.game?.lines.first?.ingredient, "Buns")
        let none = try JSONDecoder.cavnar.decode(FoodCostGameWeek.self, from: json(#"{"ok": true, "game": null}"#))
        XCTAssertNil(none.game)
        let r = try JSONDecoder.cavnar.decode(SyncedRecipesResponse.self, from: json("""
        {"ok": true, "count": 1, "source": {"synced": true, "label": "MarketMan"},
         "recipes": [{"id": 4, "name": "Margherita", "sell_price": 14, "lines": [{"name": "Dough", "qty": 1, "unit": "each"}]}]}
        """))
        XCTAssertEqual(r.recipes.first?.lines.first?.name, "Dough")
        XCTAssertEqual(r.source?.synced, true)
    }

    // MARK: #43 — invoices from files

    func testTheInvoiceScannerTakesPDFsAndImages() {
        XCTAssertTrue(InvoiceScanViewModel.importTypes.contains(.pdf))
        XCTAssertTrue(InvoiceScanViewModel.importTypes.contains(.image))
        XCTAssertEqual(InvoiceScanViewModel.maxPDFBytes, Int(4.5 * 1024 * 1024))
    }
}
