import Foundation
import UserNotifications

/// The employee tier's session, kept deliberately separate from SessionStore.
///
/// Two stores rather than a `kind` flag on one, because the two tiers are two
/// products: an owner session carries a 30-day token, a restaurant, module
/// entitlements and the whole dashboard behind it; a staff session is a shift-
/// length PIN token that may only ever reach /staff/api/*. Keeping them apart
/// means there is no code path where a staff token can be mistaken for an
/// owner one — RootView picks a surface based on which store is populated,
/// and neither can silently become the other.
///
/// What this store owns (employee audit wave 2, I1):
/// - the session token, and every way it ends — sign-out (server logout and
///   push unregister, C2 / M4), an ended session on any staff 401 (C2), a
///   PIN change or reset, an account deletion;
/// - the device's memory: the restaurants it signs in at and, per
///   restaurant, the last person — so a staff phone opens on that person's
///   PIN pad (H10), and several restaurant codes are kept (M12);
/// - the shared-device idle lock: 15 minutes in the background and the PIN
///   pad comes back (M4);
/// - sign-in with its one-shot nonce (C3), signup with the token in a
///   header (M4 / SEC-14), forgot PIN by text (H10), the location switch
///   (M12), email and account deletion (C10).
@Observable
@MainActor
final class StaffSessionStore {
    private(set) var token: String?
    private(set) var profile: StaffProfile?
    var lastError: String?
    /// The last sign-in failed because the server refused the PIN (a
    /// refusal it answered), not a network or transport failure — what
    /// turns the PIN dots red (re-audit L16).
    private(set) var lastErrorWasPin = false
    /// One sentence for the sign-in screen the person lands on after the
    /// session ended without them asking: "Your shift session ended — sign
    /// in again.", "PIN changed — sign in with your new PIN.", the idle
    /// lock's line. Cleared by the next sign-in.
    var signInNotice: String?
    /// True after 15 minutes in the background with a live session: RootView
    /// shows this person's PIN pad instead of the portal until the PIN is
    /// entered again (or "Not you?" signs out).
    private(set) var isLocked = false
    /// Raised after a sign-in on a phone that has never been asked about
    /// notifications. RootView shows the one-line ask over the portal.
    var notificationAsk = false
    /// Moves on every sign-in, so a portal built for the previous session
    /// (another location, another person) is rebuilt, not reused.
    private(set) var sessionGeneration = 0

    var isAuthenticated: Bool { token != nil }

    /// A phone that has signed in to the staff app before: it opens on the
    /// staff PIN pad, not the owner's sign-in (H10).
    var isStaffDevice: Bool { !(portalToken ?? "").isEmpty }

    private let client: APIClient
    private let defaults: UserDefaults

    init(client: APIClient = .shared, storedToken: String?? = nil, defaults: UserDefaults = .standard) {
        self.client = client
        self.defaults = defaults
        // A fresh install inherits the Keychain from the last one (Keychain
        // items outlive the app), so a reinstalled phone woke up signed in
        // as whoever used it last — or stuck on a dead token (C2). The
        // device memory below lives in UserDefaults, which an uninstall
        // does clear: no memory and no install mark means a new install.
        if storedToken == nil { Self.clearInheritedTokenOnFreshInstall(defaults) }
        self.token = storedToken ?? Keychain.get(Keychain.Key.staffSessionToken)
        self.currentCode = defaults.string(forKey: Self.portalKey)
        self.savedLocations = Self.loadLocations(defaults)
        // A process that died in the background still owes the idle lock.
        if token != nil, let away = defaults.object(forKey: Self.backgroundedKey) as? Date,
           Self.shouldLock(backgroundedAt: away, now: Date()) {
            isLocked = true
            signInNotice = Self.idleLockNotice
        }
    }

    static func clearInheritedTokenOnFreshInstall(_ defaults: UserDefaults) {
        guard !defaults.bool(forKey: installedKey) else { return }
        // An install from before this mark existed still has its staff code
        // in UserDefaults: an upgrade, not a reinstall — keep its session.
        if defaults.string(forKey: portalKey) == nil {
            Keychain.delete(Keychain.Key.staffSessionToken)
        }
        defaults.set(true, forKey: installedKey)
    }

    // MARK: - The device's restaurants and people

    /// The restaurant code this device signs in with now — the 6-character
    /// join code or the long portal token; the server reads both. Not a
    /// secret (every read still needs a PIN session), so UserDefaults.
    private var currentCode: String?

    var portalToken: String? {
        get { currentCode }
        set {
            currentCode = newValue
            if let newValue, !newValue.isEmpty {
                defaults.set(newValue, forKey: Self.portalKey)
            } else {
                defaults.removeObject(forKey: Self.portalKey)
            }
        }
    }

    /// Every restaurant this phone has signed in at, most recent first, each
    /// with the last person who did (M12: several codes, not one).
    private(set) var savedLocations: [StaffSavedLocation] = []

