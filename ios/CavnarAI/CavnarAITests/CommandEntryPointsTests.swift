import XCTest
@testable import CavnarAI

/// Friction audit, workstream I2: the ways in from outside the app (quick
/// actions, App Shortcuts, widget and Live Activity links), the command
/// sheet's matching, and the module-screen pieces that read a nav path.
/// Pure logic only — nothing here makes a request.
@MainActor
final class CommandEntryPointsTests: XCTestCase {

    // MARK: Links → nav paths (#47)

    private func nav(_ s: String) -> String? {
        guard let url = URL(string: s), case .nav(let p)? = SystemEntry.destination(for: url) else { return nil }
        return p.raw
    }

    func testWidgetLinksBecomeNavPaths() {
        XCTAssertEqual(nav("cavnarai://nav/review/412"), "review/412")
        XCTAssertEqual(nav("cavnarai://nav/reviews?filter=pending"), "reviews?filter=pending")
        XCTAssertEqual(nav("cavnarai://nav/inventory/invoices?scan=camera"), "inventory/invoices?scan=camera")
        XCTAssertEqual(SystemEntry.destination(for: URL(string: "cavnarai://command")!), .commandSheet)
    }

    func testDashboardLinksOpenTheSamePlace() {
        XCTAssertEqual(nav("https://dashboard.cavnar.ai/dashboard?nav=labor/schedule"), "labor/schedule")
        XCTAssertEqual(nav("https://dashboard.cavnar.ai/dashboard#labor/requests"), "labor/requests")
        XCTAssertEqual(nav("https://dashboard.cavnar.ai/dashboard?review=88"), "review/88")
        XCTAssertEqual(nav("https://dashboard.cavnar.ai/dashboard"), "home")
    }

    func testLinksThatAreNotOursAreLeftAlone() {
        // The auth callbacks share the scheme and must keep their own path.
        XCTAssertNil(SystemEntry.destination(for: URL(string: "cavnarai://oauth/google?code=x")!))
        XCTAssertNil(SystemEntry.destination(for: URL(string: "https://example.com/dashboard?nav=labor")!))
        XCTAssertNil(SystemEntry.destination(for: URL(string: "http://dashboard.cavnar.ai/?nav=labor")!))
    }

    func testAnAskedQuestionSurvivesTheQueryWhole() {
        let path = SystemEntry.askPath("Why was labor high & who ran over = ?")
        XCTAssertEqual(path?.head, "ask")
        XCTAssertEqual(path?.query["q"], "Why was labor high & who ran over = ?")
        XCTAssertEqual(SystemEntry.askPath("   ")?.raw, "ask")
    }

    // MARK: Quick actions (#31)

    func testQuickActionsLandOnTheItemNotTheModuleTop() {
        func raw(_ a: QuickAction) -> String? {
            if case .nav(let p) = a.destination { return p.raw }
            return nil
        }
        XCTAssertEqual(raw(.ask), "ask")
        XCTAssertEqual(raw(.approveReplies), "reviews?filter=pending")
        XCTAssertEqual(raw(.lastNight), "dsr")
        XCTAssertEqual(raw(.scanInvoice), "inventory/invoices?scan=camera")
    }

    func testApproveRepliesOnlyAppearsWhileRepliesWait() {
        XCTAssertNil(QuickAction.approveRepliesItem(waiting: 0))
        let item = QuickAction.approveRepliesItem(waiting: 6)
        XCTAssertEqual(item?.type, QuickAction.approveReplies.rawValue)
        XCTAssertEqual(item?.localizedSubtitle, "6 reviews waiting on a reply")
    }

    // MARK: Section hand-off

    /// The route is the one carrier of "where inside the module" (F3-7): the
    /// whole path, query and all, reaches the screen whoever opened it.
    func testTheRouteCarriesTheWholePathToTheModuleScreen() {
        let router = DeepLinkRouter()
        router.open(NavPath("inventory/invoices?scan=camera")!)
        let route = router.consumePendingModuleRoute(labelFor: { $0 })
        XCTAssertEqual(route?.key, "inventory")
        XCTAssertEqual(route?.navPath?.query["scan"], "camera")
        XCTAssertEqual(route?.navPath.flatMap(FoodCostAction.init(path:)), .scan(camera: true))
        // A push's own route (no query) still names its section.
        XCTAssertEqual(ModuleRoute(key: "inventory", label: "", section: "order").navPath
            .flatMap(FoodCostAction.init(path:)), .order)
        XCTAssertEqual(FoodCostAction(path: NavPath("food/order")!), .order)
    }

    func testOneLaborHandlerReadsEverySpellingOfASection() {
        XCTAssertEqual(ModuleRoute.from(NavPath("request/time_off-12")!)?.section
            .flatMap(LaborFocus.init(section:)), .timeOff)
        XCTAssertEqual(ModuleRoute.from(NavPath("request/shift-3")!)?.section
            .flatMap(LaborFocus.init(section:)), .requests)
        XCTAssertEqual(LaborFocus(section: "waiting"), .waiting)
        XCTAssertEqual(LaborFocus(section: "labor-requests"), nil)
        XCTAssertEqual(ModuleRoute.from(NavPath("person/dana-k")!)?.itemId, "dana-k")
        XCTAssertEqual(ModuleRoute.from(NavPath("person/dana-k")!)?.section
            .flatMap(LaborFocus.init(section:)), .team)
    }

