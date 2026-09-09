import XCTest
@testable import CavnarAI

/// A real TLS handshake to production, through the pinning delegate.
///
/// Network-dependent, so it is skipped unless CAVNAR_LIVE_TLS_TEST=1 — it
/// must never make CI depend on the internet. Run it by hand before a
/// release, and any time the pins or the hashing change:
///
///     CAVNAR_LIVE_TLS_TEST=1 xcodebuild test … \
///       -only-testing:CavnarAITests/PinnedLiveHandshakeTests
///
/// The unit tests next door prove the hash matches a captured certificate.
/// This proves the whole path: chain evaluation, pin comparison and the
/// URLSession challenge handler, against whatever the server is serving
/// today rather than what it served when the fixture was captured.
final class PinnedLiveHandshakeTests: XCTestCase {

    private var enabled: Bool {
        // Off by default so CI never depends on the internet. The scheme's
        // test action sets it (project.yml), or pass it on the command line.
        ProcessInfo.processInfo.environment["CAVNAR_LIVE_TLS_TEST"] == "1"
    }

    func testProductionHandshakeSurvivesPinning() async throws {
        try XCTSkipUnless(enabled, "set CAVNAR_LIVE_TLS_TEST=1 to run")

        let delegate = PinnedSessionDelegate()
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }

        let url = URL(string: "https://dashboard.cavnar.ai/health")!
        do {
            let (data, response) = try await session.data(from: url)
            let http = try XCTUnwrap(response as? HTTPURLResponse)
            XCTAssertEqual(http.statusCode, 200)
            XCTAssertTrue(String(decoding: data, as: UTF8.self).contains("\"status\""))
        } catch let error as URLError where error.code == .cancelled {
            XCTFail("""
            Pinning rejected the live chain — this is the -999 that reaches the \
            login screen as "Something went wrong. Try again." Check the console \
            for the rejected chain's SPKI hashes and update pinnedPublicKeys.
            """)
        }
    }
}
