import Foundation
import Observation

@Observable
@MainActor
final class TwoFactorViewModel {
    let pendingToken: String
    let maskedEmail: String
    var code = ""
    var rememberDevice = true
    var isLoading = false
    var errorMessage: String?

    private let sessionStore: SessionStore

    init(sessionStore: SessionStore, pendingToken: String, maskedEmail: String) {
        self.sessionStore = sessionStore
        self.pendingToken = pendingToken
        self.maskedEmail = maskedEmail
    }

    var canSubmit: Bool { code.trimmingCharacters(in: .whitespaces).count == 6 && !isLoading }

    func submit() async {
        guard canSubmit else { return }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            try await sessionStore.verifyTwoFactor(
                pendingToken: pendingToken, code: code, rememberDevice: rememberDevice
            )
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Something went wrong. Try again."
        }
    }
}