    func testFoodCostOpensWhatThePathNames() {
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/invoices?scan=camera")!), .scan(camera: true))
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/invoices")!), .scan(camera: false))
        XCTAssertEqual(FoodCostAction(path: NavPath("invoice/31")!), .scan(camera: false))
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/order")!), .order)
        XCTAssertEqual(FoodCostAction(path: NavPath("order/sysco")!), .order)
        XCTAssertEqual(FoodCostAction(path: NavPath("inventory/count")!), .count)
        XCTAssertNil(FoodCostAction(path: NavPath("inventory")!))
    }

    // MARK: Command sheet matching (#47)

    func testEveryWordOfTheQueryMustMatch() {
        let send = CommandEntry(id: "a", label: "Send next week's schedule", keywords: ["publish"], kind: "action")
        let orders = CommandEntry(id: "b", label: "Send supplier orders", keywords: ["sysco"], kind: "action")
        XCTAssertGreaterThan(send.score("send sch"), 0)
        XCTAssertEqual(orders.score("send sch"), 0)
        XCTAssertEqual(CommandMatch.rank([orders, send], query: "send sch").map(\.id), ["a"])
        XCTAssertGreaterThan(orders.score("sysco"), 0, "keywords count")
        XCTAssertEqual(send.score(""), 0)
    }

    func testTheSheetStillNavigatesWithoutTheRegistryRoute() {
        let hits = CommandMatch.rank(CommandMatch.fallbackPlaces, query: "scan")
        XCTAssertEqual(hits.first?.nav, "inventory/invoices?scan=camera")
        XCTAssertTrue(CommandMatch.fallbackPlaces.allSatisfy { NavPath($0.nav) != nil })
    }

    func testRegistryAndSearchDecodeTheContract() throws {
        let reg = try JSONDecoder().decode(CommandRegistryResponse.self, from: Data("""
        {"ok": true, "commands": [{"id": "approve_all_reviews", "label": "Publish drafted replies",
          "keywords": ["approve"], "kind": "action", "tier": 2, "nav": null,
          "action": "approve_all_reviews", "args": {"limit": 6}}]}
        """.utf8))
        XCTAssertEqual(reg.commands?.first?.tier, 2)
        XCTAssertEqual(reg.commands?.first?.args?["limit"], .int(6))

        let search = try JSONDecoder().decode(CommandSearchResponse.self, from: Data("""
        {"ok": true, "results": [
          {"type": "person", "id": "dana-k", "title": "Dana K", "subtitle": "Server", "nav": "person/dana-k",
           "location": {"id": 2, "name": "Wicker Park"}},
          {"type": "review", "id": 412, "title": "1 star from Mike", "nav": "review/412", "location": null}]}
        """.utf8))
        XCTAssertEqual(search.results?.first?.personKey, "dana-k")
        XCTAssertEqual(search.results?.last?.itemId, "412")
        XCTAssertNil(search.results?.last?.personKey)
    }

    func testWaitingRequestsAreAnsweredInPlace() {
        let off = CommandWaitingItem(key: "time_off:12", kind: "time_off", title: "Dana asked for time off",
                                     detail: nil, severity: "important", module: "labor", nav: nil, count: nil)
        XCTAssertEqual(off.request?.kind, "time_off")
        XCTAssertEqual(off.request?.id, 12)
        XCTAssertEqual(off.destination?.raw, "labor", "no nav from an older server → the module")
        let reviews = CommandWaitingItem(key: "no_response", kind: "reviews", title: "6 reviews", detail: nil,
                                         severity: "watch", module: "reviews", nav: "reviews?filter=pending", count: 6)
        XCTAssertNil(reviews.request)
        XCTAssertEqual(reviews.destination?.raw, "reviews?filter=pending")
    }

    // MARK: Widget + Live Activity (#47)

    func testTheWidgetComparesLikeForLikeAndNeverInventsAChange() {
        let week = WidgetSnapshotService.change(lastWeek: 8.2, yesterday: -3, businessDate: "2026-09-25")
        XCTAssertEqual(week?.basis, "vs last Friday")
        XCTAssertEqual(week?.up, true)
        let day = WidgetSnapshotService.change(lastWeek: nil, yesterday: -3, businessDate: "2026-09-25")
        XCTAssertEqual(day?.basis, "vs yesterday")
        XCTAssertEqual(day?.up, false)
        XCTAssertNil(WidgetSnapshotService.change(lastWeek: nil, yesterday: nil, businessDate: "2026-09-25"))
    }

