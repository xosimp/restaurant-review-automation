import CryptoKit
import Foundation
import UIKit

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
            /// 402 billing_inactive: the account is paused or unpaid. Its own
            /// kind so a screen can show the billing state rather than a
            /// generic failure hidden behind old numbers (CLIENT-22).
            case billingInactive
            /// 403 module_locked: the plan does not include this module.
            case moduleLocked
        }

        let kind: Kind
        let message: String
        /// The HTTP status and raw body of a 4xx/5xx answer, when there was
        /// one. A few routes say more than `{ok, error}` on a refusal — the
        /// publish gate answers 409 with the blockers it wants acknowledged
        /// — and a caller that needs that detail decodes `body` itself.
        let status: Int?
        let body: Data?
        /// False only when the request certainly never reached the server —
        /// no connection was made (offline, host unreachable). A timeout or
        /// a connection dropped mid-request may have been received and
        /// acted on, so a write that failed that way must not be replayed
        /// blindly: a review approve would post to Google twice (CLIENT-6).
        let mayHaveReachedServer: Bool
        var errorDescription: String? { message }

        /// `kind` defaults to `.server` so existing call sites that only pass
        /// a message keep compiling and behaving as before.
        init(kind: Kind = .server, message: String, status: Int? = nil, body: Data? = nil,
             mayHaveReachedServer: Bool = true) {
            self.kind = kind
            self.message = message
            self.status = status
            self.body = body
            self.mayHaveReachedServer = mayHaveReachedServer
        }

        /// The refusal's body decoded as `T`, or nil when there was none or
        /// it was a different shape.
        func decodeBody<T: Decodable>(_ type: T.Type) -> T? {
            guard let body else { return nil }
            return try? JSONDecoder.cavnar.decode(T.self, from: body)
        }

        /// True when retrying later could plausibly succeed — i.e. the write
        /// is worth queueing rather than discarding.
        var isRetryable: Bool { kind == .offline || kind == .timedOut }

        /// A failure that says nothing about the thing being asked about: a
        /// gateway answering for a restarting server (502/503/504 — every
        /// deploy), a dropped connection, a body that wasn't ours. A status
        /// poll waits these out; the job it is polling keeps running
        /// server-side (CLIENT-41). The app itself answering with an error —
        /// a 4xx, a 500 carrying its own message — is an outcome, not this.
        var isTransientForPolling: Bool {
            if let status { return [502, 503, 504].contains(status) }
            return kind == .offline || kind == .timedOut || kind == .decoding || kind == .server
        }
    }

    /// Thrown when the backend responds 401 with `session_expired: true` —
    /// the mobile mirror of the web app's identical JSON shape (see
    /// auth.py's login_required). Callers don't need to inspect this beyond
    /// letting it propagate; SessionStore's onSessionExpired handler (set at
    /// launch) already forces the logged-out state and clears Keychain.
    ///
    /// LocalizedError so a screen that shows `error.localizedDescription`
    /// reads a sentence, not "The operation couldn't be completed.
    /// (CavnarAI.APIClient.SessionExpiredError error 1.)" (CLIENT-50).
    struct SessionExpiredError: Error, LocalizedError {
        var errorDescription: String? { "Your session expired — please sign in again." }
    }

    private let baseURL: URL
    private let session: URLSession
    /// Uploads only. The main session caps a whole transfer at 45s, which is
    /// right for reads and wrong for sending a photo that the server then
    /// reads with a model; same ephemeral config and pinning otherwise.
    private let uploadSession: URLSession
    /// Calls that ask for more than the main session's 20s (a model
    /// generating a post, 90s). `send(timeout:)` sets the request's idle
    /// timeout, but the main session's 45s RESOURCE timeout still ended the
    /// whole transfer, so a 90s generation could never finish and the server
    /// finished work the phone had already given up on (CLIENT-20). Same
    /// ephemeral config and pinning; only the caps differ.
    private let longCallSession: URLSession
    private var token: String?
    private var onSessionExpired: (@Sendable () -> Void)?

    init(baseURL: URL = AppEnvironment.baseURL, session: URLSession? = nil) {
        self.baseURL = baseURL
        if let session {
            self.session = session
            self.longCallSession = session
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

            let longConfig = URLSessionConfiguration.ephemeral
            longConfig.timeoutIntervalForRequest = 20      // each call sets its own
            longConfig.timeoutIntervalForResource = Self.longCallResourceCap
            longConfig.waitsForConnectivity = false
            let longDelegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
            self.longCallSession = URLSession(configuration: longConfig, delegate: longDelegate, delegateQueue: nil)
        }
        let uploadConfig = URLSessionConfiguration.ephemeral
        uploadConfig.timeoutIntervalForRequest = 60
        uploadConfig.timeoutIntervalForResource = 150
        uploadConfig.waitsForConnectivity = false
        let uploadDelegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
        self.uploadSession = URLSession(configuration: uploadConfig, delegate: uploadDelegate, delegateQueue: nil)
    }

    /// The whole-transfer cap for calls that name a timeout longer than the
    /// main session's. Above the longest timeout any caller asks for (90s).
    static let longCallResourceCap: TimeInterval = 150

    func setToken(_ token: String?) {
        self.token = token
    }

    func setSessionExpiredHandler(_ handler: @escaping @Sendable () -> Void) {
        self.onSessionExpired = handler
    }

    /// Empty response body for routes that only return `{"ok": true}` with
    /// nothing else the caller needs.
    struct EmptyResponse: Decodable {}

    /// The `{ok, error}` envelope every mutating route answers with. Fifteen
    /// view models each declared their own private copy of this under six
    /// different names; one here, the copies are typealiases to it.
    struct OKResponse: Decodable { let ok: Bool; let error: String? }

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
        // The token this request carries. The actor is re-entrant across the
        // await below, so by the time the answer lands a different account
        // may be signed in (CLIENT-23).
        let sentToken = token
        let isLongCall = (timeout ?? 0) > 20
        let transport = isLongCall ? longCallSession : session
        // A long call is a model generating something the owner is waiting
        // for; switching apps to check a text must not kill it mid-flight.
        let grant = isLongCall ? await BackgroundGrant.begin(path) : nil
        defer { if let grant { Task { await grant.end() } } }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.perform(request, on: transport, mayRetry: mayRetry)
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
            if Self.isRequestedCancellation(error) { throw CancellationError() }
            let offline = await MainActor.run { !NetworkMonitor.shared.isOnline }
            let classified = Self.classify(error, deviceIsOffline: offline)
            if hapticOnError { await Haptic.error() }
            throw classified
        }

        return try await finish(data: data, response: response, sentToken: sentToken, hapticOnError: hapticOnError)
    }

    /// True only when this task was actually cancelled — its view went away.
    ///
    /// URLError.cancelled on its own is not that: PinnedSessionDelegate
    /// answers a certificate chain it does not trust (a captive portal, a
    /// TLS-intercepting proxy) with .cancelAuthenticationChallenge, which
    /// URLSession surfaces as URLError.cancelled on a task nobody cancelled.
    /// Treated as a cancel, the screen stayed silently blank; it is a failure
    /// the owner has to be told about (CLIENT-24), and `classify` says so.
    static func isRequestedCancellation(_ error: Error) -> Bool {
        Task.isCancelled || error is CancellationError
    }

    /// Uploads one file as multipart/form-data (field name `file`) — invoice
    /// photos today. Same auth, status handling and decoding as `send`;
    /// never retried on a guess, because the server runs a paid model on it.
    func upload<Response: Decodable>(
        _ path: String,
        fileData: Data,
        filename: String,
        mimeType: String,
        timeout: TimeInterval = 120
    ) async throws -> Response {
        var request = try buildRequest(path: path, method: HTTPMethod.post.rawValue, body: nil, query: [:])
        let boundary = "cavnar-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = timeout
        var body = Data()
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".utf8))
        body.append(Data("Content-Type: \(mimeType)\r\n\r\n".utf8))
        body.append(fileData)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        request.httpBody = body
        // Derived from the file itself, so the owner tapping Scan again on
        // the same photo after a timeout sends the same key: the server can
        // answer the second request from the first one's result instead of
        // paying for a second model read (CLIENT-21). A random key per call
        // would dedupe nothing.
        request.setValue(Self.idempotencyKey(path: path, fileData: fileData),
                         forHTTPHeaderField: "Idempotency-Key")
        let sentToken = token
        // Keeps the upload and the server's read of it alive when the owner
        // leaves the app — photograph the invoice, switch to the supplier's
        // email — instead of iOS suspending it at the first background
        // second and the paid scan being lost (CLIENT-21).
        let grant = await BackgroundGrant.begin(path)
        defer { Task { await grant.end() } }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await Self.perform(request, on: uploadSession, mayRetry: false)
        } catch {
            if Self.isRequestedCancellation(error) { throw CancellationError() }
            let offline = await MainActor.run { !NetworkMonitor.shared.isOnline }
            let classified = Self.classify(error, deviceIsOffline: offline)
            await Haptic.error()
            throw classified
        }
        return try await finish(data: data, response: response, sentToken: sentToken, hapticOnError: true)
    }

    /// "<path>:<sha256 of the file>" — stable across retries of the same
    /// photo, different for a different photo or route.
    static func idempotencyKey(path: String, fileData: Data) -> String {
        let digest = SHA256.hash(data: fileData).map { String(format: "%02x", $0) }.joined()
        return "\(path):\(digest)"
    }

    /// A session_expired answer describes the token that request carried.
    /// Only when that is still this client's token does it end the session:
    /// a slow request started as the previous account, answering after the
    /// next one signed in, used to sign the new account out (CLIENT-23).
    private func expireIfCurrent(_ sentToken: String?) {
        guard sentToken == token else { return }
        onSessionExpired?()
    }

    /// Status handling and decoding shared by `send` and `upload`.
    private func finish<Response: Decodable>(
        data: Data, response: URLResponse, sentToken: String?, hapticOnError: Bool
    ) async throws -> Response {
        guard let http = response as? HTTPURLResponse else {
            if hapticOnError { await Haptic.error() }
            throw APIError(message: "No response from server")
        }

        if http.statusCode == 401 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if envelope?.sessionExpired == true {
                expireIfCurrent(sentToken)
                throw SessionExpiredError()
            }
            if hapticOnError { await Haptic.error() }
            throw APIError(message: envelope?.error ?? "Your session expired — please log in again.",
                           status: 401, body: data)
        }

        if http.statusCode >= 400 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if hapticOnError { await Haptic.error() }
            throw APIError(kind: envelope?.kind ?? .server,
                           message: envelope?.error ?? "Something went wrong (\(http.statusCode)).",
                           status: http.statusCode, body: data)
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
        let sentToken = token
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIError(message: "No response from server")
        }
        if http.statusCode == 401 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if envelope?.sessionExpired == true {
                expireIfCurrent(sentToken)
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
        /// What the answer rests on: the modules it consulted, how confident
        /// it is, and any figure in it the backend could not trace back to
        /// data the model was actually handed.
        let modulesConsulted: [String]?
        let confidence: String?
        let unverifiedFigures: [String]?
        /// The answer's id — what "Was this useful?" rates.
        let messageId: Int?
        /// The answer's own concrete suggestions, keyed (`ask_tip:<hash>`).
        let suggestions: [AskSuggestion]?

        enum CodingKeys: String, CodingKey {
            case type, label, state, answer, truncated, proposals, error, confidence, suggestions
            case conversationId = "conversation_id"
            case modulesConsulted = "modules_consulted"
            case unverifiedFigures = "unverified_figures"
            case messageId = "message_id"
        }

        var evidence: AskEvidence {
            AskEvidence(modules: modulesConsulted ?? [],
                        confidence: confidence ?? "unknown",
                        unverifiedFigures: unverifiedFigures ?? [])
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
                    let sentToken = token
                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        continuation.finish(throwing: APIError(message: "Couldn't reach Cavnar AI — try again."))
                        return
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        // A refusal is read like any other route's: only an
                        // explicit session_expired ends the session, and the
                        // server's own reason (not on your plan, billing
                        // paused, rate limited) reaches the screen instead of
                        // a generic "couldn't reach" (CLIENT-53).
                        var raw = Data()
                        for try await byte in bytes {
                            raw.append(byte)
                            if raw.count >= 64 * 1024 { break }
                        }
                        let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: raw)
                        if http.statusCode == 401, envelope?.sessionExpired == true {
                            expireIfCurrent(sentToken)
                            continuation.finish(throwing: SessionExpiredError())
                            return
                        }
                        continuation.finish(throwing: APIError(
                            kind: envelope?.kind ?? .server,
                            message: envelope?.error ?? "Couldn't reach Cavnar AI — try again.",
                            status: http.statusCode, body: raw))
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
                } catch {
                    if Self.isRequestedCancellation(error) {
                        continuation.finish(throwing: CancellationError())
                        return
                    }
                    // The same connectivity truth `send` uses — this used to
                    // hard-code "online", so an offline phone was told the
                    // attempt "didn't get through" instead (CLIENT-53).
                    let offline = await MainActor.run { !NetworkMonitor.shared.isOnline }
                    continuation.finish(throwing: Self.classify(error, deviceIsOffline: offline))
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
        path: String, method: String, body: (any Encodable)?, query: [String: String],
        bearerOverride: String? = nil, omitAuth: Bool = false
    ) throws -> URLRequest {
        var url = baseURL.appendingPathComponent(path)
        if !query.isEmpty, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
            if let composed = components.url { url = composed }
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        // The staff tier passes its own bearer rather than using this actor's
        // stored owner token. Keeping the two apart is the point: a staff
        // token is never installed here, so it can never be sent to an owner
        // endpoint by a call site that forgot which tier it was on.
        if omitAuth {
            // Sign-in and roster run before any session exists.
        } else if let bearerOverride {
            request.setValue("Bearer \(bearerOverride)", forHTTPHeaderField: "Authorization")
        } else if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder.cavnar.encode(body)
        }
        return request
    }

    /// A call made before any session exists — the staff portal's roster and
    /// PIN sign-in. Never attaches the stored owner token.
    func sendUnauthenticated<Response: Decodable>(
        _ path: String,
        method: HTTPMethod = .get,
        body: (any Encodable)? = nil
    ) async throws -> Response {
        let request = try buildRequest(path: path, method: method.rawValue,
                                       body: body, query: [:], omitAuth: true)
        return try await perform(request, path: path, mayRetry: method == .get)
    }

    /// A call carrying an explicitly supplied bearer — the staff tier's
    /// authenticated reads and writes.
    func sendWithBearer<Response: Decodable>(
        _ path: String,
        method: HTTPMethod = .get,
        body: (any Encodable)? = nil,
        bearer: String
    ) async throws -> Response {
        let request = try buildRequest(path: path, method: method.rawValue,
                                       body: body, query: [:], bearerOverride: bearer)
        return try await perform(request, path: path, mayRetry: method == .get)
    }

    /// Shared transport + decode for the two helpers above. Deliberately does
    /// NOT run the owner tier's session-expired handler: a staff token going
    /// stale must sign out the staff store, not the owner one.
    private func perform<Response: Decodable>(
        _ request: URLRequest, path: String, mayRetry: Bool
    ) async throws -> Response {
        let (data, response) = try await Self.perform(request, on: session, mayRetry: mayRetry)
        guard let http = response as? HTTPURLResponse else {
            throw APIError(kind: .server, message: "The server sent something unreadable.")
        }
        if http.statusCode == 401 || http.statusCode == 403 {
            if let decoded = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data),
               let message = decoded.error {
                throw APIError(kind: .server, message: message)
            }
            throw SessionExpiredError()
        }
        guard (200..<300).contains(http.statusCode) else {
            if let decoded = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data),
               let message = decoded.error {
                throw APIError(kind: .server, message: message)
            }
            throw APIError(kind: .server, message: "That didn't work. Try again.")
        }
        do {
            return try JSONDecoder.cavnar.decode(Response.self, from: data)
        } catch {
            throw APIError(kind: .decoding, message: "The server sent something unreadable.")
        }
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
    // internal, not private, so the tests can exercise BOTH branches — the
    // online one and the genuinely-offline one. Reaching it through send()
    // only ever tests whichever state the test machine happens to be in,
    // which is how this behaviour's own test went stale without failing for
    // the right reason.
    static func classify(_ error: Error, deviceIsOffline: Bool) -> APIError {
        guard let urlError = error as? URLError else {
            return APIError(message: "Couldn't reach the server — check your connection and try again.")
        }
        switch urlError.code {
        case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff:
            guard deviceIsOffline else {
                // Online, but that attempt could not get out. Retryable.
                return APIError(kind: .timedOut,
                                message: "That didn't get through. Tap to try again.",
                                mayHaveReachedServer: false)
            }
            return APIError(kind: .offline,
                            message: "You're offline — this'll go through once you're back on Wi-Fi or cell.",
                            mayHaveReachedServer: false)
        case .timedOut:
            return APIError(kind: .timedOut,
                            message: "The server took too long to answer. Tap to retry.")
        case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
            // No connection was ever made, so nothing was received.
            return APIError(kind: .timedOut,
                            message: "Couldn't make a connection to the server. Tap to retry.",
                            mayHaveReachedServer: false)
        case .networkConnectionLost:
            return APIError(kind: .timedOut,
                            message: "The connection dropped mid-request. Tap to retry.")
        case .cancelled, .serverCertificateUntrusted, .serverCertificateHasBadDate,
             .serverCertificateNotYetValid, .serverCertificateHasUnknownRoot,
             .secureConnectionFailed, .clientCertificateRejected, .clientCertificateRequired:
            // Reached only when nobody cancelled the task (see
            // isRequestedCancellation): the connection's certificate was
            // refused — most often a hotel or venue Wi-Fi sign-in page, or
            // a network that inspects encrypted traffic.
            return APIError(message: "Couldn't open a secure connection to Cavnar. If this Wi-Fi has a "
                                   + "sign-in page, finish that or switch to cell, then try again.")
        default:
            return APIError(message: "Couldn't reach the server — check your connection and try again.")
        }
    }
}

