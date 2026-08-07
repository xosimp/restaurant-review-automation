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

    var isAuthenticated: Bool { token != nil }

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
        let storedToken = Keychain.get(Keychain.Key.sessionToken)
        self.token = storedToken
        self.isLocked = storedToken != nil
        Task {
            await client.setToken(storedToken)
            await client.setSessionExpiredHandler { [weak self] in
                Task { @MainActor in self?.handleSessionExpired() }
            }
        }
    }

    // MARK: - Login flow

    struct LoginRequestBody: Encodable {
        let username: String
        let password: String
        let deviceToken: String?

        enum CodingKeys: String, CodingKey {
            case username, password
            case deviceToken = "device_token"
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
        let response: LoginResponse = try await client.send("/mobile/api/login", method: .post, body: body)
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

        enum CodingKeys: String, CodingKey {
            case code
            case pendingToken = "pending_token"
            case rememberDevice = "remember_device"
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
        enum CodingKeys: String, CodingKey { case identityToken = "identity_token" }
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

    private func completeLogin(token: String, user: User) async throws {
        Keychain.set(token, for: Keychain.Key.sessionToken)
        await client.setToken(token)
        self.token = token
        self.currentUser = user
        self.isLocked = false
    }

    func logout() async {
        _ = try? await client.send("/mobile/api/logout", method: .post) as APIClient.EmptyResponse
        clearLocalSession()
    }

    private func handleSessionExpired() {
        clearLocalSession()
        lastError = "Your session expired — please log in again."
    }

    private func clearLocalSession() {
        Keychain.delete(Keychain.Key.sessionToken)
        Task { await client.setToken(nil) }
        token = nil
        currentUser = nil
        isLocked = false
    }

    // MARK: - Face ID re-entry lock

    /// Called when the app returns to the foreground. A no-op if there's no
    /// active session (nothing to protect) or the device has no biometrics
    /// enrolled (falls back to leaving the app unlocked rather than stranding
    /// the owner with no way in).
    func lockIfNeeded() {
        guard isAuthenticated else { return }
        isLocked = true
    }

    func unlockWithBiometrics() async -> Bool {
        let context = LAContext()
        var evaluationError: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &evaluationError) else {
            // No biometrics/passcode configured on this device — don't lock
            // the owner out of their own data over a device limitation.
            isLocked = false
            return true
        }
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
