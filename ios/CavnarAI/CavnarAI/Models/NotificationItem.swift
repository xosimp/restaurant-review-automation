import Foundation

/// Decodes one entry from GET /mobile/api/notifications (the alert_log
/// history — pull-based, distinct from the push notifications APNs
/// delivers; this is the "recent alerts" list).
struct NotificationItem: Codable, Identifiable {
    let type: String
    let label: String
    let firedAt: String
    let reviewId: Int?

    enum CodingKeys: String, CodingKey {
        case type, label
        case firedAt = "fired_at"
        case reviewId = "review_id"
    }

    var id: String { "\(type)-\(firedAt)" }

    // alert_log.fired_at is SQLite's `datetime('now')` — always UTC, always
    // "yyyy-MM-dd HH:mm:ss", no timezone suffix or offset.
    private static let firedAtFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter
    }()

    /// "2h ago" instead of the raw SQL timestamp string — falls back to the
    /// raw string on the off chance it doesn't parse, rather than showing
    /// nothing at all.
    var relativeFiredAt: String {
        guard let date = Self.firedAtFormatter.date(from: firedAt) else { return firedAt }
        return Self.relativeFormatter.localizedString(for: date, relativeTo: Date())
    }
}

/// Decodes one entry from GET /mobile/api/group-locations (owner-role
/// multi-location switcher).
struct LocationOption: Codable, Identifiable {
    let id: Int
    let name: String
    let active: Bool
}
