import Foundation
import Observation

@Observable
@MainActor
final class HomeViewModel {
    var summary: HomeSummary?
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            summary = try await client.send("/mobile/api/home")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is APIClient.SessionExpiredError {
            // SessionStore's handler already forces logout — nothing more to do.
        } catch {
            errorMessage = "Couldn't load your dashboard."
        }
    }
}
