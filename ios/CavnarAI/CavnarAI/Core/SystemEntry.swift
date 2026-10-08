import Foundation
import SwiftUI
import UIKit

/// Where a way in from OUTSIDE the app wants to go: a Home Screen quick
/// action, an App Shortcut / Siri, a widget or Live Activity tap, a
/// `cavnarai://nav/…` link or a dashboard.cavnar.ai link (Friction audit
/// #31, #47; U3-12).
enum SystemDestination: Equatable {
    case nav(NavPath)
    /// A path from a LINK — a `cavnarai://` or dashboard.cavnar.ai URL,
    /// which any web page or message can carry. It may open a place; it may
    /// not act: an Ask question is filled in, never sent, and a
    /// `location/<id>` path is offered in the switcher, never switched to
    /// (F3-12). Its context is what the link said beyond the place.
    case link(NavPath, LinkContext = LinkContext())
    /// The command sheet (find or ask anything).
    case commandSheet
}

/// What a dashboard link carries beside the place (parity audit #1): the
/// location it is about (`loc=`, or a recommendation link's `rid=`) — the
/// app switches there first when this login has it, as the web's
/// checkTabParam does, and opens it where it is otherwise — and the
/// recommendation it names (`rec=` + `src=`), whose open is recorded the way
/// the web records it on page load (rec_delivery.record_link_open).
struct LinkContext: Equatable, Sendable {
    var location: Int? = nil
    var rec: String? = nil
    var src: String? = nil
    var rid: Int? = nil

    var isEmpty: Bool { location == nil && rec == nil }
}

/// The one door every system entry point walks through. It never routes
/// anything itself: a place is posted as `.cavnarOpenNav` (the deep-link
/// router owns the handling — same path as a push, a card or a notification
/// row), and the command sheet is asked for through `CommandSheetRequest`.
///
/// A destination that arrives while the app is not active (a cold launch
/// from a quick action, a Siri shortcut that opens the app) waits here until
/// the scene is active, so it reaches the router after the router exists and
/// after the lock screen has had its say — nothing here skips Face ID.
@MainActor
enum SystemEntry {
    private static var queued: [SystemDestination] = []
    private static var lastPosted: (raw: String, at: Date)?

    static func open(_ destination: SystemDestination) {
        if UIApplication.shared.applicationState == .active {
            deliver(destination)
        } else {
            queued.append(destination)
        }
    }

    static func open(_ path: NavPath) { open(.nav(path)) }

    /// Called when the scene becomes active.
    static func flush() {
        let pending = queued
        queued = []
        pending.forEach(deliver)
    }

    @discardableResult
    static func handle(url: URL) -> Bool {
        guard let destination = destination(for: url) else { return false }
        open(fromLink(destination, context: linkContext(for: url)))
        return true
    }

    /// A URL's destination as a link: nobody in the app chose it.
    nonisolated static func fromLink(_ destination: SystemDestination) -> SystemDestination {
        fromLink(destination, context: LinkContext())
    }

    nonisolated static func fromLink(_ destination: SystemDestination, context: LinkContext) -> SystemDestination {
        if case .nav(let path) = destination { return .link(path, context) }
        return destination
    }

    /// `userInfo` key on a `.cavnarOpenNav` post that came from a link.
    nonisolated static let fromLinkKey = "cavnar.fromLink"
    /// `userInfo` key carrying the link's `LinkContext`.
    nonisolated static let linkContextKey = "cavnar.linkContext"

    @discardableResult
    static func handle(shortcut item: UIApplicationShortcutItem) -> Bool {
        guard let action = QuickAction(rawValue: item.type) else { return false }
        open(action.destination)
        return true
    }

    private static func deliver(_ destination: SystemDestination) {
        switch destination {
        case .commandSheet:
            CommandSheetRequest.request()
        case .nav(let path):
            // A link can arrive twice for one tap (SwiftUI's onOpenURL and a
            // continued user activity); the second within a second is the
            // same tap, not a new request.
            if let last = lastPosted, last.raw == path.raw, Date().timeIntervalSince(last.at) < 1 { return }
            lastPosted = (path.raw, Date())
            NotificationCenter.default.post(name: .cavnarOpenNav, object: path)
        case .link(let path, let context):
            if let last = lastPosted, last.raw == path.raw, Date().timeIntervalSince(last.at) < 1 { return }
            lastPosted = (path.raw, Date())
            NotificationCenter.default.post(name: .cavnarOpenNav, object: path,
                                            userInfo: [fromLinkKey: true, linkContextKey: context])
        }
    }

