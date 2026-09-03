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
        hapticOnError: Bool = true
    ) async throws -> Response {
        let request = try buildRequest(path: path, method: method.rawValue, body: body, query: query)

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
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
            let classified = Self.classify(error)
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
    private static func classify(_ error: Error) -> APIError {
        guard let urlError = error as? URLError else {
            return APIError(message: "Couldn't reach the server — check your connection and try again.")
        }
        switch urlError.code {
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            return APIError(kind: .offline,
                            message: "You're offline — this'll go through once you're back on Wi-Fi or cell.")
        case .timedOut, .networkConnectionLost, .cannotConnectToHost, .cannotFindHost:
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

// `nonisolated(unsafe)`: JSONDecoder/JSONEncoder are not Sendable, but these
// two are configured once at init and never mutated afterwards, and every
// use is inside this actor. The annotation records that this sharing was
// examined rather than leaving a strict-concurrency warning standing.
extension JSONDecoder {
    nonisolated(unsafe) static let cavnar: JSONDecoder = JSONDecoder()
}

extension JSONEncoder {
    nonisolated(unsafe) static let cavnar: JSONEncoder = JSONEncoder()
}
