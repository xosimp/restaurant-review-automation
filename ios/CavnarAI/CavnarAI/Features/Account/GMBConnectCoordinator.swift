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

    func connect(authorizeURL: URL) async throws {
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
