import Foundation
import Observation

struct AIVisibilityQuery: Decodable, Identifiable {
    let query: String
    let answer: String
    let appeared: Bool

    var id: String { query }
}

struct AIVisibilityRun: Decodable, Identifiable {
    let aiScore: Int
    let gbpScore: Int?
    let answered: Int?
    let appeared: Int?
    let createdAt: String
    var id: String { createdAt }
    enum CodingKeys: String, CodingKey {
        case aiScore = "ai_score"
        case gbpScore = "gbp_score"
        // Sample size behind this run, so a change can be told from a
        // difference in how many questions came back.
        case answered
        case appeared
        case createdAt = "created_at"
    }
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
    // The honest bounds on aiScore for this sample. A handful of questions
    // answered by a non-deterministic model is a range, not a point.
    let aiScoreLow: Int?
    let aiScoreHigh: Int?
    // How many of the questions we sent actually came back. When this is
    // below totalQueries the run is partial and the score is an estimate.
    let answeredQueries: Int?
    let partial: Bool?
    // False when we have no city for this restaurant, which makes two
    // locations of one brand indistinguishable in an answer — appearance
    // cannot be judged at all, and a zero would be a statement about the
    // profile rather than about the restaurant.
    let locationKnown: Bool?
    let city: String?
    let citySource: String?
    let gbpScore: Int?
    // gbpScore's two halves, split apart. presenceScore covers the
    // restaurant's actual public listing and review record; setup counts
    // this product's own configuration and is deliberately not scored.
    let presenceScore: Int?
    let setupDone: Int?
    let setupTotal: Int?
    let claimKinds: [String: String]?
    let checklist: [AIVisibilityChecklistItem]?
    let gbpConnected: Bool?

    /// True only when every question came back AND we know the city. Any
    /// other state means the score is not a measurement of this restaurant.
    var scoreIsMeasured: Bool {
        (partial == false || partial == nil) && (locationKnown ?? true) && aiScore != nil
    }

    /// Why the score cannot be read as a measurement, or nil.
    var scoreCaveat: String? {
        if locationKnown == false {
            return "We don't have a city for this restaurant, so we can't tell your listing apart from another location with the same name. Add your Google Place ID in Account."
        }
        if aiScore == nil {
            return "AI search didn't answer this time. This isn't a reading of your visibility — try again shortly."
        }
        if partial == true, let a = answeredQueries, let t = totalQueries {
            return "Only \(a) of \(t) questions came back, so this is an estimate rather than a measurement."
        }
        if citySource == "profile" {
            return "The city in these questions comes from your profile text, not from your Google listing."
        }
        return nil
    }
    // Marketing pieces logged in the trailing 30 days — not a GBP field,
    // so it rides along outside gbp_score/checklist as its own count. Powers
    // the roadmap's "Post consistently on social" auto-done detection.
    let socialPosts30d: Int?
    // Real per-restaurant numbers behind the review-volume and response-rate
    // checklist items — previously computed server-side but never left
    // client_api.py, so the roadmap could only ever show a boolean done
    // flag instead of this restaurant's own actual counts. Now used to
    // build roadmap copy like "38 of 50 reviews" instead of identical
    // boilerplate for every restaurant.
    let reviewTotal: Int?
    let respRate: Double?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, queries, checklist, partial, city
        case restaurantName = "restaurant_name"
        case appearedCount = "appeared_count"
        case totalQueries = "total_queries"
        case aiScore = "ai_score"
        case aiScoreLow = "ai_score_low"
        case aiScoreHigh = "ai_score_high"
        case answeredQueries = "answered_queries"
        case locationKnown = "location_known"
        case citySource = "city_source"
        case gbpScore = "gbp_score"
        case presenceScore = "presence_score"
        case setupDone = "setup_done"
        case setupTotal = "setup_total"
        case claimKinds = "claim_kinds"
        case gbpConnected = "gbp_connected"
        case socialPosts30d = "social_posts_30d"
        case reviewTotal = "review_total"
        case respRate = "resp_rate"
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
    var history: [AIVisibilityRun] = []
    private struct HistoryResponse: Decodable { let ok: Bool; let runs: [AIVisibilityRun] }

    /// Every past check (ai_visibility_runs) — the Orbit's trend line.
    func loadHistory() async {
        if let response: HistoryResponse = try? await client.send("/mobile/api/intel/ai-visibility/history", hapticOnError: false) {
            history = response.runs
        }
    }

    func check() async {
        isChecking = true
        defer { isChecking = false }
        do {
            result = try await client.send("/mobile/api/intel/ai-visibility")
            await loadHistory()
        } catch {
            result = nil
        }
    }
}
