import Foundation

/// The employee tier's session, kept deliberately separate from SessionStore.
///
/// Two stores rather than a `kind` flag on one, because the two tiers are two
/// products: an owner session carries a 30-day token, a restaurant, module
/// entitlements and the whole dashboard behind it; a staff session is a shift-
/// length PIN token that may only ever reach /staff/api/*. Keeping them apart
/// means there is no code path where a staff token can be mistaken for an
/// owner one — RootView picks a surface based on which store is populated,
/// and neither can silently become the other.
@Observable
@MainActor
final class StaffSessionStore {
    private(set) var token: String?
    private(set) var profile: StaffProfile?
    var lastError: String?

    var isAuthenticated: Bool { token != nil }

    private let client: APIClient

    init(client: APIClient = .shared, storedToken: String? = Keychain.get(Keychain.Key.staffSessionToken)) {
        self.client = client
        self.token = storedToken
    }

    /// The restaurant's staff link, remembered so an employee taps their name
    /// and four digits at the start of a shift rather than re-entering a code
    /// every time. It is not a secret that authorises anything on its own —
    /// every read still needs a PIN session — so UserDefaults is the right
    /// home for it, not the Keychain.
    var portalToken: String? {
        get { UserDefaults.standard.string(forKey: Self.portalKey) }
        set {
            if let newValue {
                UserDefaults.standard.set(newValue, forKey: Self.portalKey)
            } else {
                UserDefaults.standard.removeObject(forKey: Self.portalKey)
            }
        }
    }

    private static let portalKey = "cavnar.staff_portal_token"

    func roster(portal: String) async throws -> StaffRosterResponse {
        let resp: StaffRosterResponse = try await client.sendUnauthenticated(
            "/staff/api/roster/\(portal)")
        loginNonce = resp.loginNonce
        return resp
    }

    /// The nonce most recently handed out by `roster(portal:)`, spent by the
    /// next sign-in. Held here rather than passed through the view so the
    /// login screen never has to know the replay protection exists.
    private var loginNonce: String?

    func signIn(portal: String, membershipID: Int, pin: String) async -> Bool {
        lastError = nil
        return await attemptSignIn(portal: portal, membershipID: membershipID,
                                   pin: pin, retryOnStaleNonce: true)
    }

    private func attemptSignIn(portal: String, membershipID: Int, pin: String,
                               retryOnStaleNonce: Bool) async -> Bool {
        do {
            let body = StaffLoginBody(membershipID: membershipID, pin: pin,
                                      deviceID: Keychain.deviceIdentity(),
                                      nonce: loginNonce ?? "")
            let resp: StaffLoginResponse = try await client.sendUnauthenticated(
                "/staff/r/\(portal)/login", method: .post, body: body)
            if let fresh = resp.loginNonce { loginNonce = fresh }
            guard resp.ok, let token = resp.token else {
                // A stale nonce is the app's problem, not the employee's — a
                // portal left open on a host stand all afternoon should not
                // tell someone their correct PIN was wrong.
                if resp.nonceExpired == true, retryOnStaleNonce {
                    _ = try? await refreshNonce(portal: portal)
                    return await attemptSignIn(portal: portal, membershipID: membershipID,
                                               pin: pin, retryOnStaleNonce: false)
                }
                lastError = resp.error ?? "That PIN didn't match."
                return false
            }
            Keychain.set(token, for: Keychain.Key.staffSessionToken)
            self.token = token
            self.portalToken = portal
            self.loginNonce = nil
            return true
        } catch let error as APIClient.APIError {
            lastError = error.message
            return false
        } catch {
            lastError = "Could not reach the server."
            return false
        }
    }

    @discardableResult
    private func refreshNonce(portal: String) async throws -> String? {
        let resp: StaffRosterResponse = try await client.sendUnauthenticated(
            "/staff/api/roster/\(portal)")
        loginNonce = resp.loginNonce
        return loginNonce
    }

    func signOut() {
        Keychain.delete(Keychain.Key.staffSessionToken)
        token = nil
        profile = nil
    }

    /// Every authenticated staff call goes through here so the bearer token is
    /// attached in exactly one place — the staff token is never handed to
    /// APIClient's shared owner token slot, which is what keeps it from
    /// reaching an owner endpoint even by accident.
    func authed<Response: Decodable>(_ path: String,
                                     method: APIClient.HTTPMethod = .get,
                                     body: (any Encodable)? = nil) async throws -> Response {
        guard let token else { throw APIClient.APIError(message: "Not signed in.") }
        do {
            return try await client.sendWithBearer(path, method: method, body: body, bearer: token)
        } catch let error as APIClient.SessionExpiredError {
            signOut()
            throw error
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
            "/staff/api/signup/where/\(joinCode)")
        guard resp.ok, let name = resp.restaurant else {
            throw APIClient.APIError(message: resp.error ?? "We don't recognise that code.")
        }
        return name
    }

    func claimableNames(joinCode: String) async throws -> StaffClaimableResponse {
        guard let signupToken else {
            throw APIClient.APIError(message: "Verify your phone first.")
        }
        return try await client.sendUnauthenticated(
            "/staff/api/signup/claimable/\(joinCode)?signup_token=\(signupToken)")
    }

    /// The last step: creates the account, sets the PIN, and signs in.
    func claim(joinCode: String, employeeName: String, pin: String) async -> Bool {
        lastError = nil
        guard let signupToken else {
            lastError = "That signup expired. Start again."
            return false
        }
        do {
            let resp: StaffClaimResponse = try await client.sendUnauthenticated(
                "/staff/api/signup/claim", method: .post,
                body: StaffClaimBody(signupToken: signupToken, joinCode: joinCode,
                                     employeeName: employeeName, pin: pin,
                                     deviceID: Keychain.deviceIdentity()))
            guard resp.ok, let token = resp.token else {
                lastError = resp.error ?? "Could not finish setting up your account."
                return false
            }
            Keychain.set(token, for: Keychain.Key.staffSessionToken)
            self.token = token
            // Remembered as this device's staff code so the NEXT sign-in —
            // after this shift session expires — lands straight on the roster
            // picker. The join code resolves everywhere the long portal token
            // does, so one code is all an employee ever holds.
            self.portalToken = joinCode
            self.signupToken = nil
            return true
        } catch let error as APIClient.APIError {
            lastError = error.message
            return false
        } catch {
            lastError = "Could not reach the server."
            return false
        }
    }

    private struct StaffClaimBody: Encodable {
        let signupToken: String
        let joinCode: String
        let employeeName: String
        let pin: String
        let deviceID: String?

        enum CodingKeys: String, CodingKey {
            case pin
            case signupToken = "signup_token"
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
}
