import Foundation
import LocalAuthentication
import Observation

/// App-wide session state: who's logged in, the bearer token (persisted in
/// Keychain, never UserDefaults), and the Face ID re-entry lock. One
/// instance is created in CavnarAIApp and injected via `.environment(...)`.
///
/// iOS sessions are exempt from the web app's 8-hour inactivity timeout
/// (see auth.py's get_session_user — device_type='ios' skips that check)
/// and instead rely on the 30-day hard session expiry plus this Face ID
/// lock on foreground: logging a phone out every 8 hours of non-use would
/// make a "check once a day" app unusable, but leaving a restaurant's data
/// visible indefinitely without any re-entry gate would be worse.
@Observable
@MainActor
final class SessionStore {
    private(set) var currentUser: User?
    private(set) var token: String?
    var isLocked: Bool = false
    var lastError: String?
    // Set once Home's landing-hero animation has played this session — Home
    // checks this instead of its own local @State so the fade-in reveals
    // exactly once per sign-in rather than replaying every time the client
    // switches back to the Home tab. Reset on logout so the next sign-in
    // (same app process or not) gets its own landing moment.
    var hasShownHomeIntro = false
    // Set the moment AccountViewModel.send2FATest() succeeds, cleared once
    // verify2FA() succeeds or the user backs out of the flow. Backgrounding
    // the app to read the emailed code is exactly what triggers
    // lockIfNeeded() below — RootView then swaps mainTabs out for
    // LockedView, tearing down AccountView and everything sheeted under it
    // (AccountSecurityDetailView, TwoFactorSetupSheet), discarding their
    // local @State along with it. This survives that swap the same way
    // hasShownHomeIntro does, because SessionStore itself is created once
    // in CavnarAIApp and never recreated — AccountView/AccountSecurityDetailView/
    // TwoFactorSetupSheet each check this on appear and re-present
    // themselves so the flow resumes right where the user left off instead
    // of just vanishing.
    var pendingTwoFactorSetupEmail: String?
    // Which channel that pending test code went out on ("email"/"sms") —
    // survives the same relock as pendingTwoFactorSetupEmail above, so a
    // resumed setup flow verifies against the channel that was actually
    // used rather than silently defaulting back to email.
    var pendingTwoFactorSetupMethod: String = "email"

    var isAuthenticated: Bool { token != nil }

    private let client: APIClient

    convenience init(client: APIClient = .shared) {
        self.init(client: client, storedToken: Keychain.get(Keychain.Key.sessionToken))
    }

    /// Test seam for the launch path below: exercises the exact same logic
    /// as the public initializer against an explicit token, so tests don't
    /// depend on Keychain — a plain `Keychain.set` followed by `Keychain.get`
    /// in this XCTest bundle silently returns nil (no keychain-access-group
    /// shared with a real signed app), which isn't something a test seam can
    /// fix and isn't a real defect in Keychain.swift, which does work in a
    /// running app — this file's own `login()` depends on it every day.
    init(client: APIClient = .shared, storedToken: String?) {
        self.client = client
        self.token = storedToken
        self.appPasscodeSet = AppPasscode.isSet
        // Only gate a cold launch when something can actually enforce the
        // gate — with Face ID off and no passcode set, LockedView would
        // just be a screen with nothing behind its Unlock button.
        self.isLocked = storedToken != nil && (Self.biometricLockPreference || AppPasscode.isSet)
        Task {
            await client.setToken(storedToken)
            await client.setSessionExpiredHandler { [weak self] in
                Task { @MainActor in self?.handleSessionExpired() }
            }
            if storedToken != nil {
                await validateStoredSession()
            }
        }
    }

