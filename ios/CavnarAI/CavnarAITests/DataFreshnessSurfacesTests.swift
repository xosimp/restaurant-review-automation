import XCTest
@testable import CavnarAI

/// Workstream O (owner surfaces) of the data freshness audit: the Data
/// Health Score on Home, the connection lines, the last-night gap rule and
/// the stale fallbacks. Pure helpers and decoding only.
final class DataFreshnessSurfacesTests: XCTestCase {

    // MARK: - Home kicker (#21–23)

    @MainActor
    func testKickerLeadsWithTheDataHealthScoreAndSaysCurrent() {
        let health = HomeDataHealth(overall: DataHealthOverall(pct: 71, state: "aging", label: "71% data health"))
        let kicker = HomeFreshnessStrip.kicker(dataAsOf: "9/22/26",
                                               monitoring: HomeMonitoring(countLive: 4, stalestAsOf: nil),
                                               health: health)
        XCTAssertEqual(kicker, "DATA HEALTH 71% · DATA AS OF 9/22/26 · 4 CURRENT")
        XCTAssertFalse(kicker?.contains("LIVE") ?? true)
    }

    @MainActor
    func testKickerBeforeTheFirstSyncSaysSo() {
        let health = HomeDataHealth(overall: DataHealthOverall(pct: nil, state: "pending"))
        XCTAssertEqual(HomeFreshnessStrip.kicker(dataAsOf: nil, monitoring: nil, health: health),
                       "WAITING FOR FIRST SYNC")
    }

    @MainActor
    func testUnavailableFreshnessStillDrawsTheStrip() {
        XCTAssertTrue(HomeFreshnessStrip.hasContent(entries: [], health: nil, unavailable: true))
        XCTAssertFalse(HomeFreshnessStrip.hasContent(entries: [], health: nil, unavailable: false))
    }

