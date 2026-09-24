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

    // MARK: - Freshness (K4 / J2)

    private func withFields(_ fields: String) throws -> HomeSummary {
        let json = String(base.dropLast()) + ", " + fields + "}"
        return try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(json.utf8))
    }

    func testDecodesTheK4FreshnessShape() throws {
        let s = try withFields("""
            "freshness": [
              {"module": "labor", "source": "pos", "state": "current", "pct": 94, "as_of": "9/23/26",
               "basis": "POS synced 9/23/26", "key": "labor", "label": "Labor"},
              {"module": "reviews", "source": "reviews", "state": "aging", "pct": 61.6, "as_of": "2026-09-20",
               "basis": "fetched 4 days ago", "key": "reviews", "label": "Reviews"},
              {"module": "inventory", "source": "counts", "state": "stale", "pct": 20, "as_of": "9/9/26",
               "key": "inventory", "label": "Food cost"},
              {"module": "marketing", "source": "instagram", "state": "not_connected", "pct": null,
               "key": "marketing", "label": "Marketing"},
              {"module": "intel", "source": "competitors", "state": "unknown", "key": "intel", "label": "Intel"},
              {"module": "labor", "source": null, "state": "sample", "basis": "sample data — upload shifts",
               "key": "labor", "label": "Labor"}],
            "data_as_of": "2026-09-09",
            "monitoring": {"count_live": 1, "stalest_as_of": "9/9/26"}
            """)
        let e = try XCTUnwrap(s.freshness?.entries)
        XCTAssertEqual(e.map(\.state), [.current, .aging, .stale, .notConnected, .unknown, .sample])
        XCTAssertEqual(e[0].pct, 94)
        XCTAssertEqual(e[1].pct, 62)
        // Dates are M/D/YY whatever the server sent.
        XCTAssertEqual(e[1].asOf, "9/20/26")
        XCTAssertEqual(s.dataAsOfDisplay, "9/9/26")
        XCTAssertEqual(s.monitoring?.countLive, 1)
        XCTAssertEqual(e[3].caption, "not connected")
        XCTAssertEqual(e[4].caption, "age unknown")
        XCTAssertEqual(e[5].caption, "sample data — upload shifts")
        // The chip prints the server's label, never the source key (B6#9).
        XCTAssertEqual(e.map(\.name), ["Labor", "Reviews", "Food cost", "Marketing", "Intel", "Labor"])
        XCTAssertEqual(e[0].source, "pos")
        // No label: the module's name, still never the key.
        let bare = try withFields(#""freshness": [{"module": "inventory", "source": "pos", "state": "current"}]"#)
        XCTAssertEqual(bare.freshness?.entries.first?.name, "Food cost")
        let kicker = MainActor.assumeIsolated {
            HomeFreshnessStrip.kicker(dataAsOf: s.dataAsOfDisplay, monitoring: s.monitoring)
        }
        XCTAssertEqual(kicker, "DATA AS OF 9/9/26 · 1 LIVE")
        // The cache round trip keeps it.
        let again = try JSONDecoder.cavnar.decode(HomeSummary.self, from: try JSONEncoder.cavnar.encode(s))
        XCTAssertEqual(again.freshness, s.freshness)
        XCTAssertEqual(again.dataAsOfDisplay, "9/9/26")
    }

    /// The older {key, label, at, state: fresh|stale|missing|manual|sample,
    /// note} shape still reads — and a "fresh" with no date is never
    /// current: unknown age is unknown.
    func testDecodesTheOlderFreshnessShape() throws {
        let s = try withFields("""
            "freshness": [
              {"key": "reviews", "label": "Reviews", "at": "2026-09-23 06:10:00", "state": "fresh"},
              {"key": "labor", "label": "Labor", "at": null, "state": "fresh", "note": "POS"},
              {"key": "inventory", "label": "Food cost", "at": null, "state": "sample", "note": "sample data"},
              {"key": "marketing", "label": "Marketing", "at": null, "state": "manual"},
              {"key": "intel", "label": "Intel", "at": "2026-08-01", "state": "stale"},
              {"key": "x", "label": "X", "state": "missing"}]
            """)
        let e = try XCTUnwrap(s.freshness?.entries)
        XCTAssertEqual(e.map(\.state), [.current, .unknown, .sample, .notConnected, .stale, .notConnected])
        XCTAssertEqual(e[0].name, "Reviews")
        XCTAssertEqual(e[0].asOf, "9/23/26")
        XCTAssertEqual(e[1].caption, "POS")
        XCTAssertNil(s.dataAsOfDisplay)
    }

    func testFreshnessAbsentOrOddNeverFailsHome() throws {
        let absent = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(base.utf8))
        XCTAssertNil(absent.freshness)
        XCTAssertNil(absent.dataAsOf)
        XCTAssertNil(absent.monitoring)
        let odd = try withFields(#""freshness": {"labor": "current"}, "data_as_of": 7, "monitoring": [1]"#)
        XCTAssertEqual(odd.freshness?.entries, [])
        XCTAssertNil(odd.dataAsOfDisplay)
        XCTAssertNil(odd.monitoring?.countLive)
        let mixed = try withFields(#""freshness": ["labor", 3, {"module": "labor", "state": "current", "as_of": "9/23/26"}]"#)
        XCTAssertEqual(mixed.freshness?.entries.count, 1)
        XCTAssertEqual(mixed.freshness?.entries.first?.name, "Labor")
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
