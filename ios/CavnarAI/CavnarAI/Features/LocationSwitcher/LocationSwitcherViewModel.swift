import Foundation
import Observation

@Observable
@MainActor
final class LocationSwitcherViewModel {
    var locations: [LocationOption] = []
    var groupName: String?
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient
    /// Set by the view from the environment. The switch has consequences the
    /// session owns — dropping queued writes for the location being left,
    /// purging its cached data — so the switch can't just be a request.
    var session: SessionStore?

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct LocationsResponse: Decodable {
        let ok: Bool
        let locations: [LocationOption]
        let groupName: String?

        enum CodingKeys: String, CodingKey {
            case ok, locations
            case groupName = "group_name"
        }
    }

    /// Every location's status, what needs the owner there and last night's
    /// net, plus the portfolio strip and the attention across them — the
    /// group brief (GET /mobile/api/home/brief/group, the web's
    /// `hbSwitcherFill` / group Home; parity audit #8). Nil for a single
    /// location, a login that may not switch, or an older server — the rows
    /// then read as names alone, as before.
    var group: LocationGroupBrief?

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: LocationsResponse = try await client.send("/mobile/api/group-locations")
            locations = response.locations
            groupName = response.groupName
            if response.locations.count > 1 {
                await loadGroup()
            } else {
                group = nil
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            errorMessage = "Couldn't load locations."
        }
    }

    /// The group read alone — the all-locations screen's pull-to-refresh
    /// passes `fresh` so the server rebuilds it rather than serving its
    /// 60-second copy (parity audit #32).
    func loadGroup(fresh: Bool = false) async {
        do {
            let g: LocationGroupBrief = try await client.send("/mobile/api/home/brief/group",
                                                              query: fresh ? ["fresh": "1"] : [:],
                                                              hapticOnError: false)
            group = g.ok ? g : nil
        } catch let error as APIClient.APIError where fresh {
            errorMessage = error.message
        } catch {
            if !fresh { group = nil }
        }
    }

    private struct SwitchBody: Encodable {
        let restaurantId: Int
        enum CodingKeys: String, CodingKey { case restaurantId = "restaurant_id" }
    }

    private struct SwitchResponse: Decodable {
        let ok: Bool
        let restaurantId: Int
        let restaurantName: String

        enum CodingKeys: String, CodingKey {
            case ok
            case restaurantId = "restaurant_id"
            case restaurantName = "restaurant_name"
        }
    }

    /// One location's row from the group brief, by id.
    func signal(for location: LocationOption) -> LocationGroupBrief.Location? {
        group?.locations.first { $0.id == location.id }
    }

    /// Returns true on success — the caller (LocationSwitcherView) reloads
    /// Home and dismisses on success, and shows errorMessage otherwise.
    func switchTo(_ location: LocationOption) async -> Bool {
        do {
            let response: SwitchResponse = try await client.send(
                "/mobile/api/switch-location", method: .post, body: SwitchBody(restaurantId: location.id)
            )
            // Everything held for the previous location — queued offline
            // writes, cached labor/schedule data — belongs to that location,
            // not this one.
            // The switcher's own label for it (the location's name, which
            // tells a group's stores apart; the response's is the brand's).
            await session?.didSwitchLocation(to: response.restaurantId,
                                             name: location.name.isEmpty ? response.restaurantName : location.name)
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            return false
        } catch {
            errorMessage = "Couldn't switch locations."
            return false
        }
    }
}
