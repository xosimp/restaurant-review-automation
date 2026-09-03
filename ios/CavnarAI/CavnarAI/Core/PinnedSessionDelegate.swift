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
    /// SHA-256 of a SubjectPublicKeyInfo in the server's chain, base64-encoded.
    ///
    /// These pin the **intermediate and root**, deliberately NOT the leaf.
    /// dashboard.cavnar.ai is fronted by Railway, whose leaf certificate is
    /// issued by Google Trust Services and auto-renewed roughly every 90 days
    /// with a brand-new key that nobody can know in advance. Pinning the leaf
    /// would therefore hard-fail every request on every installed build at the
    /// next renewal, recoverable only by an App Store update — the exact
    /// failure mode pinning is supposed to prevent.
    ///
    /// Pinning the issuing chain instead means an attacker needs a certificate
    /// for cavnar.ai issued under Google Trust Services, rather than one from
    /// any of the ~150 CAs iOS trusts by default. That is a large reduction in
    /// attack surface, and it survives leaf rotation untouched.
    ///
    /// Regenerate (prints leaf, intermediate and root):
    ///   openssl s_client -connect dashboard.cavnar.ai:443 \
    ///       -servername dashboard.cavnar.ai -showcerts </dev/null 2>/dev/null \
    ///     | awk '/BEGIN CERT/,/END CERT/' \
    ///     | csplit -sz -f /tmp/cert- - '/BEGIN CERT/' '{*}' \
    ///     && for f in /tmp/cert-*; do \
    ///          openssl x509 -in "$f" -pubkey -noout \
    ///            | openssl pkey -pubin -outform der \
    ///            | openssl dgst -sha256 -binary | base64; \
    ///        done
    ///
    /// Re-check before each release, and whenever Railway announces a CA
    /// change. GTS WE1 expires 2029-02-20; GTS Root R4 expires 2028-01-28.
    /// An empty set disables pinning (fail-open) so an unconfigured build
    /// cannot brick itself.
    static let pinnedPublicKeys: Set<String> = [
        // Intermediate — C=US, O=Google Trust Services, CN=WE1 (exp 2029-02-20)
        "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=",
        // Root — C=US, O=Google Trust Services LLC, CN=GTS Root R4 (exp 2028-01-28)
        "mEflZT5enoR1FuXLgYYGqnVEoZvmf9c2bVBpiOjYQ0c=",
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