    /// Confirms a token restored from Keychain is still good against
    /// whichever server this build actually talks to, before the app
    /// trusts it for anything.
    ///
    /// A token minted by a different backend — a local dev server, an old
    /// ngrok tunnel from a Debug build later reinstalled as Release — is
    /// byte-for-byte indistinguishable from a real one until a request is
    /// made with it. Without this check, `isAuthenticated` (token != nil)
    /// goes true at launch purely from Keychain's presence, so the app
    /// skips straight past the login screen into a UI that can never load
    /// anything: every screen's own fetch 401s the same way, forever,
    /// with no path back to Sign In short of finding a Sign Out button —
    /// and Account's, the only one that exists, is itself unreachable for
    /// the identical reason (its content only renders once its own load
    /// succeeds). Root-caused live on Sep 7 2026 rebuilding a Debug
    /// install as Release right before a client visit: production had
    /// never seen a single request from the app, yet Face ID unlocked
    /// straight into a dead dashboard.
    ///
    /// Deliberately narrow: only a confirmed rejection from the server
    /// clears the session. `APIClient.APIError.isRetryable` (offline,
    /// timed out) leaves it alone — a phone with no signal at launch must
    /// still open to its last known state, per this file's whole offline-
    /// first design, not get logged out because the network hasn't come
    /// up yet.
    private func validateStoredSession() async {
        guard token != nil else { return }
        do {
            let response: MeResponse = try await client.send("/mobile/api/me", hapticOnError: false)
            currentUser = response.user
        } catch is APIClient.SessionExpiredError {
            // The client's own onSessionExpired handler also fires for
            // this, asynchronously — call it here too rather than wait on
            // that hop, so a UI observing isAuthenticated sees the change
            // as soon as this validation resolves. clearLocalSession() is
            // idempotent, so the handler firing a second time is harmless.
            handleSessionExpired()
        } catch let error as APIClient.APIError where !error.isRetryable {
            handleSessionExpired()
        } catch {
            // Offline, timed out, or an odd decode — say nothing here;
            // whichever screen the user lands on already has its own
            // Retry for this.
        }
    }

    // MARK: - Login flow

    struct LoginRequestBody: Encodable {
        let username: String
        let password: String
        let deviceToken: String?
        // Excluded from the memberwise init on purpose (always this exact
        // expression, never caller-supplied) — see Keychain.deviceIdentity()
        // and auth.py's create_session() for why every login-family body
        // in this file carries one.
        let deviceId: String = Keychain.deviceIdentity()

        enum CodingKeys: String, CodingKey {
            case username, password
            case deviceToken = "device_token"
            case deviceId = "device_id"
        }
    }

    struct LoginResponse: Decodable {
        let ok: Bool
        let requiresTwoFactor: Bool
        let token: String?
        let user: User?
        let pendingToken: String?
        let maskedEmail: String?

        enum CodingKeys: String, CodingKey {
            case ok, token, user
            case requiresTwoFactor = "requires_2fa"
            case pendingToken = "pending_token"
            case maskedEmail = "masked_email"
        }
    }

    /// Returns `.twoFactorRequired` if the account needs a 2FA code, or logs
    /// the session in directly. Sends any Keychain-remembered device token
    /// so a device the owner already verified skips 2FA again.
    func login(username: String, password: String) async throws -> LoginOutcome {
        let rememberedDevice = Keychain.get(Keychain.Key.deviceRememberToken)
        let body = LoginRequestBody(username: username, password: password, deviceToken: rememberedDevice)
        // Retryable: signing in either authenticates or it doesn't, so
        // repeating an attempt that never reached the server is safe — and
        // this is the request most likely to be the first after launch, when
        // the network path is still coming up and URLSession reports -1009
        // on a device that is plainly online.
        let response: LoginResponse = try await client.send(
            "/mobile/api/login", method: .post, body: body, retryTransient: true)
        if response.requiresTwoFactor, let pendingToken = response.pendingToken {
            return .twoFactorRequired(pendingToken: pendingToken, maskedEmail: response.maskedEmail ?? "")
        }
        guard let token = response.token, let user = response.user else {
            throw APIClient.APIError(message: "Unexpected response from server.")
        }
        try await completeLogin(token: token, user: user)
        return .loggedIn
    }

    enum LoginOutcome {
        case loggedIn
        case twoFactorRequired(pendingToken: String, maskedEmail: String)
    }

    struct VerifyTwoFactorBody: Encodable {
        let pendingToken: String
        let code: String
        let rememberDevice: Bool
        let deviceId: String = Keychain.deviceIdentity()

