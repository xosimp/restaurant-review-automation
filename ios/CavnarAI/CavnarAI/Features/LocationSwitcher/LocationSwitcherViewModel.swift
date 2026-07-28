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

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: LocationsResponse = try await client.send("/mobile/api/group-locations")
            locations = response.locations
            groupName = response.groupName
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load locations."
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

    /// Returns true on success — the caller (LocationSwitcherView) reloads
    /// Home and dismisses on success, and shows errorMessage otherwise.
    func switchTo(_ location: LocationOption) async -> Bool {
        do {
            let _: SwitchResponse = try await client.send(
                "/mobile/api/switch-location", method: .post, body: SwitchBody(restaurantId: location.id)
            )
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
