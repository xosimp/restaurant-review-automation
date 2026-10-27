import Foundation

/// What the app tells the server when it could not decode an answer
/// (release-safety audit #25, A06-04): a key renamed, a null where Swift
/// holds a non-optional, a fraction where it holds an Int — one such change
/// blanks a screen on every installed build, and the owner saw only
/// "Couldn't understand the server's response." with nothing reaching anyone.
///
/// POST /mobile/api/client-errors (client_compat.record_report), fire and
/// forget from APIClient's decoding catch: the route, the method, the HTTP
/// status, the DecodingError's coding path and description — NEVER the
/// response body — the device's id (the server keeps only a hash) and, in
/// the request's headers, the build (ClientIdentity). One per route per hour
/// per device (`DecodeFailureThrottle`).
struct ClientErrorReport: Encodable, Equatable, Sendable {
    let route: String
    let method: String
    let status: Int?
    let codingPath: String
    let description: String
    let kind: String
    let deviceId: String

    enum CodingKeys: String, CodingKey {
        case route, method, status, description, kind
        case codingPath = "coding_path"
        case deviceId = "device_id"
    }

    /// `verb`, not `method:` — tests/test_ios_request_body_parity.py reads
    /// every call naming `method:` as a write to follow.
    init(route: String, verb: String, status: Int?, codingPath: String, description: String, kind: String,
         deviceId: String) {
        self.route = route
        self.method = verb
        self.status = status
        self.codingPath = codingPath
        self.description = description
        self.kind = kind
        self.deviceId = deviceId
    }

    static let maxPath = 300
    static let maxDescription = 500

    /// The coding path ("$.facts.blocks.sales.metrics[2].net") and what went
    /// wrong, from a DecodingError. Its context's debugDescription names the
    /// expected type and the kind found, never the value; the underlying
    /// error (which can quote the body) is left out.
    static func describe(_ error: Error) -> (path: String, description: String) {
        guard let decoding = error as? DecodingError else {
            return ("$", "Not a decoding error: \(type(of: error))")
        }
        switch decoding {
        case .keyNotFound(let key, let context):
            return (render(context.codingPath + [key]), "Missing key \"\(key.stringValue)\". \(context.debugDescription)")
        case .typeMismatch(let type, let context):
            return (render(context.codingPath), "Type mismatch for \(type). \(context.debugDescription)")
        case .valueNotFound(let type, let context):
            return (render(context.codingPath), "Null where \(type) was expected. \(context.debugDescription)")
        case .dataCorrupted(let context):
            return (render(context.codingPath), "Data corrupted. \(context.debugDescription)")
        @unknown default:
            return ("$", "Decoding failed.")
        }
    }

    /// "$" then ".key" for an object key and "[n]" for an array index.
    static func render(_ path: [CodingKey]) -> String {
        "$" + path.map { key in key.intValue.map { "[\($0)]" } ?? ".\(key.stringValue)" }.joined()
    }
}

/// One report per route per hour on this device, remembered across launches
/// (UserDefaults: route keys and times only). Thread-safe: APIClient claims
/// from its actor, StaffTasksAPI through it.
final class DecodeFailureThrottle: @unchecked Sendable {
    static let window: TimeInterval = 3600

    private let defaults: UserDefaults
    private let key: String
    private let lock = NSLock()

    init(defaults: UserDefaults = .standard, key: String = "cavnar.decodeReports.lastSent") {
        self.defaults = defaults
        self.key = key
    }

    /// True — and the hour starts now — when `route` has not been reported
    /// in the last hour; false otherwise.
    func claim(route: String, now: Date = Date()) -> Bool {
        let route = Self.routeKey(route)
        return lock.withLock {
            var sent = (defaults.dictionary(forKey: key) as? [String: Double]) ?? [:]
            let at = now.timeIntervalSince1970
            if let last = sent[route], at - last < Self.window, at >= last { return false }
            // Old entries go, so the record stays a handful of routes.
            sent = sent.filter { at - $0.value < Self.window * 24 }
            sent[route] = at
            defaults.set(sent, forKey: key)
            return true
        }
    }

    /// A route without its query, with ids as ":id" — /mobile/api/reviews/412
    /// and /reviews/413 are one route, as the server counts them.
    static func routeKey(_ path: String) -> String {
        let bare = path.split(separator: "?", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? path
        return bare.split(separator: "/", omittingEmptySubsequences: false).map { part -> String in
            let s = String(part)
            if !s.isEmpty, s.allSatisfy(\.isNumber) { return ":id" }
            if s.count == 10, s.range(of: #"^\d{4}-\d{2}-\d{2}$"#, options: .regularExpression) != nil { return ":id" }
            if s.count >= 16, s.allSatisfy({ $0.isHexDigit || $0 == "-" }) { return ":id" }
            return s
        }.joined(separator: "/")
    }
}

/// What APIClient needs to report: the throttle and the device's id. A
/// client built on a test's mock session reports only when the test hands
/// it one — every other test's mock would otherwise see an extra request.
struct DecodeFailureReporting: Sendable {
    let throttle: DecodeFailureThrottle
    let deviceId: @Sendable () -> String

    static let standard = DecodeFailureReporting(throttle: DecodeFailureThrottle(),
                                                 deviceId: { Keychain.deviceIdentity() })
}