    /// The person whose PIN pad this phone opens on for `code`.
    func lastPerson(for code: String?) -> StaffRosterEntry? {
        guard let code, let saved = savedLocations.first(where: { $0.code == code }),
              let id = saved.membershipID, let name = saved.employeeName, !name.isEmpty else { return nil }
        return StaffRosterEntry(membershipID: id, name: name)
    }

    /// The restaurant name remembered for `code`, for the PIN pad's kicker
    /// before the roster has answered.
    func restaurantName(for code: String?) -> String? {
        guard let code else { return nil }
        let name = savedLocations.first(where: { $0.code == code })?.restaurant
        return (name?.isEmpty == false) ? name : nil
    }

    /// "Not you?" — this phone stops opening on that person's pad.
    func forgetPerson(for code: String?) {
        guard let code, let i = savedLocations.firstIndex(where: { $0.code == code }) else { return }
        savedLocations[i].membershipID = nil
        savedLocations[i].employeeName = nil
        persistLocations()
    }

    /// A code the server no longer recognises (404): it leaves the list and,
    /// when it was the current one, the device (M10).
    func forgetCode(_ code: String) {
        savedLocations.removeAll { $0.code == code }
        persistLocations()
        if portalToken == code { portalToken = savedLocations.first?.code }
    }

    /// Remembers a restaurant (and who signed in there) at the front of the
    /// list. One entry per restaurant: matched by its id when both sides
    /// know it, else by code, else by name.
    func remember(code: String, restaurant: String?, restaurantID: Int? = nil,
                  membershipID: Int? = nil, employeeName: String? = nil) {
        let updated = Self.merged(savedLocations, code: code, restaurant: restaurant, restaurantID: restaurantID,
                                  membershipID: membershipID, employeeName: employeeName)
        savedLocations = updated
        persistLocations()
    }

    /// Pure, so the dedupe rule is unit-tested.
    nonisolated static func merged(_ list: [StaffSavedLocation], code: String, restaurant: String?, restaurantID: Int?,
                       membershipID: Int?, employeeName: String?) -> [StaffSavedLocation] {
        var list = list
        let name = (restaurant ?? "").trimmingCharacters(in: .whitespaces)
        let match = list.firstIndex { entry in
            if let restaurantID, let known = entry.restaurantID { return known == restaurantID }
            if entry.code == code { return true }
            return !name.isEmpty && entry.restaurant.caseInsensitiveCompare(name) == .orderedSame
        }
        var entry = match.map { list.remove(at: $0) }
            ?? StaffSavedLocation(code: code, restaurant: name, restaurantID: restaurantID)
        entry.code = code
        if !name.isEmpty { entry.restaurant = name }
        if let restaurantID { entry.restaurantID = restaurantID }
        if let membershipID, let employeeName, !employeeName.isEmpty {
            entry.membershipID = membershipID
            entry.employeeName = employeeName
        }
        list.insert(entry, at: 0)
        return Array(list.prefix(maxSavedLocations))
    }

    nonisolated static let maxSavedLocations = 8

    private func persistLocations() {
        if let data = try? JSONEncoder().encode(savedLocations) {
            defaults.set(data, forKey: Self.locationsKey)
        }
    }

    private static func loadLocations(_ defaults: UserDefaults) -> [StaffSavedLocation] {
        guard let data = defaults.data(forKey: locationsKey),
              let list = try? JSONDecoder().decode([StaffSavedLocation].self, from: data) else { return [] }
        return list
    }

    private static let portalKey = "cavnar.staff_portal_token"
    private static let locationsKey = "cavnar.staff.locations"
    private static let installedKey = "cavnar.staff.installed"
    private static let backgroundedKey = "cavnar.staff.backgrounded_at"
    private static let askDeclinedKey = "cavnar.staff.notification_ask_declined_at"

    // MARK: - Shared-device idle lock (M4 / WF-25)

    nonisolated static let idleLockAfter: TimeInterval = 15 * 60
    static let idleLockNotice = "Locked after 15 minutes away. Enter your PIN to carry on."

    nonisolated static func shouldLock(backgroundedAt: Date, now: Date) -> Bool {
        now.timeIntervalSince(backgroundedAt) >= idleLockAfter
    }

    /// RootView, on `.background`.
    func noteBackgrounded(now: Date = Date()) {
        guard isAuthenticated, !isLocked else { return }
        defaults.set(now, forKey: Self.backgroundedKey)
    }

    /// RootView, on `.active`: back on the PIN pad after 15 minutes away.
    func lockIfIdle(now: Date = Date()) {
        defer { defaults.removeObject(forKey: Self.backgroundedKey) }
        guard isAuthenticated, !isLocked,
              let away = defaults.object(forKey: Self.backgroundedKey) as? Date,
              Self.shouldLock(backgroundedAt: away, now: now) else { return }
        isLocked = true
        signInNotice = Self.idleLockNotice
    }

    // MARK: - Sign-in (C3)

