import AuthenticationServices
import UIKit

/// Passkeys on the iPhone (iOS parity #57): Face ID / Touch ID sign-in with
/// the same WebAuthn credentials the website uses. The relying party is the
/// one the server names in its options (passkeys.app_host() —
/// dashboard.cavnar.ai), which the app's `webcredentials:` associated
/// domain lets it use, so a passkey made here signs in on the web and one
/// made on the web signs in here.
///
/// Two ceremonies, both over the bearer-token twins:
///   sign in   POST /mobile/api/passkey/options → the system sheet (or the
///             QuickType bar, AutoFill-assisted, on the login field) →
///             POST /mobile/api/passkey/verify → the {token, user} /login gives.
///   add       POST /mobile/api/passkeys/options (with the password) → the
///             system's "Save a passkey?" → POST /mobile/api/passkeys.
enum PasskeyError: Error, Equatable {
    case cancelled
    case unreadableOptions
    case failed(String)

    var message: String? {
        switch self {
        case .cancelled: return nil
        case .unreadableOptions: return "The server's passkey request couldn't be read. Try again."
        case .failed(let m): return m
        }
    }
}

// MARK: - Wire shapes (py_webauthn's JSON, base64url throughout)

enum Base64URL {
    static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    static func decode(_ s: String) -> Data? {
        var b = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b.count % 4 != 0 { b += "=" }
        return Data(base64Encoded: b)
    }
}

/// generate_registration_options → options_to_json.
struct PasskeyRegistrationOptions: Decodable {
    struct RP: Decodable { let id: String }
    struct User: Decodable { let id: String; let name: String }
    struct Descriptor: Decodable { let id: String }
    let rp: RP
    let user: User
    let challenge: String
    var excludeCredentials: [Descriptor]? = nil
}

/// generate_authentication_options → options_to_json.
struct PasskeyAssertionOptions: Decodable {
    let challenge: String
    let rpId: String
}

/// The credential the server's finish_registration / finish_authentication
/// read: {id, rawId, type, response: {...}, clientExtensionResults}. Keys
/// that don't apply to a ceremony are left out, never sent null.
struct PasskeyCredentialJSON: Encodable, Equatable {
    let id: String
    let rawId: String
    var type = "public-key"
    var authenticatorAttachment = "platform"
    let response: Response
    var clientExtensionResults: [String: String] = [:]

    struct Response: Encodable, Equatable {
        let clientDataJSON: String
        var attestationObject: String? = nil
        var transports: [String]? = nil
        var authenticatorData: String? = nil
        var signature: String? = nil
        var userHandle: String? = nil
    }

    static func registration(credentialID: Data, clientDataJSON: Data, attestationObject: Data) -> PasskeyCredentialJSON {
        let id = Base64URL.encode(credentialID)
        return PasskeyCredentialJSON(id: id, rawId: id, response: Response(
            clientDataJSON: Base64URL.encode(clientDataJSON),
            attestationObject: Base64URL.encode(attestationObject),
            transports: ["internal", "hybrid"]))
    }

    static func assertion(credentialID: Data, clientDataJSON: Data, authenticatorData: Data,
                          signature: Data, userID: Data?) -> PasskeyCredentialJSON {
        let id = Base64URL.encode(credentialID)
        return PasskeyCredentialJSON(id: id, rawId: id, response: Response(
            clientDataJSON: Base64URL.encode(clientDataJSON),
            authenticatorData: Base64URL.encode(authenticatorData),
            signature: Base64URL.encode(signature),
            userHandle: userID.map(Base64URL.encode)))
    }
}

// MARK: - The system ceremony

