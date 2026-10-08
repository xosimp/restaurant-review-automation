import BackgroundTasks
import Foundation
import UIKit
import WidgetKit

/// Keeps the widgets, the "How was last night?" answer and the quick
/// actions current while the app stays closed (parity audit #31). They were
/// refreshed only when the app became active, so a widget read in the
/// morning showed whatever the phone saw the evening before.
///
/// Two ways in, both ending in WidgetSnapshotService.refresh(force:):
/// - a BGAppRefreshTask, asked for each time the app goes to the background
///   (iOS decides when it actually runs, and how often);
/// - a silent push (content-available, push.fire_silent) the server sends
///   when last night's report is delivered and when the waiting count
///   changes — the refresh happens when there is something new.
enum BackgroundRefresh {
    /// Listed in Info.plist's BGTaskSchedulerPermittedIdentifiers (project.yml).
    static let taskIdentifier = "ai.cavnar.CavnarAI.refresh"
    /// The earliest the next refresh may run; iOS usually waits longer.
    static let interval: TimeInterval = 30 * 60

    static func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: taskIdentifier)
        request.earliestBeginDate = Date(timeIntervalSinceNow: interval)
        try? BGTaskScheduler.shared.submit(request)
    }

    /// The refresh task's body (CavnarAIApp's `.backgroundTask`): ask for
    /// the next one first, then refresh.
    @MainActor
    static func run() async {
        schedule()
        await WidgetSnapshotService.shared.refresh(force: true)
    }

    /// Whether a remote notification is one of the server's silent widget
    /// pushes: content-available and a `cavnar.silent` reason.
    static func isSilentRefresh(_ userInfo: [AnyHashable: Any]) -> Bool {
        guard let aps = userInfo["aps"] as? [String: Any],
              (aps["content-available"] as? Int) == 1 || (aps["content-available"] as? NSNumber)?.intValue == 1,
              let cavnar = userInfo["cavnar"] as? [String: Any],
              let reason = cavnar["silent"] as? String else { return false }
        return ["dsr", "waiting"].contains(reason)
    }
}

extension AppDelegate {
    /// A silent push woke the app (#31): refresh the snapshot, reload the
    /// widget timelines, and tell iOS whether anything new was written —
    /// it budgets later wakes by that answer.
    @MainActor
    @objc func application(_ application: UIApplication,
                           didReceiveRemoteNotification userInfo: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        guard BackgroundRefresh.isSilentRefresh(userInfo) else { return .noData }
        let wrote = await WidgetSnapshotService.shared.refresh(force: true)
        return wrote ? .newData : .noData
    }
}
