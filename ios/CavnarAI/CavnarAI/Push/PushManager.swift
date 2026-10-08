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

    /// The staff tier, set by RootView. While it holds a session, this
    /// phone's APNs token is filed under the staff login through
    /// /staff/api/device-tokens with the STAFF bearer — never the owner
    /// route, which refuses a PIN session (C4) — and staff notice taps open
    /// the staff app (StaffDeepLinkCenter), not the owner router.
    weak var staffSession: StaffSessionStore?

    /// Whether a tap on a staff notice belongs to the staff app: a staff
    /// session is open, or this phone has no owner session at all (a staff
    /// phone whose session ended — the link waits for the next sign-in).
    /// A manager's console phone that also gets a staff notice routes it in
    /// the owner app, which degrades it to Labor (DeepLinkRouter).
    private func routesToStaffApp() -> Bool {
        if staffSession?.isAuthenticated == true { return true }
        return Keychain.get(Keychain.Key.sessionToken) == nil
    }

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
    /// A message to this login — an employee's to the managers
    /// (employee_message) or a teammate's (team_message): Reply is typed on
    /// the lock screen and posted in the background (parity #10, #71).
    nonisolated private static let messageCategory       = "CAVNAR_MESSAGE"
    /// Tonight's lineup brief waiting for approval: Approve publishes the
    /// draft as written; Open shows it first (parity #26).
    nonisolated private static let lineupCategory        = "CAVNAR_LINEUP"
    /// Parity audit 10/7/26 (#35, #55): push.py's CATEGORY_REC … CATEGORY_DSR.
    nonisolated static let recCategory            = "CAVNAR_REC"
    nonisolated static let recAskCategory         = "CAVNAR_REC_ASK"
    nonisolated static let publishHeldCategory    = "CAVNAR_PUBLISH_HELD"
    nonisolated static let stockCategory          = "CAVNAR_STOCK"
    nonisolated static let loginCategory          = "CAVNAR_LOGIN"
    nonisolated static let connectionCategory     = "CAVNAR_CONNECTION"
    nonisolated static let dsrCategory            = "CAVNAR_DSR"
    nonisolated private static let openAction     = "CAVNAR_OPEN"
    nonisolated private static let askAction      = "CAVNAR_ASK"
    nonisolated static let approvePostAction      = "CAVNAR_APPROVE_POST"
    nonisolated static let undoAction             = "CAVNAR_UNDO"
    nonisolated static let approveRequestAction   = "CAVNAR_APPROVE_REQUEST"
    nonisolated static let denyRequestAction      = "CAVNAR_DENY_REQUEST"
    /// A drafted week (iOS parity #16): Review opens it in the editor; Send
    /// to staff — on CAVNAR_SCHEDULE only, the push the server marked
    /// one_tap_safe — checks and publishes in the background.
    nonisolated private static let scheduleCategory       = "CAVNAR_SCHEDULE"
    nonisolated private static let scheduleReviewCategory = "CAVNAR_SCHEDULE_REVIEW"
    nonisolated static let sendScheduleAction             = "CAVNAR_SEND_SCHEDULE"
    nonisolated static let replyMessageAction     = "CAVNAR_REPLY_MESSAGE"
    nonisolated static let approveLineupAction    = "CAVNAR_APPROVE_LINEUP"
    /// A reply typed on the lock screen: saved as the draft, then approved
    /// with that exact text as `expected_draft` (#54).
    nonisolated static let replyTextAction        = "CAVNAR_REPLY_TEXT"
    nonisolated static let recDoneAction          = "CAVNAR_REC_DONE"
    nonisolated static let recNotForUsAction      = "CAVNAR_REC_NOT_FOR_US"
    nonisolated static let sendNowAction          = "CAVNAR_SEND_NOW"
    nonisolated static let notMeAction            = "CAVNAR_NOT_ME"
    /// Foreground buttons that open a named place (not the push's own nav).
    nonisolated static let draftOrderAction       = "CAVNAR_DRAFT_ORDER"
    nonisolated static let reconnectAction        = "CAVNAR_RECONNECT"
    nonisolated static let askLastNightAction     = "CAVNAR_ASK_LAST_NIGHT"

    /// Set by RootView: a push that arrives while the app is open re-reads
    /// the bell's count (parity audit #81).
    var onForegroundPush: (@MainActor () -> Void)?

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
    /// the issue category has no button of its own now. "Reply" on a review
    /// takes the reply right there (parity audit #54): typed on the lock
    /// screen, saved as the draft and posted — it used to only open the app.
    /// The actionable ones act: no `.foreground`, and
    /// `.authenticationRequired` so a locked phone asks for Face ID before a
    /// reply posts or a send is stopped.
    private static var categories: Set<UNNotificationCategory> {
        let reply = UNTextInputNotificationAction(identifier: replyTextAction, title: "Reply",
                                                  options: [.authenticationRequired],
                                                  textInputButtonTitle: "Post", textInputPlaceholder: "Your reply")
        let writeOwn = UNTextInputNotificationAction(identifier: replyTextAction, title: "Write my own",
                                                     options: [.authenticationRequired],
                                                     textInputButtonTitle: "Post",
                                                     textInputPlaceholder: "Your reply")
        let openReview = UNNotificationAction(identifier: openAction, title: "Open", options: [.foreground])
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
        let sendSchedule = UNNotificationAction(identifier: sendScheduleAction, title: "Send to staff",
                                                options: [.authenticationRequired])
        let replyMessage = UNTextInputNotificationAction(identifier: replyMessageAction, title: "Reply",
                                                         options: [.authenticationRequired],
                                                         textInputButtonTitle: "Send",
                                                         textInputPlaceholder: "Your reply")
        let approveLineup = UNNotificationAction(identifier: approveLineupAction, title: "Approve",
                                                 options: [.authenticationRequired])
        let openLineup = UNNotificationAction(identifier: openAction, title: "Open", options: [.foreground])
        // Parity audit #35 / #55.
        let done = UNNotificationAction(identifier: recDoneAction, title: "Done", options: [.authenticationRequired])
        let notForUs = UNNotificationAction(identifier: recNotForUsAction, title: "Not for us",
                                            options: [.authenticationRequired])
        let sendNow = UNNotificationAction(identifier: sendNowAction, title: "Send now",
                                           options: [.authenticationRequired])
        let reviewWeek = UNNotificationAction(identifier: openAction, title: "Review", options: [.foreground])
        let draftOrder = UNNotificationAction(identifier: draftOrderAction, title: "Draft order",
                                              options: [.foreground])
        let notMe = UNNotificationAction(identifier: notMeAction, title: "This wasn\u{2019}t me",
                                         options: [.destructive, .authenticationRequired])
        let reconnect = UNNotificationAction(identifier: reconnectAction, title: "Reconnect", options: [.foreground])
        let askLastNight = UNNotificationAction(identifier: askLastNightAction, title: "Ask about last night",
                                                options: [.foreground])
        return [
            UNNotificationCategory(identifier: scheduleCategory, actions: [review, sendSchedule],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: scheduleReviewCategory, actions: [review],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: reviewCategory, actions: [reply, openReview],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: reviewDraftedCategory, actions: [approvePost, writeOwn, edit],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: recCategory, actions: [done, notForUs],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: recAskCategory, actions: [done, notForUs, ask],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: publishHeldCategory, actions: [sendNow, reviewWeek],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: stockCategory, actions: [draftOrder],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: loginCategory, actions: [notMe],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: connectionCategory, actions: [reconnect],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: dsrCategory, actions: [askLastNight],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: undoableCategory, actions: [undo, review],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: requestCategory, actions: [approve, deny],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: briefCategory, actions: [ask],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: issueCategory, actions: [],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: messageCategory, actions: [replyMessage],
                                   intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: lineupCategory, actions: [approveLineup, openLineup],
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
        /// The queued send an Undo stops — its Live Activity ends with it
        /// (F3-9).
        var cancelsActionId: Int? = nil
        /// An Approve & post: the answer says whether Google took it.
        var postsReply = false
        /// The JSON body for an action that carries more than a decision —
        /// a typed reply ({body}, or {recipient_id, body}) or the brief's
        /// day ({day, text: null}: approve the draft as written).
        var payload: PushActionBody? = nil
        /// What the call sends when it is more than `{}` or `{decision}` —
        /// every key the route reads (parity round rule).
        var body: ActionBody? = nil
        /// The typed reply, saved as the review's draft before the approve
        /// (`/reviews/<id>/save-draft`, #54).
        var savesDraft: SavedDraft? = nil
        /// Whether the action reaches someone outside the app (a reply, a
        /// staff decision, a send, signing a login out everywhere). An app
        /// passcode keeps those in the app (F3-13); an Undo or an answer to a
        /// recommendation is not one.
        var outward = true
        /// Said on a local notification when it worked — only where the
        /// owner needs to know what happened next ("check your email").
        var successTitle: String? = nil
    }

    struct SavedDraft: Equatable {
        let path: String
        let text: String
    }

    /// The JSON body of a lock-screen action, by route.
    enum ActionBody: Encodable, Equatable {
        /// `/reviews/<id>/approve` — the reply the owner saw (`expected_draft`):
        /// changed since, and nothing posts (409 draft_changed).
        case approve(expectedDraft: String)
        /// `/labor/publish-schedule` — the week and the blocker keys the push
        /// named (client_api._publish_schedule_request).
        case publish(scheduleId: Int, acknowledge: [String])
        /// `/recs/event` — Done or Not for us.
        case recAnswer(APIClient.RecEventBody)
        /// `/account/not-me` — the login the sign-in was.
        case notMe(loginUserId: Int)

        private enum Keys: String, CodingKey {
            case expectedDraft = "expected_draft"
            case scheduleId = "schedule_id"
            case acknowledge
            case loginUserId = "login_user_id"
        }

        func encode(to encoder: Encoder) throws {
            switch self {
            case .approve(let text):
                var c = encoder.container(keyedBy: Keys.self)
                try c.encode(text, forKey: .expectedDraft)
            case .publish(let id, let keys):
                var c = encoder.container(keyedBy: Keys.self)
                try c.encode(id, forKey: .scheduleId)
                try c.encode(keys, forKey: .acknowledge)
            case .recAnswer(let body):
                try body.encode(to: encoder)
            case .notMe(let id):
                var c = encoder.container(keyedBy: Keys.self)
                try c.encode(id, forKey: .loginUserId)
            }
        }
    }

    /// What an Approve & post answer means for the owner who pressed it from
    /// the lock screen (F3-3). The route answers 200 `{ok: true}` when the
    /// reply was APPROVED — `post_status` failed / not_connected / not_google
    /// when it didn't go live — so `ok` alone read as "posted" when nothing
    /// went live. Nil: it posted.
    nonisolated static func approvePostShortfall(_ outcome: ReviewPostOutcome) -> String? {
        guard let why = outcome.shortfall else { return nil }
        return "Approved, but not posted: \(why) Tap to open it."
    }

    /// The call a background action identifier makes for this payload, or
    /// nil when the payload doesn't carry what it needs (then the action
    /// just opens, like a tap).
    nonisolated static func backgroundAction(for actionIdentifier: String,
                                             cavnar: [String: Any],
                                             userText: String? = nil) -> BackgroundAction? {
        switch actionIdentifier {
        case replyMessageAction:
            // An empty reply sends nothing: the notification just opens.
            let text = (userText ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { return nil }
            let body = String(text.prefix(1000))
            let type = alertType(cavnar)
            if type == "team_message", let sender = reviewId(from: cavnar["sender_id"]) {
                return BackgroundAction(path: "/mobile/api/team/messages", decision: nil,
                                        failureTitle: "Couldn't send your reply",
                                        payload: PushActionBody(["recipient_id": .int(sender), "body": .string(body)]))
            }
            if let thread = reviewId(from: cavnar["thread_id"]) {
                return BackgroundAction(path: "/mobile/api/labor/inbox/threads/\(thread)/reply", decision: nil,
                                        failureTitle: "Couldn't send your reply", payload: PushActionBody(["body": .string(body)]))
            }
            return nil
        case approveLineupAction:
            guard let day = (cavnar["day"] as? String).flatMap({ DSRFormat.isISODate($0) ? $0 : nil }) else {
                return nil
            }
            return BackgroundAction(path: "/mobile/api/staff-brief/approve", decision: nil,
                                    failureTitle: "Couldn't approve the brief",
                                    payload: PushActionBody(["day": .string(day), "text": .null]))
        case approvePostAction:
            guard let id = reviewId(from: cavnar["review_id"]) else { return nil }
            // The reply the notification showed (push.py `draft`, #36), sent
            // back so a draft changed since is not posted unread — only when
            // the push carried it whole (`draft_complete`).
            var action = BackgroundAction(path: "/mobile/api/reviews/\(id)/approve", decision: nil,
                                          failureTitle: "Couldn't post that reply", postsReply: true)
            if let shown = shownDraft(cavnar) { action.body = .approve(expectedDraft: shown) }
            return action
        case replyTextAction:
            // A typed reply (#54): saved as the draft, then approved with
            // exactly that text — the web's save-then-approve, never a reply
            // the owner did not write. An empty reply does nothing but open.
            guard let id = reviewId(from: cavnar["review_id"]),
                  let text = userText?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty
            else { return nil }
            let reply = String(text.prefix(4000))
            return BackgroundAction(path: "/mobile/api/reviews/\(id)/approve", decision: nil,
                                    failureTitle: "Couldn't post your reply", postsReply: true,
                                    body: .approve(expectedDraft: reply),
                                    savesDraft: SavedDraft(path: "/mobile/api/reviews/\(id)/save-draft", text: reply))
        case recDoneAction, recNotForUsAction:
            // Done / Not for us on a recommendation the app may answer
            // (push.py `answerable` + `rec_key`, #35) — POST /recs/event,
            // the cards' own body.
            guard cavnar["answerable"] as? Bool == true || (cavnar["answerable"] as? NSNumber)?.boolValue == true,
                  let key = (cavnar["rec_key"] as? String)?.trimmingCharacters(in: .whitespaces), !key.isEmpty
            else { return nil }
            let answer: RecAnswer = actionIdentifier == recDoneAction ? .completed : .notForUs
            let body = APIClient.recEventBody(key: String(key.prefix(160)), answer: answer,
                                              surface: surface(cavnar) ?? "alert_push",
                                              module: recModule(cavnar))
            return BackgroundAction(path: "/mobile/api/recs/event", decision: nil,
                                    failureTitle: answer == .completed ? "Couldn't mark that done"
                                                                       : "Couldn't pass on that",
                                    body: .recAnswer(body), outward: false)
        case sendNowAction:
            // The held week, sent now (#55): the blocker keys the push named
            // are what the owner acknowledged by reading it
            // (delayed._tell_owner_schedule_held).
            guard let id = reviewId(from: cavnar["schedule_id"]),
                  let keys = cavnar["blocker_keys"] as? [Any] else { return nil }
            let ack = keys.compactMap { ($0 as? String) ?? ($0 as? NSNumber)?.stringValue }
            return BackgroundAction(path: "/mobile/api/labor/publish-schedule", decision: nil,
                                    failureTitle: "Couldn't send the week", body: .publish(scheduleId: id, acknowledge: ack))
        case notMeAction:
            guard let uid = reviewId(from: cavnar["login_user_id"]) else { return nil }
            return BackgroundAction(path: "/mobile/api/account/not-me", decision: nil,
                                    failureTitle: "Couldn't sign that login out", body: .notMe(loginUserId: uid),
                                    successTitle: "Signed out everywhere")
        case undoAction:
            guard let id = reviewId(from: cavnar["delayed_action_id"]) else { return nil }
            return BackgroundAction(path: "/mobile/api/actions/\(id)/cancel", decision: nil,
                                    failureTitle: "Couldn't undo that", cancelsActionId: id, outward: false)
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

    /// The drafted reply a push carried whole (push.py `draft` with
    /// `draft_complete`), or nil — a clipped one is never sent back as the
    /// text approved.
    nonisolated static func shownDraft(_ cavnar: [String: Any]) -> String? {
        let complete = (cavnar["draft_complete"] as? Bool) ?? (cavnar["draft_complete"] as? NSNumber)?.boolValue
        guard complete == true, let text = cavnar["draft"] as? String,
              !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return text
    }

    /// The ledger module a recommendation answer is filed under: the push's
    /// `module` in the ledger's own names (Food Cost is "food", Intel
    /// "intel"), as the module screens send it.
    nonisolated static func recModule(_ cavnar: [String: Any]) -> String {
        let raw = ((cavnar["module"] as? String) ?? "").lowercased()
        switch raw {
        case "inventory": return "food"
        case "competitor": return "intel"
        case "": return "home"
        default: return String(raw.prefix(20))
        }
    }

    /// The question "Ask about last night" sends — the night the report is
    /// for, in the owner's date form (M/D/YY).
    nonisolated static func lastNightQuestion(_ cavnar: [String: Any]) -> String {
        if let date = businessDate(cavnar) {
            return "Walk me through the daily report for \(CavnarDate.mdy(date))."
        }
        return "How did last night go?"
    }

    /// Where a foreground button opens when it is not the notification's own
    /// place (#55): Draft order the supplier order, Reconnect the
    /// integrations, Ask about last night Ask. Nil: the push's own `nav`.
    nonisolated static func foregroundNav(for actionIdentifier: String) -> String? {
        switch actionIdentifier {
        case draftOrderAction: return "inventory/order"
        case reconnectAction: return "account/integrations"
        case askLastNightAction: return "ask"
        default: return nil
        }
    }

    private struct DecisionBody: Encodable { let decision: String }
    private struct EmptyBody: Encodable {}
    private struct SaveDraftBody: Encodable { let draft: String }
    private struct SaveDraftResponse: Decodable { let ok: Bool; let error: String? }

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
        // An app passcode is the owner saying this phone is shared: the
        // device's own unlock (all `.authenticationRequired` asks for) is not
        // enough to post a reply or decide a request from the lock screen.
        // Undo — the safe direction — still works (F3-13).
        if Self.needsAppUnlock(action, passcodeSet: AppPasscode.isSet) {
            await postFailure(action.failureTitle, "Open Cavnar AI to do this.", userInfo: userInfo)
            return
        }
        // The server acts on the location this phone is signed into. An
        // alert about another location of the group can't be answered from
        // here without switching, so it says so instead of failing oddly.
        // The persisted id, not only this process's: a lock-screen button
        // can launch the app in the background with no session built yet,
        // where this process's id is still 0 and the guard used to be
        // skipped (F3-13).
        let active = await MainActor.run { SessionScope.activeRestaurantId }
        if let restaurantId, restaurantId > 0, active > 0, restaurantId != active {
            await postFailure(action.failureTitle,
                              "It's for another location — tap to open it there.", userInfo: userInfo)
            return
        }
        do {
            // A typed reply is the draft first; the approve below names it.
            if let save = action.savesDraft {
                let saved: SaveDraftResponse = try await APIClient.shared.sendWithBearer(
                    save.path, method: .post, body: SaveDraftBody(draft: save.text), bearer: token)
                guard saved.ok else {
                    await postFailure(action.failureTitle, saved.error ?? "Tap to open it.", userInfo: userInfo)
                    return
                }
            }
            let response: ReviewPostOutcome
            if let payload = action.payload {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: payload, bearer: token)
            } else if let body = action.body {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: body, bearer: token)
            } else if let decision = action.decision {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: DecisionBody(decision: decision), bearer: token)
            } else {
                response = try await APIClient.shared.sendWithBearer(
                    action.path, method: .post, body: EmptyBody(), bearer: token)
            }
            if !response.ok {
                await postFailure(action.failureTitle, response.error ?? "Tap to open it.", userInfo: userInfo)
            } else if action.postsReply,
                      let shortfall = approvePostShortfall(response) {
                await postFailure("Reply approved, not posted", shortfall, userInfo: userInfo)
            } else if let id = action.cancelsActionId {
                await MainActor.run {
                    PendingSendActivities.finish(actionId: id, status: "stopped", note: nil)
                }
            } else if let title = action.successTitle {
                await postNotice(title, "Nobody can sign in until the password is reset \u{2014} "
                                 + "check your email for the link.")
            }
        } catch let error as APIClient.APIError {
            await postFailure(action.failureTitle, error.message, userInfo: userInfo)
        } catch {
            await postFailure(action.failureTitle, "Tap to open it and try again.", userInfo: userInfo)
        }
    }

    /// Whether "Send to staff" may run from this payload: a week named, and
    /// the server's own one-tap verdict on it. The publish route checks again.
    nonisolated static func scheduleSendAllowed(_ cavnar: [String: Any]) -> Int? {
        guard let id = reviewId(from: cavnar["schedule_id"]) else { return nil }
        let safe = (cavnar["one_tap_safe"] as? Bool) ?? ((cavnar["one_tap_safe"] as? NSNumber)?.boolValue ?? false)
        return safe ? id : nil
    }

    /// Whether publish-check still finds the week safe for one tap — the
    /// rule LaborWaitingOnYou applies: unsent, not replaced, this login may
    /// send, nothing to read first, somebody to reach.
    nonisolated static func publishCheckAllowsOneTap(_ check: PublishCheck, scheduleId: Int) -> Bool {
        check.ok && check.scheduleId == scheduleId && check.publishedAt == nil && check.replacedReason == nil
            && check.canPublish && check.shown.lines.isEmpty && (check.reach?.total ?? 0) > 0
    }

    private struct ScheduleSendAnswer: Decodable {
        let ok: Bool
        let error: String?
        let queued: Bool?
        let undoMinutes: Int?
        let alreadyPublished: Bool?
        enum CodingKeys: String, CodingKey {
            case ok, error, queued
            case undoMinutes = "undo_minutes"
            case alreadyPublished = "already_published"
        }
    }

    /// "Send to staff" from the drafted push: publish-check, then publish,
    /// with the owner's stored session. Anything to read first, another
    /// location, an app passcode or a failure posts a local notification
    /// that opens the week instead — never a silent no.
    nonisolated static func performScheduleSend(cavnar: [String: Any], userInfo: [AnyHashable: Any],
                                                restaurantId: Int?) async {
        let title = "The schedule wasn\u{2019}t sent"
        guard let scheduleId = scheduleSendAllowed(cavnar) else {
            await postFailure(title, "It has something to read first \u{2014} tap to open the week.", userInfo: userInfo)
            return
        }
        guard let token = Keychain.get(Keychain.Key.sessionToken) else {
            await postFailure(title, "Open Cavnar AI and sign in to do this.", userInfo: userInfo)
            return
        }
        if AppPasscode.isSet {
            await postFailure(title, "Open Cavnar AI to do this.", userInfo: userInfo)
            return
        }
        let active = await MainActor.run { SessionScope.activeRestaurantId }
        if let restaurantId, restaurantId > 0, active > 0, restaurantId != active {
            await postFailure(title, "It's for another location \u{2014} tap to open it there.", userInfo: userInfo)
            return
        }
        do {
            let check: PublishCheck = try await APIClient.shared.sendWithBearer(
                "/mobile/api/labor/publish-check", query: ["schedule_id": String(scheduleId)], bearer: token)
            guard publishCheckAllowsOneTap(check, scheduleId: scheduleId) else {
                await postFailure(title, "Something changed since \u{2014} tap to read it before it goes out.",
                                  userInfo: userInfo)
                return
            }
            let sent: ScheduleSendAnswer = try await APIClient.shared.sendWithBearer(
                "/mobile/api/labor/publish-schedule", method: .post,
                body: PublishScheduleViewModel.PublishBody(scheduleId: scheduleId, acknowledge: false), bearer: token)
            guard sent.ok else {
                await postFailure(title, sent.error ?? "Tap to open the week.", userInfo: userInfo)
                return
            }
            let content = UNMutableNotificationContent()
            content.title = sent.alreadyPublished == true ? "Already sent" : "Schedule sent"
            content.body = sent.queued == true
                ? "Goes to staff in \(sent.undoMinutes ?? 0) min \u{2014} undo from Home."
                : (sent.alreadyPublished == true ? "Nothing went out twice." : "Your staff have the week.")
            content.userInfo = userInfo
            content.interruptionLevel = .passive
            try? await UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: "cavnar-schedule-sent-\(scheduleId)", content: content, trigger: nil))
        } catch let error as APIClient.APIError where error.status == 409 {
            await postFailure(title, "There is something to read first \u{2014} tap to open the week.", userInfo: userInfo)
        } catch let error as APIClient.APIError {
            await postFailure(title, error.message, userInfo: userInfo)
        } catch {
            await postFailure(title, "Tap to open the week and try again.", userInfo: userInfo)
        }
    }

    /// Whether a lock-screen action must wait for the app's own unlock:
    /// anything outward (post a reply, decide a request) while an app
    /// passcode is set. Undo stops a send and is always allowed.
    nonisolated static func needsAppUnlock(_ action: BackgroundAction, passcodeSet: Bool) -> Bool {
        passcodeSet && action.outward && action.cancelsActionId == nil
    }

    /// A plain notice after an action that worked and has a next step —
    /// carries no payload, so tapping it just opens the app.
    nonisolated private static func postNotice(_ title: String, _ body: String) async {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        let request = UNNotificationRequest(identifier: "cavnar-action-done-\(UUID().uuidString)",
                                            content: content, trigger: nil)
        try? await UNUserNotificationCenter.current().add(request)
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

    /// The app icon's number: what is still unread on the bell. The backend
    /// sends the unread count on every push (push.py `_badge_for`), and the
    /// bell sets it whenever it re-reads (NotificationsBadgeViewModel). It
    /// used to be zeroed whenever the list was read, while rows in it were
    /// still unread (parity audit #81).
    func setBadge(_ count: Int) {
        UNUserNotificationCenter.current().setBadgeCount(max(0, count))
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
        // A staff session files the token under the staff login, with the
        // staff bearer (C4). The owner route would refuse it, and an owner
        // token on the same phone must not claim a staff phone's pushes.
        if let staff = staffSession, staff.isAuthenticated {
            do {
                try await staff.registerDevice(apnsToken: pending.token, environment: pending.environment)
                registeredToken = pending.token
                pendingToken = nil
            } catch {
                // Stays queued for the next sign-in or foreground.
            }
            return
        }
        // No owner session on this phone (a staff phone between shifts):
        // the owner route would only answer "session expired". The token
        // waits for whichever sign-in comes next.
        guard Keychain.get(Keychain.Key.sessionToken) != nil else { return }
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

    // MARK: - Staff (C4)

    /// After every staff sign-in, and on a launch into a live staff session.
    /// Ending a staff session on the server removes this phone's staff push
    /// row (a PIN change or reset, a switch, a sign-out), so the token is
    /// filed again every time. Never shows the system prompt by itself: the
    /// staff app asks first, in one line (StaffNotificationAskCard), and
    /// "Turn on" calls `promptNow`. Returns where permission stands.
    @discardableResult
    func staffDidSignIn() async -> UNAuthorizationStatus {
        installAsNotificationDelegate()
        let status = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        authorizationDenied = status == .denied
        authorizationUndetermined = status == .notDetermined
        switch status {
        case .authorized, .provisional, .ephemeral:
            if pendingToken == nil, let registeredToken {
                pendingToken = (registeredToken, currentEnvironment)
            }
            UIApplication.shared.registerForRemoteNotifications()
            await flushPendingToken()
        default:
            break
        }
        return status
    }

    /// A staff sign-out has removed this phone's row on the server: the
    /// token waits for the next sign-in (staff or owner) to file it again.
    func staffDeviceReleased() {
        guard let token = registeredToken else { return }
        pendingToken = (token, currentEnvironment)
        registeredToken = nil
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
        // The bell's count moves with the banner, not at the next launch
        // (parity audit #81). Only Cavnar AI's own pushes; a local notice
        // this class posted carries no "cavnar" payload.
        if Self.cavnarPayload(notification.request.content.userInfo) != nil {
            await MainActor.run { self.onForegroundPush?() }
        }
        return [.banner, .sound, .list]
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
        let actionIdentifier = response.actionIdentifier
        // Draft order, Reconnect, Ask about last night open their own place
        // rather than the notification's (#55).
        let nav = Self.foregroundNav(for: actionIdentifier) ?? Self.nav(cavnar)
        let userText = (response as? UNTextInputNotificationResponse)?.userText
        // Dismissals are ignored rather than routed.
        guard actionIdentifier != UNNotificationDismissActionIdentifier else { return }
        // Send a drafted week to staff (iOS parity #16): the server's own
        // check, then its publish, in the background behind the unlock.
        if actionIdentifier == Self.sendScheduleAction {
            await Self.performScheduleSend(cavnar: cavnar, userInfo: userInfo, restaurantId: restaurantId)
            return
        }
        // Approve & post, a typed Reply, Undo, Approve / Deny, Done / Not for
        // us, Send now, This wasn't me: done here, in the background, and
        // nothing opens (friction audit #22, parity audit #35 #54 #55).
        if let action = Self.backgroundAction(for: actionIdentifier, cavnar: cavnar, userText: userText) {
            await Self.perform(action, userInfo: userInfo, restaurantId: restaurantId)
            return
        }
        // A staff notice (push.STAFF_ALERT_TYPES, module "staff") opens the
        // staff app on its tab and item — `tab` + id, or `nav`
        // "staff/<tab>[/<id>]" (C4). Held by StaffDeepLinkCenter until the
        // portal is on screen, so a tap that launches a signed-out staff
        // phone opens it right after the PIN.
        if StaffDeepLink.isStaffNotice(alertType: alertType, module: module),
           let staffLink = StaffDeepLink.from(cavnar: cavnar, alertType: alertType) {
            let handled = await MainActor.run { () -> Bool in
                guard self.routesToStaffApp() else { return false }
                StaffDeepLinkCenter.shared.post(staffLink)
                return true
            }
            if handled { return }
        }
        // Every other button (Open, Edit, Review, Ask about this) and the
        // tap itself open the notification's own place; "Ask about this"
        // and "Ask about last night" also send the question.
        let lastNight = actionIdentifier == Self.askLastNightAction
        let tap = Tap(alertType: alertType, reviewId: reviewId,
                      askPrompt: lastNight ? Self.lastNightQuestion(cavnar) : askPrompt, alertId: alertId,
                      recKey: recKey, module: module, restaurantId: restaurantId,
                      businessDate: businessDate, surface: surface, nav: nav,
                      askAutoSend: actionIdentifier == Self.askAction || lastNight)
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

/// The JSON body a lock-screen action posts beyond a decision — a typed
/// reply (`{body}` to a staff thread, `{recipient_id, body}` to a teammate)
/// or the lineup brief's `{day, text: null}` (approve the draft as written).
/// Sendable, so it crosses into the background task that posts it.
struct PushActionBody: Encodable, Equatable, Sendable {
    enum Value: Equatable, Sendable {
        case string(String), int(Int), null
    }

    let fields: [String: Value]

    init(_ fields: [String: Value]) { self.fields = fields }

    private struct Key: CodingKey {
        let stringValue: String
        init(stringValue: String) { self.stringValue = stringValue }
        var intValue: Int? { nil }
        init?(intValue: Int) { nil }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        for (k, v) in fields {
            switch v {
            case .string(let s): try c.encode(s, forKey: Key(stringValue: k))
            case .int(let n): try c.encode(n, forKey: Key(stringValue: k))
            case .null: try c.encodeNil(forKey: Key(stringValue: k))
            }
        }
    }
}
