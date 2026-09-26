import Foundation
import Observation

/// Routes a tapped push notification, OR a tapped row in the in-app
/// Notifications history sheet (same underlying alert_log data, see
/// NotificationsListView), to the right screen. notify.py's alert-firing
/// code sends the same unprefixed alert_type strings it logs internally
/// ("1star", "neg_spike", "no_response", ...) plus a review_id when there
/// is one — see push.py's fire_push() call sites in notify.py.
@Observable
@MainActor
final class DeepLinkRouter {
    var pendingTab: AppTab?
    var pendingModuleKey: String?
    /// The module screen to open with its focus — the filter, section or
    /// item the notification or card was about (friction audit #3). Set with
    /// pendingModuleKey (same key) so anything reading the key alone still
    /// lands in the right module.
    var pendingModuleRoute: ModuleRoute?
    var pendingReviewID: Int?
    /// A question for Ask Cavnar. Set by the morning brief push, whose lines
    /// each carry the question an owner would ask about them, and by every
    /// "Ask about this".
    var pendingAskPrompt: String?
    /// Whether pendingAskPrompt is sent as well as filled in. An explicit
    /// "Ask about this" (a Home link, the push's own Ask button, the command
    /// sheet) was the decision, so it sends — as the web's hbAsk does
    /// (friction audit #15). A plain tap on a brief's body only fills it in.
    var pendingAskAutoSend = false
    /// The screen an "Ask about this" came from, sent with pendingAskPrompt.
    var pendingAskScreen: AskScreen?
    /// A past Ask chat to reopen — `ask?conversation=<id>`, a command-sheet
    /// hit for an old conversation (F3-15). RootView consumes it.
    var pendingAskConversation: Int?
    /// The Account sheet a path named — `account/<section>` (nav.py's
    /// sections: security, notifications, integrations, people, …) or
    /// "recs". AccountView consumes it (F3-15).
    var pendingAccountSection: String?
    /// A queued automatic send (delayed_actions id) to show with Undo /
    /// Review — the "goes out at 11am" push and its notification row
    /// (friction audit #3). RootView presents PendingActionSheet for it.
    var pendingActionId: PendingActionRef?
    /// A stored Ask proposal to show again with its confirm card
    /// (`proposal/<id>`, or `action/<id>?kind=proposal`). A different id
    /// space from `pendingActionId`: an Ask proposal opened as a queued send
    /// read "already went out", or showed an unrelated send with the same
    /// number (F3-2). RootView presents ProposalReopenSheet for it.
    var pendingProposalId: PendingActionRef?
    /// A link asked to change location: RootView opens the switcher on it
    /// rather than switching (F3-12). Consumed by RootView.
    var pendingLocationPicker = false
    /// A Daily Sales Report to open on Home's stack — set by a `dsr` push
    /// (`business_date` → that night) or its row in the notification list
    /// (no date there → the list of nights). HomeView consumes it.
    var pendingDailyReport: DailyReportRoute?
    /// Bumped each time the active location changed — by RootView, from
    /// SessionStore.onLocationSwitched, whichever screen switched — so Home
    /// reloads for the location it now shows.
    var locationSwitches = 0
    /// Switches the session to another location of the group; set by
    /// RootView (it owns the SessionStore). Returns true on success; on
    /// failure it may set `locationSwitchFailure` to the server's reason.
    var switchLocation: (@MainActor (Int) async -> Bool)?
    /// The location the session is on now; set by RootView. The persisted
    /// one until /me answers: on a cold launch from a push tap this process
    /// has no session scope of its own yet, and a 0 here skipped the switch
    /// and opened location A's alert inside location B.
    var activeRestaurantId: () -> Int = { SessionScope.activeRestaurantId }
    /// Why a notification about another location was not opened: its
    /// location switch failed. Routing on regardless opened the alert inside
    /// the wrong location ("not found", or another store's review). RootView
    /// shows it and clears it.
    var locationSwitchFailure: String?
    static let switchFailedMessage = "Couldn't switch to the location this is about, so it wasn't opened. "
        + "Try again, or switch locations from the header first."

