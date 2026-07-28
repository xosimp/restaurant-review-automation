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
        let message: String
        var errorDescription: String? { message }
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

    init(baseURL: URL = AppEnvironment.baseURL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
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
        query: [String: String] = [:]
    ) async throws -> Response {
        var url = baseURL.appendingPathComponent(path)
        if !query.isEmpty, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
            if let composed = components.url { url = composed }
        }

        var request = URLRequest(url: url)
        request.httpMethod = method.rawValue
        if let token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder.cavnar.encode(body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError(message: "Couldn't reach the server — check your connection and try again.")
        }

        guard let http = response as? HTTPURLResponse else {
            throw APIError(message: "No response from server")
        }

        if http.statusCode == 401 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            if envelope?.sessionExpired == true {
                onSessionExpired?()
                throw SessionExpiredError()
            }
            throw APIError(message: envelope?.error ?? "Your session expired — please log in again.")
        }

        if http.statusCode >= 400 {
            let envelope = try? JSONDecoder.cavnar.decode(ErrorEnvelope.self, from: data)
            throw APIError(message: envelope?.error ?? "Something went wrong (\(http.statusCode)).")
        }

        do {
            return try JSONDecoder.cavnar.decode(Response.self, from: data)
        } catch {
            throw APIError(message: "Couldn't understand the server's response.")
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

extension JSONDecoder {
    static let cavnar: JSONDecoder = JSONDecoder()
}

extension JSONEncoder {
    static let cavnar: JSONEncoder = JSONEncoder()
}
