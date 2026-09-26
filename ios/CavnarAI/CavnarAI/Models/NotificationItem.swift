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
    /// Read once opened, on either client (the row's alert_log id in
    /// notification_opens) — flipped here the moment a row is opened.
    var unread: Bool?
    /// Where the row opens — the server's module map (push.NOTIFICATION_
    /// MODULE), the same one the web bell uses. Optional for older servers.
    let module: String?
    /// Where the row opens (nav.py) — the item or section, not the module's
    /// top — and which location it is about. Both optional: an older server
    /// sends neither and the router falls back to `module`.
    var nav: String? = nil
    var restaurantId: Int? = nil
    /// The alert_log row's own id — two alerts of one type in the same
    /// second (two 1-star reviews from one fetch) are two rows (F3-10).
    var alertId: Int? = nil
    /// The server's own rule for approving in place (a drafted, unflagged
    /// reply at this location), and the draft itself — shown in full on the
    /// row before Approve can publish it.
    var canApprove: Bool? = nil
    var draft: String? = nil
    /// The review's own words, and whether it has been answered since.
    var snippet: String? = nil
    var resolved: Bool? = nil
    /// The queued send a "going out" row is about (delayed_actions.id), when
    /// the server names it — Undo then stops exactly that send.
    var delayedActionId: Int? = nil
    /// The server's rule for Undo on this row: still pending, at the
    /// location the session is on, and a login the cancel route allows.
    /// `false` hides Undo whatever the pending list says; nil (an older
    /// server) falls back to the client's own match.
    var canUndo: Bool? = nil
    /// An urgent row whose subject the server cannot read: opening it is
    /// handling it, so `resolved` follows the open (as the server counts).
    var resolvesOnOpen: Bool? = nil

    enum CodingKeys: String, CodingKey {
        case type, label, priority, urgent, unread, module, nav, draft, snippet, resolved
        case firedAt = "fired_at"
        case reviewId = "review_id"
        case restaurantId = "restaurant_id"
        case alertId = "id"
        case canApprove = "can_approve"
        case delayedActionId = "delayed_action_id"
        case canUndo = "can_undo"
        case resolvesOnOpen = "resolves_on_open"
    }

    /// The delayed.py kind a "going out" row is about — the row that can
    /// be undone in place (friction audit #22).
    var undoableKind: String? {
        switch type {
        case "schedule_publish_pending": return "schedule_publish"
        case "order_send_pending": return "order_send"
        default: return nil
        }
    }

    var isUrgent: Bool { urgent ?? ((priority ?? 3) <= 1) }
    /// Urgent and not yet handled — what "Needs you", the ember dot and the
    /// bell's red count mean (the web bell's `urgent && !resolved`).
    var needsYou: Bool { isUrgent && resolved != true }
    var isUnread: Bool { unread ?? false }

    var id: String { alertId.map { "alert-\($0)" } ?? "\(type)-\(firedAt)" }

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
        if let days = calendar.dateComponents([.day], from: date, to: Date()).day, days < 7 {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US")
            formatter.dateFormat = "EEEE"
            return formatter.string(from: date)
        }
        // M/D/YY, the one owner-facing date form — "12 Sep" wasn't.
        return CavnarDate.mdy(date)
    }
}

/// Decodes one entry from GET /mobile/api/group-locations (owner-role
/// multi-location switcher).
struct LocationOption: Codable, Identifiable {
    let id: Int
    let name: String
    let active: Bool
}
