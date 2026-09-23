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

    func handleNotificationTap(alertType: String, reviewId: Int?, askPrompt: String? = nil) {
        // What the product knew was how many notifications it SENT. Whether
        // any of them were worth sending had no answer anywhere — not for
        // the owner, not for Will. Best effort: a failure here must never
        // interfere with actually opening the thing.
        if !alertType.isEmpty {
            Task { await Self.recordOpen(alertType) }
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
        // Reviews now lives inside the Modules tab (no per-module tabs
        // anymore), so switch there and let ModulesGridView push into the
        // right module screen itself once pendingModuleKey is set.
        pendingTab = .modules
        pendingModuleKey = Self.moduleKey(for: alertType)
        pendingReviewID = reviewId
    }

    private struct OpenedBody: Encodable { let type: String }
    private struct RecOpenedBody: Encodable { let key: String; let event: String; let surface: String }

    /// A tapped push that carried a recommendation key: recorded as
    /// "opened" against it (POST /mobile/api/recs/event). Best effort.
    nonisolated static func recordRecOpened(_ key: String, surface: String) async {
        let _: APIClient.OKResponse? = try? await APIClient.shared.send(
            "/mobile/api/recs/event", method: .post,
            body: RecOpenedBody(key: key, event: "opened", surface: surface), hapticOnError: false)
    }

    private static func recordOpen(_ alertType: String) async {
        let _: APIClient.EmptyResponse? = try? await APIClient.shared.send(
            "/mobile/api/notifications/opened", method: .post,
            body: OpenedBody(type: alertType), hapticOnError: false)
    }

    func consumePendingReviewID() -> Int? {
        defer { pendingReviewID = nil }
        return pendingReviewID
    }

    func consumePendingModuleKey() -> String? {
        defer { pendingModuleKey = nil }
        return pendingModuleKey
    }

    // Mirrors client_api.py's _NOTIFICATION_MODULE — every alert type is
    // review/rating-driven except the labor ones: labor_over, and
    // schedule_drafted (strategy_jobs' Thursday auto-draft), whose draft is
    // waiting in Labor's schedule history.
    private static func moduleKey(for alertType: String) -> String {
        switch alertType {
        case "labor_over", "schedule_drafted": return "labor"
        default: return "reviews"
        }
    }
}
