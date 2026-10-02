import Foundation
import Observation

/// The staff app's tabs as the server names them (push.STAFF_TABS):
/// today · tasks · requests · me · inbox. "messages" is read as inbox (the
/// server maps it the same way), and the portal's older names — schedule,
/// profile — are read too, so a link never strands.
enum StaffTab: String, CaseIterable, Sendable {
    case today, tasks, requests, me, inbox

    init?(server raw: String?) {
        switch (raw ?? "").trimmingCharacters(in: .whitespaces).lowercased() {
        case "today", "schedule": self = .today
        case "tasks": self = .tasks
        case "requests": self = .requests
        case "me", "profile": self = .me
        case "inbox", "messages", "message": self = .inbox
        default: return nil
        }
    }
}

/// Where a staff push (or any staff link) opens: a tab, and the item on it.
///
/// Built from the payload push.py sends every staff notice (fix_B2):
/// `cavnar.nav` = "staff/<tab>[/<id>]", `cavnar.tab`, and the id under its
/// own key (`request_id`, `announcement_id`, `thread_id`, `assignment_id`).
/// `nav` wins when present; then `tab`; then the type's default tab
/// (push.STAFF_DEFAULT_TAB). The portal reads `tab` and `itemID` (and
/// `kind` / `event` / `requestKind` when it wants to say more).
struct StaffDeepLink: Equatable, Sendable {
    let tab: StaffTab
    /// The request, announcement, message thread or task assignment.
    let itemID: Int?
    let alertType: String
    /// What the notice is about: schedule, request, notice, reminder,
    /// announcement, urgent, message.
    let kind: String?
    /// The finer event: schedule_posted, swap_asked, shift_starting, task_due…
    let event: String?
    /// For a request notice: "time_off", "drop", "swap", …
    let requestKind: String?

    /// push.STAFF_ALERT_TYPES. Not every "staff_…" type: "staff_signin",
    /// "staff_claim" and "staff_account_deleted" are OWNER notices about
    /// staff, and they open the owner app.
    static let staffAlertTypes: Set<String> = [
        "staff_announcement", "staff_message", "staff_notice", "staff_reminder",
        "staff_request", "staff_schedule", "staff_urgent",
    ]

    static func isStaffNotice(alertType: String, module: String?) -> Bool {
        staffAlertTypes.contains(alertType) || module?.lowercased() == "staff"
    }

    /// push.STAFF_DEFAULT_TAB.
    static func defaultTab(for alertType: String) -> StaffTab {
        switch alertType {
        case "staff_request": return .requests
        case "staff_notice", "staff_announcement", "staff_urgent", "staff_message": return .inbox
        default: return .today
        }
    }

    private static let idKeys = ["request_id", "announcement_id", "thread_id", "assignment_id"]

    /// From a push payload's `cavnar` dictionary. Nil only when nothing in it
    /// names a staff place and the type is not a staff type.
    static func from(cavnar: [String: Any], alertType: String) -> StaffDeepLink? {
        let fromNav = parse(nav: cavnar["nav"] as? String)
        let tab = fromNav?.tab
            ?? StaffTab(server: cavnar["tab"] as? String)
            ?? (staffAlertTypes.contains(alertType) || (cavnar["module"] as? String) == "staff"
                ? defaultTab(for: alertType) : nil)
        guard let tab else { return nil }
        let itemID = fromNav?.id ?? idKeys.lazy.compactMap { positiveInt(cavnar[$0]) }.first
        return StaffDeepLink(tab: tab, itemID: itemID, alertType: alertType,
                             kind: bounded(cavnar["kind"]), event: bounded(cavnar["event"]),
                             requestKind: bounded(cavnar["request_kind"]))
    }

    /// From a nav path alone ("staff/requests/41"), e.g. a row in a list.
    static func from(nav: String?, alertType: String = "") -> StaffDeepLink? {
        guard let parsed = parse(nav: nav) else { return nil }
        return StaffDeepLink(tab: parsed.tab, itemID: parsed.id, alertType: alertType,
                             kind: nil, event: nil, requestKind: nil)
    }

    /// "staff/<tab>[/<id>]" → its tab and id. Anything else → nil.
    static func parse(nav: String?) -> (tab: StaffTab, id: Int?)? {
        guard let raw = nav?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return nil }
        let path = raw.split(separator: "?", maxSplits: 1).first.map(String.init) ?? raw
        let parts = path.split(separator: "/").map(String.init)
        guard parts.count >= 2, parts[0].lowercased() == "staff", let tab = StaffTab(server: parts[1]) else {
            return nil
        }
        return (tab, parts.count > 2 ? positiveInt(parts[2]) : nil)
    }

    private static func positiveInt(_ raw: Any?) -> Int? {
        let value: Int?
        switch raw {
        case let n as Int: value = n
        case let s as String: value = Int(s.trimmingCharacters(in: .whitespaces))
        case let n as NSNumber: value = n.intValue
        default: value = nil
        }
        return value.flatMap { $0 > 0 ? $0 : nil }
    }

    private static func bounded(_ raw: Any?) -> String? {
        guard let s = (raw as? String)?.trimmingCharacters(in: .whitespaces), !s.isEmpty else { return nil }
        return String(s.prefix(64))
    }
}

/// The one place a staff link waits for the portal.
///
/// PushManager posts here on a staff notice tap; the portal (StaffPortalView)
/// consumes it:
///
/// ```swift
/// .onAppear { if let link = StaffDeepLinkCenter.shared.consume() { open(link) } }
/// .onReceive(NotificationCenter.default.publisher(for: .cavnarStaffDeepLink)) { _ in
///     if let link = StaffDeepLinkCenter.shared.consume() { open(link) }
/// }
/// ```
///
/// A link that arrives while the phone is on the PIN pad (signed out, or
/// the idle lock) stays pending and opens once the portal appears.
@Observable
@MainActor
final class StaffDeepLinkCenter {
    static let shared = StaffDeepLinkCenter()

    private(set) var pending: StaffDeepLink?

    func post(_ link: StaffDeepLink) {
        pending = link
        NotificationCenter.default.post(name: .cavnarStaffDeepLink, object: nil)
    }

    /// The pending link, handed out once.
    func consume() -> StaffDeepLink? {
        defer { pending = nil }
        return pending
    }
}

extension Notification.Name {
    /// Posted when a staff link is waiting in StaffDeepLinkCenter.
    static let cavnarStaffDeepLink = Notification.Name("cavnarStaffDeepLink")
}
