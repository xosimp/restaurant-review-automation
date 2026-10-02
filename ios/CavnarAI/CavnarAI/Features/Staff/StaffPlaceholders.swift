import Foundation
import Observation
import SwiftUI

// TEMPORARY — DELETE THIS WHOLE FILE AT MERGE (employee audit wave 2, I2).
//
// The tab container (StaffPortalView) names types the other iOS agents own
// and build in parallel. So this branch compiles on its own, they are
// stood in for here:
//   - I1's Push/StaffDeepLink.swift: StaffTab, StaffDeepLink,
//     StaffDeepLinkCenter, Notification.Name.cavnarStaffDeepLink — copied
//     from I1's in-progress file, the API the container calls.
//   - I3's StaffMeView(store:), StaffInboxView(store:) and
//     StaffMessageThreadView(store:shiftDate:) — the names the container
//     presents; each is a minimal stand-in.
// Once the real files merge, this file must go, or the names collide.

// MARK: - I1: Push/StaffDeepLink.swift (copy)

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

struct StaffDeepLink: Equatable, Sendable {
    let tab: StaffTab
    let itemID: Int?
    let alertType: String
    let kind: String?
    let event: String?
    let requestKind: String?

    static func from(nav: String?, alertType: String = "") -> StaffDeepLink? {
        guard let parsed = parse(nav: nav) else { return nil }
        return StaffDeepLink(tab: parsed.tab, itemID: parsed.id, alertType: alertType,
                             kind: nil, event: nil, requestKind: nil)
    }

    static func parse(nav: String?) -> (tab: StaffTab, id: Int?)? {
        guard let raw = nav?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else { return nil }
        let path = raw.split(separator: "?", maxSplits: 1).first.map(String.init) ?? raw
        let parts = path.split(separator: "/").map(String.init)
        guard parts.count >= 2, parts[0].lowercased() == "staff", let tab = StaffTab(server: parts[1]) else {
            return nil
        }
        return (tab, parts.count > 2 ? Int(parts[2]).flatMap { $0 > 0 ? $0 : nil } : nil)
    }
}

@Observable
@MainActor
final class StaffDeepLinkCenter {
    static let shared = StaffDeepLinkCenter()

    private(set) var pending: StaffDeepLink?

    func post(_ link: StaffDeepLink) {
        pending = link
        NotificationCenter.default.post(name: .cavnarStaffDeepLink, object: nil)
    }

    func consume() -> StaffDeepLink? {
        defer { pending = nil }
        return pending
    }
}

extension Notification.Name {
    static let cavnarStaffDeepLink = Notification.Name("cavnarStaffDeepLink")
}

// MARK: - I3: the Me tab (stand-in)

/// Stand-in for I3's StaffMeView(store:): the old Profile and Requests
/// settings, so nothing an employee could do before is lost on this
/// branch. A full-tab view: it brings its own scroll.
struct StaffMeView: View {
    let store: StaffPortalStore
    @Environment(StaffSessionStore.self) private var staff

    var body: some View {
        StaffTabScroll(refresh: { await store.reloadProfile() }) {
            StaffScreenTitle(title: "Me")
            if let profile = store.profile.value {
                HomeMixedText.make("\(profile.name) \u{00B7} \(profile.restaurant)", size: CavnarType.body,
                                   color: .cavnarInk2)
            }
            StaffAvailabilitySection()
            StaffPreferencesSection()
            StaffChangePinSection()
            Button("Sign out", role: .destructive) {
                // An explicit sign-out leaves no copy of this person's week
                // on a shared phone (StaffCache).
                StaffCache.purgeAll()
                staff.signOut()
            }
                .font(.cavnarBody(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarRed)
                .frame(minHeight: 44)
        }
    }
}

// MARK: - I3: the Inbox and the manager thread (stand-ins)

struct StaffInboxView: View {
    let store: StaffPortalStore

    var body: some View {
        NavigationStack {
            CavnarEmptyHearth(title: "Inbox", message: "Announcements and your manager\u{2019}s replies show here.")
                .accountSheetChrome("Inbox")
        }
    }
}

struct StaffMessageThreadView: View {
    let store: StaffPortalStore
    let shiftDate: String?

    var body: some View {
        NavigationStack {
            CavnarEmptyHearth(title: "Message your manager",
                              message: shiftDate.map { "About your shift on \(CavnarDate.mdy($0))." })
                .accountSheetChrome("Message")
        }
    }
}
