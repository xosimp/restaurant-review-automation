import Foundation

/// An admin viewing the app as a client (10/8/26) — the phone's twin of the
/// web's /admin/view-as/<id>. The server opens a view-as session on the
/// restaurant's owner login (auth.create_view_as_session: two hours,
/// absolute, the admin named on it, every write recorded as his) and hands
/// its token over; the app wears it while the admin's own token stays in
/// `Keychain.Key.sessionToken`, untouched.
///
/// Keeping the admin's token in the ordinary slot is deliberate: everything
/// that runs outside the app's screens — the widget, the Share extension,
/// Live Activities, lock-screen actions, Siri — reads that slot, so none of
/// it ever acts as the client or is filed under the client's account. Only
/// the app's own requests (APIClient's token) wear the view.
struct ViewAsSession: Codable, Equatable {
    let restaurantId: Int
    let restaurantName: String
    let endsAt: Date
    let readOnly: Bool
    /// The admin's own login, to put back when the view ends — offline too,
    /// without waiting on /me.
    let returnUser: User

    private static let defaultsKey = "cavnar.view_as"

    /// The view in force, or nil. A record without its token is a leftover
    /// (the token went with a sign-out) and is dropped.
    static func load() -> ViewAsSession? {
        guard Keychain.get(Keychain.Key.viewAsToken) != nil,
              let data = UserDefaults.standard.data(forKey: defaultsKey),
              let session = try? JSONDecoder().decode(ViewAsSession.self, from: data) else {
            clearRecord()
            return nil
        }
        return session
    }

    func save() {
        if let data = try? JSONEncoder().encode(self) {
            UserDefaults.standard.set(data, forKey: Self.defaultsKey)
        }
    }

    static func clearRecord() {
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }

    /// Whether this phone is viewing as a client now — read outside the
    /// session store (push registration).
    static var isActive: Bool { Keychain.get(Keychain.Key.viewAsToken) != nil }
}

/// GET /mobile/api/admin/clients.
struct ViewAsClientsResponse: Decodable {
    let ok: Bool
    let clients: [ViewAsClient]
    let hours: Int?
    let readOnly: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, clients, hours, error
        case readOnly = "read_only"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decode(Bool.self, forKey: .ok)
        clients = try c.decodeIfPresent([ViewAsClient].self, forKey: .clients) ?? []
        hours = try c.decodeIfPresent(Int.self, forKey: .hours)
        readOnly = try c.decodeIfPresent(Bool.self, forKey: .readOnly)
        error = try c.decodeIfPresent(String.self, forKey: .error)
    }
}

struct ViewAsClient: Decodable, Identifiable, Equatable {
    let id: Int
    let name: String
    let locationName: String?
    let neighborhood: String?
    let isDemo: Bool
    let billingStatus: String?

    enum CodingKeys: String, CodingKey {
        case id, name, neighborhood
        case locationName = "location_name"
        case isDemo = "is_demo"
        case billingStatus = "billing_status"
    }

    /// The second line under the name: the location, else the neighborhood.
    var detail: String? {
        let parts = [locationName, neighborhood].compactMap { $0?.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        return parts.first
    }
}

/// POST /mobile/api/admin/view-as/<id>.
struct ViewAsStartResponse: Decodable {
    let ok: Bool
    let token: String
    let user: User
    let restaurantId: Int
    let restaurantName: String
    let endsAt: String
    let readOnly: Bool

    enum CodingKeys: String, CodingKey {
        case ok, token, user
        case restaurantId = "restaurant_id"
        case restaurantName = "restaurant_name"
        case endsAt = "ends_at"
        case readOnly = "read_only"
    }
}