    func roster(portal: String) async throws -> StaffRosterResponse {
        let resp: StaffRosterResponse = try await client.sendUnauthenticated(
            "/staff/api/roster/\(Self.pathComponent(portal))")
        loginNonce = resp.loginNonce
        if let name = resp.restaurant, !name.isEmpty, savedLocations.contains(where: { $0.code == portal }) {
            remember(code: portal, restaurant: name)
        }
        return resp
    }

    /// The nonce most recently handed out by the roster (or a switch), spent
    /// by the next sign-in. Held here so the login screen never has to know
    /// the replay protection exists.
    private var loginNonce: String?

    /// A fresh nonce, fetched when a name is tapped: the one from the roster
    /// may have been spent by a mistyped PIN or aged out while the phone sat
    /// on the name screen (C3 / WF-02).
    func refreshNonce(portal: String) async {
        if let resp: StaffRosterResponse = try? await client.sendUnauthenticated(
            "/staff/api/roster/\(Self.pathComponent(portal))") {
            loginNonce = resp.loginNonce
        }
    }

    /// The switch hands back the other location's nonce with its PIN pad.
    func useNonce(_ nonce: String?) {
        if let nonce, !nonce.isEmpty { loginNonce = nonce }
    }

    func signIn(portal: String, membershipID: Int, pin: String,
                name: String? = nil, restaurant: String? = nil) async -> Bool {
        lastError = nil
        lastErrorWasPin = false
        if loginNonce == nil { await refreshNonce(portal: portal) }
        return await attemptSignIn(portal: portal, membershipID: membershipID, pin: pin, name: name,
                                   restaurant: restaurant, retryOnStaleNonce: true)
    }

    private func attemptSignIn(portal: String, membershipID: Int, pin: String, name: String?,
                               restaurant: String?, retryOnStaleNonce: Bool) async -> Bool {
        let body = StaffLoginBody(membershipID: membershipID, pin: pin,
                                  deviceID: Keychain.deviceIdentity(), nonce: loginNonce ?? "")
        let resp: StaffLoginResponse
        do {
            resp = try await client.sendUnauthenticated(
                "/staff/r/\(Self.pathComponent(portal))/login", method: .post, body: body)
        } catch let error as APIClient.APIError {
            // A refusal says more than its sentence: a wrong PIN carries a
            // fresh nonce for the next try, a stale nonce says so (C3). The
            // transport throws on every non-2xx; the body is read here.
            guard let refusal = error.decodeBody(StaffLoginResponse.self) else {
                lastError = error.message
                return false
            }
            resp = refusal
        } catch {
            lastError = "Couldn't reach the server — check your connection and try again."
            return false
        }
        if let fresh = resp.loginNonce, !fresh.isEmpty { loginNonce = fresh }
        guard resp.ok, let token = resp.token else {
            // A stale nonce is the app's problem, not the employee's: fetch
            // a fresh one and send the same PIN once more.
            if resp.nonceExpired == true, retryOnStaleNonce {
                loginNonce = nil
                await refreshNonce(portal: portal)
                return await attemptSignIn(portal: portal, membershipID: membershipID, pin: pin, name: name,
                                           restaurant: restaurant, retryOnStaleNonce: false)
            }
            if resp.nonceExpired == true { loginNonce = nil }
            lastError = resp.error ?? "That PIN didn't match."
            lastErrorWasPin = resp.nonceExpired != true
            return false
        }
        didSignIn(token: token, code: portal, restaurant: restaurant, membershipID: membershipID, name: name)
        return true
    }

    /// Every way in ends here: the token, the device's memory, the lock and
    /// the notice. Push registration and the person's own details follow
    /// from RootView (`refreshAfterSignIn`), once the portal is on screen.
    private func didSignIn(token: String, code: String, restaurant: String?, restaurantID: Int? = nil,
                           membershipID: Int?, name: String?) {
        Keychain.set(token, for: Keychain.Key.staffSessionToken)
        self.token = token
        portalToken = code
        remember(code: code, restaurant: restaurant, restaurantID: restaurantID,
                 membershipID: membershipID, employeeName: name)
        loginNonce = nil
        isLocked = false
        signInNotice = nil
        lastError = nil
        defaults.removeObject(forKey: Self.backgroundedKey)
        sessionGeneration += 1
    }

    /// After a sign-in (and on a cold launch into a live session): the
    /// person's own details for the device's memory, then push — permission
    /// with a one-line ask, the token filed under the staff login (C4).
    func refreshAfterSignIn() async {
        if let details = try? await loadAccount(), let code = portalToken {
            remember(code: code, restaurant: details.restaurant, restaurantID: details.restaurantID,
                     membershipID: details.membershipID, employeeName: details.name)
        }
        guard isAuthenticated else { return }
        let status = await PushManager.shared.staffDidSignIn()
        notificationAsk = status == .notDetermined && !askDeclinedRecently()
    }

    // MARK: - Notifications ask (C4)

