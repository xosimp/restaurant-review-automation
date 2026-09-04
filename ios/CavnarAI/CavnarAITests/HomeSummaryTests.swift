import XCTest
@testable import CavnarAI

final class HomeSummaryTests: XCTestCase {
    private let base = """
    {"username": "brian", "restaurant_name": "Gia Mia", "location_name": null, "brand_color": null,
     "reviews_awaiting_approval": 3, "quiet_hours_active": false, "alert_quiet_end": null,
     "modules": [
       {"key": "reviews", "label": "Reviews", "icon": "reviews", "status": "available",
        "kpi": {"value": "12/14", "sublabel": "86% response rate"},
        "pulse": {"value": "12/14", "label": "replies · 86%", "tone": "good"}}
     ],
     "needs_attention": [
       {"type": "reviews_awaiting_approval", "module": "reviews",
        "title": "3 reviews awaiting approval", "detail": "AI responses drafted — publish in one tap",
        "cta": "Publish 3 replies", "secondary": "Read them first", "action": "publish_replies"}
     ],
     "total_value_delivered": 18420,
     "value_history": [{"date": "2026-09-01", "value": 17180}, {"date": "2026-09-04", "value": 18420}],
     "overnight": {"answered": 3, "flagged": 2, "window_hours": 24},
     "weekly_receipts": [{"module": "reviews", "emphasis": "9 replies", "text": "published to Google — 100% within 24h"}]}
    """

    func testDecodesTheHomeRebuildFields() throws {
        let summary = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(base.utf8))
        XCTAssertEqual(summary.overnight?.answered, 3)
        XCTAssertEqual(summary.overnight?.flagged, 2)
        XCTAssertEqual(summary.weeklyReceipts?.first?.emphasis, "9 replies")
        XCTAssertEqual(summary.modules.first?.pulse?.tone, "good")
        XCTAssertEqual(summary.modules.first?.pulse?.label, "replies · 86%")
        let item = try XCTUnwrap(summary.needsAttention.first)
        XCTAssertEqual(item.cta, "Publish 3 replies")
        XCTAssertEqual(item.secondary, "Read them first")
        XCTAssertTrue(item.isPublishAction)
    }

    /// A summary cached before the rebuild shipped (no overnight, receipts,
    /// pulse or CTAs) still decodes — every new field is optional.
    func testDecodesAPreRebuildSummaryWithTheNewFieldsMissing() throws {
        let json = """
        {"username": "jamie", "restaurant_name": "Test Co", "location_name": null, "brand_color": null,
         "reviews_awaiting_approval": 0, "quiet_hours_active": false,
         "modules": [{"key": "labor", "label": "Labor", "icon": "labor", "status": "available", "kpi": null}],
         "needs_attention": [{"type": "labor_overtime", "module": "labor", "title": "2 staff in overtime", "detail": "Est. $76+"}],
         "total_value_delivered": 0, "value_history": []}
        """
        let summary = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(json.utf8))
        XCTAssertNil(summary.overnight)
        XCTAssertNil(summary.weeklyReceipts)
        XCTAssertNil(summary.modules.first?.pulse)
        let item = try XCTUnwrap(summary.needsAttention.first)
        XCTAssertNil(item.cta)
        XCTAssertFalse(item.isPublishAction)
    }

    func testBulkPublishResultDecodes() throws {
        let json = #"{"ok": true, "approved": 3, "posted": 2, "failed": 0, "remaining": 0}"#
        let result = try JSONDecoder.cavnar.decode(BulkPublishResult.self, from: Data(json.utf8))
        XCTAssertEqual(result.approved, 3)
        XCTAssertEqual(result.posted, 2)
        XCTAssertEqual(result.failed, 0)
    }
}

final class HomeMixedTextTests: XCTestCase {
    private func runs(_ s: String) -> [(String, Bool)] {
        HomeMixedText.runs(s).map { ($0.text, $0.isNumber) }
    }

    func testSplitsNumbersOutOfProse() {
        let r = runs("Publish 3 replies")
        XCTAssertEqual(r.map(\.0), ["Publish ", "3", " replies"])
        XCTAssertEqual(r.map(\.1), [false, true, false])
    }

    func testKeepsFractionsPercentagesAndThousandsAsOneRun() {
        XCTAssertEqual(runs("12/14 replies · 86%").map(\.0), ["12/14", " replies · ", "86%"])
        XCTAssertEqual(runs("$1,840 of salmon waste").map(\.0), ["$1,840", " of salmon waste"])
        XCTAssertEqual(runs("22.3% labor · on target").map(\.0), ["22.3%", " labor · on target"])
        XCTAssertEqual(runs("+$1,240 this month").map(\.0), ["+$1,240", " this month"])
    }

    func testAFullStopAfterANumberStaysInTheProse() {
        XCTAssertEqual(runs("Sep 3.").map(\.0), ["Sep ", "3", "."])
    }

    func testProseWithNoNumbersIsOneRun() {
        let r = runs("Read them first")
        XCTAssertEqual(r.count, 1)
        XCTAssertFalse(r[0].1)
    }
}
