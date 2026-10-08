import XCTest
@testable import CavnarAI

final class AIVisibilityTests: XCTestCase {
    func testDecodesAIVisibilityResult() throws {
        let json = """
        {"ok": true, "restaurant_name": "Gia Mia", "queries": [
           {"query": "Top restaurants in River North", "answer": "Gia Mia is a favorite...", "appeared": true}
         ], "appeared_count": 1, "total_queries": 3, "ai_score": 33, "gbp_score": 70,
         "checklist": [{"label": "Add your Google Place ID", "done": false, "pts": 10,
                        "action": "Go to Account...", "needs_gmb": false}],
         "gbp_connected": true}
        """
        let result = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(json.utf8))
        XCTAssertTrue(result.ok)
        XCTAssertEqual(result.aiScore, 33)
        XCTAssertEqual(result.queries?.first?.appeared, true)
        XCTAssertEqual(result.checklist?.first?.needsGmb, false)
    }

    func testDecodesGoogleSearchQuestionsAndGoogleVsAI() throws {
        // A question from the restaurant's Google searches carries its
        // Google volume and position; the payload says Google vs AI (10/7/26).
        let json = """
        {"ok": true, "queries": [
           {"query": "restaurants near St. Charles, IL", "answer": "Try Mio Modo.", "appeared": false,
            "kind": "search", "sources": ["https://www.tripadvisor.com/x", "https://opentable.com/y", "not a url"],
            "competitors_named": ["Mio Modo"],
            "search": {"queries": ["restaurants near me", "st charles restaurants"], "impressions": 5256,
                       "clicks": 153, "position": 4.0}},
           {"query": "Tell me about Simple EJ's in St. Charles, IL", "answer": "A sports bar.", "appeared": true,
            "kind": "branded"}
         ],
         "search_demand": {"questions": 4, "named": 0, "impressions": 8281, "covered_pct": 0, "basis": "b"}}
        """
        let result = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(json.utf8))
        let q = try XCTUnwrap(result.queries?.first)
        XCTAssertEqual(q.search?.impressions, 5256)
        XCTAssertEqual(q.search?.position, 4.0)
        XCTAssertEqual(q.search?.queries?.count, 2)
        XCTAssertEqual(q.sourceDomains, ["tripadvisor.com", "opentable.com"])
        XCTAssertNil(result.queries?.last?.search)
        XCTAssertEqual(result.searchDemand?.coveredPct, 0)
        XCTAssertEqual(result.searchDemand?.impressions, 8281)
    }

    func testDecodesRateLimitedErrorResult() throws {
        let json = """
        {"ok": false, "error": "Too many visibility checks — please wait a moment and try again."}
        """
        let result = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(json.utf8))
        XCTAssertFalse(result.ok)
        XCTAssertNil(result.checklist)
    }

    /// Re-audit P1/P8 (10/7/26): the GET on appear says "not measured yet"
    /// with a reason and no score; a stored run says when it was measured.
    func testDecodesNotMeasuredAndAStoredRun() throws {
        let none = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data("""
        {"ok": true, "state": "not_measured", "measured": false, "ai_score": null, "queries": [],
         "reason": "No AI visibility check is on record yet. The weekly check runs on Mondays; Check runs one now."}
        """.utf8))
        XCTAssertTrue(none.isNotMeasured)
        XCTAssertNil(none.aiScore)
        XCTAssertEqual(none.reason?.value?.hasPrefix("No AI visibility check"), true)
        let stored = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data("""
        {"ok": true, "state": "partial", "measured": true, "partial": true, "ai_score": 40,
         "answered_queries": 6, "total_queries": 8, "measured_at": "2026-10-05 12:00:00", "cached": true}
        """.utf8))
        XCTAssertFalse(stored.isNotMeasured)
        XCTAssertFalse(stored.scoreIsMeasured)
        XCTAssertNotNil(stored.measuredLine)
        let older = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(#"{"ok": true}"#.utf8))
        XCTAssertFalse(older.isNotMeasured)
    }
}
