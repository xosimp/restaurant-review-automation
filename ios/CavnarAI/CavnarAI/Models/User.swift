import Foundation

/// Mirrors mobile_api.py's `_public_user()` shape — deliberately never
/// includes the password hash.
struct User: Codable, Equatable {
    let id: Int
    let username: String
    let email: String
    /// var, not let: an owner switching locations changes which restaurant
    /// this session acts on, and SessionStore.didSwitchLocation updates it
    /// in place so nothing downstream keeps acting on the old one.
    var restaurantId: Int
    let role: String
    let isAdmin: Bool

    enum CodingKeys: String, CodingKey {
        case id, username, email, role
        case restaurantId = "restaurant_id"
        case isAdmin = "is_admin"
    }

    /// An owner login: 'client' (a restaurant's owner or co-owner — the
    /// backend default, so an empty role counts) or 'owner' (the
    /// multi-location login). Matches permissions.TEAM_INVITE server-side.
    /// Was `role != "member"`, which also counted managers — who then saw
    /// owner-only controls the server refuses them.
    var isOwner: Bool { role.isEmpty || role == "client" || role == "owner" || isAdmin }
}
