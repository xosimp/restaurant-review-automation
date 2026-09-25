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
    var pendingReviewID: Int?
    /// A question to prefill in Ask Cavnar. Set by the morning brief push,
    /// whose lines each carry the question an owner would ask about them.
    /// Prefilled, never auto-sent: the owner decides whether to ask it.
    var pendingAskPrompt: String?
    /// A Daily Sales Report to open on Home's stack — set by a `dsr` push
    /// (`business_date` → that night) or its row in the notification list
    /// (no date there → the list of nights). HomeView consumes it.
    var pendingDailyReport: DailyReportRoute?
    /// Bumped each time a tap switched the active location, so Home reloads
    /// for the location it now shows.
    var locationSwitches = 0
    /// Switches the session to another location of the group; set by
    /// RootView (it owns the SessionStore). Returns true on success.
    var switchLocation: ((Int) async -> Bool)?
    /// The location the session is on now; set by RootView.
    var activeRestaurantId: () -> Int = { SessionScope.restaurantId }

    /// `module` is the server's own routing (push.NOTIFICATION_MODULE, sent
    /// in every payload and every history row); `restaurantId` is the
    /// location the notification is about. A group owner's phone hears
    /// every location's alerts, so a tap on location A's alert while the
    /// app is on B switches to A first — otherwise A's review is "not
    /// found" inside B and the open is recorded against B (re-audit A-14).
    func handleNotificationTap(alertType: String, reviewId: Int?, askPrompt: String? = nil,
                               alertId: Int? = nil, recKey: String? = nil,
                               module: String? = nil, restaurantId: Int? = nil,
                               businessDate: String? = nil, surface: String? = nil) {
        let current = activeRestaurantId()
        if let target = restaurantId, target > 0, current > 0, target != current, let switchLocation {
            Task {
                if await switchLocation(target) { locationSwitches += 1 }
                route(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt,
                      alertId: alertId, recKey: recKey, module: module, businessDate: businessDate,
                      surface: surface)
            }
            return
        }
        route(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt,
              alertId: alertId, recKey: recKey, module: module, businessDate: businessDate,
              surface: surface)
    }

    private func route(alertType: String, reviewId: Int?, askPrompt: String?,
                       alertId: Int?, recKey: String?, module: String?, businessDate: String?,
                       surface: String?) {
        // What the product knew was how many notifications it SENT. Whether
        // any of them were worth sending had no answer anywhere — not for
        // the owner, not for Will. Best effort: a failure here must never
        // interfere with actually opening the thing.
        if !alertType.isEmpty {
            Task { await Self.recordOpen(alertType, alertId: alertId, recKey: recKey, surface: surface) }
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
            if let askPrompt, !askPrompt.isEmpty { pendingAskPrompt = askPrompt }
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

    func consumePendingReviewID() -> Int? {
        defer { pendingReviewID = nil }
        return pendingReviewID
    }

    /// The nightly report's push type is `dsr`; a server that maps it in
    /// push.NOTIFICATION_MODULE may send `module: "dsr"` as well.
    static func isDailyReport(alertType: String, module: String?) -> Bool {
        alertType == "dsr" || module == "dsr"
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
