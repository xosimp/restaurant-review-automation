import UIKit
import UserNotifications

/// Registers for and handles APNs push. The sandbox/production environment
/// tag matters: getting it wrong silently drops every notification, since
/// they're separate APNs token namespaces (see push.py's `environment`
/// column on device_tokens) — #if DEBUG reliably tracks which one a build
/// was signed for, and project.yml now sets the matching aps-environment
/// entitlement per configuration so the two can't disagree.
@MainActor
final class PushManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = PushManager()

    var router: DeepLinkRouter?

    /// mainTabs is torn down and rebuilt on every Face ID unlock, so its
    /// .task fires many times a day — and the token has not changed between
    /// them. Registering once per launch drops a dozen redundant authenticated
    /// round-trips and database writes per device per day (audit 3.5).
    private var hasRegisteredThisLaunch = false

    /// The APNs token arrives at launch, which is exactly when registration is
    /// most likely to fail — no session yet behind the Face ID gate, or no
    /// network. It used to be logged to a print and dropped forever, so the
    /// owner simply never received alerts again with nothing in the UI to
    /// explain why (audit 4.3). Held here until a send succeeds.
    private var pendingToken: (token: String, environment: String)?

    func requestAuthorizationAndRegister() {
        guard !hasRegisteredThisLaunch else { return }
        hasRegisteredThisLaunch = true
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, _ in
            guard granted else { return }
            Task { @MainActor in
                UIApplication.shared.registerForRemoteNotifications()
            }
        }
    }

    func didRegister(deviceToken: Data) {
        let tokenString = deviceToken.map { String(format: "%02x", $0) }.joined()
        #if DEBUG
        let environment = "sandbox"
        #else
        let environment = "production"
        #endif
        pendingToken = (tokenString, environment)
        Task { await flushPendingToken() }
    }

    private struct DeviceTokenBody: Encodable {
        let apnsToken: String
        let environment: String
        enum CodingKeys: String, CodingKey {
            case apnsToken = "apns_token"
            case environment
        }
    }

    /// Retried after login (SessionStore.completeLogin) and on foreground.
    /// A no-op once the token has been accepted.
    func flushPendingToken() async {
        guard let pending = pendingToken else { return }
        do {
            let _: APIClient.EmptyResponse = try await APIClient.shared.send(
                "/mobile/api/device-tokens", method: .post,
                body: DeviceTokenBody(apnsToken: pending.token, environment: pending.environment),
                hapticOnError: false
            )
            pendingToken = nil
        } catch {
            // Stays queued for the next attempt — a failed push registration
            // must not be silently permanent.
        }
    }

    /// Show the banner even while the app is open — an owner mid-task
    /// should still see "1★ review received" rather than it silently
    /// landing only in Notification Center.
    ///
    /// `nonisolated`: UNNotification/UNUserNotificationCenter are not
    /// Sendable, so accepting them directly into a @MainActor method is an
    /// error in the Swift 6 language mode (audit 2.3).
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        // Extract only Sendable values before crossing to the main actor —
        // the notification objects themselves must not cross.
        let userInfo = response.notification.request.content.userInfo
        guard let cavnar = userInfo["cavnar"] as? [String: Any] else { return }
        let alertType = cavnar["alert_type"] as? String ?? ""
        // Reject nonsense ids before they become a URL path component. The
        // backend is still the authority on whether this review belongs to
        // this account; this is shape validation, not authorization (audit 1.9).
        let reviewId = (cavnar["review_id"] as? Int).flatMap { $0 > 0 ? $0 : nil }
        await MainActor.run {
            router?.handleNotificationTap(alertType: alertType, reviewId: reviewId)
        }
    }
}

/// Bridges UIKit's remote-notification registration callbacks into
/// PushManager — SwiftUI's App lifecycle has no direct hook for these.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Global nav-bar title color — every screen's navigationTitle (the
        // three tab roots, plus every pushed detail screen) otherwise
        // renders through UIKit's default dark-mode label color, which is
        // literal white. The app-wide rule is cream everywhere text would
        // otherwise read as white (see Color+Cavnar's "Ink" — the same
        // cream every other label/headline already uses), and SwiftUI's
        // .navigationTitle has no direct color modifier, so this is set
        // once via UINavigationBar's appearance proxy instead of per-screen.
        let creamColor = UIColor(named: "Ink") ?? .white
        let appearance = UINavigationBarAppearance()
        appearance.titleTextAttributes = [.foregroundColor: creamColor]
        appearance.largeTitleTextAttributes = [.foregroundColor: creamColor]
        UINavigationBar.appearance().standardAppearance = appearance
        UINavigationBar.appearance().scrollEdgeAppearance = appearance
        UINavigationBar.appearance().compactAppearance = appearance
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        Task { @MainActor in PushManager.shared.didRegister(deviceToken: deviceToken) }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        // Kept behind #if DEBUG: this is the only logging left in the target,
        // and a release build should not narrate registration failures.
        #if DEBUG
        print("[push] failed to register for remote notifications: \(error)")
        #endif
    }
}
