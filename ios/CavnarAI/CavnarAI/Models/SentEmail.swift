import Foundation

/// One row of email_log for this restaurant.
///
/// `status` is the real outcome, not an assumption: until the delivery
/// rework, email_log had a status column nothing could write, so every row
/// claimed "sent" whether or not it reached anyone.
struct SentEmail: Decodable, Identifiable, Hashable {
    let label: String
    let toEmail: String
    let subject: String?
    let sentAt: String
    let status: String
    let failed: Bool

    var id: String { "\(sentAt)-\(toEmail)-\(label)" }

    enum CodingKeys: String, CodingKey {
        case label, subject, status, failed
        case toEmail = "to_email"
        case sentAt = "sent_at"
    }
}