    // MARK: - Links

    /// The hosts whose links open in the app. Anything else — including the
    /// `cavnarai://` auth callbacks, which ASWebAuthenticationSession
    /// consumes itself — is not ours to route.
    nonisolated static let webHosts: Set<String> = ["dashboard.cavnar.ai"]
    nonisolated static let dashboardPaths: Set<String> = ["", "/", "/dashboard", "/dashboard/"]

    /// `cavnarai://nav/review/412?x=1` → "review/412?x=1";
    /// `cavnarai://command` → the command sheet;
    /// `https://dashboard.cavnar.ai/?nav=labor/schedule` or
    /// `…/#labor/schedule` → that path; `?review=412` → review/412;
    /// `?ask=<question>` → Ask with it filled in; `?tab=<module>` (an older
    /// sender's) → that module. The links are the dashboard's own
    /// (notify.alert_url, rec_delivery.ask_url, morning_brief, reporter) and
    /// what hosted_dashboard's apple-app-site-association claims.
    /// Nil for anything else.
    nonisolated static func destination(for url: URL) -> SystemDestination? {
        let scheme = url.scheme?.lowercased()
        if scheme == "cavnarai" {
            switch url.host?.lowercased() {
            case "command":
                return .commandSheet
            case "nav":
                var raw = url.path.hasPrefix("/") ? String(url.path.dropFirst()) : url.path
                if let q = url.query, !q.isEmpty { raw += "?" + q }
                return NavPath(raw).map { .nav($0) }
            default:
                return nil
            }
        }
        guard scheme == "https", let host = url.host?.lowercased(), webHosts.contains(host) else { return nil }
        // The dashboard only (`/`, and `/dashboard` from the F3-16 links):
        // the sign-in, billing, admin and public staff/guest pages are the
        // web's, as the apple-app-site-association says.
        guard dashboardPaths.contains(url.path.lowercased()) else { return nil }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if let nav = items.first(where: { $0.name == "nav" })?.value, let path = NavPath(nav) {
            return .nav(path)
        }
        // An email's "Ask about this →" (`/?ask=<question>`, morning_brief /
        // rec_delivery): Ask with the question filled in — a link's question
        // is never sent on its own (openFromLink), the owner sends it.
        if let ask = items.first(where: { $0.name == "ask" })?.value,
           !ask.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
           let path = askPath(String(ask.prefix(300))) {
            return .nav(path)
        }
        if let review = items.first(where: { $0.name == "review" })?.value, let id = Int(review), id > 0 {
            return NavPath("review/\(id)").map { .nav($0) }
        }
        // A question from an email's "Ask about this" (rec_delivery.ask_url):
        // Ask, with it filled in — a link never sends it (F3-12).
        if let ask = items.first(where: { $0.name == "ask" })?.value,
           !ask.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, let path = askPath(ask) {
            return .nav(path)
        }
        if let fragment = url.fragment, let path = NavPath(fragment) {
            return .nav(path)
        }
        if let tab = items.first(where: { $0.name == "tab" })?.value, let path = NavPath(tabPath(tab)) {
            return .nav(path)
        }
        // A bare dashboard link: the app, on whatever it opens on.
        return NavPath("home").map { .nav($0) }
    }

    /// A web tab id (`?tab=`, dashboard.html's `tab-<id>` buttons) as the
    /// app's module head: the web calls Intel "competitor" and Food Cost
    /// "inventory" or "food".
    nonisolated static func tabPath(_ tab: String) -> String {
        let t = tab.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch t {
        case "competitor", "competitors": return "intel"
        case "food", "foodcost", "food_cost": return "inventory"
        case "": return "home"
        default: return t
        }
    }

    /// Everything a dashboard link says beside its place (`loc=`, `rid=`,
    /// `rec=`, `src=`). Empty for a widget or `cavnarai://` link.
    nonisolated static func linkContext(for url: URL) -> LinkContext {
        guard url.scheme?.lowercased() == "https" else { return LinkContext() }
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        func value(_ name: String) -> String? {
            let v = items.first(where: { $0.name == name })?.value?.trimmingCharacters(in: .whitespaces)
            return (v?.isEmpty == false) ? v : nil
        }
        func id(_ name: String) -> Int? { value(name).flatMap(Int.init).flatMap { $0 > 0 ? $0 : nil } }
        let rid = id("rid")
        return LinkContext(location: id("loc") ?? rid,
                           rec: value("rec").map { String($0.prefix(160)) },
                           src: value("src").map { String($0.prefix(32)) },
                           rid: rid)
    }

