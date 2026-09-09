import XCTest
import Security
@testable import CavnarAI

/// Certificate pinning against the real chain dashboard.cavnar.ai serves.
///
/// The bug this exists to prevent: the pins are SHA-256 over the DER
/// SubjectPublicKeyInfo (what openssl and every pin generator emit), but the
/// delegate hashed `SecKeyCopyExternalRepresentation` — which is the bare key
/// material, not the SPKI. For an EC key that is the uncompressed point
/// 04‖X‖Y with no algorithm header. Nothing in the chain could ever match, so
/// pinning rejected every TLS handshake, the request died as
/// URLError.cancelled (-999), and the login screen of every Release build
/// said "Something went wrong. Try again." Debug never saw it, because the
/// delegate only installs for the production host — and no test exercised the
/// hash, so it shipped.
final class PinnedSessionDelegateTests: XCTestCase {

    /// The live WE1 intermediate served by dashboard.cavnar.ai, captured
    /// 2026-09-09 — the same DER bytes the device receives.
    private static let we1IntermediateDER = """
    MIICnzCCAiWgAwIBAgIQf/MZd5csIkp2FV0TttaF4zAKBggqhkjOPQQDAzBHMQswCQYDVQQG
    EwJVUzEiMCAGA1UEChMZR29vZ2xlIFRydXN0IFNlcnZpY2VzIExMQzEUMBIGA1UEAxMLR1RT
    IFJvb3QgUjQwHhcNMjMxMjEzMDkwMDAwWhcNMjkwMjIwMTQwMDAwWjA7MQswCQYDVQQGEwJV
    UzEeMBwGA1UEChMVR29vZ2xlIFRydXN0IFNlcnZpY2VzMQwwCgYDVQQDEwNXRTEwWTATBgcq
    hkjOPQIBBggqhkjOPQMBBwNCAARvzTr+Z1dHTCEDhUDCR127WEcPQMFcF4XGGTfn1Xzthkub
    gdnXGhOlCgP4mMTG6J7/EFmPLCaY9eYmJbsPAvpWo4H+MIH7MA4GA1UdDwEB/wQEAwIBhjAd
    BgNVHSUEFjAUBggrBgEFBQcDAQYIKwYBBQUHAwIwEgYDVR0TAQH/BAgwBgEB/wIBADAdBgNV
    HQ4EFgQUkHeSNWfE/6jMqeZ72YB5e8yT+TgwHwYDVR0jBBgwFoAUgEzW63T/STaj1dj8tT7F
    avCUHYwwNAYIKwYBBQUHAQEEKDAmMCQGCCsGAQUFBzAChhhodHRwOi8vaS5wa2kuZ29vZy9y
    NC5jcnQwKwYDVR0fBCQwIjAgoB6gHIYaaHR0cDovL2MucGtpLmdvb2cvci9yNC5jcmwwEwYD
    VR0gBAwwCjAIBgZngQwBAgEwCgYIKoZIzj0EAwMDaAAwZQIxAOcCq1HW90OVznX+0RGU1cxA
    QXomvtgM8zItPZCuFQ8jSBJSjz5keROv9aYsAm5VsQIwJonMaAFi54mrfhfoFNZEfuNMSQ6/
    bIBiNLiyoX46FohQvKeIoJ99cx7sUkFN7uJW
    """

    private func certificate(fromBase64 b64: String) throws -> SecCertificate {
        let compact = b64.filter { !$0.isWhitespace }
        let data = try XCTUnwrap(Data(base64Encoded: compact), "fixture is not valid base64")
        return try XCTUnwrap(SecCertificateCreateWithData(nil, data as CFData),
                             "fixture is not a DER certificate")
    }

    func testTheLiveIntermediateHashesToAShippedPin() throws {
        let cert = try certificate(fromBase64: Self.we1IntermediateDER)
        let digest = try XCTUnwrap(PinnedSessionDelegate.spkiSHA256(of: cert),
                                   "could not compute an SPKI hash at all")
        XCTAssertEqual(digest, "kIdp6NNEd8wsugYyyIYFsi1ylMCED3hZbSR8ZFsa/A4=",
                       "SPKI hashing no longer matches openssl's output")
        XCTAssertTrue(PinnedSessionDelegate.pinnedPublicKeys.contains(digest),
                      "the live chain no longer matches any shipped pin — every Release build will fail TLS")
    }

    func testTheHashIsTheSPKINotTheBareKey() throws {
        // The precise regression: the bare EC point for this key hashes to
        // something else entirely. If spkiSHA256 ever returns that again,
        // pinning silently rejects every connection.
        let cert = try certificate(fromBase64: Self.we1IntermediateDER)
        let digest = try XCTUnwrap(PinnedSessionDelegate.spkiSHA256(of: cert))
        XCTAssertNotEqual(digest, "H7AMYAvicN2+UcFPBz3kJXCDmGrTItZh4ujUBK8hoWg=",
                          "this is the raw-point hash — the header is missing again")
    }

    func testPinsAreConfiguredSoPinningIsActuallyOn() {
        XCTAssertTrue(PinnedSessionDelegate.isConfigured)
        XCTAssertEqual(PinnedSessionDelegate.pinnedPublicKeys.count, 2)
    }
}
