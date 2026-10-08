import Foundation

/// What the owner is watching on Labor right now, for one decision: whether
/// a `schedule_drafted` push that arrives while the app is open is worth a
/// banner (parity follow-up 10/7/26). The push tells an owner who LEFT the
/// screen that their week landed; one still watching it land has it on
/// screen already, so PushManager's willPresent keeps the banner down.
///
/// LaborView says when it is on screen and which week it shows;
/// LaborViewModel says which generation it is polling. The push names both
/// (`job_id`, `schedule_id` — schedule_engine.notify_generation_watchers),
/// so either one matching is "that same generation".
@MainActor
final class ScheduleDraftWatch {
    static let shared = ScheduleDraftWatch()

    /// Labor is the screen in front.
    var laborOnScreen = false
    /// The generation job this phone is polling now.
    var pollingJobId: String?
    /// The drafted week on Labor's screen (its history id).
    var onScreenScheduleId: Int?

    struct Snapshot: Equatable, Sendable {
        var laborOnScreen = false
        var pollingJobId: String?
        var onScreenScheduleId: Int?
    }

    var snapshot: Snapshot {
        Snapshot(laborOnScreen: laborOnScreen, pollingJobId: pollingJobId, onScreenScheduleId: onScreenScheduleId)
    }

    /// Stop naming `jobId` as polled — only if it is still the one named,
    /// so an older poll ending never clears a newer one's.
    func pollEnded(_ jobId: String) {
        if pollingJobId == jobId { pollingJobId = nil }
    }

    /// Whether a push payload is the drafted week the owner is watching:
    /// a `schedule_drafted` push, Labor on screen, and its job or its week
    /// the one there.
    nonisolated static func isWatching(_ cavnar: [String: Any], _ watch: Snapshot) -> Bool {
        guard watch.laborOnScreen, PushManager.alertType(cavnar) == "schedule_drafted" else { return false }
        if let job = (cavnar["job_id"] as? String) ?? (cavnar["job_id"] as? NSNumber)?.stringValue,
           !job.isEmpty, job == watch.pollingJobId {
            return true
        }
        if let id = PushManager.reviewId(from: cavnar["schedule_id"]), id == watch.onScreenScheduleId {
            return true
        }
        return false
    }
}
