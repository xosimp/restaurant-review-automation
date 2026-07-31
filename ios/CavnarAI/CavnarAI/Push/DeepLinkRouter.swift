import Foundation
import Observation

/// Routes a tapped push notification to the right screen. notify.py's
/// alert-firing code sends the same unprefixed alert_type strings it logs
/// internally ("1star", "neg_spike", "no_response", ...) plus a review_id
/// when there is one — see push.py's fire_push() call sites in notify.py.
@Observable
@MainActor
final class DeepLinkRouter {
    var pendingTab: AppTab?
    var pendingReviewID: Int?

    func handleNotificationTap(alertType: String, reviewId: Int?) {
        // Every v1 push category (health/1star/2star/5star/neg_spike/
        // no_response) is review-related — Reviews now lives inside the
        // Modules tab (no per-module tabs anymore), so switch there and let
        // ModulesGridView push into Reviews itself once pendingReviewID is set.
        pendingTab = .modules
        pendingReviewID = reviewId
    }

    func consumePendingReviewID() -> Int? {
        defer { pendingReviewID = nil }
        return pendingReviewID
    }
}
