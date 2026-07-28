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
        // no_response) is review-related — all of them land on Reviews.
        pendingTab = .reviews
        pendingReviewID = reviewId
    }

    func consumePendingReviewID() -> Int? {
        defer { pendingReviewID = nil }
        return pendingReviewID
    }
}
