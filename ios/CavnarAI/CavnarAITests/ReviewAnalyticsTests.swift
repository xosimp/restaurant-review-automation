import XCTest
@testable import CavnarAI

final class ReviewAnalyticsTests: XCTestCase {
    func testDecodesResponsePerformance() throws {
        let json = """
        {"total": 10, "days": 90, "approved_as_is": 6, "edited": 3, "regenerated": 1}
        """
        let perf = try JSONDecoder.cavnar.decode(ResponsePerformance.self, from: Data(json.utf8))
        XCTAssertEqual(perf.total, 10)
        XCTAssertEqual(perf.approvedAsIs, 6)
        XCTAssertEqual(perf.regenerated, 1)
    }

    func testDecodesTopicHeatmapEntry() throws {
        let json = """
        {"category": "food_quality", "label": "Food Quality", "count": 5, "positive": 3,
         "negative": 1, "neutral": 1, "pct_positive": 60, "pct_negative": 20, "trend": "up"}
        """
        let entry = try JSONDecoder.cavnar.decode(TopicHeatmapEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.label, "Food Quality")
        XCTAssertEqual(entry.pctPositive, 60)
        XCTAssertEqual(entry.trend, "up")
    }

    func testDecodesSentimentWeek() throws {
        let json = """
        {"label": "8/1", "week_key": "2026-W31", "positive": 4, "negative": 1,
         "neutral": 0, "total": 5, "avg_rating": 4.2}
        """
        let week = try JSONDecoder.cavnar.decode(SentimentWeek.self, from: Data(json.utf8))
        XCTAssertEqual(week.label, "8/1")
        XCTAssertEqual(week.total, 5)
        XCTAssertEqual(week.avgRating, 4.2)
    }

    func testDecodesResponseTemplate() throws {
        let json = """
        {"id": 1, "title": "Thanks!", "body": "Thanks for the kind words!",
         "category": "positive", "use_count": 3}
        """
        let template = try JSONDecoder.cavnar.decode(ResponseTemplate.self, from: Data(json.utf8))
        XCTAssertEqual(template.title, "Thanks!")
        XCTAssertEqual(template.useCount, 3)
    }
}
