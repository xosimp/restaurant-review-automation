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

    func testDecodesRateLimitedErrorResult() throws {
        let json = """
        {"ok": false, "error": "Too many visibility checks — please wait a moment and try again."}
        """
        let result = try JSONDecoder.cavnar.decode(AIVisibilityResult.self, from: Data(json.utf8))
        XCTAssertFalse(result.ok)
        XCTAssertNil(result.checklist)
    }
}
