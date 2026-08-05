import XCTest
@testable import CavnarAI

final class ChangelogTests: XCTestCase {
    func testDecodesChangelogEntry() throws {
        let json = """
        {"id": 1, "title": "New Labor Analytics", "body": "See 8-week trends and AI insights.",
         "tag": "feature", "published_at": "2026-08-01T00:00:00"}
        """
        let entry = try JSONDecoder.cavnar.decode(ChangelogEntry.self, from: Data(json.utf8))
        XCTAssertEqual(entry.title, "New Labor Analytics")
        XCTAssertEqual(entry.tag, "feature")
    }

    func testDecodesChangelogEntryWithNilBodyAndTag() throws {
        let json = """
        {"id": 2, "title": "Small fix", "body": null, "tag": null, "published_at": null}
        """
        let entry = try JSONDecoder.cavnar.decode(ChangelogEntry.self, from: Data(json.utf8))
        XCTAssertNil(entry.body)
        XCTAssertNil(entry.tag)
    }
}
