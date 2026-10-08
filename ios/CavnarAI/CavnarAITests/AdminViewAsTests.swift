import XCTest
@testable import CavnarAI

/// Admin view-as from the app (10/8/26): what the server hands back decodes,
/// and the banner says what the web's says.
@MainActor
final class AdminViewAsTests: XCTestCase {

    func testTheOpenResponseDecodesToTheClientsOwnerLogin() throws {
        let body = """
        {"ok": true, "token": "tok", "restaurant_id": 5, "restaurant_name": "Simple EJ's",
         "ends_at": "2026-10-08T23:15:00Z", "read_only": false, "hours": 2,
         "user": {"id": 41, "username": "erik", "email": "e@x.test", "restaurant_id": 5,
                  "role": "client", "is_admin": false, "can_manage_team": true}}
        """
        let r = try JSONDecoder().decode(ViewAsStartResponse.self, from: Data(body.utf8))
        XCTAssertEqual(r.user.id, 41)
        XCTAssertFalse(r.user.isInternal)
        XCTAssertEqual(r.restaurantName, "Simple EJ's")
        XCTAssertNotNil(CavnarISODate.parse(r.endsAt))
    }

    func testTheClientListToleratesMissingOptionalFields() throws {
        let body = """
        {"ok": true, "hours": 2, "read_only": false,
         "clients": [{"id": 5, "name": "Simple EJ's", "location_name": null, "neighborhood": "Orland Park",
                      "is_demo": false, "billing_status": "active"}]}
        """
        let r = try JSONDecoder().decode(ViewAsClientsResponse.self, from: Data(body.utf8))
        XCTAssertEqual(r.clients.first?.detail, "Orland Park")
        let refused = try JSONDecoder().decode(ViewAsClientsResponse.self,
                                               from: Data(#"{"ok": false, "error": "Only a Cavnar AI admin can do this."}"#.utf8))
        XCTAssertTrue(refused.clients.isEmpty)
    }

    func testOnlyAnInternalLoginIsOfferedTheView() {
        let admin = User(id: 1, username: "will", email: "w@x.test", restaurantId: 1, role: "client", isAdmin: true)
        let support = User(id: 2, username: "sam", email: "s@x.test", restaurantId: 1, role: "support", isAdmin: false)
        let owner = User(id: 3, username: "erik", email: "e@x.test", restaurantId: 5, role: "client", isAdmin: false)
        XCTAssertTrue(admin.isInternal)
        XCTAssertTrue(support.isInternal)
        XCTAssertFalse(owner.isInternal)
    }

    func testTheBannerSaysWhatAChangeDoesAndWhenItEnds() {
        let admin = User(id: 1, username: "will", email: "w@x.test", restaurantId: 1, role: "client", isAdmin: true)
        let ends = Date(timeIntervalSince1970: 1_791_000_000)
        let writable = ViewAsSession(restaurantId: 5, restaurantName: "Simple EJ's", endsAt: ends,
                                     readOnly: false, returnUser: admin)
        XCTAssertTrue(ViewAsBanner.detail(writable).hasPrefix("Changes are recorded under your name."))
        XCTAssertTrue(ViewAsBanner.detail(writable).contains("Ends at"))
        let readOnly = ViewAsSession(restaurantId: 5, restaurantName: "Simple EJ's", endsAt: ends,
                                     readOnly: true, returnUser: admin)
        XCTAssertTrue(ViewAsBanner.detail(readOnly).hasPrefix("Read-only: nothing can be changed."))
    }

    func testTheRecordKeepsTheAdminToPutBack() throws {
        let admin = User(id: 1, username: "will", email: "w@x.test", restaurantId: 1, role: "client", isAdmin: true)
        let view = ViewAsSession(restaurantId: 5, restaurantName: "Simple EJ's", endsAt: Date(),
                                 readOnly: false, returnUser: admin)
        let round = try JSONDecoder().decode(ViewAsSession.self, from: JSONEncoder().encode(view))
        XCTAssertEqual(round.returnUser, admin)
    }
}
