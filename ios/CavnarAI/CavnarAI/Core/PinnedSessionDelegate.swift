import CryptoKit
import Foundation

/// Certificate pinning for the production host.
///
/// Without this, every request — login credentials, bearer tokens, staff wage
/// data — is accepted from any CA the device trusts, so an MDM profile, a
/// rogue root, or a proxy on the restaurant's own Wi-Fi can read and rewrite
/// all of it (audit 1.3). That matters more here than in most apps because
/// the billing screen opens a URL the server supplies (see
/// AccountBillingDetailView's validatedPortalURL) — a MITM that can rewrite
/// responses is one step from a convincing Stripe phishing page.
///
/// Pinning is applied ONLY to the production host. A local dev server or an
/// ngrok tunnel legitimately presents a different chain, so
/// AppEnvironment.isProductionHost gates whether this delegate is installed
/// at all.
final class PinnedSessionDelegate: NSObject, URLSessionDelegate {
    /// SHA-256 of the server's SubjectPublicKeyInfo, base64-encoded.
    ///
    /// Regenerate with:
    ///   openssl s_client -connect dashboard.cavnar.ai:443 </dev/null 2>/dev/null \
    ///     | openssl x509 -pubkey -noout \
    ///     | openssl pkey -pubin -outform der \
    ///     | openssl dgst -sha256 -binary | base64
    ///
    /// ALWAYS keep at least two: the key in use, plus the next rotation key.
    /// A single pin turns a routine certificate renewal into every installed
    /// build failing every request with no way to recover but an App Store
    /// update. An empty set disables pinning (fail-open) for exactly that
    /// reason — an unconfigured pin must not brick the app.
    static let pinnedPublicKeys: Set<String> = [
        // TODO: populate before the next release — see the command above.
        // Left empty deliberately: see the fail-open note in shouldPin.
    ]

    /// False while no pins are configured, so shipping this file without
    /// filling in the hashes changes nothing about how the app behaves.
    static var isConfigured: Bool { !pinnedPublicKeys.isEmpty }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge
    ) async -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            return (.performDefaultHandling, nil)
        }

        // The chain must still validate normally — pinning is layered on top
        // of standard trust evaluation, never instead of it.
        guard SecTrustEvaluateWithError(trust, nil) else {
            return (.cancelAuthenticationChallenge, nil)
        }

        guard Self.isConfigured else {
            return (.useCredential, URLCredential(trust: trust))
        }

        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate] else {
            return (.cancelAuthenticationChallenge, nil)
        }

        // Any key in the chain may match, so pinning an intermediate is also
        // valid — that survives leaf rotation without an app update.
        for certificate in chain {
            guard let key = SecCertificateCopyKey(certificate),
                  let der = SecKeyCopyExternalRepresentation(key, nil) as Data? else { continue }
            let digest = Data(SHA256.hash(data: der)).base64EncodedString()
            if Self.pinnedPublicKeys.contains(digest) {
                return (.useCredential, URLCredential(trust: trust))
            }
        }
        return (.cancelAuthenticationChallenge, nil)
    }
}
