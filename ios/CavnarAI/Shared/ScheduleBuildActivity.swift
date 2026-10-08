import ActivityKit
import Foundation

/// "Building next week · 3 of 7 days drafted · about 9:41 left" on the Lock
/// Screen and in the Dynamic Island while a schedule generation runs
/// (parity audit #38). A generation runs 15–40 minutes; the Labor screen's
/// progress was only visible while the app stayed open.
///
/// The app starts it when an owner starts a generation (LaborViewModel →
/// ScheduleBuildActivities), updates it from each poll, and ends it on the
/// result. The server pushes the same ContentState from the job itself
/// (live_activities.schedule_build_state — the JSON keys below, key for
/// key) as each day is drafted and when the job lands, so it keeps moving
/// with the app closed.
struct ScheduleBuildAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// Days the answer has finished so far, of the days it drafts — a
        /// real count, never a percentage (AI cost audit #36).
        var daysDrafted: Int
        var daysTotal: Int
        /// building | done | failed
        var status: String
        /// When a typical generation here would finish (the measured median,
        /// schedule_engine.typical_generation_seconds); nil until two have
        /// run — no timer is drawn then, never a guessed one.
        var estimatedEnd: Date?
        /// The job's own sentence when it failed.
        var note: String?

        init(daysDrafted: Int = 0, daysTotal: Int = 0, status: String = "building",
             estimatedEnd: Date? = nil, note: String? = nil) {
            self.daysDrafted = daysDrafted
            self.daysTotal = daysTotal
            self.status = status
            self.estimatedEnd = estimatedEnd
            self.note = note
        }

        /// "3 of 7 days drafted"; nil before the first day is finished.
        var progressLine: String? {
            guard daysTotal > 0, daysDrafted > 0 else { return nil }
            let n = min(daysDrafted, daysTotal)
            return "\(n) of \(daysTotal) \(daysTotal == 1 ? "day" : "days") drafted"
        }

        /// The estimate's countdown, only while it is still ahead.
        func timerRange(now: Date = Date()) -> ClosedRange<Date>? {
            guard status == "building", let end = estimatedEnd, end > now else { return nil }
            return now...end
        }
    }

    /// The job the activity follows — what the server keys its pushes by.
    let jobId: String
    /// "Next week" / "Week of 10/12/26" / "Redoing 2 days".
    let weekLabel: String
}