    /// "Turn on" on the one-line ask: the system prompt, then the token is
    /// filed under this login (PushManager routes it to the staff route).
    func enableNotifications() async {
        notificationAsk = false
        _ = await PushManager.shared.promptNow()
    }

    /// "Not now": asked again after a week, never on every sign-in.
    func declineNotifications(now: Date = Date()) {
        notificationAsk = false
        defaults.set(now, forKey: Self.askDeclinedKey)
    }

    private func askDeclinedRecently(now: Date = Date()) -> Bool {
        guard let at = defaults.object(forKey: Self.askDeclinedKey) as? Date else { return false }
        return now.timeIntervalSince(at) < 7 * 24 * 3600
    }

    /// POST /staff/api/device-tokens with this session's bearer — never the
    /// owner route (C4). PushManager calls it from `flushPendingToken`.
    func registerDevice(apnsToken: String, environment: String) async throws {
        let _: APIClient.OKResponse = try await authed(
            "/staff/api/device-tokens", method: .post,
            body: StaffDeviceTokenBody(apnsToken: apnsToken, environment: environment))
    }

    // MARK: - Signing out (C2, M4)

    /// Sign out from anywhere, whatever the screen managed to load. The phone
    /// is signed out at once; the server is told in the background — this
    /// phone's push row removed (DELETE /staff/api/device-tokens), then the
    /// session ended (POST /staff/api/logout) — and a failure there never
    /// keeps anyone signed in. The device keeps its restaurant and last
    /// person, so the next sign-in is the PIN pad.
    /// Runs on an explicit sign-out — Sign out, "Not you?", a deleted
    /// account — and never on an ended session or the idle lock, so a
    /// person's cached screens leave the phone only when they leave it.
    /// RootView wires the portal's cache purge here (I2's
    /// `StaffCache.purgeAll()`).
    var onExplicitSignOut: (() -> Void)?

    func signOut() {
        let bearer = token
        clearLocal(notice: nil)
        onExplicitSignOut?()
        guard let bearer else { return }
        let client = self.client
        Task {
            let apns = PushManager.shared.registeredToken
            if let apns, !apns.isEmpty {
                let _: APIClient.OKResponse? = try? await client.sendWithBearer(
                    "/staff/api/device-tokens/\(Self.pathComponent(apns))", method: .delete, bearer: bearer)
            }
            let _: APIClient.OKResponse? = try? await client.sendWithBearer(
                "/staff/api/logout", method: .post, body: StaffLogoutBody(apnsToken: apns), bearer: bearer)
            PushManager.shared.staffDeviceReleased()
        }
    }

    /// "Not you?" on a locked pad or the remembered person's pad: sign out
    /// and stop opening on this person.
    func notMe() {
        forgetPerson(for: portalToken)
        if isAuthenticated {
            signOut()
        } else {
            onExplicitSignOut?()
        }
    }

    /// The session ended on the server (expired, PIN reset, deactivated).
    /// Only when it is still this store's session: a slow request from the
    /// previous sign-in must not end the next one. Not an explicit sign-out:
    /// the person lands on their PIN pad with the server's sentence and
    /// their cached screens stay. Transports outside `authed` (the tasks
    /// routes' StaffTasksAPI) call this on a SessionExpiredError.
    func sessionEnded(sentToken: String?, message: String?) {
        guard sentToken != nil, sentToken == token else { return }
        clearLocal(notice: message ?? "Your shift session ended — sign in again.")
    }

    private func clearLocal(notice: String?) {
        Keychain.delete(Keychain.Key.staffSessionToken)
        token = nil
        profile = nil
        isLocked = false
        notificationAsk = false
        loginNonce = nil
        signInNotice = notice
        defaults.removeObject(forKey: Self.backgroundedKey)
    }

    /// Every authenticated staff call goes through here so the bearer token is
    /// attached in exactly one place — the staff token is never handed to
    /// APIClient's shared owner token slot, which is what keeps it from
    /// reaching an owner endpoint even by accident. Any 401 on /staff/api is
    /// an ended session and signs this store out (C2); `wrongCredential401`
    /// is for Change PIN, whose 401 is a wrong current PIN.
    ///
    /// A query string travels as `query:`, never inside `path`: the path is
    /// appended as a path component, which escapes "?" (a 404 on the
    /// server). `headers` carries values a route reads from a header (the
    /// tasks version, `X-Staff-Tasks-Version`).
    func authed<Response: Decodable>(_ path: String,
                                     method: APIClient.HTTPMethod = .get,
                                     body: (any Encodable)? = nil,
                                     query: [String: String] = [:],
                                     headers: [String: String] = [:],
                                     wrongCredential401: Bool = false) async throws -> Response {
        guard let token else { throw APIClient.SessionExpiredError(message: "Sign in again.") }
        do {
            return try await client.sendWithBearer(path, method: method, body: body, query: query,
                                                   headers: headers, bearer: token,
                                                   wrongCredential401: wrongCredential401)
        } catch let error as APIClient.SessionExpiredError {
            sessionEnded(sentToken: token, message: error.message)
            throw error
        }
    }