        enum CodingKeys: String, CodingKey {
            case code
            case pendingToken = "pending_token"
            case rememberDevice = "remember_device"
            case deviceId = "device_id"
        }
    }

    struct VerifyTwoFactorResponse: Decodable {
        let ok: Bool
        let token: String
        let user: User
        let deviceToken: String?

        enum CodingKeys: String, CodingKey {
            case ok, token, user
            case deviceToken = "device_token"
        }
    }

    func verifyTwoFactor(pendingToken: String, code: String, rememberDevice: Bool) async throws {
        let body = VerifyTwoFactorBody(pendingToken: pendingToken, code: code, rememberDevice: rememberDevice)
        let response: VerifyTwoFactorResponse = try await client.send("/mobile/api/verify-2fa", method: .post, body: body)
        if rememberDevice, let deviceToken = response.deviceToken {
            Keychain.set(deviceToken, for: Keychain.Key.deviceRememberToken)
        }
        try await completeLogin(token: response.token, user: response.user)
    }

    private struct MeResponse: Decodable {
        let ok: Bool
        let user: User
    }

    /// The Google Sign-In flow hands back a bearer token via a cavnarai://
    /// deep link, not the /login response body, so there's no `user` object
    /// yet — resolve it via /mobile/api/me before finishing the session the
    /// same way password login does.
    func completeGoogleLogin(token: String) async throws {
        let previousToken = self.token
        await client.setToken(token)
        do {
            let response: MeResponse = try await client.send("/mobile/api/me")
            try await completeLogin(token: token, user: response.user)
        } catch {
            await client.setToken(previousToken)
            throw error
        }
    }

    private struct AppleSignInBody: Encodable {
        let identityToken: String
        let deviceId: String = Keychain.deviceIdentity()
        enum CodingKeys: String, CodingKey {
            case identityToken = "identity_token"
            case deviceId = "device_id"
        }
    }

    private struct AppleSignInResponse: Decodable {
        let ok: Bool
        let token: String
        let user: User
    }

    /// Unlike Google's flow, /mobile/api/apple-signin hands back {token,
    /// user} in one response — no separate /me round-trip needed, since
    /// there's no redirect/deep-link step for a native sign-in to lose that
    /// context across.
    func loginWithApple(identityToken: String) async throws {
        let response: AppleSignInResponse = try await client.send(
            "/mobile/api/apple-signin", method: .post, body: AppleSignInBody(identityToken: identityToken)
        )
        try await completeLogin(token: response.token, user: response.user)
    }

    // MARK: - Self-serve signup + password reset

    struct RegisterBody: Encodable {
        let restaurantName: String
        let ownerName: String
        let email: String
        let username: String
        let password: String
        let phone: String
        let deviceId: String = Keychain.deviceIdentity()
        enum CodingKeys: String, CodingKey {
            case restaurantName = "restaurant_name"
            case ownerName = "owner_name"
            case email, username, password, phone
            case deviceId = "device_id"
        }
    }

    /// /mobile/api/register hands back the same {token, user} /login does,
    /// so a new account lands on Home already signed in rather than
    /// bouncing back to the login form to type it all again.
    func register(_ body: RegisterBody) async throws {
        let response: AppleSignInResponse = try await client.send(
            "/mobile/api/register", method: .post, body: body
        )
        try await completeLogin(token: response.token, user: response.user)
    }

    private struct ForgotBody: Encodable { let email: String }

    /// Emails a 6-digit reset code. Always resolves ok whether or not the
    /// email exists — the server deliberately doesn't say, so this can't
    /// enumerate accounts.
    func requestPasswordReset(email: String) async throws {
        _ = try await client.send(
            "/mobile/api/forgot-password", method: .post, body: ForgotBody(email: email)
        ) as APIClient.EmptyResponse
    }

    private struct ResetBody: Encodable {
        let email: String
        let code: String
        let newPassword: String
        enum CodingKeys: String, CodingKey { case email, code; case newPassword = "new_password" }
    }

