import Foundation

/// The task routes' own transport, for the two things the shared staff call
/// (`StaffSessionStore.authed` → `APIClient.sendWithBearer`) can't do yet:
/// send `X-Staff-Tasks-Version: 2` (so /tasks leaves out the legacy flat
/// `tasks` list, PERF-10) and upload a photo as multipart with the staff
/// bearer (PERF-06 — base64 inside JSON added a third and hit the 5 MB body
/// cap). Same rules as APIClient: an ephemeral session (nothing on disk),
/// the pinning delegate on the production host, the staff token attached
/// here and never installed in APIClient's owner slot, and a 401 without a
/// message ends the staff session.
///
/// Candidate to fold into APIClient (`sendWithBearer(headers:)` and a
/// bearer `upload`) once that file's owner adds them.
struct StaffTasksAPI: Sendable {
    static let versionHeader = "X-Staff-Tasks-Version"
    static let version = "2"

    static let tasksPath = "/staff/api/tasks"
    static let completePath = "/staff/api/tasks/complete"
    static let photoPath = "/staff/api/tasks/photo"
    static let signoffPath = "/staff/api/tasks/signoff"

    private let baseURL: URL
    private let session: URLSession
    private let uploadSession: URLSession

    static let shared = StaffTasksAPI()

    init(baseURL: URL = AppEnvironment.baseURL, session: URLSession? = nil) {
        self.baseURL = baseURL
        if let session {
            self.session = session
            self.uploadSession = session
            return
        }
        let config = URLSessionConfiguration.ephemeral
        // A walk-in cooler fails fast, then the tick is parked offline.
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 30
        config.waitsForConnectivity = false
        let delegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
        self.session = URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
        let upload = URLSessionConfiguration.ephemeral
        upload.timeoutIntervalForRequest = 45
        upload.timeoutIntervalForResource = 90
        upload.waitsForConnectivity = false
        let uploadDelegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
        self.uploadSession = URLSession(configuration: upload, delegate: uploadDelegate, delegateQueue: nil)
    }

    // MARK: Calls

    /// GET /staff/api/tasks, without the legacy flat list.
    func tasks(bearer: String) async throws -> StaffTasksPayload {
        let request = try makeRequest(Self.tasksPath, method: "GET", bearer: bearer)
        return try await perform(request, on: session)
    }

    struct TickBody: Codable, Equatable, Sendable {
        let assignment_id: Int
        let line_id: Int
        let done: Bool
        let value: String?
    }

    func complete(_ body: TickBody, bearer: String) async throws -> StaffTickResponse {
        var request = try makeRequest(Self.completePath, method: "POST", bearer: bearer)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return try await perform(request, on: session)
    }

    /// POST /staff/api/tasks/photo as multipart: the JPEG in `file`, the
    /// sheet and line as form fields. The server stores it only once the
    /// sheet is known to be this person's (B4, LG-31).
    func photo(jpeg: Data, assignmentID: Int, lineID: Int, bearer: String) async throws -> StaffTickResponse {
        var request = try makeRequest(Self.photoPath, method: "POST", bearer: bearer)
        let boundary = "cavnar-\(UUID().uuidString)"
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        request.httpBody = Self.multipart(boundary: boundary,
                                          fields: ["assignment_id": String(assignmentID), "line_id": String(lineID)],
                                          file: jpeg, filename: "proof.jpg", mime: "image/jpeg")
        return try await perform(request, on: uploadSession)
    }

    struct SignoffBody: Encodable, Sendable {
        let shift_kind: String
        let note: String
    }

    func signOff(_ body: SignoffBody, bearer: String) async throws -> StaffTickResponse {
        var request = try makeRequest(Self.signoffPath, method: "POST", bearer: bearer)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        return try await perform(request, on: session)
    }

    /// A proof photo's bytes (GET /staff/api/tasks/photo/<token>), for the
    /// line's thumbnail. The server sends it `no-store`; the screen keeps it
    /// in memory only.
    func photoData(token: String, bearer: String) async throws -> Data {
        // Tokens are URL-safe base64 (secrets.token_urlsafe); anything else
        // is not one of ours.
        guard !token.isEmpty, token.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }) else {
            throw APIClient.APIError(kind: .server, message: "That photo couldn't be loaded.")
        }
        let request = try makeRequest(Self.photoPath + "/" + token, method: "GET", bearer: bearer)
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw APIClient.APIError(kind: .server, message: "That photo couldn't be loaded.",
                                     status: (response as? HTTPURLResponse)?.statusCode)
        }
        return data
    }

    // MARK: Plumbing

    private func makeRequest(_ path: String, method: String, bearer: String) throws -> URLRequest {
        // As APIClient.buildRequest builds it, so a base URL with a path
        // (a tunnel, a LAN address) resolves the same way for both.
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        request.setValue(Self.version, forHTTPHeaderField: Self.versionHeader)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        return request
    }

    /// The error body every staff route answers with.
    private struct Envelope: Decodable {
        let error: String?
        let sessionExpired: Bool?
        enum CodingKeys: String, CodingKey {
            case error
            case sessionExpired = "session_expired"
        }
    }

    /// Transport, status and decode — the staff tier's rules, as
    /// APIClient's `classifyAuthRefusal` has them: any 401 on /staff/api, or
    /// a refusal that says the session expired, is SessionExpiredError, which
    /// the store turns into an ended session (the PIN pad); a 403 with the
    /// server's sentence is that sentence. A transport failure is rethrown as the URLError it
    /// is, so the caller can park the tick (StaffOfflineQueue.isTransport).
    private func perform<T: Decodable>(_ request: URLRequest, on session: URLSession) async throws -> T {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw APIClient.APIError(kind: .server, message: "The server sent something unreadable.")
        }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: data)
        // The staff transport's one rule (APIClient.classifyAuthRefusal):
        // any 401 on /staff/api is an ended session even with a sentence —
        // the server's session gate always sends one (C2). Every route here
        // is /staff/api/tasks*, none of which uses 401 for a wrong credential.
        switch APIClient.classifyAuthRefusal(status: http.statusCode, body: data, authenticated: true,
                                             expiresOn401: true) {
        case .sessionEnded(let message)?:
            throw APIClient.SessionExpiredError(message: message)
        case .refused(let message)?:
            throw APIClient.APIError(kind: .server, message: message, status: http.statusCode, body: data)
        case nil:
            break
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIClient.APIError(kind: .server,
                                     message: envelope?.error ?? (http.statusCode == 413
                                        ? "That photo is too large to send."
                                        : "That didn't work. Try again."),
                                     status: http.statusCode, body: data)
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIClient.APIError(kind: .decoding, message: "The server sent something unreadable.",
                                     status: http.statusCode)
        }
    }

    static func multipart(boundary: String, fields: [String: String], file: Data,
                          filename: String, mime: String) -> Data {
        var body = Data()
        for (name, value) in fields.sorted(by: { $0.key < $1.key }) {
            body.append(Data("--\(boundary)\r\n".utf8))
            body.append(Data("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n".utf8))
            body.append(Data("\(value)\r\n".utf8))
        }
        body.append(Data("--\(boundary)\r\n".utf8))
        body.append(Data("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".utf8))
        body.append(Data("Content-Type: \(mime)\r\n\r\n".utf8))
        body.append(file)
        body.append(Data("\r\n--\(boundary)--\r\n".utf8))
        return body
    }
}
