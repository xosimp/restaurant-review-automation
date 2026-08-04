import XCTest
@testable import CavnarAI

final class MarketingAnalyticsTests: XCTestCase {
    func testDecodesMarketingPerformanceWithTopPost() throws {
        let json = """
        {"ok": true, "published": 4, "has_data": true, "total_reach": 800,
         "total_engagement": 42, "top_post": {"topic": "Taco Tuesday",
         "platform": "instagram", "reach": 500, "likes": 30, "comments": 4, "shares": 1}}
        """
        let perf = try JSONDecoder.cavnar.decode(MarketingPerformance.self, from: Data(json.utf8))
        XCTAssertTrue(perf.hasData)
        XCTAssertEqual(perf.topPost?.topic, "Taco Tuesday")
        XCTAssertEqual(perf.totalReach, 800)
    }

    func testDecodesMarketingPerformanceWithoutTopPost() throws {
        let json = """
        {"ok": true, "published": 0, "has_data": false, "total_reach": 0,
         "total_engagement": 0, "top_post": null}
        """
        let perf = try JSONDecoder.cavnar.decode(MarketingPerformance.self, from: Data(json.utf8))
        XCTAssertFalse(perf.hasData)
        XCTAssertNil(perf.topPost)
    }

    func testDecodesGuestContact() throws {
        let json = """
        {"id": 1, "name": "Jamie", "phone": "+13125550100", "consent": true, "last_visit": null}
        """
        let contact = try JSONDecoder.cavnar.decode(GuestContact.self, from: Data(json.utf8))
        XCTAssertEqual(contact.name, "Jamie")
        XCTAssertEqual(contact.consent, true)
    }
}
