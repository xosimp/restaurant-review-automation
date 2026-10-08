import Foundation
import Observation

@Observable
@MainActor
final class LoginViewModel {
    var username = ""
    var password = ""
    var isLoading = false
    var errorMessage: String? {
        didSet { if errorMessage != nil { errorShake += 1 } }
    }
    // Bumped every time a real error lands — the fields key their shake
    // off it so a second identical error still shakes.
    var errorShake = 0

    /// Clearing the error at the START of an attempt is what made the whole
    /// sign-in screen bounce. `errorMessage = nil` unmounts LoginErrorBar,
    /// the form gets shorter, and because the block is vertically centered
    /// everything above and below it slides — wordmark, Apple, Google — then
    /// slides back a moment later when the failure re-mounts the bar. Two
    /// full layout animations per tap, so holding down Sign In against a
    /// failing server made the page jump continuously.
    ///
    /// The bar stays mounted instead. A repeated failure reassigns the same
    /// message, which still fires `didSet` and still bumps `errorShake`, so
    /// the red bar and the fields shake in place and nothing else moves.
    /// The error is cleared when the attempt SUCCEEDS, or when the person
    /// edits a field — the two moments it stops being true.
    func clearErrorOnEdit() {
        if errorMessage != nil { errorMessage = nil }
    }
    var twoFactorPendingToken: String?
    var twoFactorMaskedEmail: String?
    var twoFactorChannel: String?

    private let sessionStore: SessionStore
    private let googleSignIn = GoogleSignInCoordinator()
    private let appleSignIn = AppleSignInCoordinator()
    private let passkeys = PasskeyCoordinator()
    private var autoFillTask: Task<Void, Never>?

    init(sessionStore: SessionStore) {
        self.sessionStore = sessionStore
    }

    var canSubmit: Bool {
        !username.trimmingCharacters(in: .whitespaces).isEmpty && !password.isEmpty && !isLoading
    }

    func submit() async {
        guard canSubmit else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let outcome = try await sessionStore.login(username: username, password: password)
            errorMessage = nil
            switch outcome {
            case .loggedIn:
                break // SessionStore.isAuthenticated flips; RootView reacts to it.
            case .twoFactorRequired(let pendingToken, let maskedEmail, let channel):
                twoFactorPendingToken = pendingToken
                twoFactorMaskedEmail = maskedEmail
                twoFactorChannel = channel
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch is APIClient.SessionExpiredError {
            errorMessage = "Please try signing in again."
            Haptic.error()
        } catch {
            errorMessage = "Something went wrong. Try again."
            Haptic.error()
        }
    }

    func signInWithGoogle() async {
        guard !isLoading else { return }
        stopPasskeyAutoFill()
        isLoading = true
        defer { isLoading = false }
        do {
            let token = try await googleSignIn.signIn(baseURL: AppEnvironment.baseURL)
            try await sessionStore.completeGoogleLogin(token: token)
            errorMessage = nil
        } catch GoogleSignInError.cancelled {
            // User backed out of the browser sheet — not an error worth a banner or a buzz.
        } catch GoogleSignInError.serverError(let code) {
            errorMessage = googleSignInErrorMessage(for: code)
            Haptic.error()
        } catch GoogleSignInError.missingToken {
            errorMessage = "Couldn't sign in with Google. Try again."
            Haptic.error()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch {
            errorMessage = "Couldn't sign in with Google. Try again."
            Haptic.error()
        }
    }

    // MARK: - Passkeys (iOS parity #57)

    /// How long one AutoFill request is left open before it is asked again
    /// with a fresh challenge: the server's lasts PASSKEY_CHALLENGE_MINUTES
    /// (5), and a passkey picked from the keyboard bar after that was refused
    /// as expired (re-audit 10/8/26, #9). Under it, with room for the round trip.
    static let autoFillRefreshSeconds: Double = 270
    /// Set by the refresh timer just before it ends the request it outlived.
    @ObservationIgnored private var autoFillExpired = false

    /// Offers this phone's passkey in the QuickType bar over the username
    /// field (AutoFill-assisted): the request waits until the person picks
    /// it, signs in another way, or the screen goes — asked again with a
    /// fresh challenge every autoFillRefreshSeconds. Silent on every
    /// failure — the password form is right there.
    func startPasskeyAutoFill() {
        autoFillTask?.cancel()
        autoFillTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                guard let options = try? await sessionStore.passkeySignInOptions(), !Task.isCancelled else { return }
                // Before the challenge expires, this request is ended and the
                // loop asks for a new one; the person never sees the swap.
                autoFillExpired = false
                let timer = Task { @MainActor [weak self] in
                    try? await Task.sleep(for: .seconds(Self.autoFillRefreshSeconds))
                    guard !Task.isCancelled, let self else { return }
                    self.autoFillExpired = true
                    self.passkeys.cancel()
                }
                do {
                    let credential = try await passkeys.assert(options, autoFill: true)
                    timer.cancel()
                    await finishPasskey(credential)
                    return
                } catch {
                    timer.cancel()
                    // Cancelled or unavailable — the form stands. Only the
                    // refresh above asks again.
                    if !autoFillExpired { return }
                }
            }
        }
    }

    func stopPasskeyAutoFill() {
        autoFillTask?.cancel()
        autoFillTask = nil
        passkeys.cancel()
    }

    /// "Sign in with a passkey": the system sheet, for a person who'd
    /// rather tap than pick from the keyboard bar.
    func signInWithPasskey() async {
        guard !isLoading else { return }
        stopPasskeyAutoFill()
        do {
            let options = try await sessionStore.passkeySignInOptions()
            let credential = try await passkeys.assert(options)
            await finishPasskey(credential)
        } catch let error as PasskeyError {
            if let message = error.message { errorMessage = message; Haptic.error() }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch {
            errorMessage = "Couldn't sign in with a passkey. Try again."
            Haptic.error()
        }
    }

    private func finishPasskey(_ credential: PasskeyCredentialJSON) async {
        isLoading = true
        defer { isLoading = false }
        do {
            try await sessionStore.loginWithPasskey(credential)
            errorMessage = nil
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch {
            errorMessage = "Couldn't sign in with a passkey. Try again."
            Haptic.error()
        }
    }

    func signInWithApple() async {
        guard !isLoading else { return }
        stopPasskeyAutoFill()
        isLoading = true
        defer { isLoading = false }
        do {
            let identityToken = try await appleSignIn.signIn()
            try await sessionStore.loginWithApple(identityToken: identityToken)
            errorMessage = nil
        } catch let appleError as AppleSignInError {
            switch appleError {
            case .cancelled:
                break // User backed out of Apple's sheet — not an error worth a banner or a buzz.
            case .missingToken, .other:
                errorMessage = "Couldn't sign in with Apple. Try again."
                Haptic.error()
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            Haptic.error()
        } catch {
            errorMessage = "Couldn't sign in with Apple. Try again."
            Haptic.error()
        }
    }
}