    /// "ask" or "ask?q=<the question>", percent-encoded so a question with
    /// "&" or "=" in it survives NavPath's query parsing whole.
    nonisolated static func askPath(_ question: String?) -> NavPath? {
        let text = (question ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return NavPath("ask") }
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        let encoded = text.addingPercentEncoding(withAllowedCharacters: allowed) ?? ""
        return NavPath("ask?q=" + encoded)
    }

    /// The widget / Live Activity deep link for a nav path.
    nonisolated static func url(for path: String) -> URL? {
        URL(string: "cavnarai://nav/" + path)
    }
}

// MARK: - Home Screen quick actions

/// Long-press the app icon (U3-12 a). Three are static (Info.plist, so they
/// exist before the first launch); "Approve replies" is added at runtime,
/// only while replies are waiting, with the count as its subtitle.
enum QuickAction: String, CaseIterable {
    case ask = "ai.cavnar.quick.ask"
    case approveReplies = "ai.cavnar.quick.approve-replies"
    case lastNight = "ai.cavnar.quick.last-night"
    case scanInvoice = "ai.cavnar.quick.scan-invoice"

    var destination: SystemDestination {
        switch self {
        case .ask: return .nav(NavPath("ask")!)
        case .approveReplies: return .nav(NavPath("reviews?filter=pending")!)
        case .lastNight: return .nav(NavPath("dsr")!)
        case .scanInvoice: return .nav(NavPath("inventory/invoices?scan=camera")!)
        }
    }

    /// The runtime item for replies waiting — nil when none are.
    @MainActor
    static func approveRepliesItem(waiting: Int) -> UIApplicationShortcutItem? {
        guard waiting > 0 else { return nil }
        return UIApplicationShortcutItem(
            type: QuickAction.approveReplies.rawValue,
            localizedTitle: "Approve replies",
            localizedSubtitle: waiting == 1 ? "1 review waiting on a reply" : "\(waiting) reviews waiting on a reply",
            icon: UIApplicationShortcutIcon(systemImageName: "checkmark.bubble"),
            userInfo: nil)
    }
}

// MARK: - Scene hooks

/// Quick actions reach a SwiftUI app only through a scene delegate: the
/// cold-launch item arrives in the connection options, a warm one in
/// `performActionFor`. URLs come to both this and SwiftUI's `.onOpenURL`.
@MainActor
final class CavnarSceneDelegate: NSObject, UIWindowSceneDelegate {
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        if let item = connectionOptions.shortcutItem {
            SystemEntry.handle(shortcut: item)
        }
        // A widget tap or link that LAUNCHED the app arrives here, in the
        // connection options. With this class as the scene delegate,
        // SwiftUI's .onOpenURL was never shown to fire for that cold launch
        // (F3-18), so the delegate routes it too. A second delivery of the
        // same link within a second is dropped by SystemEntry.
        for context in connectionOptions.urlContexts {
            SystemEntry.handle(url: context.url)
        }
        for activity in connectionOptions.userActivities
        where activity.activityType == NSUserActivityTypeBrowsingWeb {
            if let url = activity.webpageURL { SystemEntry.handle(url: url) }
        }
    }

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for context in URLContexts { SystemEntry.handle(url: context.url) }
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        guard userActivity.activityType == NSUserActivityTypeBrowsingWeb, let url = userActivity.webpageURL else { return }
        SystemEntry.handle(url: url)
    }

    func windowScene(_ windowScene: UIWindowScene, performActionFor shortcutItem: UIApplicationShortcutItem,
                     completionHandler: @escaping (Bool) -> Void) {
        completionHandler(SystemEntry.handle(shortcut: shortcutItem))
    }

    func sceneDidBecomeActive(_ scene: UIScene) {
        SystemEntry.flush()
        Task { await WidgetSnapshotService.shared.refresh() }
    }
}

extension AppDelegate {
    @objc func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: nil, sessionRole: connectingSceneSession.role)
        config.delegateClass = CavnarSceneDelegate.self
        return config
    }
}
