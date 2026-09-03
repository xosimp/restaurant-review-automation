import AuthenticationServices
import UIKit

enum GoogleSignInError: Error {
    case cancelled
    case missingToken
    case serverError(String)
}

/// Drives the same server-side OAuth flow the web login page uses
/// (/auth/google-sso) through a system browser sheet, then captures the
/// cavnarai://auth-callback redirect the backend sends back for ?mobile=1
/// requests instead of a web session cookie. Holds the ASWebAuthenticationSession
/// strongly for the duration of the flow — Apple's own docs warn it can be
/// torn down early otherwise — and supplies the presentation anchor it needs.
@MainActor
final class GoogleSignInCoordinator: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?

    func signIn(baseURL: URL) async throws -> String {
        // baseURL comes from AppEnvironment, which reads an environment
        // variable — the only non-literal input any URL construction in this
        // app takes. Force-unwrapping it crashed sign-in on a malformed
        // override instead of reporting it (audit 2.7).
        guard var components = URLComponents(
            url: baseURL.appendingPathComponent("auth/google-sso"),
            resolvingAgainstBaseURL: false
        ) else {
            throw GoogleSignInError.serverError("Sign-in isn't configured correctly on this build.")
        }
        components.queryItems = [
            URLQueryItem(name: "mobile", value: "1"),
            URLQueryItem(name: "device_id", value: Keychain.deviceIdentity()),
        ]
        guard let authorizeURL = components.url else {
            throw GoogleSignInError.serverError("Sign-in isn't configured correctly on this build.")
        }

        return try await withThrowingTaskGroup(of: String.self) { group in
            group.addTask { try await self.runSession(authorizeURL: authorizeURL) }
            group.addTask {
                // See GMBConnectCoordinator.connect for why (audit 4.4).
                try await Task.sleep(for: .seconds(180))
                throw GoogleSignInError.serverError("Sign-in timed out — try again.")
            }
            defer { group.cancelAll() }
            guard let first = try await group.next() else {
                throw GoogleSignInError.missingToken
            }
            return first
        }
    }

    private func runSession(authorizeURL: URL) async throws -> String {
        try await withCheckedThrowingContinuation { continuation in
            let session = ASWebAuthenticationSession(
                url: authorizeURL,
                callbackURLScheme: "cavnarai"
            ) { callbackURL, error in
                if let error {
                    let nsError = error as NSError
                    if nsError.domain == ASWebAuthenticationSessionError.errorDomain,
                       nsError.code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        continuation.resume(throwing: GoogleSignInError.cancelled)
                    } else {
                        continuation.resume(throwing: GoogleSignInError.serverError(error.localizedDescription))
                    }
                    return
                }
                guard let callbackURL,
                      let items = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?.queryItems else {
                    continuation.resume(throwing: GoogleSignInError.missingToken)
                    return
                }
                if let serverError = items.first(where: { $0.name == "error" })?.value {
                    continuation.resume(throwing: GoogleSignInError.serverError(serverError))
                    return
                }
                guard let token = items.first(where: { $0.name == "token" })?.value else {
                    continuation.resume(throwing: GoogleSignInError.missingToken)
                    return
                }
                continuation.resume(returning: token)
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            self.session = session
            session.start()
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }
            .first { $0.isKeyWindow } ?? ASPresentationAnchor()
    }
}

/// Mirrors templates/login.html's `errs` dict so the same failure reads the
/// same way on both platforms.
func googleSignInErrorMessage(for code: String) -> String {
    switch code {
    case "google_denied": return "Google sign-in was cancelled."
    case "no_account": return "No account found for that Google email. Contact will@cavnar.ai."
    case "state_mismatch": return "Security check failed — please try again."
    case "google_token_failed": return "Could not connect to Google. Try again."
    case "no_email": return "Google did not return an email address."
    default: return "Couldn't sign in with Google. Try again."
    }
}
