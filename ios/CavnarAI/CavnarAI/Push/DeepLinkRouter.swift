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

    func handleNotificationTap(alertType: String, reviewId: Int?) {
        // Reviews now lives inside the Modules tab (no per-module tabs
        // anymore), so switch there and let ModulesGridView push into the
        // right module screen itself once pendingModuleKey is set.
        pendingTab = .modules
        pendingModuleKey = Self.moduleKey(for: alertType)
        pendingReviewID = reviewId
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
    // review/rating-driven except labor_over, which has no review to jump
    // to at all.
    private static func moduleKey(for alertType: String) -> String {
        alertType == "labor_over" ? "labor" : "reviews"
    }
}
