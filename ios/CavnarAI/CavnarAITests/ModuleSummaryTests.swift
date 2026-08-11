import XCTest
@testable import CavnarAI

final class ModuleSummaryTests: XCTestCase {
    func testDecodesFullModuleSummary() throws {
        let json = """
        {"key": "reviews", "label": "Reviews", "icon": "reviews", "status": "available",
         "kpi": {"value": "0/2", "sublabel": "0% response rate"}}
        """
        let module = try JSONDecoder.cavnar.decode(ModuleSummary.self, from: Data(json.utf8))
        XCTAssertEqual(module.key, "reviews")
        XCTAssertEqual(module.label, "Reviews")
        XCTAssertTrue(module.isAvailable)
        XCTAssertEqual(module.kpi?.value, "0/2")
        XCTAssertEqual(module.kpi?.sublabel, "0% response rate")
    }

    func testDecodesModuleWithNullKPI() throws {
        // Intel with no competitor_intel data yet returns kpi as a real
        // object server-side, but future modules with no computed KPI at
        // all should still decode cleanly with kpi == nil.
        let json = """
        {"key": "waitlist", "label": "Waitlist", "icon": "waitlist", "status": "coming_soon", "kpi": null}
        """
        let module = try JSONDecoder.cavnar.decode(ModuleSummary.self, from: Data(json.utf8))
        XCTAssertNil(module.kpi)
        XCTAssertFalse(module.isAvailable)
    }

    func testHomeSummaryDecodesGenericModulesArrayOfAnyLength() throws {
        let json = """
        {"username": "jamie", "restaurant_name": "Test Co", "location_name": null, "brand_color": null,
         "reviews_awaiting_approval": 0,
         "modules": [
           {"key": "reviews", "label": "Reviews", "icon": "reviews", "status": "available", "kpi": null},
           {"key": "labor", "label": "Labor", "icon": "labor", "status": "available", "kpi": null},
           {"key": "marketing", "label": "Marketing", "icon": "marketing", "status": "available", "kpi": null}
         ],
         "needs_attention": []}
        """
        let summary = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(json.utf8))
        XCTAssertEqual(summary.modules.count, 3)
        XCTAssertEqual(summary.modules.map(\.key), ["reviews", "labor", "marketing"])
    }

    func testNeedsAttentionItemDecodesModuleKeyNotTabName() throws {
        let json = """
        {"type": "reviews_awaiting_approval", "module": "reviews",
         "title": "2 reviews awaiting approval", "detail": "AI responses drafted"}
        """
        let item = try JSONDecoder.cavnar.decode(NeedsAttentionItem.self, from: Data(json.utf8))
        XCTAssertEqual(item.module, "reviews")
    }
}

final class ModuleIconTests: XCTestCase {
    func testKnownKeysMapToDistinctSymbols() {
        XCTAssertEqual(ModuleIcon.symbolName(for: "reviews"), "star.bubble.fill")
        XCTAssertEqual(ModuleIcon.symbolName(for: "labor"), "person.2.fill")
        XCTAssertEqual(ModuleIcon.symbolName(for: "marketing"), "megaphone.fill")
        XCTAssertEqual(ModuleIcon.symbolName(for: "intel"), "binoculars.fill")
    }

    func testUnknownFutureKeyFallsBackToGenericSymbolInsteadOfCrashing() {
        // A brand-new backend module iOS doesn't recognize yet must never
        // crash or render nothing — it gets a sensible generic icon until
        // the app is updated to know its specific key.
        XCTAssertEqual(ModuleIcon.symbolName(for: "some_future_module_key"), "square.grid.2x2")
        XCTAssertEqual(ModuleIcon.symbolName(for: ""), "square.grid.2x2")
    }
}