    /// `module` is the server's own routing (push.NOTIFICATION_MODULE, sent
    /// in every payload and every history row); `restaurantId` is the
    /// location the notification is about. A group owner's phone hears
    /// every location's alerts, so a tap on location A's alert while the
    /// app is on B switches to A first — otherwise A's review is "not
    /// found" inside B and the open is recorded against B (re-audit A-14).
    func handleNotificationTap(alertType: String, reviewId: Int?, askPrompt: String? = nil,
                               alertId: Int? = nil, recKey: String? = nil,
                               module: String? = nil, restaurantId: Int? = nil,
                               businessDate: String? = nil, surface: String? = nil,
                               nav: String? = nil, askAutoSend: Bool = false) {
        let current = activeRestaurantId()
        if let target = restaurantId, target > 0, current > 0, target != current, let switchLocation {
            Task {
                guard await switchedOrExplained(switchLocation, to: target) else { return }
                route(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt,
                      alertId: alertId, recKey: recKey, module: module, businessDate: businessDate,
                      surface: surface, nav: nav, askAutoSend: askAutoSend)
            }
            return
        }
        route(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt,
              alertId: alertId, recKey: recKey, module: module, businessDate: businessDate,
              surface: surface, nav: nav, askAutoSend: askAutoSend)
    }

    /// Open a nav path (nav.py / Core/NavPath.swift) — a card's, a
    /// notification's, the command sheet's (`.cavnarOpenNav`). One router
    /// for every way in. `restaurantId` switches location first when the
    /// path belongs to another one. `askAutoSend` decides whether an
    /// `ask?q=` path sends or only fills in.
    func open(_ nav: NavPath, restaurantId: Int? = nil, askAutoSend: Bool = true, askPrompt: String? = nil) {
        let current = activeRestaurantId()
        if let target = restaurantId, target > 0, current > 0, target != current, let switchLocation {
            Task {
                guard await switchedOrExplained(switchLocation, to: target) else { return }
                apply(nav, askAutoSend: askAutoSend, askPrompt: askPrompt)
            }
            return
        }
        apply(nav, askAutoSend: askAutoSend, askPrompt: askPrompt)
    }

    /// Runs the switch; on failure nothing is routed and the owner is told
    /// why (the switcher's own reason when it gave one).
    private func switchedOrExplained(_ switchLocation: @MainActor (Int) async -> Bool, to target: Int) async -> Bool {
        locationSwitchFailure = nil
        if await switchLocation(target) { return true }
        if locationSwitchFailure == nil { locationSwitchFailure = Self.switchFailedMessage }
        return false
    }

    /// A path from a link anyone could have written (SystemEntry `.link`):
    /// it opens a place and nothing more. An Ask question is filled in for
    /// the owner to send; a location is offered in the switcher, not
    /// switched to (F3-12).
    func openFromLink(_ nav: NavPath) {
        if nav.head == "location" {
            pendingLocationPicker = true
            return
        }
        open(nav, askAutoSend: false)
    }

    /// The destination of a nav path. Unknown heads degrade to Home, never
    /// to a wrong module (nav.py's contract: a newer server never strands
    /// an older app).
    private func apply(_ nav: NavPath, askAutoSend: Bool, askPrompt: String?) {
        pendingReviewID = nil
        switch nav.head {
        case "home", "issue":
            pendingTab = .home
            pendingModuleKey = nil
            pendingModuleRoute = nil
        case "action", "proposal":
            // The queued send's own sheet — or the Ask proposal's — over
            // whatever is on screen.
            let isProposal = nav.head == "proposal" || nav.query["kind"] == "proposal"
            if let id = nav.target.flatMap({ Int($0) }), id > 0 {
                if isProposal {
                    pendingProposalId = PendingActionRef(id: id)
                } else {
                    pendingActionId = PendingActionRef(id: id)
                }
            } else {
                pendingTab = .home
            }
        case "location":
            // location/<id>: switch there, then Home shows it.
            pendingModuleKey = nil
            pendingModuleRoute = nil
            if let id = nav.target.flatMap({ Int($0) }), id > 0, id != activeRestaurantId(),
               let switchLocation {
                Task {
                    _ = await switchLocation(id)
                    pendingTab = .home
                }
            } else {
                pendingTab = .home
            }
        case "dsr":
            pendingTab = .home
            pendingModuleKey = nil
            pendingModuleRoute = nil
            pendingDailyReport = Self.dailyReportRoute(nav)
        case "ask":
            pendingTab = .ask
            pendingModuleKey = nil
            pendingModuleRoute = nil
            if let chat = nav.query["conversation"].flatMap({ Int($0) }), chat > 0 {
                pendingAskConversation = chat
                return
            }
            let fromPath = nav.query["q"].map { String($0.prefix(300)) }
            let question = (fromPath?.isEmpty == false ? fromPath : nil) ?? askPrompt
            if let question, !question.isEmpty {
                pendingAskAutoSend = askAutoSend
                pendingAskPrompt = question
            }
        case "account", "recs":
            pendingTab = .account
            pendingModuleKey = nil
            pendingModuleRoute = nil
            pendingAccountSection = nav.head == "recs" ? "recs" : nav.target?.lowercased()
        default:
            guard let target = ModuleRoute.from(nav) else {
                pendingTab = .home
                pendingModuleKey = nil
                pendingModuleRoute = nil
                return
            }
            if target.key == "reviews", let item = target.itemId, let id = Int(item), id > 0 {
                pendingReviewID = id
            }
            pendingModuleRoute = target
            pendingModuleKey = target.key
            pendingTab = .modules
        }
    }

