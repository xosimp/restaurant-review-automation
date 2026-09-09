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
            guard let digest = Self.spkiSHA256(of: certificate) else { continue }
            if Self.pinnedPublicKeys.contains(digest) {
                return (.useCredential, URLCredential(trust: trust))
            }
        }
        // Nothing in the chain matched. This is indistinguishable to
        // URLSession from any other cancellation — it surfaces as
        // URLError.cancelled (-999), which APIClient reasonably reads as
        // "the user navigated away" and swallows. Say it out loud here so a
        // pin that has genuinely rotated is one console line away from being
        // understood, instead of a generic "Something went wrong."
        NSLog("""
        ⚠️ CAVNAR: certificate pinning REJECTED \
        \(challenge.protectionSpace.host). No key in the served chain matched \
        PinnedSessionDelegate.pinnedPublicKeys. Chain SPKI hashes: \
        \(chain.compactMap { Self.spkiSHA256(of: $0) }.joined(separator: ", ")). \
        If the CA rotated, update the pins (see this file's header) and ship.
        """)
        return (.cancelAuthenticationChallenge, nil)
    }

    /// SHA-256 over the certificate's DER SubjectPublicKeyInfo — the value
    /// `openssl … -pubkey | openssl pkey -pubin -outform der | dgst -sha256`
    /// produces, which is what the pins above are and what every pin
    /// generator emits.
    ///
    /// This used to hash `SecKeyCopyExternalRepresentation` directly, which
    /// is NOT the SPKI: for an EC key it returns the bare uncompressed point
    /// (04‖X‖Y) and for RSA a PKCS#1 RSAPublicKey — in both cases the key
    /// material without the algorithm header that SPKI wraps it in. Every
    /// certificate in the chain therefore hashed to something that could
    /// never equal a pin, so pinning rejected every connection, the request
    /// failed as URLError.cancelled, and the app reported "Something went
    /// wrong. Try again." on the login screen of every Release build. It was
    /// invisible in Debug because the delegate is only installed for the
    /// production host.
    ///
    /// dashboard.cavnar.ai's chain is EC throughout (leaf and WE1 are
    /// P-256, GTS Root R4 is P-384); RSA headers are here so a CA change
    /// doesn't reintroduce the same silent failure.
    static func spkiSHA256(of certificate: SecCertificate) -> String? {
        guard let key = SecCertificateCopyKey(certificate),
              let raw = SecKeyCopyExternalRepresentation(key, nil) as Data?,
              let attributes = SecKeyCopyAttributes(key) as? [CFString: Any] else { return nil }
        let type = attributes[kSecAttrKeyType] as? String
        let bits = (attributes[kSecAttrKeySizeInBits] as? NSNumber)?.intValue ?? 0
        guard let header = asn1Header(keyType: type, bits: bits) else { return nil }
        return Data(SHA256.hash(data: header + raw)).base64EncodedString()
    }

    /// The ASN.1 SubjectPublicKeyInfo prefix for a given key type and size.
    /// Fixed byte strings — the same table every pinning library carries,
    /// because Security.framework will not hand back the encoded SPKI.
    private static func asn1Header(keyType: String?, bits: Int) -> Data? {
        let ec = kSecAttrKeyTypeECSECPrimeRandom as String
        let rsa = kSecAttrKeyTypeRSA as String
        switch (keyType, bits) {
        case (ec, 256):
            return Data([0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02,
                         0x01, 0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07, 0x03,
                         0x42, 0x00])
        case (ec, 384):
            return Data([0x30, 0x76, 0x30, 0x10, 0x06, 0x07, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x02,
                         0x01, 0x06, 0x05, 0x2b, 0x81, 0x04, 0x00, 0x22, 0x03, 0x62, 0x00])
        case (rsa, 2048):
            return Data([0x30, 0x82, 0x01, 0x22, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86,
                         0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00, 0x03, 0x82, 0x01, 0x0f, 0x00])
        case (rsa, 4096):
            return Data([0x30, 0x82, 0x02, 0x22, 0x30, 0x0d, 0x06, 0x09, 0x2a, 0x86, 0x48, 0x86,
                         0xf7, 0x0d, 0x01, 0x01, 0x01, 0x05, 0x00, 0x03, 0x82, 0x02, 0x0f, 0x00])
        default:
            return nil
        }
    }
}