    // MARK: - Account (C10, M12, H10)

    /// GET /staff/api/me — the person's own details and their locations.
    func loadAccount() async throws -> StaffAccountDetails {
        let resp: StaffAccountResponse = try await authed("/staff/api/me")
        guard resp.ok, let employee = resp.employee else {
            throw APIClient.APIError(message: resp.error ?? "Couldn't load your account.")
        }
        return employee
    }

    /// POST /staff/api/me/email — "" clears it. Returns what was saved.
    func saveEmail(_ email: String) async throws -> String {
        let resp: StaffEmailResponse = try await authed("/staff/api/me/email", method: .post,
                                                        body: ["email": email])
        guard resp.ok else { throw APIClient.APIError(message: resp.error ?? "Couldn't save that email.") }
        return resp.email ?? email
    }

    /// Change PIN (UX-23): the current PIN, then the new one. On success the
    /// server has ended this session (and this phone's push row with it),
    /// so the person lands on their own PIN pad with the sentence that
    /// says what to do.
    func changePin(current: String, new: String) async -> StaffChangePinOutcome {
        let sentToken = token
        do {
            let resp: StaffChangePinResponse = try await authed(
                "/staff/api/pin", method: .post, body: ["current_pin": current, "new_pin": new],
                wrongCredential401: true)
            guard resp.ok else { return .refused(resp.error ?? "Couldn't change your PIN.") }
            clearLocal(notice: Self.pinChangedNotice)
            return .changed
        } catch let error as APIClient.APIError {
            let refusal = error.decodeBody(StaffChangePinResponse.self)
            if refusal?.signedOut == true {
                sessionEnded(sentToken: sentToken, message: refusal?.error ?? error.message)
                return .signedOut(refusal?.error ?? error.message)
            }
            if error.status == 401 { return .wrongCurrent(refusal?.error ?? error.message) }
            return .refused(refusal?.error ?? error.message)
        } catch let error as APIClient.SessionExpiredError {
            return .signedOut(error.errorDescription ?? "Sign in again.")
        } catch {
            return .refused("Couldn't reach the server — check your connection and try again.")
        }
    }

    static let pinChangedNotice = "PIN changed — sign in with your new PIN."

    /// Delete my account here (C10). Nil on success; otherwise the reason.
    func deleteAccount() async -> String? {
        do {
            let resp: StaffDeleteResponse = try await authed("/staff/api/account/delete", method: .post,
                                                             body: ["confirm": true])
            guard resp.ok else { return resp.error ?? "Couldn't delete your account." }
            forgetPerson(for: portalToken)
            clearLocal(notice: "Your account here is deleted. Your manager has been told.")
            onExplicitSignOut?()
            return nil
        } catch let error as APIClient.APIError {
            return error.message
        } catch let error as APIClient.SessionExpiredError {
            return error.errorDescription
        } catch {
            return "Couldn't reach the server — check your connection and try again."
        }
    }

    /// POST /staff/api/switch — what the other location's PIN pad needs.
    /// Never a session: that location's PIN signs in there (M12).
    func switchTarget(restaurantID: Int) async throws -> StaffSwitchResponse {
        let resp: StaffSwitchResponse = try await authed("/staff/api/switch", method: .post,
                                                         body: ["restaurant_id": restaurantID])
        guard resp.ok else { throw APIClient.APIError(message: resp.error ?? "Couldn't open that location.") }
        useNonce(resp.loginNonce)
        return resp
    }

    // MARK: - Forgot PIN by text (H10)

    func forgotStart(code: String, phone: String) async throws -> StaffForgotStartResponse {
        try await client.sendUnauthenticated("/staff/api/pin/forgot/start", method: .post,
                                             body: ["join_code": code, "phone": phone])
    }

    func forgotVerify(phone: String, code: String) async throws -> StaffForgotVerifyResponse {
        let resp: StaffForgotVerifyResponse = try await client.sendUnauthenticated(
            "/staff/api/pin/forgot/verify", method: .post, body: ["phone": phone, "code": code])
        guard resp.ok, resp.resetToken != nil else {
            throw APIClient.APIError(message: resp.error ?? "That code didn't match or has expired. Ask for a new one.")
        }
        return resp
    }

