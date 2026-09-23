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

    /// Set by RootView on its first appearance — which, on a launch caused
    /// by tapping a notification, is AFTER iOS has already delivered that
    /// tap. A tap that arrives with no router yet is held and replayed here
    /// rather than dropped (CLIENT-7).
    var router: DeepLinkRouter? {
        didSet {
            guard let router, let tap = heldTap else { return }
            heldTap = nil
            router.handleNotificationTap(alertType: tap.alertType, reviewId: tap.reviewId, askPrompt: tap.askPrompt)
        }
    }
    private var heldTap: (alertType: String, reviewId: Int?, askPrompt: String?)?

    /// Whether the phone will actually show anything. A denial is permanent
    /// and silent: the app never asked, so an owner who tapped "Don't Allow"
    /// once was unreachable by push forever and nothing anywhere said so —
    /// not the app, not the backend, not Will. Account reads this to offer a
    /// way back (AccountAlertsDetailView).
    private(set) var authorizationDenied = false

    /// Never asked yet. On a fresh install the system prompt waits for the
    /// second open (see launchCountKey), so on launch one there has to be a
    /// way to ask for it — otherwise the deferral is a trap: no prompt, no
    /// token, and nothing in Account offering either.
    private(set) var authorizationUndetermined = false

    /// Categories are what let an owner act from the lock screen instead of
    /// unlocking, finding the module and starting again. The identifiers
    /// match push.py's CATEGORY_* constants.
    private static let reviewCategory = "CAVNAR_REVIEW"
    private static let briefCategory  = "CAVNAR_BRIEF"
    private static let issueCategory  = "CAVNAR_ISSUE"
    private static let openAction     = "CAVNAR_OPEN"
    private static let askAction      = "CAVNAR_ASK"

    /// The system prompt used to fire within seconds of the first login,
    /// before the owner had seen a single number. Asking on the second open
    /// means they have seen what the app does first — and Account can ask
    /// for it directly at any time (see `promptNow`).
    private static let launchCountKey = "cavnar.launchCount"

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

    /// The token this install last registered with the backend. Apple issues
    /// one per install and it is stable across launches, so holding it lets
    /// sign-out unregister the device — without it, a signed-out phone kept
    /// receiving that restaurant's review alerts and daily digests forever.
    ///
    /// Persisted in the Keychain, not just held for this launch: a sign-out
    /// from the Face ID gate (LockedView's "Forgot your passcode? Sign out")
    /// happens before mainTabs has ever asked APNs for the token, so an
    /// in-memory copy was nil there and /logout went out with
    /// apns_token: nil — the phone kept receiving the restaurant's alerts
    /// after signing out (CLIENT-8).
    private(set) var registeredToken: String? = Keychain.get(PushManager.registeredTokenKey) {
        didSet {
            if let registeredToken {
                Keychain.set(registeredToken, for: Self.registeredTokenKey)
            } else {
                Keychain.delete(Self.registeredTokenKey)
            }
        }
    }
    private static let registeredTokenKey = "cavnar.apns_registered_token"

    /// Installed from AppDelegate's didFinishLaunching. Apple hands the tap
    /// that launched the app only to a delegate that is set before launch
    /// finishes; this used to be set in requestAuthorizationAndRegister,
    /// after sign-in and unlock, so a tap from a closed app opened Home and
    /// the alert it was about was lost (CLIENT-7).
    func installAsNotificationDelegate() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(Self.categories)
    }

    func requestAuthorizationAndRegister() {
        // NOT marked handled here. It used to be, at the top, before this
        // knew whether it had actually registered — so the .notDetermined
        // early return below burned the launch: no prompt, no
        // registerForRemoteNotifications, no APNs token, and every later
        // flushPendingToken() a no-op because nothing was ever queued. An
        // owner who then allowed notifications in iOS Settings stayed
        // unregistered until the next cold launch, and the backend
        // truthfully reported "no device registered for your login".
        //
        // Cheap to re-enter: getNotificationSettings is local, and the
        // authorized path is what sets the flag. mainTabs rebuilds on every
        // Face ID unlock, which is now exactly when we want another look.
        guard !hasRegisteredThisLaunch else { return }
        let center = UNUserNotificationCenter.current()
        installAsNotificationDelegate()

        let defaults = UserDefaults.standard
        let launches = defaults.integer(forKey: Self.launchCountKey) + 1
        defaults.set(launches, forKey: Self.launchCountKey)

        center.getNotificationSettings { settings in
            Task { @MainActor in
                switch settings.authorizationStatus {
                case .denied:
                    // Surfaced in Account rather than retried: iOS will not
                    // show the prompt again, so only Settings can undo it.
                    self.authorizationDenied = true
                    self.authorizationUndetermined = false
                case .notDetermined:
                    self.authorizationDenied = false
                    self.authorizationUndetermined = true
                    guard launches >= 2 else { return }
                    await self.promptNow()
                default:
                    self.authorizationDenied = false
                    self.authorizationUndetermined = false
                    self.hasRegisteredThisLaunch = true
                    UIApplication.shared.registerForRemoteNotifications()
                    await self.flushPendingToken()
                }
            }
        }
    }

    /// Ask for permission now. Called on the second app open, and directly
    /// from Account when the owner asks for notifications themselves.
    @discardableResult
    func promptNow() async -> Bool {
        let granted = (try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound, .badge])) ?? false
        authorizationDenied = !granted
        authorizationUndetermined = false
        if granted {
            UIApplication.shared.registerForRemoteNotifications()
            await flushPendingToken()
        }
        return granted
    }

    /// Re-read the real state — the owner may have changed it in Settings
    /// while the app was backgrounded.
    func refreshAuthorization() async {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        authorizationDenied = settings.authorizationStatus == .denied
        authorizationUndetermined = settings.authorizationStatus == .notDetermined
        if settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional {
            UIApplication.shared.registerForRemoteNotifications()
            await flushPendingToken()
        }
    }

    private static var categories: Set<UNNotificationCategory> {
        let open = UNNotificationAction(identifier: openAction, title: "Open", options: [.foreground])
        let respond = UNNotificationAction(identifier: openAction, title: "Respond", options: [.foreground])
        let ask = UNNotificationAction(identifier: askAction, title: "Ask about this", options: [.foreground])
        return [
            UNNotificationCategory(identifier: reviewCategory, actions: [respond],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: briefCategory, actions: [ask],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: issueCategory, actions: [open],
                                   intentIdentifiers: [], options: []),
        ]
    }

    /// Clear the app icon badge. The backend now sends the unread count on
    /// every push (push.py `_badge_for`); nothing cleared it, and a number
    /// that only ever goes up is a number people stop reading.
    func clearBadge() {
        UNUserNotificationCenter.current().setBadgeCount(0)
    }

    func didRegister(deviceToken: Data) {
        let tokenString = deviceToken.map { String(format: "%02x", $0) }.joined()
        pendingToken = (tokenString, Self.apnsEnvironment)
        Task { await flushPendingToken() }
    }

    /// Which APNs host this token belongs to, read from the entitlement that
    /// actually decides it.
    ///
    /// This was `#if DEBUG`, on the assumption that a build configuration
    /// tracks its aps-environment. It does not. The entitlement comes from
    /// the PROVISIONING PROFILE, and Xcode signs with a development profile
    /// when you Run to a device — including a Release build. That yields a
    /// sandbox token from a build that confidently reports "production", the
    /// backend sends it to api.push.apple.com, and Apple answers
    /// BadDeviceToken. Which reads exactly like a dead device.
    ///
    /// embedded.mobileprovision is the profile the app was actually signed
    /// with, so its aps-environment is the truth. Simulator builds have no
    /// profile; they fall back to the build configuration, which is right
    /// there because the simulator cannot receive remote push anyway.
    static let apnsEnvironment: String = {
        if let url = Bundle.main.url(forResource: "embedded", withExtension: "mobileprovision"),
           let data = try? Data(contentsOf: url),
           let text = String(data: data, encoding: .isoLatin1),
           let start = text.range(of: "<?xml"),
           let end = text.range(of: "</plist>") {
            let plist = String(text[start.lowerBound..<end.upperBound])
            if let plistData = plist.data(using: .isoLatin1),
               let parsed = try? PropertyListSerialization.propertyList(
                   from: plistData, options: [], format: nil) as? [String: Any],
               let entitlements = parsed["Entitlements"] as? [String: Any],
               let aps = entitlements["aps-environment"] as? String {
                // Apple spells it "development"; APNs hosts are named
                // sandbox/production, which is what device_tokens stores.
                return aps == "production" ? "production" : "sandbox"
            }
        }
        #if DEBUG
        return "sandbox"
        #else
        return "production"
        #endif
    }()

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
            registeredToken = pending.token
            pendingToken = nil
        } catch {
            // Stays queued for the next attempt — a failed push registration
            // must not be silently permanent.
        }
    }

    /// Called after a location switch. The backend files a device token
    /// under the restaurant of the session that registered it, so the token
    /// stayed pointed at the launch-time location: a multi-location owner
    /// who switched to Dallas kept getting Chicago's alerts and none of
    /// Dallas's (CLIENT-8). Registering again re-points the row
    /// (push.register_device_token upserts by token).
    func reregisterForActiveLocation() async {
        if pendingToken == nil, let registeredToken {
            pendingToken = (registeredToken, currentEnvironment)
        }
        await flushPendingToken()
    }

    /// Called on sign-out, before the bearer token is cleared. The backend
    /// deletes the row scoped to the caller's own restaurant; the token stays
    /// queued locally so the next sign-in re-registers it.
    func unregisterCurrentDevice() async {
        guard let token = registeredToken else { return }
        do {
            let _: APIClient.EmptyResponse = try await APIClient.shared.send(
                "/mobile/api/device-tokens/\(token)", method: .delete, hapticOnError: false
            )
        } catch {
            // Best effort — sign-out must never be blocked by the push
            // registry. The backend also unregisters from /logout's body.
        }
        pendingToken = (token, currentEnvironment)
        registeredToken = nil
    }

    private var currentEnvironment: String { Self.apnsEnvironment }

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
        let reviewId = Self.reviewId(from: cavnar["review_id"])
        // Bounded like Ask's own input: the prompt is prefilled into a text
        // field, never executed, but there's no reason to accept an
        // arbitrarily long payload into one.
        let askPrompt = (cavnar["ask_prompt"] as? String).map { String($0.prefix(300)) }
        // The recommendation the push led with (morning brief): its tap is
        // an "opened" in rec_ledger, the same as a tap on the brief email.
        let recKey = (cavnar["rec"] as? String).map { String($0.prefix(160)) }
        // Every action we register is .foreground and lands on the same
        // screen the notification itself does, so the action identifier
        // changes nothing here — it is the tap that matters. Dismissals are
        // ignored rather than routed.
        guard response.actionIdentifier != UNNotificationDismissActionIdentifier else { return }
        if let recKey, !recKey.isEmpty {
            Task { await DeepLinkRouter.recordRecOpened(recKey, surface: alertType == "morning_brief" ? "brief_push" : "alert_push") }
        }
        await MainActor.run {
            guard let router else {
                heldTap = (alertType, reviewId, askPrompt)
                return
            }
            router.handleNotificationTap(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt)
        }
    }

    /// A positive review id, whether the payload carried it as a JSON
    /// number or as a string — `as? Int` alone dropped "42", and the tap
    /// landed on the inbox instead of the review (CLIENT-51).
    nonisolated static func reviewId(from raw: Any?) -> Int? {
        let parsed: Int?
        switch raw {
        case let n as Int: parsed = n
        case let s as String: parsed = Int(s.trimmingCharacters(in: .whitespaces))
        case let n as NSNumber: parsed = n.intValue
        default: parsed = nil
        }
        return parsed.flatMap { $0 > 0 ? $0 : nil }
    }
}

/// Bridges UIKit's remote-notification registration callbacks into
/// PushManager — SwiftUI's App lifecycle has no direct hook for these.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        // Before anything else: the notification tap that launched the app
        // is delivered only to a delegate set before this returns (CLIENT-7).
        PushManager.shared.installAsNotificationDelegate()

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
