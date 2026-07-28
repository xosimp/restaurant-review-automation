import Foundation

/// Mirrors mobile_api.py's `_public_user()` shape — deliberately never
/// includes the password hash.
struct User: Codable, Equatable {
    let id: Int
    let username: String
    let email: String
    let restaurantId: Int
    let role: String
    let isAdmin: Bool

    enum CodingKeys: String, CodingKey {
        case id, username, email, role
        case restaurantId = "restaurant_id"
        case isAdmin = "is_admin"
    }

    var isOwner: Bool { role == "owner" }
}