    /// What the Modules tab opens next: the focused route when one was set,
    /// else a bare module key (an older caller). Consumed — both cleared —
    /// so a later reappearance can't push it twice.
    func consumePendingModuleRoute(labelFor: (String) -> String) -> ModuleRoute? {
        defer {
            pendingModuleRoute = nil
            pendingModuleKey = nil
        }
        if let route = pendingModuleRoute {
            return ModuleRoute(key: route.key, label: labelFor(route.key), filter: route.filter,
                               section: route.section, itemId: route.itemId, nav: route.nav)
        }
        guard let key = pendingModuleKey else { return nil }
        return ModuleRoute(key: key, label: labelFor(key))
    }

    private func route(alertType: String, reviewId: Int?, askPrompt: String?,
                       alertId: Int?, recKey: String?, module: String?, businessDate: String?,
                       surface: String?, nav: String? = nil, askAutoSend: Bool = false) {
        // What the product knew was how many notifications it SENT. Whether
        // any of them were worth sending had no answer anywhere — not for
        // the owner, not for Will. Best effort: a failure here must never
        // interfere with actually opening the thing.
        if !alertType.isEmpty {
            Task { await Self.recordOpen(alertType, alertId: alertId, recKey: recKey, surface: surface) }
        }
        // The server's own address for it (push.nav_for / the notification
        // row's `nav`): the review, the pending send, the request — not the
        // module's top. The mirror below is only for an older server.
        if let path = NavPath(nav) {
            apply(path, askAutoSend: askAutoSend, askPrompt: askPrompt)
            return
        }
        // The nightly Daily Sales Report opens on Home's stack: that night
        // when the push names it, the list of nights when it doesn't (a row
        // in the notification history carries no date). Checked before the
        // module map so it can't fall through to Reviews, whatever
        // `module` an older server sends for it.
        if Self.isDailyReport(alertType: alertType, module: module) {
            pendingTab = .home
            pendingModuleKey = nil
            pendingReviewID = nil
            pendingDailyReport = DSRFormat.isISODate(businessDate) ? .report(date: businessDate) : .list
            return
        }
        // Both of these are cross-module reads that arrive WITH a question
        // (data.ask_prompt), so they open the assistant on it rather than
        // guessing a module to drop the owner into.
        // Any push that carries a question — the monthly review's "August
        // is in" among them — is the same shape as a brief.
        if alertType == "morning_brief" || alertType == "intraday_pulse"
            || alertType == "monthly_review" || (askPrompt?.isEmpty == false) {
            pendingTab = .ask
            pendingModuleKey = nil
            pendingReviewID = nil
            if let askPrompt, !askPrompt.isEmpty {
                pendingAskAutoSend = askAutoSend
                pendingAskPrompt = askPrompt
            }
            return
        }
        // "login" isn't a product module — it has nowhere to deep-link to
        // inside Modules, so this switches to Account (where Security,
        // and the sign-in notifications setting that fired it, live)
        // instead of falling through to moduleKey's Reviews default,
        // which would have actively mis-routed a sign-in alert.
        guard alertType != "login" else {
            pendingTab = .account
            pendingModuleKey = nil
            pendingReviewID = nil
            return
        }
        let webModule = (module?.isEmpty == false ? module : nil) ?? Self.webModule(for: alertType)
        switch webModule {
        case "ask":
            pendingTab = .ask
            pendingModuleKey = nil
            pendingReviewID = nil
            return
        case "account":
            // The web lists issues under Account; the app lists them on Home.
            pendingTab = (alertType == "issue" || alertType == "issue_escalated") ? .home : .account
            pendingModuleKey = nil
            pendingReviewID = nil
            return
        default:
            break
        }
        // Reviews now lives inside the Modules tab (no per-module tabs
        // anymore), so switch there and let ModulesGridView push into the
        // right module screen itself once pendingModuleKey is set.
        pendingTab = .modules
        pendingModuleKey = Self.appModuleKey(forWebModule: webModule)
        pendingReviewID = reviewId
    }