/// Background execution time for one request the owner is waiting on.
///
/// iOS suspends an app within seconds of it leaving the screen, and a
/// suspended URLSession task on an ephemeral session simply dies. Asking
/// for time (beginBackgroundTask) gives the request the ~30s iOS grants to
/// finish; the grant is always ended — when the request returns, or when
/// iOS says time is up — so it can never keep the app awake on its own.
@MainActor
final class BackgroundGrant {
    private var id: UIBackgroundTaskIdentifier = .invalid

    static func begin(_ name: String) -> BackgroundGrant {
        let grant = BackgroundGrant()
        grant.id = UIApplication.shared.beginBackgroundTask(withName: name) { [weak grant] in
            grant?.end()
        }
        return grant
    }

    func end() {
        guard id != .invalid else { return }
        UIApplication.shared.endBackgroundTask(id)
        id = .invalid
    }
}

private struct ErrorEnvelope: Decodable {
    let ok: Bool
    let error: String?
    let sessionExpired: Bool?
    let billingInactive: Bool?
    let moduleLocked: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, error
        case sessionExpired = "session_expired"
        case billingInactive = "billing_inactive"
        case moduleLocked = "module_locked"
    }

    var kind: APIClient.APIError.Kind {
        if billingInactive == true { return .billingInactive }
        if moduleLocked == true { return .moduleLocked }
        return .server
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
