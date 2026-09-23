import Foundation

/// Decodes one entry from GET /mobile/api/notifications (the alert_log
/// history — pull-based, distinct from the push notifications APNs
/// delivers; this is the "recent alerts" list).
struct NotificationItem: Codable, Identifiable {
    let type: String
    let label: String
    let firedAt: String
    let reviewId: Int?
    /// The executive tier (push.PRIORITY). 0 is a health or safety mention;
    /// 5 is background. Optional so an older build talking to a newer
    /// backend — or the reverse — degrades to "informational" rather than
    /// failing to decode the whole list.
    let priority: Int?
    /// P0/P1: only worth anything while it can still be acted on.
    let urgent: Bool?
    let unread: Bool?
    /// Where the row opens — the server's module map (push.NOTIFICATION_
    /// MODULE), the same one the web bell uses. Optional for older servers.
    let module: String?

    enum CodingKeys: String, CodingKey {
        case type, label, priority, urgent, unread, module
        case firedAt = "fired_at"
        case reviewId = "review_id"
    }

    var isUrgent: Bool { urgent ?? ((priority ?? 3) <= 1) }
    var isUnread: Bool { unread ?? false }

    var id: String { "\(type)-\(firedAt)" }

    // alert_log.fired_at is SQLite's `datetime('now')` — always UTC, always
    // "yyyy-MM-dd HH:mm:ss", no timezone suffix or offset.
    // Thread-local, not shared singletons — Foundation formatters aren't
    // Sendable and relativeFiredAt is read from view bodies while decoding
    // may still be running off the main actor (audit 2.2).
    private static let firedAtFormatter = ThreadLocalFormatter<DateFormatter> {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }

    private static let relativeFormatter = ThreadLocalFormatter<RelativeDateTimeFormatter> {
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .short
        return formatter
    }

    /// "2h ago" instead of the raw SQL timestamp string — falls back to the
    /// raw string on the off chance it doesn't parse, rather than showing
    /// nothing at all.
    var relativeFiredAt: String {
        guard let date = Self.firedAtFormatter.value.date(from: firedAt) else { return firedAt }
        return Self.relativeFormatter.value.localizedString(for: date, relativeTo: Date())
    }

    var firedAtDate: Date? { Self.firedAtFormatter.value.date(from: firedAt) }

    /// "Today" / "Yesterday" / "Tuesday" / "12 Sep" — the heading this row
    /// groups under. A flat list of twenty rows reads as an audit log; the
    /// same rows under a day heading read as what happened when.
    var dayGroup: String {
        guard let date = firedAtDate else { return "Earlier" }
        let calendar = Calendar.current
        if calendar.isDateInToday(date) { return "Today" }
        if calendar.isDateInYesterday(date) { return "Yesterday" }
        let formatter = DateFormatter()
        if let days = calendar.dateComponents([.day], from: date, to: Date()).day, days < 7 {
            formatter.dateFormat = "EEEE"
        } else {
            formatter.dateFormat = "d MMM"
        }
        return formatter.string(from: date)
    }
}

/// Decodes one entry from GET /mobile/api/group-locations (owner-role
/// multi-location switcher).
struct LocationOption: Codable, Identifiable {
    let id: Int
    let name: String
    let active: Bool
}
