import Foundation
import Observation

@Observable
@MainActor
final class LoginViewModel {
    var username = ""
    var password = ""
    var isLoading = false
    var errorMessage: String?
    var twoFactorPendingToken: String?
    var twoFactorMaskedEmail: String?

    private let sessionStore: SessionStore
    private let googleSignIn = GoogleSignInCoordinator()
    private let appleSignIn = AppleSignInCoordinator()

    init(sessionStore: SessionStore) {
        self.sessionStore = sessionStore
    }

    var canSubmit: Bool {
        !username.trimmingCharacters(in: .whitespaces).isEmpty && !password.isEmpty && !isLoading
    }

    func submit() async {
        guard canSubmit else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let outcome = try await sessionStore.login(username: username, password: password)
            switch outcome {
            case .loggedIn:
                break // SessionStore.isAuthenticated flips; RootView reacts to it.
            case .twoFactorRequired(let pendingToken, let maskedEmail):
                twoFactorPendingToken = pendingToken
                twoFactorMaskedEmail = maskedEmail
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
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let token = try await googleSignIn.signIn(baseURL: AppEnvironment.baseURL)
            try await sessionStore.completeGoogleLogin(token: token)
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

    func signInWithApple() async {
        guard !isLoading else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let identityToken = try await appleSignIn.signIn()
            try await sessionStore.loginWithApple(identityToken: identityToken)
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