    func testTheWidgetSaysWhenItsNumbersAreOld() {
        var snap = WidgetSnapshot.empty
        snap.updatedAt = Date()
        XCTAssertFalse(snap.isStale())
        snap.updatedAt = Date().addingTimeInterval(-WidgetSnapshot.staleAfter - 60)
        XCTAssertTrue(snap.isStale())
        snap.waitingCount = 0
        XCTAssertEqual(snap.waitingLine, "Nothing waiting")
        snap.waitingCount = 1
        XCTAssertEqual(snap.waitingLine, "1 thing waiting")
        snap.waitingCount = 3
        XCTAssertEqual(snap.waitingLine, "3 things waiting")
    }

    func testOnlyOutwardSendsStillAheadGetACountdown() throws {
        let rows = try JSONDecoder().decode([PendingSendActivities.PendingAction].self, from: Data("""
        [{"id": 1, "kind": "schedule_publish", "label": "Week of 9/28/26", "execute_at": "2099-01-01T11:00:00Z", "status": "pending"},
         {"id": 2, "kind": "order_send", "label": null, "execute_at": "2099-01-01T11:00:00Z", "status": "pending"},
         {"id": 3, "kind": "schedule_publish", "label": null, "execute_at": "2001-01-01T11:00:00Z", "status": "pending"},
         {"id": 4, "kind": "some_other_kind", "label": null, "execute_at": "2099-01-01T11:00:00Z", "status": "pending"},
         {"id": 5, "kind": "order_send", "label": null, "execute_at": "2099-01-01T11:00:00.250Z", "status": "pending"}]
        """.utf8))
        XCTAssertEqual(PendingSendActivities.countdowns(rows).map { $0.0.id }, [1, 2, 5])
    }

    // MARK: Marketing (#41)

    func testPostToAllNamesOnlyTheChannelsItWillReach() {
        let vm = MarketingViewModel()
        vm.channels = MarketingChannels(instagram: true, facebook: true)
        XCTAssertEqual(vm.socialTargets(hasMedia: true), ["Instagram", "Facebook"])
        XCTAssertEqual(vm.socialTargets(hasMedia: false), ["Facebook"], "Instagram needs a photo")
        vm.facebookSelected = false
        XCTAssertEqual(vm.socialTargets(hasMedia: true), ["Instagram"])
        XCTAssertEqual(MarketingViewModel.channelList(["Instagram", "Facebook"]), "Instagram and Facebook")
        XCTAssertEqual(MarketingViewModel.channelList(["Facebook"]), "Facebook")
    }

    // MARK: Person record (#25)

    func testAPersonSaveSendsOnlyWhatChanged() throws {
        let person = try JSONDecoder().decode(PersonRecord.self, from: Data("""
        {"key": "dana-k", "name": "Dana K", "role": "Server", "active": true, "pin_set": true,
         "phone": "3125550100", "email": null, "hours": {"min": 20, "max": 32}, "availability": ["Mon", "Tue"],
         "rating": 4.5, "certifications": ["Food handler"], "pos_id": 1042, "pay_rate": "15.5"}
        """.utf8))
        XCTAssertEqual(person.posId, "1042")
        XCTAssertEqual(person.payRate, 15.5)
        XCTAssertEqual(PersonRecord.describe(person.availability), "Mon, Tue")
        XCTAssertEqual(PersonRecord.describe(person.rating), "4.5")
        XCTAssertNil(PersonRecord.describe(.null), "unset is never shown as zero")

        let vm = PersonSheetViewModel()
        vm.role = "Server"
        vm.phone = "3125550100"
        vm.email = ""
        vm.posId = "1042"
        // Pay is the role's rate and read only on the sheet (F3-5): never sent.
        XCTAssertTrue(vm.changes(from: person).isEmpty)
        vm.phone = "3125550199"
        XCTAssertEqual(vm.changes(from: person), ["phone": .string("3125550199")])
    }

    // MARK: Staff portal requests (#49)

    func testStaffRequestsPostTheWebPortalsKeys() throws {
        var swap = StaffShiftChangeBody(date: "2026-09-28", shiftStart: "4:00pm", reason: "class")
        swap.kind = "swap"
        swap.targetName = "Bob"
        swap.targetDate = "2026-09-30"
        swap.targetStart = "4:00pm"
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(swap)) as? [String: Any]
        XCTAssertEqual(json?["kind"] as? String, "swap")
        XCTAssertEqual(json?["shift_start"] as? String, "4:00pm")
        XCTAssertEqual(json?["target_name"] as? String, "Bob")
        XCTAssertEqual(json?["target_start"] as? String, "4:00pm")

        let off = StaffTimeOffBody(startDate: "2026-10-02", endDate: "2026-10-04", reason: "")
        let offJSON = try JSONSerialization.jsonObject(with: JSONEncoder().encode(off)) as? [String: Any]
        XCTAssertEqual(offJSON?["start_date"] as? String, "2026-10-02")
        XCTAssertEqual(offJSON?["end_date"] as? String, "2026-10-04")
    }
}
