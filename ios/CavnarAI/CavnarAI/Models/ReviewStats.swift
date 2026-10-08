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

    /// What one bulk publish may post (models.BULK_PUBLISHABLE_SQL) and how
    /// many drafts it holds back for a read — the inbox's "Publish N ready"
    /// (parity audit 10/7/26 #34). Nil from an older server.
    var publishable: Int? = nil
    var publishHeld: Int? = nil

    /// "6 new this month · avg reply 5h" — the web header's two segments.
    /// The reply time reads "—" when there is none, or it is over 30 days.
    var receivedLine: String {
        "\(receivedThisMonth) new this month \u{00B7} avg reply \(Self.replyTime(avgResponseHours))"
    }

    /// The web header's rule: hours under a day, days under a week, weeks
    /// up to 30 days; anything else is not a reply time worth stating.
    static func replyTime(_ hours: Double?) -> String {
        guard let h = hours, h > 0, h <= 720 else { return "\u{2014}" }
        if h < 24 {
            let r = (h * 10).rounded() / 10
            return r == r.rounded() ? "\(Int(r))h" : "\(r)h"
        }
        if h < 168 { return "\(Int(h / 24))d" }
        return "\(Int(h / 168))w"
    }

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
        case publishable
        case publishHeld = "publish_held"
    }
}
