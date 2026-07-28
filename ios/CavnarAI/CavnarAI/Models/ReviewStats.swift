import Foundation

/// Decodes GET /mobile/api/review-stats — the same dict models.get_review_stats()
/// returns on the web side, flattened directly into the JSON response (no
/// wrapper key).
struct ReviewStats: Codable {
    let total: Int
    let positive: Int
    let positivePct: Int
    let negative: Int
    let neutral: Int
    let urgent: Int
    let avgRating: Double
    let avgRating30d: Double
    let awaitingApproval: Int
    let needsResponse: Int
    let posted: Int
    let responded: Int
    let skipped: Int
    let thisMonth: Int
    let receivedThisMonth: Int
    let last30d: Int
    let responseRate: Double
    let avgResponseHours: Double?

    enum CodingKeys: String, CodingKey {
        case total, positive, negative, neutral, urgent, posted, responded, skipped
        case positivePct = "positive_pct"
        case avgRating = "avg_rating"
        case avgRating30d = "avg_rating_30d"
        case awaitingApproval = "awaiting_approval"
        case needsResponse = "needs_response"
        case thisMonth = "this_month"
        case receivedThisMonth = "received_this_month"
        case last30d = "last_30d"
        case responseRate = "response_rate"
        case avgResponseHours = "avg_response_hours"
    }
}
