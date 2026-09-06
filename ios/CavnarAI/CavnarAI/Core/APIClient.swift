import Foundation

/// Thin URLSession wrapper for the /mobile/api/* backend — no third-party
/// networking library, since the route count doesn't justify one. An actor
/// so the mutable `token`/`onSessionExpired` state is safe to touch from
/// concurrent callers without a separate lock.
actor APIClient {
    static let shared = APIClient()

    enum HTTPMethod: String {
        case get = "GET"
        case post = "POST"
        case delete = "DELETE"
    }

    struct APIError: Error, LocalizedError, Equatable {
        /// What actually went wrong. Every transport failure used to collapse
        /// into one string, so the app couldn't tell an offline device from a
        /// dead backend — and couldn't decide whether a retry was even worth
        /// attempting, or whether a write should be queued (audit 4.5).
        enum Kind: Equatable {
            case offline
            case timedOut
            case server
            case decoding
        }

        let kind: Kind
        let message: String
        var errorDescription: String? { message }

        /// `kind` defaults to `.server` so existing call sites that only pass
        /// a message keep compiling and behaving as before.
        init(kind: Kind = .server, message: String) {
            self.kind = kind
            self.message = message
        }

        /// True when retrying later could plausibly succeed — i.e. the write
        /// is worth queueing rather than discarding.
        var isRetryable: Bool { kind == .offline || kind == .timedOut }
    }

    /// Thrown when the backend responds 401 with `session_expired: true` —
    /// the mobile mirror of the web app's identical JSON shape (see
    /// auth.py's login_required). Callers don't need to inspect this beyond
    /// letting it propagate; SessionStore's onSessionExpired handler (set at
    /// launch) already forces the logged-out state and clears Keychain.
    struct SessionExpiredError: Error {}

    private let baseURL: URL
    private let session: URLSession
    private var token: String?
    private var onSessionExpired: (@Sendable () -> Void)?

    init(baseURL: URL = AppEnvironment.baseURL, session: URLSession? = nil) {
        self.baseURL = baseURL
        if let session {
            self.session = session
        } else {
            // .ephemeral, not .default: the shared URLCache writes eligible
            // responses to Library/Caches unencrypted, and every GET here
            // returns business data — reviews, labor costs, billing (audit
            // 1.5). Ephemeral keeps nothing on disk.
            let config = URLSessionConfiguration.ephemeral
            // 60s (URLSession's default) is far too long for a phone that has
            // just walked into a walk-in cooler; the user force-quits long
            // before the error lands, and pre-queue that discarded their work
            // (audit 6.2).
            config.timeoutIntervalForRequest = 20
            config.timeoutIntervalForResource = 45
            // False deliberately: PendingWriteQueue handles deferral
            // explicitly, with UI. A silently-waiting URLSession task gives
            // the user no feedback at all.
            config.waitsForConnectivity = false
            // Pinning applies only to the real deployment — a local server or
            // an ngrok tunnel legitimately presents a different chain.
            let delegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
            self.session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        }
    }

    func setToken(_ token: String?) {
        self.token = token
    }

    func setSessionExpiredHandler(_ handler: @escaping @Sendable () -> Void) {
        self.onSessionExpired = handler
    }

    /// Empty response body for routes that only return `{"ok": true}` with
    /// nothing else the caller needs.
    struct EmptyResponse: Decodable {}

    @discardableResult
    func send<Response: Decodable>(
        _ path: String,
        method: HTTPMethod = .get,
        body: (any Encodable)? = nil,
        query: [String: String] = [:],
        // Defaults on for the common case (a failure the caller surfaces to
        // the user should feel like a failure). Callers whose own catch
        // block already treats the error as silent/non-fatal — a secondary
        // background load like Account's billing/sessions fetch, or Review
        // template loading — pass false, since buzzing the exact same
        // "you failed to log in" pattern for a background enrichment call
        // the user never sees fail is misleading, not informative. Was
        // previously unconditional here, which is what made Account (whose
        // .task fires two of these silent loads back to back) feel like it
        // had a distinct "double error" haptic that Home/Modules never
        // triggered.
        hapticOnError: Bool = true,
        // Some endpoints run a model and genuinely take ten or twenty
        // seconds. The session-wide 20s is right for ordinary reads (a phone
        // in a walk-in cooler should fail fast) and wrong for those, where it
        // cancelled work the server then finished anyway.
        timeout: TimeInterval? = nil,
        // One silent retry for an attempt that died in transit. Safe by
        // default only for GET; a POST that publishes or sends must never be
        // repeated on a guess, so those opt in explicitly.
        retryTransient: Bool? = nil
    ) async throws -> Response {
        var request = try buildRequest(path: path, method: method.rawValue, body: body, query: query)
        if let timeout { request.timeoutInterval = timeout }
        let mayRetry = retryTransient ?? (method == .get)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.perform(request, on: session, mayRetry: mayRetry)
        } catch {
            // A request cancelled because its view went away (a `.task`
            // torn down by tapping Back, or by leaving a module screen
            // before its loads finished) is NOT a failure — URLSession
            // surfaces it as URLError.cancelled, and this path used to
            // buzz the error pattern for every one of them. That was the
            // stray "error" haptic after backing out of a detail screen,
            // and the burst of doubled/tripled buzzes from tapping a tile,
            // leaving fast, and tapping another: each abandoned screen's
            // in-flight fetches all cancelled at once, each firing its own
            // error haptic. Rethrow as a plain cancellation, silently.
            if Task.isCancelled || error is CancellationError || (error as? URLError)?.code == .cancelled {
                throw CancellationError()
            }
            let offline = await MainActor.run { !NetworkMonitor.shared.isOnline }
            let classified = Self.classify(error, deviceIsOffline: offline)
            if hapticOnError { await Haptic.error() }
            throw classified
        }

        guard let http = response as? HTTPURLResponse else {
            if hapticOnError { await Haptic.error() }
            throw APIError(message: "No response from server")
        }

        if http.statusCode == 401 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if envelope?.sessionExpired == true {
                onSessionExpired?()
                throw SessionExpiredError()
            }
            if hapticOnError { await Haptic.error() }
            throw APIError(message: envelope?.error ?? "Your session expired — please log in again.")
        }

        if http.statusCode >= 400 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if hapticOnError { await Haptic.error() }
            throw APIError(message: envelope?.error ?? "Something went wrong (\(http.statusCode)).")
        }

        do {
            return try JSONDecoder.cavnar.decode(Response.self, from: data)
        } catch {
            if hapticOnError { await Haptic.error() }
            throw APIError(kind: .decoding, message: "Couldn't understand the server's response.")
        }
    }

    /// Replays a write from PendingWriteQueue. Bypasses `send`'s generic
    /// decode (the original caller is long gone and there is no one to hand a
    /// response to) but keeps auth, status handling and error classification
    /// identical, so a queued write fails the same way a live one would.
    func sendQueuedWrite(path: String, method: String, bodyJSON: Data?) async throws {
        var request = try buildRequest(path: path, method: method, body: nil, query: [:])
        if let bodyJSON {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = bodyJSON
        }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError(message: "No response from server")
        }
        if http.statusCode == 401 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if envelope?.sessionExpired == true {
                onSessionExpired?()
                throw SessionExpiredError()
            }
            throw APIError(message: "Session expired")
        }
        if http.statusCode >= 400 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            throw APIError(message: envelope?.error ?? "Something went wrong (\(http.statusCode)).")
        }
    }

    // MARK: - Server-sent events

    /// One event from an SSE stream — Ask Cavnar's /stream routes only, for
    /// now. Same envelope shape both the web client and the backend's
    /// generator emit: "progress" while a tool runs, "answer" once, or
    /// "error".
    struct SSEEvent: Decodable {
        let type: String
        let label: String?
        /// One of ask_cavnar.ORB_STATES — what the orb should look like
        /// while this happens. Sent alongside the label so the client
        /// renders the right motion without string-matching the text.
        let state: String?
        let answer: String?
        let truncated: Bool?
        let proposals: [AskProposal]?
        let error: String?
        /// Which chat the answer was filed under — tells a client that
        /// started a fresh conversation what its id is now.
        let conversationId: Int?

        enum CodingKeys: String, CodingKey {
            case type, label, state, answer, truncated, proposals, error
            case conversationId = "conversation_id"
        }
    }

    /// POSTs `path` and yields each `data: {...}` line as it arrives.
    ///
    /// Built on URLSession's native byte stream rather than a third-party
    /// SSE client — the app only ever consumes one shape of event, on one
    /// route family, so a dependency would outweigh what it replaces.
    /// Ends the stream (rather than throwing) on a decode failure for a
    /// single line: one malformed progress event should not blow up an
    /// answer that already arrived.
    func stream<Body: Encodable>(
        _ path: String, body: Body
    ) -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try buildRequest(path: path, method: "POST", body: body, query: [:])
                    let (bytes, response) = try await session.bytes(for: request)
                    if let http = response as? HTTPURLResponse, http.statusCode == 401 {
                        onSessionExpired?()
                        continuation.finish(throwing: SessionExpiredError())
                        return
                    }
                    guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                        continuation.finish(throwing: APIError(message: "Couldn't reach Cavnar AI — try again."))
                        return
                    }
                    for try await line in bytes.lines {
                        guard line.hasPrefix("data: ") else { continue }
                        let jsonText = String(line.dropFirst(6))
                        guard let data = jsonText.data(using: .utf8),
                              let event = try? JSONDecoder.cavnar.decode(SSEEvent.self, from: data)
                        else { continue }
                        continuation.yield(event)
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish(throwing: CancellationError())
                } catch {
                    continuation.finish(throwing: Self.classify(error, deviceIsOffline: false))
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// One attempt, then — for a transient failure on a request that is safe
    /// to repeat — one more after a short pause.
    ///
    /// The pause matters: the common cause is a connection URLSession had
    /// pooled and the server had already closed, and retrying instantly can
    /// pick the same dead socket out of the pool again.
    private static func perform(
        _ request: URLRequest, on session: URLSession, mayRetry: Bool
    ) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(for: request)
        } catch {
            guard mayRetry, isTransient(error), !Task.isCancelled else { throw error }
            try? await Task.sleep(for: .milliseconds(400))
            try Task.checkCancellation()
            return try await session.data(for: request)
        }
    }

    private func buildRequest(
        path: String, method: String, body: (any Encodable)?, query: [String: String]
    ) throws -> URLRequest {
        var url = baseURL.appendingPathComponent(path)
        if !query.isEmpty, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
            if let composed = components.url { url = composed }
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder.cavnar.encode(body)
        }
        return request
    }

    /// Turns a URLError into a message the user can actually act on — "move
    /// nearer the router" and "the server is down" are different problems and
    /// used to read identically.
    /// URLError codes that mean "this attempt died in transit", not "this
    /// request was answered and refused". They are worth one silent retry
    /// because the overwhelmingly common cause is a keep-alive socket the
    /// server had already closed — URLSession reuses it, the write fails
    /// instantly, and the request never reached anyone. That is the "failed,
    /// tapped again, worked" shape.
    private static let transientCodes: Set<URLError.Code> = [
        .networkConnectionLost,      // -1005, the stale-socket case
        .notConnectedToInternet,     // -1009, spurious when the path is up
        .cannotConnectToHost,
        .cannotFindHost,
        .dnsLookupFailed,
    ]

    private static func isTransient(_ error: Error) -> Bool {
        guard let urlError = error as? URLError else { return false }
        return transientCodes.contains(urlError.code)
    }

    /// `deviceIsOffline` comes from NetworkMonitor — the app's actual source
    /// of connectivity truth — not from the error code.
    ///
    /// -1009 was being reported as "You're offline" on faith. URLSession
    /// raises it whenever its path evaluation is unsatisfied at that instant,
    /// which happens routinely on the first request after launch or a
    /// Wi-Fi/cell handoff, on a device that is plainly online. Telling
    /// someone standing on their own Wi-Fi that they are offline is worse
    /// than saying nothing, and it sent them looking at their router instead
    /// of tapping the button again.
    private static func classify(_ error: Error, deviceIsOffline: Bool) -> APIError {
        guard let urlError = error as? URLError else {
            return APIError(message: "Couldn't reach the server — check your connection and try again.")
        }
        switch urlError.code {
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            guard deviceIsOffline else {
                // Online, but that attempt could not get out. Retryable.
                return APIError(kind: .timedOut,
                                message: "That didn't get through. Tap to try again.")
            }
            return APIError(kind: .offline,
                            message: "You're offline — this'll go through once you're back on Wi-Fi or cell.")
        case .timedOut:
            return APIError(kind: .timedOut,
                            message: "The server took too long to answer. Tap to retry.")
        case .networkConnectionLost, .cannotConnectToHost, .cannotFindHost:
            return APIError(kind: .timedOut,
                            message: "The connection dropped mid-request. Tap to retry.")
        default:
            return APIError(message: "Couldn't reach the server — check your connection and try again.")
        }
    }
}

private struct ErrorEnvelope: Decodable {
    let ok: Bool
    let error: String?
    let sessionExpired: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case sessionExpired = "session_expired"
    }
}

// JSONDecoder/JSONEncoder are Sendable in this SDK, and both are configured
// once and never mutated, so plain `static let` is already concurrency-safe.
extension JSONDecoder {
    static let cavnar: JSONDecoder = JSONDecoder()
}

extension JSONEncoder {
    static let cavnar: JSONEncoder = JSONEncoder()
}