@MainActor
final class PasskeyCoordinator: NSObject, ASAuthorizationControllerDelegate,
                                ASAuthorizationControllerPresentationContextProviding {
    private var continuation: CheckedContinuation<PasskeyCredentialJSON, Error>?
    private var controller: ASAuthorizationController?

    /// Signs in: the system sheet, or — `autoFill` — the passkey offered in
    /// the QuickType bar of a field marked `.textContentType(.username)`,
    /// pending until the person picks it or the request is cancelled.
    func assert(_ options: PasskeyAssertionOptions, autoFill: Bool = false) async throws -> PasskeyCredentialJSON {
        guard let challenge = Base64URL.decode(options.challenge) else { throw PasskeyError.unreadableOptions }
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: options.rpId)
        let request = provider.createCredentialAssertionRequest(challenge: challenge)
        request.userVerificationPreference = .required
        return try await run([request], autoFill: autoFill)
    }

    /// Adds a passkey to the signed-in login.
    func register(_ options: PasskeyRegistrationOptions) async throws -> PasskeyCredentialJSON {
        guard let challenge = Base64URL.decode(options.challenge),
              let userID = Base64URL.decode(options.user.id) else { throw PasskeyError.unreadableOptions }
        let provider = ASAuthorizationPlatformPublicKeyCredentialProvider(relyingPartyIdentifier: options.rp.id)
        let request = provider.createCredentialRegistrationRequest(challenge: challenge, name: options.user.name,
                                                                   userID: userID)
        request.userVerificationPreference = .required
        if #available(iOS 17.4, *) {
            request.excludedCredentials = (options.excludeCredentials ?? []).compactMap {
                Base64URL.decode($0.id).map { ASAuthorizationPlatformPublicKeyCredentialDescriptor(credentialID: $0) }
            }
        }
        return try await run([request], autoFill: false)
    }

    /// Ends a pending AutoFill request (the login screen going away, or the
    /// person signing in another way).
    func cancel() {
        controller?.cancel()
        controller = nil
    }

    private func run(_ requests: [ASAuthorizationRequest], autoFill: Bool) async throws -> PasskeyCredentialJSON {
        cancel()
        return try await withCheckedThrowingContinuation { continuation in
            self.continuation?.resume(throwing: PasskeyError.cancelled)
            self.continuation = continuation
            let controller = ASAuthorizationController(authorizationRequests: requests)
            controller.delegate = self
            controller.presentationContextProvider = self
            self.controller = controller
            if autoFill {
                controller.performAutoFillAssistedRequests()
            } else {
                controller.performRequests()
            }
        }
    }

    func authorizationController(controller: ASAuthorizationController,
                                 didCompleteWithAuthorization authorization: ASAuthorization) {
        let result: Result<PasskeyCredentialJSON, Error>
        if let reg = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialRegistration,
           let attestation = reg.rawAttestationObject {
            result = .success(.registration(credentialID: reg.credentialID, clientDataJSON: reg.rawClientDataJSON,
                                            attestationObject: attestation))
        } else if let a = authorization.credential as? ASAuthorizationPlatformPublicKeyCredentialAssertion {
            result = .success(.assertion(credentialID: a.credentialID, clientDataJSON: a.rawClientDataJSON,
                                         authenticatorData: a.rawAuthenticatorData, signature: a.signature,
                                         userID: a.userID))
        } else {
            result = .failure(PasskeyError.failed("That passkey couldn't be read. Try again."))
        }
        self.controller = nil
        continuation?.resume(with: result)
        continuation = nil
    }

    func authorizationController(controller: ASAuthorizationController, didCompleteWithError error: Error) {
        self.controller = nil
        let code = (error as? ASAuthorizationError)?.code
        if code == .canceled {
            continuation?.resume(throwing: PasskeyError.cancelled)
        } else if code == .failed || code == .notHandled || code == .invalidResponse {
            continuation?.resume(throwing: PasskeyError.failed("Passkeys aren't available for this sign-in right now."))
        } else {
            continuation?.resume(throwing: PasskeyError.failed(error.localizedDescription))
        }
        continuation = nil
    }

    func presentationAnchor(for controller: ASAuthorizationController) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes
            .compactMap { $0 as? UIWindowScene }
            .flatMap { $0.windows }
            .first { $0.isKeyWindow } ?? ASPresentationAnchor()
    }
}