    /// Second half of the in-app reset — the code from that email plus the
    /// new password. Doesn't sign in on success (a reset must not skip a
    /// 2FA-enabled account's own login); the sheet closes back to Sign In.
    func resetPassword(email: String, code: String, newPassword: String) async throws {
        _ = try await client.send(
            "/mobile/api/reset-password", method: .post,
            body: ResetBody(email: email, code: code, newPassword: newPassword)
        ) as APIClient.EmptyResponse
    }

    private func completeLogin(token: String, user: User) async throws {
        Keychain.set(token, for: Keychain.Key.sessionToken)
        await client.setToken(token)
        self.token = token
        self.currentUser = user
        self.isLocked = false
        // The APNs token usually arrives at launch, before a session exists,
        // so its registration 401s and used to be dropped forever (audit
        // 4.3). Now there is a session, flush anything queued.
        await PushManager.shared.flushPendingToken()
        // Same for writes queued while offline — signing in is a reconnect
        // signal in its own right.
        await PendingWriteQueue.shared.drain()
    }

    private struct LogoutBody: Encodable {
        let apnsToken: String?
        enum CodingKeys: String, CodingKey { case apnsToken = "apns_token" }
    }

    func logout() async {
        // Unregister push while the bearer token is still valid. Signing out
        // used to leave the device_tokens row in place, so the phone kept
        // getting that restaurant's review alerts and daily digests — the
        // apns_token also rides along in the logout body so a single request
        // still clears it if the DELETE above didn't land.
        let apnsToken = PushManager.shared.registeredToken
        await PushManager.shared.unregisterCurrentDevice()
        _ = try? await client.send(
            "/mobile/api/logout", method: .post, body: LogoutBody(apnsToken: apnsToken)
        ) as APIClient.EmptyResponse
        clearLocalSession()
    }

    private func handleSessionExpired() {
        clearLocalSession()
        lastError = "Your session expired — please log in again."
    }

    private func clearLocalSession() {
        Keychain.delete(Keychain.Key.sessionToken)
        // The app passcode belongs to the session that set it — the next
        // person to sign in on this device starts without one (and a
        // forgotten passcode is recovered by signing out and back in).
        AppPasscode.clear()
        appPasscodeSet = false
        // Staff schedules, labor costs and AI insights are cached on disk and
        // used to survive sign-out entirely — the next person to pick up a
        // shared back-office device inherited the previous account's data,
        // and configureCaching() would happily restore it (audit 1.2).
        SecureCache.purgeAll()
        // Anything queued offline belongs to the session that queued it.
        Task { await PendingWriteQueue.shared.clear() }
        Task { await client.setToken(nil) }
        token = nil
        currentUser = nil
        isLocked = false
        hasShownHomeIntro = false
        pendingTwoFactorSetupEmail = nil
        pendingTwoFactorSetupMethod = "email"
    }

    // MARK: - Face ID re-entry lock

    /// Device-local preference — not synced to the backend or across a
    /// user's devices. Defaults to true (opt-out) so existing users keep
    /// today's mandatory-lock behavior until they explicitly turn it off;
    /// a new install also starts locked, matching every install to date.
    private static let biometricLockDefaultsKey = "cavnar.biometric_lock_enabled"
    // A stored property, not a computed get/set over UserDefaults: @Observable
    // only tracks stored properties, so the computed version never told the
    // Security sheet's switch to re-render after a tap — it wrote the new
    // value and the control stayed exactly where it was.
    var biometricLockEnabled: Bool = SessionStore.biometricLockPreference {
        didSet { UserDefaults.standard.set(biometricLockEnabled, forKey: Self.biometricLockDefaultsKey) }
    }

    private static var biometricLockPreference: Bool {
        guard UserDefaults.standard.object(forKey: biometricLockDefaultsKey) != nil else { return true }
        return UserDefaults.standard.bool(forKey: biometricLockDefaultsKey)
    }

    // MARK: - App passcode

    /// Mirrors AppPasscode.isSet as observable state so Security settings
    /// and LockedView re-render when it changes (a Keychain read can't
    /// drive SwiftUI on its own).
    private(set) var appPasscodeSet: Bool