    /// Sets the new PIN and signs in with it. `.expired` sends the person
    /// back to the start of the reset.
    func forgotSet(resetToken: String, pin: String, fallbackCode: String?) async -> StaffForgotSetOutcome {
        do {
            let resp: StaffForgotSetResponse = try await client.sendUnauthenticated(
                "/staff/api/pin/forgot/set", method: .post,
                body: StaffForgotSetBody(resetToken: resetToken, pin: pin, deviceID: Keychain.deviceIdentity()))
            guard resp.ok, let token = resp.token else {
                return .refused(resp.error ?? "Couldn't set that PIN.")
            }
            let code = (resp.portalToken?.isEmpty == false ? resp.portalToken : nil) ?? fallbackCode ?? ""
            didSignIn(token: token, code: code, restaurant: resp.restaurant,
                      membershipID: resp.membershipID, name: resp.employeeName)
            return .signedIn
        } catch let error as APIClient.APIError {
            let refusal = error.decodeBody(StaffForgotSetResponse.self)
            if refusal?.resetExpired == true { return .expired(refusal?.error ?? error.message) }
            return .refused(refusal?.error ?? error.message)
        } catch {
            return .refused("Couldn't reach the server — check your connection and try again.")
        }
    }

    // MARK: - Self-signup

    /// Verified between `verifySignupCode` and `claim`. Held here rather than
    /// passed through five screens, so the signup views only ever deal with
    /// what the person is looking at.
    private var signupToken: String?

    /// optin must be true — this is the A2P 10DLC consent Twilio's campaign
    /// review requires. The server refuses the send without it; the
    /// unchecked-by-default box on StaffSignupView is what makes this call
    /// honest rather than a formality.
    func startSignup(phone: String, optin: Bool) async throws -> StaffSignupStartResponse {
        try await client.sendUnauthenticated("/staff/api/signup/start", method: .post,
                                             body: StaffSignupStartBody(phone: phone, optin: optin))
    }

    private struct StaffSignupStartBody: Encodable {
        let phone: String
        let optin: Bool
    }

    func verifySignupCode(phone: String, code: String) async throws -> Bool {
        let resp: StaffSignupVerifyResponse = try await client.sendUnauthenticated(
            "/staff/api/signup/verify", method: .post, body: ["phone": phone, "code": code])
        guard resp.ok, let token = resp.signupToken else {
            throw APIClient.APIError(message: resp.error ?? "That code didn't match.")
        }
        signupToken = token
        return true
    }

    func restaurantFor(joinCode: String) async throws -> String {
        let resp: StaffRestaurantLookup = try await client.sendUnauthenticated(
            "/staff/api/signup/where/\(Self.pathComponent(joinCode))")
        guard resp.ok, let name = resp.restaurant else {
            throw APIClient.APIError(message: resp.error ?? "We don't recognise that code.")
        }
        return name
    }

    /// The token travels in `X-Signup-Token`, never the URL: a query string
    /// reaches proxy and edge logs (M4 / SEC-14).
    func claimableNames(joinCode: String) async throws -> StaffClaimableResponse {
        guard let signupToken else { throw StaffSignupExpiredError() }
        do {
            return try await client.sendUnauthenticated(
                "/staff/api/signup/claimable/\(Self.pathComponent(joinCode))",
                headers: ["X-Signup-Token": signupToken])
        } catch let error as APIClient.APIError where error.status == 401 {
            self.signupToken = nil
            throw StaffSignupExpiredError()
        }
    }

    /// The last step: creates the account, sets the PIN, and signs in.
    func claim(joinCode: String, employeeName: String, pin: String,
               restaurant: String? = nil) async -> StaffClaimOutcome {
        lastError = nil
        guard let signupToken else { return .expired(StaffSignupExpiredError.message) }
        do {
            let resp: StaffClaimResponse = try await client.sendUnauthenticated(
                "/staff/api/signup/claim", method: .post,
                body: StaffClaimBody(joinCode: joinCode, employeeName: employeeName, pin: pin,
                                     deviceID: Keychain.deviceIdentity()),
                headers: ["X-Signup-Token": signupToken])
            guard resp.ok, let token = resp.token else {
                return .failed(resp.error ?? "Couldn't finish setting up your account.")
            }
            // The join code is remembered as this device's restaurant code,
            // so the next sign-in lands on this person's PIN pad. The
            // membership id arrives with /staff/api/me (refreshAfterSignIn).
            self.signupToken = nil
            didSignIn(token: token, code: joinCode, restaurant: restaurant, membershipID: nil,
                      name: resp.employeeName ?? employeeName)
            return .signedIn
        } catch let error as APIClient.APIError {
            let refusal = error.decodeBody(StaffClaimRefusal.self)
            if error.status == 409 || refusal?.hasAccount == true {
                // This phone already has a login here: the way back in is
                // Forgot PIN, never a second claim (C9 / LG-14).
                return .hasAccount(name: refusal?.employeeName, message: refusal?.error ?? error.message)
            }
            if Self.isExpiredSignup(error.message) {
                self.signupToken = nil
                return .expired(error.message)
            }
            return .failed(error.message)
        } catch {
            return .failed("Couldn't reach the server — check your connection and try again.")
        }
    }

    nonisolated static func isExpiredSignup(_ message: String) -> Bool {
        let m = message.lowercased()
        return m.contains("signup expired") || m.contains("verify your phone first")
    }

