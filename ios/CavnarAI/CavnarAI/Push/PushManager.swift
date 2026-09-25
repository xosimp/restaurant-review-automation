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
            tap.deliver(to: router)
        }
    }
    private var heldTap: Tap?

    /// Everything a tap routes on — only Sendable values, read out of the
    /// payload before crossing to the main actor.
    struct Tap: Sendable {
        var alertType: String
        var reviewId: Int?
        var askPrompt: String?
        var alertId: Int?
        var recKey: String?
        var module: String?
        var restaurantId: Int?
        var businessDate: String?
        var surface: String?
        /// Where it opens (push.nav_for): the review, the pending send, the
        /// request. Nil from an older server — the router's mirror answers.
        var nav: String?
        /// The push's own "Ask about this" button: send the question, not
        /// just fill it in (friction audit #15).
        var askAutoSend = false

        @MainActor
        func deliver(to router: DeepLinkRouter) {
            router.handleNotificationTap(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt,
                                         alertId: alertId, recKey: recKey,
                                         module: module, restaurantId: restaurantId,
                                         businessDate: businessDate, surface: surface,
                                         nav: nav, askAutoSend: askAutoSend)
        }
    }

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
    nonisolated private static let reviewCategory = "CAVNAR_REVIEW"
    nonisolated private static let briefCategory  = "CAVNAR_BRIEF"
    nonisolated private static let issueCategory  = "CAVNAR_ISSUE"
    /// The actionable kinds (friction audit #22): the button does the work
    /// in the background, behind the phone's own unlock, and nothing opens.
    nonisolated private static let reviewDraftedCategory = "CAVNAR_REVIEW_DRAFTED"
    nonisolated private static let undoableCategory      = "CAVNAR_UNDOABLE"
    nonisolated private static let requestCategory       = "CAVNAR_REQUEST"
    nonisolated private static let openAction     = "CAVNAR_OPEN"
    nonisolated private static let askAction      = "CAVNAR_ASK"
    nonisolated static let approvePostAction      = "CAVNAR_APPROVE_POST"
    nonisolated static let undoAction             = "CAVNAR_UNDO"
    nonisolated static let approveRequestAction   = "CAVNAR_APPROVE_REQUEST"
    nonisolated static let denyRequestAction      = "CAVNAR_DENY_REQUEST"

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

    /// "Open" on an issue did exactly what tapping the notification does, so
    /// the issue category has no button of its own now; "Respond" on a
    /// review only opened the app, so it says "Reply". The actionable ones
    /// act: no `.foreground`, and `.authenticationRequired` so a locked
    /// phone asks for Face ID before a reply posts or a send is stopped.
    private static var categories: Set<UNNotificationCategory> {
        let reply = UNNotificationAction(identifier: openAction, title: "Reply", options: [.foreground])
        let ask = UNNotificationAction(identifier: askAction, title: "Ask about this", options: [.foreground])
        let approvePost = UNNotificationAction(identifier: approvePostAction, title: "Approve & post",
                                               options: [.authenticationRequired])
        let edit = UNNotificationAction(identifier: openAction, title: "Edit", options: [.foreground])
        let undo = UNNotificationAction(identifier: undoAction, title: "Undo",
                                        options: [.destructive, .authenticationRequired])
        let review = UNNotificationAction(identifier: openAction, title: "Review", options: [.foreground])
        let approve = UNNotificationAction(identifier: approveRequestAction, title: "Approve",
                                           options: [.authenticationRequired])
        let deny = UNNotificationAction(identifier: denyRequestAction, title: "Deny",
                                        options: [.destructive, .authenticationRequired])
        return [
            UNNotificationCategory(identifier: reviewCategory, actions: [reply],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: reviewDraftedCategory, actions: [approvePost, edit],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: undoableCategory, actions: [undo, review],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: requestCategory, actions: [approve, deny],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: briefCategory, actions: [ask],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: issueCategory, actions: [],
                                   intentIdentifiers: [], options: []),
        ]
    }

    // MARK: - Acting from the notification (friction audit #22)

    /// What a background button does: the route it calls, what it sends,
    /// and the sentence to post if it could not be done.
    struct BackgroundAction: Equatable {
        let path: String
        let decision: String?
        let failureTitle: String
    }

    /// The call a background action identifier makes for this payload, or
    /// nil when the payload doesn't carry what it needs (then the action
    /// just opens, like a tap).
    nonisolated static func backgroundAction(for actionIdentifier: String,
                                             cavnar: [String: Any]) -> BackgroundAction? {
        switch actionIdentifier {
        case approvePostAction:
            guard let id = reviewId(from: cavnar["review_id"]) else { return nil }
            return BackgroundAction(path: "/mobile/api/reviews/\(id)/approve", decision: nil,
                                    failureTitle: "Couldn't post that reply")
        case undoAction:
            guard let id = reviewId(from: cavnar["delayed_action_id"]) else { return nil }
            return BackgroundAction(path: "/mobile/api/actions/\(id)/cancel", decision: nil,
                                    failureTitle: "Couldn't undo that")
        case approveRequestAction, denyRequestAction:
            guard let id = reviewId(from: cavnar["request_id"]) else { return nil }
            let kind = (cavnar["request_kind"] as? String ?? "shift").lowercased()
            let base = kind.contains("time") ? "/mobile/api/labor/time-off" : "/mobile/api/labor/shift-requests"
            let approve = actionIdentifier == approveRequestAction
            return BackgroundAction(path: "\(base)/\(id)/decide", decision: approve ? "approve" : "deny",
                                    failureTitle: approve ? "Couldn't approve that request"
                                                          : "Couldn't deny that request")
        default:
            return nil
        }
    }

    private struct DecisionBody: Encodable { let decision: String }
    private struct EmptyBody: Encodable {}

    /// Runs a background action with the stored owner session. The app may
    /// have been launched just for this — no view has set up APIClient yet —
    /// so the token is read from the Keychain and sent explicitly. A failure
    /// is never silent: a local notification says so, carrying the original
    /// payload, so tapping it opens the thing to do it by hand.
    nonisolated static func perform(_ action: BackgroundAction, userInfo: [AnyHashable: Any],
                                    restaurantId: Int?) async {
        guard let token = Keychain.get(Keychain.Key.sessionToken) else {
            await postFailure(action.failureTitle, "Open Cavnar AI and sign in to do this.", userInfo: userInfo)
            return
        }
        // The server acts on the location this phone is signed into. An
        // alert about another location of the group can't be answered from
        // here without switching, so it says so instead of failing oddly.
        let active = await MainActor.run { SessionScope.restaurantId }
        if let restaurantId, restaurantId > 0, active > 0, restaurantId != active {
            await postFailure(action.failureTitle,
                              "It's for another location — tap to open it there.", userInfo: userInfo)
            return
        }
        do {
            let response: APIClient.OKResponse
            if let decision = action.decision {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: DecisionBody(decision: decision), bearer: token)
            } else {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: EmptyBody(), bearer: token)
            }
            if !response.ok {
                await postFailure(action.failureTitle, response.error ?? "Tap to open it.", userInfo: userInfo)
            }
        } catch let error as APIClient.APIError {
            await postFailure(action.failureTitle, error.message, userInfo: userInfo)
        } catch {
            await postFailure(action.failureTitle, "Tap to open it and try again.", userInfo: userInfo)
        }
    }

    nonisolated private static func postFailure(_ title: String, _ body: String,
                                                userInfo: [AnyHashable: Any]) async {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.userInfo = userInfo
        let request = UNNotificationRequest(identifier: "cavnar-action-failed-\(UUID().uuidString)",
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
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
        // Every push.py payload nests its data under "cavnar". The daily
        // report's is specified as {"type": "dsr", "business_date": ...};
        // read that shape too — nested or at the top level — so the tap
        // routes whichever way the delivery side ends up sending it.
        guard let cavnar = Self.cavnarPayload(userInfo) else { return }
        let alertType = Self.alertType(cavnar)
        let businessDate = Self.businessDate(cavnar)
        // Reject nonsense ids before they become a URL path component. The
        // backend is still the authority on whether this review belongs to
        // this account; this is shape validation, not authorization (audit 1.9).
        let reviewId = Self.reviewId(from: cavnar["review_id"])
        // Bounded like Ask's own input: the prompt is prefilled into a text
        // field, never executed, but there's no reason to accept an
        // arbitrarily long payload into one.
        let askPrompt = (cavnar["ask_prompt"] as? String).map { String($0.prefix(300)) }
        // Which notification this was (its alert_log row) and the
        // recommendation it carries, so the open ties back to the send
        // (time-to-open) and to the recommendation's trail.
        let alertId = Self.reviewId(from: cavnar["alert_id"])
        let recKey = (cavnar["rec_key"] as? String).map { String($0.prefix(160)) }
        // Which ledger surface delivered it (`alert_push` / `brief_push`),
        // forwarded as sent so a brief-push open is not counted as an alert's
        // (rec-ROI #7). The server validates it and falls back to the type.
        let surface = Self.surface(cavnar)
        // Where it opens (push.NOTIFICATION_MODULE, the web's own map) and
        // which location it is about (A-7 / A-14). Both optional: an older
        // server sends neither and the router falls back to its mirror.
        let module = (cavnar["module"] as? String).map { String($0.prefix(32)) }
        let restaurantId = Self.reviewId(from: cavnar["restaurant_id"])
        let nav = Self.nav(cavnar)
        let actionIdentifier = response.actionIdentifier
        // Dismissals are ignored rather than routed.
        guard actionIdentifier != UNNotificationDismissActionIdentifier else { return }
        // Approve & post, Undo, Approve / Deny: done here, in the
        // background, and nothing opens (friction audit #22).
        if let action = Self.backgroundAction(for: actionIdentifier, cavnar: cavnar) {
            await Self.perform(action, userInfo: userInfo, restaurantId: restaurantId)
            return
        }
        // Every other button (Reply, Edit, Review, Ask about this) and the
        // tap itself open the notification's own place; "Ask about this"
        // also sends the question.
        let tap = Tap(alertType: alertType, reviewId: reviewId, askPrompt: askPrompt, alertId: alertId,
                      recKey: recKey, module: module, restaurantId: restaurantId,
                      businessDate: businessDate, surface: surface, nav: nav,
                      askAutoSend: actionIdentifier == Self.askAction)
        await MainActor.run {
            guard let router else {
                heldTap = tap
                return
            }
            tap.deliver(to: router)
        }
    }

    /// `cavnar["nav"]` (push.nav_for), bounded; nil when absent or empty.
    nonisolated static func nav(_ cavnar: [String: Any]) -> String? {
        guard let raw = cavnar["nav"] as? String else { return nil }
        let s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return s.isEmpty ? nil : String(s.prefix(400))
    }

    /// `cavnar["surface"]`, bounded; nil when absent or empty.
    nonisolated static func surface(_ cavnar: [String: Any]) -> String? {
        guard let raw = cavnar["surface"] as? String else { return nil }
        let s = raw.trimmingCharacters(in: .whitespaces)
        return s.isEmpty ? nil : String(s.prefix(32))
    }

    /// The payload's data: push.py's nested "cavnar" dictionary, or — for a
    /// payload that carries `type` at the top level (the DSR's specified
    /// shape) — the top level itself. Nil for anything that is neither.
    nonisolated static func cavnarPayload(_ userInfo: [AnyHashable: Any]) -> [String: Any]? {
        if let nested = userInfo["cavnar"] as? [String: Any] { return nested }
        guard userInfo["type"] is String else { return nil }
        var flat: [String: Any] = [:]
        for (k, v) in userInfo { if let key = k as? String, key != "aps" { flat[key] = v } }
        return flat
    }

    /// push.py sends `alert_type`; the DSR spec names it `type`.
    nonisolated static func alertType(_ cavnar: [String: Any]) -> String {
        let raw = (cavnar["alert_type"] as? String) ?? (cavnar["type"] as? String) ?? ""
        return String(raw.prefix(64))
    }

    /// A YYYY-MM-DD business date, or nil — shape-checked before it can
    /// become a URL path component; the server decides whose night it is.
    nonisolated static func businessDate(_ cavnar: [String: Any]) -> String? {
        let raw = (cavnar["business_date"] as? String)?.trimmingCharacters(in: .whitespaces)
        return DSRFormat.isISODate(raw) ? raw : nil
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
