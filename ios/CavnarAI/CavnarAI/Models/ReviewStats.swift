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
    // Reviews we hold but could not analyse. Non-zero means the sentiment
    // split and the topic charts cover fewer reviews than the totals above.
    let unanalysed: Int?
    let sentimentComplete: Bool?
    // Google's own all-time rating, beside our own average over the reviews
    // we actually hold. These are different numbers over different
    // populations and both were shown unlabelled.
    let officialRating: Double?
    let officialReviewCount: Int?
    let isFullHistory: Bool?

    /// avgRating is an average over `total` reviews, not the restaurant's
    /// whole history, unless this says otherwise.
    var holdsEveryReview: Bool { isFullHistory ?? true }

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
        case unanalysed
        case sentimentComplete = "sentiment_complete"
        case officialRating = "official_rating"
        case officialReviewCount = "official_review_count"
        case isFullHistory = "is_full_history"
    }
}
