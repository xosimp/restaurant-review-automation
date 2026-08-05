import Foundation
import Observation

@Observable
@MainActor
final class ChangelogViewModel {
    var entries: [ChangelogEntry] = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Response: Decodable {
        let ok: Bool
        let entries: [ChangelogEntry]
    }

    /// Loading this list marks it seen server-side (same as the web route),
    /// which is what clears the unread badge.
    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: Response = try await client.send("/mobile/api/changelog")
            entries = response.entries
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load what's new."
        }
    }
}

@Observable
@MainActor
final class ChangelogBadgeViewModel {
    var unreadCount = 0

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct CountResponse: Decodable {
        let ok: Bool
        let count: Int
    }

    func refresh() async {
        if let response: CountResponse = try? await client.send("/mobile/api/changelog/unread-count") {
            unreadCount = response.count
        }
    }
}