    /// Set by LockedView's "Set one" prompt: the user passed Face ID with
    /// the intent to add a passcode, so RootView opens AppPasscodeSheet
    /// the moment the lock drops. Setting a passcode is never allowed
    /// from BEHIND the gate — only after it.
    var pendingPasscodeSetup = false

    /// True when reopening the app is actually gated by something — Face
    /// ID or a passcode. False means "Require Face ID" is off with no
    /// passcode set, which Security settings calls out in amber.
    var reentryProtected: Bool { biometricLockEnabled || appPasscodeSet }

    func setAppPasscode(_ code: String) {
        AppPasscode.set(code)
        appPasscodeSet = true
    }

    func removeAppPasscode() {
        AppPasscode.clear()
        appPasscodeSet = false
    }

    enum PasscodeAttempt {
        case unlocked
        case wrong(remaining: Int)
        case lockedOut(seconds: Int)
    }

    /// Seconds left on an active passcode lockout, 0 when none.
    var passcodeLockoutRemaining: Int { Int(AppPasscode.lockoutRemaining.rounded(.up)) }

    /// `consumeUnlock: false` checks the code without unlocking the session
    /// — AppPasscodeSheet uses that to confirm the current passcode before
    /// changing or removing it. The attempt throttle applies either way.
    func unlockWithPasscode(_ code: String, consumeUnlock: Bool = true) -> PasscodeAttempt {
        let lockout = passcodeLockoutRemaining
        if lockout > 0 { return .lockedOut(seconds: lockout) }
        if AppPasscode.matches(code) {
            AppPasscode.resetFailures()
            if consumeUnlock {
                isLocked = false
                Haptic.success()
            }
            return .unlocked
        }
        AppPasscode.recordFailure()
        let after = passcodeLockoutRemaining
        if after > 0 { return .lockedOut(seconds: after) }
        return .wrong(remaining: max(0, 5 - AppPasscode.failures))
    }

    /// Called when the app returns to the foreground. A no-op if there's no
    /// active session (nothing to protect) or nothing is set up to enforce
    /// the gate — Face ID off AND no app passcode.
    func lockIfNeeded() {
        guard isAuthenticated, reentryProtected else { return }
        isLocked = true
    }

    // MARK: - Lock delay (Security -> "Lock when reopening")

    private var backgroundedAt: Date?

    /// .background: lock now, or start the grace clock if a delay is set.
    /// The app-switcher shield (RootView.privacyShieldUp) still covers the
    /// snapshot either way — the delay only decides whether Face ID is
    /// asked for on the way back in.
    func noteBackgrounded() {
        guard isAuthenticated, reentryProtected else { return }
        let delay = AppPreferences.shared.lockDelaySeconds
        if delay <= 0 {
            isLocked = true
        } else {
            backgroundedAt = Date()
        }
    }

    /// .active: engage the lock if the grace period ran out.
    func lockIfGraceExpired() {
        guard let at = backgroundedAt else { return }
        backgroundedAt = nil
        guard isAuthenticated, reentryProtected else { return }
        if Date().timeIntervalSince(at) >= Double(AppPreferences.shared.lockDelaySeconds) {
            isLocked = true
        }
    }

    /// True when the lock is switched on but the device cannot actually
    /// enforce it (no passcode or biometrics enrolled). The app still fails
    /// open — locking an owner out of their own data over a device setting
    /// would be worse — but it no longer does so silently: Security settings
    /// surfaces this so they know the gate isn't protecting anything (audit
    /// 1.7).
    private(set) var biometricsUnavailable = false

    func unlockWithBiometrics() async -> Bool {
        let context = LAContext()
        var evaluationError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &evaluationError) else {
            biometricsUnavailable = true
            // With an app passcode set there's a real fallback, so don't
            // fail open — LockedView switches to the passcode pad instead.
            if appPasscodeSet { return false }
            isLocked = false
            return true
        }
        biometricsUnavailable = false
        do {
            let success = try await context.evaluatePolicy(
                .deviceOwnerAuthentication,
                localizedReason: "Unlock to view your restaurant's dashboard"
            )
            if success {
                isLocked = false
                Haptic.success()
            } else {
                Haptic.error()
            }
            return success
        } catch {
            Haptic.error()
            return false
        }
    }
}
