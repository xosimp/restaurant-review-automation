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

    /// review_date's format depends entirely on which source fetched it
    /// (fetcher.py/gmb.py) — there's no single shape:
    ///   - Google Places API fetch: "yyyy-MM-dd'T'HH:mm:ss" (local time, no offset)
    ///   - Yelp Fusion API's time_created: "yyyy-MM-dd HH:mm:ss" (space, not "T")
    ///   - Google Business Profile (GMB) updateTime: RFC3339, e.g.
    ///     "2026-08-01T14:30:00.123456Z" (fractional seconds + Z)
    ///   - CSV import: whatever the uploaded file's "date" column contained,
    ///     often just "yyyy-MM-dd"
    /// A review with no date, or a date in a shape none of these match,
    /// simply has no formattedDate — that's the actual explanation for why
    /// some rows show a date and others don't, not a bug in any one of them.
    private static let sourceDateFormatters: [DateFormatter] = {
        ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"].map { format in
            let formatter = DateFormatter()
            formatter.dateFormat = format
            formatter.locale = Locale(identifier: "en_US_POSIX")
            return formatter
        }
    }()

    private static let isoFormatterWithFractionalSeconds: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let isoFormatter = ISO8601DateFormatter()

    private static let displayDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d"
        return formatter
    }()

    var formattedDate: String? {
        guard let reviewDate, !reviewDate.isEmpty else { return nil }
        let date = Self.isoFormatterWithFractionalSeconds.date(from: reviewDate)
            ?? Self.isoFormatter.date(from: reviewDate)
            ?? Self.sourceDateFormatters.lazy.compactMap { $0.date(from: reviewDate) }.first
        guard let date else { return nil }
        return Self.displayDateFormatter.string(from: date)
    }

    var platformDisplayName: String {
        switch platform {
        case "google": return "Google"
        case "yelp": return "Yelp"
        default: return platform.capitalized
        }
    }

    /// A copy with a new status — used to reflect an approve/skip result in
    /// the list immediately, without a full reload from the server.
    func withStatus(_ newStatus: String) -> Review {
        Review(
            id: id, platform: platform, author: author, rating: rating, text: text,
            reviewDate: reviewDate, sentiment: sentiment, urgency: urgency,
            draftResponse: draftResponse, responseStatus: newStatus, categories: categories
        )
    }
}