    /// A code or token as one URL path component.
    nonisolated static func pathComponent(_ raw: String) -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#")
        return trimmed.addingPercentEncoding(withAllowedCharacters: allowed) ?? trimmed
    }

    // MARK: - Bodies

    private struct StaffClaimBody: Encodable {
        let joinCode: String
        let employeeName: String
        let pin: String
        let deviceID: String?

        enum CodingKeys: String, CodingKey {
            case pin
            case joinCode = "join_code"
            case employeeName = "employee_name"
            case deviceID = "device_id"
        }
    }

    private struct StaffLoginBody: Encodable {
        let membershipID: Int
        let pin: String
        let deviceID: String?
        let nonce: String

        enum CodingKeys: String, CodingKey {
            case membershipID = "membership_id"
            case pin
            case deviceID = "device_id"
            case nonce
        }
    }

    private struct StaffForgotSetBody: Encodable {
        let resetToken: String
        let pin: String
        let deviceID: String

        enum CodingKeys: String, CodingKey {
            case pin
            case resetToken = "reset_token"
            case deviceID = "device_id"
        }
    }

    private struct StaffDeviceTokenBody: Encodable {
        let apnsToken: String
        let environment: String
        enum CodingKeys: String, CodingKey {
            case apnsToken = "apns_token"
            case environment
        }
    }

    private struct StaffLogoutBody: Encodable {
        let apnsToken: String?
        enum CodingKeys: String, CodingKey { case apnsToken = "apns_token" }
    }
}

// MARK: - The device's memory

/// One restaurant this phone signs in at, and who signed in there last.
struct StaffSavedLocation: Codable, Hashable, Identifiable {
    var code: String
    var restaurant: String
    var restaurantID: Int?
    var membershipID: Int?
    var employeeName: String?

    var id: String { code }

    init(code: String, restaurant: String, restaurantID: Int? = nil,
         membershipID: Int? = nil, employeeName: String? = nil) {
        self.code = code
        self.restaurant = restaurant
        self.restaurantID = restaurantID
        self.membershipID = membershipID
        self.employeeName = employeeName
    }
}

// MARK: - Outcomes

enum StaffChangePinOutcome: Equatable {
    case changed
    case wrongCurrent(String)
    case signedOut(String)
    case refused(String)
}

enum StaffForgotSetOutcome: Equatable {
    case signedIn
    case expired(String)
    case refused(String)
}

enum StaffClaimOutcome: Equatable {
    case signedIn
    /// 409: this phone already has a login here — Forgot PIN.
    case hasAccount(name: String?, message: String)
    /// The verified phone ran out (30 minutes): start again, number kept.
    case expired(String)
    case failed(String)
}

struct StaffSignupExpiredError: Error, LocalizedError {
    static let message = "That signup expired. Start again."
    var errorDescription: String? { Self.message }
}

// MARK: - Payloads (staff_account_routes.py, staff_device_routes.py)
//
// The account half of /staff/api/me and the routes beside it. Shared with
// the Me tab (I3) and the portal (I2): decode these rather than declaring a
// second copy.

/// One location this person works at (`employee.locations` on /staff/api/me).
struct StaffAccountLocation: Decodable, Hashable, Identifiable {
    let restaurantID: Int
    let restaurant: String
    let membershipID: Int?
    let employeeName: String?
    let portalToken: String?
    let joinCode: String?
    let current: Bool

    var id: Int { restaurantID }

    enum CodingKeys: String, CodingKey {
        case restaurant, current
        case restaurantID = "restaurant_id"
        case membershipID = "membership_id"
        case employeeName = "employee_name"
        case portalToken = "portal_token"
        case joinCode = "join_code"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        restaurantID = try c.decode(Int.self, forKey: .restaurantID)
        restaurant = (try? c.decodeIfPresent(String.self, forKey: .restaurant)) ?? ""
        membershipID = try? c.decodeIfPresent(Int.self, forKey: .membershipID)
        employeeName = try? c.decodeIfPresent(String.self, forKey: .employeeName)
        portalToken = try? c.decodeIfPresent(String.self, forKey: .portalToken)
        joinCode = try? c.decodeIfPresent(String.self, forKey: .joinCode)
        current = (try? c.decodeIfPresent(Bool.self, forKey: .current)) ?? false
    }
}

/// How the person is reached (`employee.notifications`).
struct StaffAccountChannels: Decodable, Hashable {
    let push: Bool
    let texts: Bool
    let textsConsent: Bool
    let email: Bool

    enum CodingKeys: String, CodingKey {
        case push, texts, email
        case textsConsent = "texts_consent"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        push = (try? c.decodeIfPresent(Bool.self, forKey: .push)) ?? false
        texts = (try? c.decodeIfPresent(Bool.self, forKey: .texts)) ?? false
        textsConsent = (try? c.decodeIfPresent(Bool.self, forKey: .textsConsent)) ?? false
        email = (try? c.decodeIfPresent(Bool.self, forKey: .email)) ?? false
    }
}

