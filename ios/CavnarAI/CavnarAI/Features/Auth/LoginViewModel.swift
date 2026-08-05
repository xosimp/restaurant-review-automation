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
        } catch is APIClient.SessionExpiredError {
            errorMessage = "Please try signing in again."
        } catch {
            errorMessage = "Something went wrong. Try again."
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
            // User backed out of the browser sheet — not an error worth a banner.
        } catch GoogleSignInError.serverError(let code) {
            errorMessage = googleSignInErrorMessage(for: code)
        } catch GoogleSignInError.missingToken {
            errorMessage = "Couldn't sign in with Google. Try again."
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't sign in with Google. Try again."
        }
    }
}
