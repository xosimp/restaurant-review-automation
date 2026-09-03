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

    /// The restaurant's own login (role 'client', the backend default) or
    /// Will's multi-restaurant login ('owner') — anyone who isn't an invited
    /// teammate ('member'). The first cut checked == "owner", which no client
    /// login has, so the Team row never appeared for anyone (and the server
    /// 403'd them too). See auth.py's invite_team_member for the vocabulary.
    var isOwner: Bool { role != "member" }
}