/// /staff/api/me's `employee`, the account fields. Every field degrades on
/// its own — the server says each part "degrades to its empty value on its
/// own error, so the profile always loads", and so does this.
struct StaffAccountDetails: Decodable, Hashable {
    let name: String
    let role: String?
    let restaurant: String
    let restaurantID: Int?
    let membershipID: Int?
    let hasPin: Bool
    let phoneMasked: String
    let hasPhone: Bool
    let email: String
    let notifications: StaffAccountChannels?
    let locations: [StaffAccountLocation]

    enum CodingKeys: String, CodingKey {
        case name, role, restaurant, email, notifications, locations
        case restaurantID = "restaurant_id"
        case membershipID = "membership_id"
        case hasPin = "has_pin"
        case phoneMasked = "phone_masked"
        case hasPhone = "has_phone"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = (try? c.decodeIfPresent(String.self, forKey: .name)) ?? ""
        role = try? c.decodeIfPresent(String.self, forKey: .role)
        restaurant = (try? c.decodeIfPresent(String.self, forKey: .restaurant)) ?? ""
        restaurantID = try? c.decodeIfPresent(Int.self, forKey: .restaurantID)
        membershipID = try? c.decodeIfPresent(Int.self, forKey: .membershipID)
        hasPin = (try? c.decodeIfPresent(Bool.self, forKey: .hasPin)) ?? false
        phoneMasked = (try? c.decodeIfPresent(String.self, forKey: .phoneMasked)) ?? ""
        hasPhone = (try? c.decodeIfPresent(Bool.self, forKey: .hasPhone)) ?? false
        email = (try? c.decodeIfPresent(String.self, forKey: .email)) ?? ""
        notifications = try? c.decodeIfPresent(StaffAccountChannels.self, forKey: .notifications)
        // One odd location never drops the rest.
        if var list = try? c.nestedUnkeyedContainer(forKey: .locations) {
            var out: [StaffAccountLocation] = []
            while !list.isAtEnd {
                if let loc = try? list.decode(StaffAccountLocation.self) {
                    out.append(loc)
                } else {
                    _ = try? list.decode(StaffSkip.self)
                }
            }
            locations = out
        } else {
            locations = []
        }
    }

    /// Whether the person works at more than one location.
    var hasOtherLocations: Bool { locations.contains { !$0.current } }
}

/// Consumes one element of an unkeyed container whatever its shape.
private struct StaffSkip: Decodable {
    init(from decoder: Decoder) throws {}
}

struct StaffAccountResponse: Decodable {
    let ok: Bool
    let error: String?
    let employee: StaffAccountDetails?
}

struct StaffEmailResponse: Decodable {
    let ok: Bool
    let error: String?
    let email: String?
}

struct StaffChangePinResponse: Decodable {
    let ok: Bool
    let error: String?
    let locked: Bool?
    let signedOut: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error, locked
        case signedOut = "signed_out"
    }
}

struct StaffDeleteResponse: Decodable {
    let ok: Bool
    let error: String?
    let deleted: Bool?
}

struct StaffSwitchResponse: Decodable {
    let ok: Bool
    let error: String?
    let requiresPin: Bool?
    let current: Bool?
    let restaurantID: Int?
    let restaurant: String?
    let membershipID: Int?
    let employeeName: String?
    let portalToken: String?
    let joinCode: String?
    let loginNonce: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, current, restaurant
        case requiresPin = "requires_pin"
        case restaurantID = "restaurant_id"
        case membershipID = "membership_id"
        case employeeName = "employee_name"
        case portalToken = "portal_token"
        case joinCode = "join_code"
        case loginNonce = "login_nonce"
    }
}

struct StaffForgotStartResponse: Decodable {
    let ok: Bool
    let error: String?
    let message: String?
    /// Only from a local dev backend with STAFF_SIGNUP_DEV_CODE=1 and no Twilio.
    let devCode: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, message
        case devCode = "dev_code"
    }
}

struct StaffForgotVerifyResponse: Decodable {
    let ok: Bool
    let error: String?
    let resetToken: String?
    let employeeName: String?
    let restaurant: String?

    enum CodingKeys: String, CodingKey {
        case ok, error, restaurant
        case resetToken = "reset_token"
        case employeeName = "employee_name"
    }
}

struct StaffForgotSetResponse: Decodable {
    let ok: Bool
    let error: String?
    let token: String?
    let employeeName: String?
    let membershipID: Int?
    let restaurant: String?
    let portalToken: String?
    let resetExpired: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error, token, restaurant
        case employeeName = "employee_name"
        case membershipID = "membership_id"
        case portalToken = "portal_token"
        case resetExpired = "reset_expired"
    }
}

/// The claim's 409 (`has_account`) and its other refusals.
struct StaffClaimRefusal: Decodable {
    let ok: Bool?
    let error: String?
    let hasAccount: Bool?
    let employeeName: String?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case hasAccount = "has_account"
        case employeeName = "employee_name"
    }
}