    /// `alert_id` is the notification's own history row and `rec_key` the
    /// recommendation it carried — both from the push payload, both optional
    /// (a tap from the in-app history list has neither).
    /// `surface` is the push payload's own (`alert_push` / `brief_push`),
    /// forwarded as sent; omitted when the payload had none (a tap from the
    /// in-app history list), and the server falls back to the type.
    struct OpenedBody: Encodable, Equatable {
        let type: String
        let alert_id: Int?
        let rec_key: String?
        var surface: String? = nil
    }

    private static func recordOpen(_ alertType: String, alertId: Int? = nil, recKey: String? = nil,
                                   surface: String? = nil) async {
        let _: APIClient.EmptyResponse? = try? await APIClient.shared.send(
            "/mobile/api/notifications/opened", method: .post,
            body: OpenedBody(type: alertType, alert_id: alertId, rec_key: recKey, surface: surface),
            hapticOnError: false)
    }

    /// The tab a route asked for, handed out once. RootView switches on it
    /// and this clears it, so the NEXT route to the same tab is a change
    /// again — onChange never fired for a second "go to Modules" while the
    /// first one's .modules was still sitting here (F3-1).
    func consumePendingTab() -> AppTab? {
        defer { pendingTab = nil }
        return pendingTab
    }

    func consumePendingAccountSection() -> String? {
        defer { pendingAccountSection = nil }
        return pendingAccountSection
    }

    func consumePendingAskConversation() -> Int? {
        defer { pendingAskConversation = nil }
        return pendingAskConversation
    }

    func consumePendingReviewID() -> Int? {
        defer { pendingReviewID = nil }
        return pendingReviewID
    }

    /// The nightly report's push type is `dsr`; a server that maps it in
    /// push.NOTIFICATION_MODULE may send `module: "dsr"` as well.
    static func isDailyReport(alertType: String, module: String?) -> Bool {
        alertType == "dsr" || module == "dsr"
    }

    /// The web's reading of a dsr path (dashboard.html cavNavRegister('dsr')):
    /// bare "dsr" is the LATEST night's report — what "Last night's report"
    /// (the quick action, the Siri shortcut, the command chip, the registry)
    /// means; it used to open the list of nights (F3-15). "dsr/night/<date>"
    /// or "dsr/<date>" is that night, "dsr/week[/<date>]" the week's grid,
    /// "dsr/list" the list.
    static func dailyReportRoute(_ nav: NavPath) -> DailyReportRoute {
        var rest = nav.rest
        let kind = rest.first.map { $0.lowercased() }
        if kind == "list" || kind == "nights" { return .list }
        if kind == "week" || kind == "night" || kind == "period" { rest.removeFirst() }
        let date = rest.first.flatMap { DSRFormat.isISODate($0) ? $0 : nil }
        if kind == "week" { return .week(date: date) }
        if kind == "period" { return .period(date: date) }
        return .report(date: date)
    }

    func consumePendingDailyReport() -> DailyReportRoute? {
        defer { pendingDailyReport = nil }
        return pendingDailyReport
    }

    func consumePendingModuleKey() -> String? {
        defer { pendingModuleKey = nil }
        return pendingModuleKey
    }

    /// The fallback when a payload or row carries no `module` (an older
    /// server). Mirrors push.NOTIFICATION_MODULE — it used to know two
    /// labor types and send every food-cost, intel, coverage, issue and
    /// order notification to Reviews (re-audit A-7).
    static func webModule(for alertType: String) -> String {
        switch alertType {
        case "labor_over", "schedule_drafted", "coverage", "schedule_publish_pending",
             "schedule_publish_held", "shift_request", "labor_reminder":
            return "labor"
        case "food_waste", "critical_low", "price_spike", "order_send_pending",
             "order_send_held", "order_send_voided":
            return "inventory"
        case "ai_visibility_drop", "competitor_move":
            return "competitor"
        case "demand_opportunity":
            return "marketing"
        case "morning_brief", "daily_briefing", "intraday_pulse", "closing_summary",
             "weekly_review", "monthly_review", "outcome_achieved", "milestone":
            return "ask"
        case "issue", "issue_escalated", "login", "staff_signin", "connection_lost",
             "data_source_down", "data_source_restored":
            return "account"
        default:
            return "reviews"
        }
    }

    /// The web's module ids are the tab ids; the app's module screens
    /// (ModuleDestinationView) call Intel "intel".
    static func appModuleKey(forWebModule module: String) -> String {
        switch module {
        case "competitor": return "intel"
        case "labor", "inventory", "marketing", "reviews", "intel": return module
        default: return "reviews"
        }
    }
}

/// A queued automatic send to present (delayed_actions.id). Identifiable so
/// RootView can drive `.sheet(item:)` with it.
struct PendingActionRef: Identifiable, Hashable {
    let id: Int
}
