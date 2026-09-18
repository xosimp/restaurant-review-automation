import Foundation

/// Decodes one row from GET /mobile/api/reviews (models.get_reviews_data() —
/// a raw `SELECT * FROM reviews` dict). Only the fields the app's UI
/// actually uses are modeled; Codable silently ignores the rest (fetched_at,
/// review_name, deleted_at, draft_edited, regenerate_count,
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
    // Set when the draft generated cleanly but states something this system
    // cannot stand behind — a specific action the restaurant may never have
    // taken. Advisory: the owner can still post it, having been shown why.
    let draftNeedsReview: Bool?
    let draftReviewReason: String?
    // When the guest edited their own review after leaving it, and what they
    // first rated. A one-star raised to five used to stay a one-star here
    // forever, because the fetch was insert-only.
    let editedAt: String?
    let originalRating: Int?
    /// Server-computed: posted AND google AND a real review_name.
    let canRetractFlag: Bool?
    /// False while the review is still waiting on Claude's analysis — its
    /// sentiment, categories and urgency are all absent, and showing it as
    /// "neutral" claimed a reading nobody made.
    let processed: Bool?
    /// Cavnar's own one-line read of this review. The analyser has written
    /// one on every review since the product existed and NOTHING rendered it
    /// on either platform — the only per-review AI reasoning the system
    /// produced was output the client paid for and never saw.
    let summary: String?
    /// The single concrete thing that went wrong, in at most eight words.
    let specificComplaint: String?
    /// How serious, beyond the binary urgency that drives alerting:
    /// safety / legal / operational / service / minor.
    let severity: String?
    let severityLabel: String?
    /// The dish, role, daypart and service mode the review actually named.
    /// Everything here came out of the guest's own words — the analyser is
    /// forbidden from inferring a dish from a category or a role from a
    /// complaint, so an absent field means the review did not say.
    let entities: ReviewEntities?

    struct ReviewEntities: Codable, Hashable {
        let dishes: [String]?
        let staffRoles: [String]?
        let daypart: String?
        let serviceMode: String?

        enum CodingKeys: String, CodingKey {
            case dishes, daypart
            case staffRoles = "staff_roles"
            case serviceMode = "service_mode"
        }

        /// Chips, in the order they should read: what was eaten, who served
        /// it, when. Empty when the review named nothing, which is common
        /// and correct — it is not a gap to fill.
        var chips: [String] {
            var out: [String] = []
            out.append(contentsOf: (dishes ?? []).prefix(2))
            out.append(contentsOf: (staffRoles ?? []).prefix(2).map { $0.replacingOccurrences(of: "_", with: " ") })
            if let daypart { out.append(daypart.replacingOccurrences(of: "_", with: " ")) }
            if let serviceMode { out.append(serviceMode.replacingOccurrences(of: "_", with: " ")) }
            return out
        }
    }

    var isAnalysed: Bool { processed ?? true }

    /// The two tiers an owner must not scroll past. The other three are real
    /// and used for ordering, but a badge on every review is a badge on none.
    var isHighSeverity: Bool { severity == "safety" || severity == "legal" }

    enum CodingKeys: String, CodingKey {
        case id, platform, author, rating, text, sentiment, urgency, categories, processed
        case summary, severity, entities
        case specificComplaint = "specific_complaint"
        case severityLabel = "severity_label"
        case canRetractFlag = "can_retract"
        case reviewDate = "review_date"
        case draftResponse = "draft_response"
        case responseStatus = "response_status"
        case draftNeedsReview = "draft_needs_review"
        case draftReviewReason = "draft_review_reason"
        case editedAt = "edited_at"
        case originalRating = "original_rating"
    }

    var draftIsFlagged: Bool { draftNeedsReview == true }

    /// The guest changed their rating after the fact.
    var ratingWasChanged: Bool {
        guard let originalRating, let rating else { return false }
        return originalRating != rating
    }

    var isAwaitingApproval: Bool { responseStatus == "drafted" }
    /// Still sitting in the owner's queue: a drafted reply waiting on a
    /// decision, or a review with no draft yet. This is what "To approve"
    /// means on the server (models.get_reviews_data) and now on the web —
    /// the phone's version was drafted-only, so a review whose draft had
    /// not been written yet was missing from the chip that counts the work.
    var isInQueue: Bool { responseStatus == "pending" || responseStatus == "drafted" }
    /// Whether Retract can possibly succeed, computed server-side. The app
    /// cannot derive it: review_name (the Business Profile resource our own
    /// auto-post sets) is deliberately not decoded here, so the detail
    /// screen offered "Retract from Google" on every posted review —
    /// including Yelp ones, and including imported reviews that were never
    /// posted by us — and every one of those 400s.
    var canRetract: Bool { canRetractFlag ?? false }
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
    /// All four formatters are thread-local rather than shared singletons:
    /// formattedDate is reachable from both the APIClient actor's decode path
    /// and @MainActor view bodies, and Foundation formatters are not Sendable
    /// (audit 2.2). See ThreadLocalFormatter.
    private static let sourceDateFormatters = ThreadLocalFormatter<NSArray> {
        let formatters = ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd"].map { format -> DateFormatter in
            let formatter = DateFormatter()
            formatter.dateFormat = format
            formatter.locale = Locale(identifier: "en_US_POSIX")
            return formatter
        }
        return formatters as NSArray
    }

    private static let isoFormatterWithFractionalSeconds = ThreadLocalFormatter<ISO8601DateFormatter> {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }

    private static let isoFormatter = ThreadLocalFormatter<ISO8601DateFormatter> { ISO8601DateFormatter() }

    private static let displayDateFormatter = ThreadLocalFormatter<DateFormatter> {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d"
        return formatter
    }

    var formattedDate: String? {
        guard let reviewDate, !reviewDate.isEmpty else { return nil }
        let fallbacks = (Self.sourceDateFormatters.value as? [DateFormatter]) ?? []
        let date = Self.isoFormatterWithFractionalSeconds.value.date(from: reviewDate)
            ?? Self.isoFormatter.value.date(from: reviewDate)
            ?? fallbacks.lazy.compactMap { $0.date(from: reviewDate) }.first
        guard let date else { return nil }
        return Self.displayDateFormatter.value.string(from: date)
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
    ///
    /// A Google review that just became "posted" did so through our own
    /// auto-post, which is precisely the thing that gives it a review_name
    /// — so it becomes retractable here rather than waiting for a refresh
    /// to learn that from the server.
    func withStatus(_ newStatus: String) -> Review {
        let retractable = (newStatus == "posted" && platform == "google") ? true : canRetractFlag
        return Review(
            id: id, platform: platform, author: author, rating: rating, text: text,
            reviewDate: reviewDate, sentiment: sentiment, urgency: urgency,
            draftResponse: draftResponse, responseStatus: newStatus, categories: categories,
            draftNeedsReview: draftNeedsReview, draftReviewReason: draftReviewReason,
            editedAt: editedAt, originalRating: originalRating,
            canRetractFlag: retractable, processed: processed,
            summary: summary, specificComplaint: specificComplaint,
            severity: severity, severityLabel: severityLabel, entities: entities
        )
    }
}
