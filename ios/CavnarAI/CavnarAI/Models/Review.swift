import Foundation

/// Decodes one row from GET /mobile/api/reviews (models.get_reviews_data() —
/// a raw `SELECT * FROM reviews` dict). Only the fields the app's UI
/// actually uses are modeled; Codable silently ignores the rest (fetched_at,
/// review_name, processed, deleted_at, draft_edited, regenerate_count,
/// response_action, ...) rather than needing every column mirrored here.
struct Review: Codable, Identifiable, Hashable {
    let id: Int
    let platform: String
    let author: String?
    let rating: Int?
    let text: String?
    let reviewDate: String?
    let sentiment: String?
    let urgency: String
    let draftResponse: String?
    let responseStatus: String
    let categories: [String]

    enum CodingKeys: String, CodingKey {
        case id, platform, author, rating, text, sentiment, urgency, categories
        case reviewDate = "review_date"
        case draftResponse = "draft_response"
        case responseStatus = "response_status"
    }

    var isAwaitingApproval: Bool { responseStatus == "drafted" }
    var isPosted: Bool { responseStatus == "posted" }
    var isApproved: Bool { responseStatus == "approved" }
    var isUrgent: Bool { urgency == "high" }
}
