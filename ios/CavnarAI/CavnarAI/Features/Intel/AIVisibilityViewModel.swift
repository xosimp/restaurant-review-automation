import Foundation
import Observation

struct AIVisibilityQuery: Decodable, Identifiable {
    let query: String
    let answer: String
    let appeared: Bool

    var id: String { query }
}

struct AIVisibilityChecklistItem: Decodable, Identifiable {
    let label: String
    let done: Bool
    let pts: Int
    let action: String
    let needsGmb: Bool

    var id: String { label }

    enum CodingKeys: String, CodingKey {
        case label, done, pts, action
        case needsGmb = "needs_gmb"
    }
}

struct AIVisibilityResult: Decodable {
    let ok: Bool
    let restaurantName: String?
    let queries: [AIVisibilityQuery]?
    let appearedCount: Int?
    let totalQueries: Int?
    let aiScore: Int?
    let gbpScore: Int?
    let checklist: [AIVisibilityChecklistItem]?
    let gbpConnected: Bool?
    // Marketing pieces logged in the trailing 30 days — not a GBP field,
    // so it rides along outside gbp_score/checklist as its own count. Powers
    // the roadmap's "Post consistently on social" auto-done detection.
    let socialPosts30d: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, queries, checklist
        case restaurantName = "restaurant_name"
        case appearedCount = "appeared_count"
        case totalQueries = "total_queries"
        case aiScore = "ai_score"
        case gbpScore = "gbp_score"
        case gbpConnected = "gbp_connected"
        case socialPosts30d = "social_posts_30d"
    }
}

@Observable
@MainActor
final class AIVisibilityViewModel {
    var result: AIVisibilityResult?
    var isChecking = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    /// Deliberately NOT auto-loaded on screen appear — each check fires real,
    /// billable Perplexity queries (same 3-call/60s limit the web route
    /// shares), so this only runs when the owner explicitly asks for it.
    func check() async {
        isChecking = true
        defer { isChecking = false }
        do {
            result = try await client.send("/mobile/api/intel/ai-visibility")
        } catch {
            result = nil
        }
    }
}
