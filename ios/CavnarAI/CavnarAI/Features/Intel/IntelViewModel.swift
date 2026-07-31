import Foundation
import Observation

struct IntelSection: Codable, Identifiable {
    let name: String
    let bullets: [String]

    var id: String { name }
}

struct IntelSummary: Decodable {
    let ok: Bool
    let hasData: Bool
    let intro: String?
    let recommendations: [String]
    let sections: [IntelSection]

    enum CodingKeys: String, CodingKey {
        case ok
        case hasData = "has_data"
        case intro, recommendations, sections
    }
}

/// Read-only — refreshing competitor intel is a long-running, desktop-
/// triggered operation; this screen just reads whatever that last produced
/// (see mobile_api.py's _do_mobile_intel docstring).
@Observable
@MainActor
final class IntelViewModel {
    var summary: IntelSummary?
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
            summary = try await client.send("/mobile/api/intel")
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load competitor intel."
        }
    }
}
