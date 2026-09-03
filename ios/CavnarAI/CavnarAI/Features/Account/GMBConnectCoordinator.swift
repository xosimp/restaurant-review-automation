import AuthenticationServices
import UIKit

enum GMBConnectError: Error {
    case cancelled
    case server(String)
}

/// Drives the mobile Google Business connect flow through a system
/// browser sheet: opens the authorize URL the backend hands back from
/// /mobile/api/connections/google/authorize, then captures the
/// cavnarai://gmb-callback redirect auth_routes.py's gmb_mobile_callback
/// sends back. Structurally identical to GoogleSignInCoordinator (same
/// scheme, same reason for holding the session strongly) — kept as its
/// own type since this flow authenticates an already-logged-in
/// restaurant rather than establishing a new session.
@MainActor
final class GMBConnectCoordinator: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?

    /// Races the browser sheet against a wall-clock timeout. ASWebAuthenticationSession
    /// reliably reports user cancellation, but if the sheet is torn down by an
    /// OS-level interruption (a call, the auth service being killed under
    /// memory pressure) the completion may never fire — leaving the
    /// continuation suspended for the life of the process, with the caller's
    /// spinner running forever and no way to retry short of force-quitting
    /// (audit 4.4).
    func connect(authorizeURL: URL) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await self.runSession(authorizeURL: authorizeURL) }
            group.addTask {
                try await Task.sleep(for: .seconds(180))
                throw GMBConnectError.server("Connection timed out — try again.")
            }
            defer { group.cancelAll() }
            try await group.next()
        }
    }

    private func runSession(authorizeURL: URL) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let session = ASWebAuthenticationSession(
                url: authorizeURL,
                callbackURLScheme: "cavnarai"
            ) { callbackURL, error in
                if let error {
                    let nsError = error as NSError
                    if nsError.domain == ASWebAuthenticationSessionError.errorDomain,
                       nsError.code == ASWebAuthenticationSessionError.canceledLogin.rawValue {
                        continuation.resume(throwing: GMBConnectError.cancelled)
                    } else {
                        continuation.resume(throwing: GMBConnectError.server(error.localizedDescription))
                    }
                    return
                }
                guard let callbackURL,
                      let items = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?.queryItems else {
                    continuation.resume(throwing: GMBConnectError.server("No response from Google"))
                    return
                }
                let status = items.first(where: { $0.name == "status" })?.value
                if status == "connected" {
                    continuation.resume()
                } else {
                    let msg = items.first(where: { $0.name == "msg" })?.value ?? "Couldn't connect Google Business"
                    continuation.resume(throwing: GMBConnectError.server(msg))
                }
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