    func testHomeDecodesDataHealthAndTheUnavailableFlagLeniently() throws {
        let json = """
        {"username": "a", "restaurant_name": "R", "reviews_awaiting_approval": 0, "quiet_hours_active": false,
         "modules": [], "needs_attention": [], "total_value_delivered": 0, "value_history": [],
         "freshness": [], "freshness_unavailable": true,
         "data_health": {"overall": {"pct": 71.0, "state": "aging", "label": "71% data health",
                                     "caps_applied": ["stale_pos"], "reason": "POS is a day late"},
                         "worst_line": "POS sales: synced 9/22/26"}}
        """
        let s = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(json.utf8))
        XCTAssertTrue(s.freshnessUnavailable)
        XCTAssertEqual(s.dataHealth?.overall?.pct, 71)
        XCTAssertEqual(s.dataHealth?.overall?.capsApplied, ["stale_pos"])
        XCTAssertEqual(s.dataHealth?.worstLine, "POS sales: synced 9/22/26")
        // An odd shape never fails Home.
        let odd = json.replacingOccurrences(of: "\"freshness_unavailable\": true", with: "\"freshness_unavailable\": \"yes\"")
            .replacingOccurrences(of: "\"worst_line\": \"POS sales: synced 9/22/26\"", with: "\"worst_line\": 3")
        let o = try JSONDecoder.cavnar.decode(HomeSummary.self, from: Data(odd.utf8))
        XCTAssertFalse(o.freshnessUnavailable)
        XCTAssertNil(o.dataHealth?.worstLine)
        // The cache round trip keeps it.
        let again = try JSONDecoder.cavnar.decode(HomeSummary.self, from: try JSONEncoder.cavnar.encode(s))
        XCTAssertEqual(again.dataHealth, s.dataHealth)
        XCTAssertTrue(again.freshnessUnavailable)
    }

    func testDataHealthSnapshotDecodesAndPicksAModulesWeakestSource() throws {
        let json = """
        {"ok": true, "overall": {"pct": 64, "state": "aging"},
         "sources": [
           {"key": "pos", "label": "POS sales", "tone": "ok", "health_pct": 92, "line": "POS sales: synced 9/24/26",
            "reliability": {"basis": "6 of the last 7 syncs succeeded"}, "can_sync_now": true},
           {"key": "inventory", "label": "Inventory", "tone": "warn", "health_pct": 40, "line": "Inventory: counted 9/2/26",
            "expected_line": "Count your inventory on Food Cost"},
           "not an object"],
         "not_connected": [{"key": "reviews", "label": "Google reviews", "next": "Connect Google in Account → Connections"}],
         "modules": [{"module": "food_cost", "title": "Food cost", "decision": "caveat", "sources": ["pos", "inventory"],
                      "confidence_impact": {"line": "Food cost recommendations 81% → 94% once inventory counts are current"}},
                     {"module": "labor", "sources": ["pos"], "confidence_impact": null}]}
        """
        let snap = try JSONDecoder.cavnar.decode(DataHealthSnapshot.self, from: Data(json.utf8))
        XCTAssertEqual(snap.sources.count, 2)
        XCTAssertTrue(snap.canSyncNow)
        XCTAssertEqual(snap.sources[0].reliability?.basis, "6 of the last 7 syncs succeeded")
        XCTAssertEqual(snap.notConnected.first?.next, "Connect Google in Account → Connections")
        XCTAssertEqual(snap.impactLines, ["Food cost recommendations 81% → 94% once inventory counts are current"])
        XCTAssertEqual(snap.sources(forModule: "food_cost").first?.key, "inventory")
        XCTAssertTrue(snap.sources(forModule: "marketing").isEmpty)
    }

    func testSyncNowAlreadyRunningDecodes() throws {
        let json = #"{"ok": true, "already_syncing": true, "message": "A sync is already running — this updates when it finishes."}"#
        let r = try JSONDecoder.cavnar.decode(DataHealthSyncResult.self, from: Data(json.utf8))
        XCTAssertTrue(r.ok)
        XCTAssertTrue(r.alreadySyncing)
        XCTAssertEqual(r.message, "A sync is already running — this updates when it finishes.")
    }

    // MARK: - Last night (#37)

    func testLastNightGapFollowsTheWebRule() {
        XCTAssertEqual(LastNightGap.days(from: "2026-09-23", to: "2026-09-24"), 1)
        XCTAssertEqual(LastNightGap.kicker(gap: 0), "Tonight")
        XCTAssertEqual(LastNightGap.kicker(gap: 1), "Last night")
        XCTAssertEqual(LastNightGap.kicker(gap: 3), "Latest report")
        XCTAssertEqual(LastNightGap.kicker(gap: nil), "Latest report")
        XCTAssertFalse(LastNightGap.lastNightMissing(gap: 1))
        XCTAssertTrue(LastNightGap.lastNightMissing(gap: 2))
        // local_now carries a time; only its date is read.
        XCTAssertEqual(LastNightGap.days(from: "2026-09-21", to: "2026-09-24T07:30:00-05:00"), 3)
        XCTAssertNil(LastNightGap.days(from: "2026-09-21", to: nil))
    }

    // MARK: - Labor (#37)

    @MainActor
    func testLaborStaleIconReadsTheServerFirst() {
        let current = LaborFreshness(state: "current", stale: false)
        let stale = LaborFreshness(state: "stale", stale: true)
        // The server says current: a 30-day-old window is not flagged by the phone's rule.
        XCTAssertFalse(LaborView.shiftDataIsStale(isLive: true, daysOld: 30, server: current))
        XCTAssertTrue(LaborView.shiftDataIsStale(isLive: true, daysOld: 2, server: stale))
        // An older server: the 21-day rule.
        XCTAssertTrue(LaborView.shiftDataIsStale(isLive: true, daysOld: 22, server: nil))
        XCTAssertFalse(LaborView.shiftDataIsStale(isLive: true, daysOld: 5, server: nil))
        // Sample data is always flagged.
        XCTAssertTrue(LaborView.shiftDataIsStale(isLive: false, daysOld: 0, server: current))
    }

    @MainActor
    func testLaborCachedNotice() {
        let now = Date()
        let old = now.addingTimeInterval(-3600)
        XCTAssertNil(LaborViewModel.cachedNotice(hasStats: false, loadedAt: nil, cachedAt: old, refreshFailed: true, now: now))
        XCTAssertNotNil(LaborViewModel.cachedNotice(hasStats: true, loadedAt: nil, cachedAt: old, refreshFailed: false, now: now))
        XCTAssertNil(LaborViewModel.cachedNotice(hasStats: true, loadedAt: nil, cachedAt: now, refreshFailed: false, now: now))
        XCTAssertNil(LaborViewModel.cachedNotice(hasStats: true, loadedAt: now, cachedAt: nil, refreshFailed: false, now: now))
        let failed = LaborViewModel.cachedNotice(hasStats: true, loadedAt: now, cachedAt: nil, refreshFailed: true, now: now)
        XCTAssertTrue(failed?.hasPrefix("Couldn\u{2019}t refresh") ?? false)
    }

    @MainActor
    func testLaborInsightFallbackNote() {
        XCTAssertNil(LaborAnalyticsViewModel.insightFallbackNote(hasInsight: true, serverSaysOlder: false,
                                                                 fetchFailed: false, cachedAt: nil))
        XCTAssertNil(LaborAnalyticsViewModel.insightFallbackNote(hasInsight: true, serverSaysOlder: true,
                                                                 fetchFailed: true, cachedAt: nil))
        XCTAssertNotNil(LaborAnalyticsViewModel.insightFallbackNote(hasInsight: true, serverSaysOlder: false,
                                                                    fetchFailed: true, cachedAt: Date()))
    }

    // MARK: - Connections (#17, #34)

    func testPOSRowPrefersTheServersLine() throws {
        let json = """
        {"google_business": {"connected": true, "fetch_line": {"line": "Checked 11:02am · next check 4pm", "tone": "ok"}},
         "instagram": {"connected": false},
         "toast": {"connected": true, "sync_state": "current", "age_days": 0,
                   "sync_line": "Last sync 3:02am · Sales through 9/19/26", "sync_tone": "ok"},
         "square": {"connected": false}, "clover": {"connected": false},
         "pos_line": {"line": "Last sync 3:02am · Sales through 9/19/26", "tone": "ok", "provider": "toast"}}
        """
        let c = try JSONDecoder.cavnar.decode(AccountConnections.self, from: Data(json.utf8))
        XCTAssertEqual(c.googleBusiness.fetchLine?.line, "Checked 11:02am · next check 4pm")
        let line = c.toast.posStatusLine(provider: "toast", posLine: c.posLine)
        XCTAssertEqual(line?.text, "Last sync 3:02am · Sales through 9/19/26")
        XCTAssertEqual(line?.tone, .good)
        XCTAssertNil(c.square.posStatusLine(provider: "square", posLine: c.posLine))
        // An older server keeps the old wording.
        let old = ConnectionStatus(connected: true, syncState: "current", ageDays: 0)
        XCTAssertEqual(old.posStatusLine(provider: "toast", posLine: nil)?.text, "Syncing · last data today")
        // A malformed line never fails the payload.
        let odd = json.replacingOccurrences(of: "\"pos_line\": {\"line\"", with: "\"pos_line\": {\"nope\"")
        XCTAssertNil(try JSONDecoder.cavnar.decode(AccountConnections.self, from: Data(odd.utf8)).posLine)
    }

    // MARK: - AI visibility (#35)

    func testAIVisibilityMeasuredLineAndBackgroundNote() throws {
        let json = #"{"ok": true, "cached": true, "measured_at": "2026-09-10 15:00:00"}"#
        let r = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(json.utf8))
        XCTAssertTrue(r.measuredLine?.hasPrefix("Measured 9/") ?? false)
        let soon = try XCTUnwrap(CavnarDate.timestamp("2026-09-12 15:00:00"))
        XCTAssertNil(r.backgroundNote(now: soon))
        let later = try XCTUnwrap(CavnarDate.timestamp("2026-09-24 15:00:00"))
        XCTAssertTrue(r.backgroundNote(now: later)?.contains("treat it as background") ?? false)
        let fresh = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(#"{"ok": true}"#.utf8))
        XCTAssertNil(fresh.measuredLine)
        XCTAssertNil(fresh.backgroundNote())
    }
}
