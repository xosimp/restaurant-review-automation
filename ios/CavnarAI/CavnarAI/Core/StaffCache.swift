import CryptoKit
import Foundation

/// The staff app's warm start: each screen's last good answer, kept per
/// employee on this phone and shown labelled "As of 3:42pm" until the live
/// one lands (employee audit H6 / C7). Never presented as live, and never
/// shown to anybody but the employee it was saved for.
///
/// Storage is SecureCache (complete file protection, encrypted at rest and
/// unreadable while the phone is locked; an owner sign-out's purge clears
/// it too). Keys are `staff.<scope>.<key>`.
///
/// **Scopes.** A staff phone is often shared (a host stand, a kitchen
/// iPad), so a cached week must never paint before the app knows whose
/// session this is:
/// - `memberScope(restaurant:name:)` — the employee, from /staff/api/me.
///   Everything is saved under this.
/// - `sessionScope(token:)` — the PIN session. `link(session:to:)` records
///   which employee a session belongs to once /me has answered, so a
///   relaunch inside the same session warms at once
///   (`memberScope(forSession:)`); a NEW session waits for /me first.
///
/// API (other staff screens may use it the same way):
/// ```swift
/// StaffCache.save(payload, key: "inbox", scope: scope)          // Encodable
/// if let hit = StaffCache.load(StaffInbox.self, key: "inbox", scope: scope) {
///     show(hit.value); stamp = StaffCache.asOf(hit.savedAt)       // "As of 3:42pm"
/// }
/// StaffCache.purge(scope: scope)   // this employee's copies
/// StaffCache.purgeAll()            // every staff copy (sign-out)
/// ```
enum StaffCache {
    struct Entry<Value> {
        let value: Value
        let savedAt: Date
    }

    // MARK: Scopes

    static func memberScope(restaurant: String, name: String) -> String {
        "m" + digest(restaurant.lowercased().trimmingCharacters(in: .whitespaces) + "|"
                     + name.lowercased().split(separator: " ").joined(separator: " "))
    }

    static func sessionScope(token: String) -> String {
        "s" + digest(token)
    }

    /// Remembers that `session` is `member`'s, so a relaunch in the same
    /// session can paint the cache before /me answers.
    static func link(session: String, to member: String) {
        var index = readIndex()
        guard index.sessions[session] != member else { return }
        index.sessions = index.sessions.filter { $0.value != member }
        index.sessions[session] = member
        writeIndex(index)
    }

    static func memberScope(forSession session: String) -> String? {
        readIndex().sessions[session]
    }

    // MARK: Values

    static func save<Value: Encodable>(_ value: Value, key: String, scope: String) {
        guard let data = try? encoder.encode(value) else { return }
        SecureCache.write(data, key: fileKey(key, scope))
        var index = readIndex()
        if !(index.keys[scope] ?? []).contains(key) {
            index.keys[scope, default: []].append(key)
            writeIndex(index)
        }
    }

    static func load<Value: Decodable>(_ type: Value.Type, key: String, scope: String) -> Entry<Value>? {
        let file = fileKey(key, scope)
        guard let data = SecureCache.read(key: file),
              let value = try? decoder.decode(Value.self, from: data) else { return nil }
        return Entry(value: value, savedAt: SecureCache.modifiedAt(key: file) ?? .distantPast)
    }

    /// One employee's copies, gone.
    static func purge(scope: String) {
        var index = readIndex()
        for key in index.keys[scope] ?? [] { SecureCache.delete(key: fileKey(key, scope)) }
        index.keys[scope] = nil
        index.sessions = index.sessions.filter { $0.value != scope }
        writeIndex(index)
    }

    /// The copies under the keys `match` picks, in every scope but `kept` —
    /// a read cached per session (StaffReadCache) drops the last session's
    /// copy as the next one writes, so a shared phone holds one person's.
    static func purge(keys match: (String) -> Bool, except kept: String? = nil) {
        var index = readIndex()
        for (scope, keys) in index.keys where scope != kept {
            let gone = keys.filter(match)
            guard !gone.isEmpty else { continue }
            for key in gone { SecureCache.delete(key: fileKey(key, scope)) }
            let left = keys.filter { !match($0) }
            index.keys[scope] = left.isEmpty ? nil : left
        }
        writeIndex(index)
    }

    /// Every staff copy on this phone — for an explicit sign-out.
    static func purgeAll() {
        let index = readIndex()
        for (scope, keys) in index.keys {
            for key in keys { SecureCache.delete(key: fileKey(key, scope)) }
        }
        SecureCache.delete(key: indexKey)
    }

    /// "As of 3:42pm" (today) / "As of 9/30/26 · 3:42pm".
    static func asOf(_ savedAt: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        StaffFreshness.stamp(at: savedAt, fromCache: true, now: now, calendar: calendar)
            ?? "As of \(CavnarDate.mdyTime(savedAt))"
    }

    // MARK: Plumbing

    private struct Index: Codable {
        var sessions: [String: String] = [:]
        var keys: [String: [String]] = [:]
    }

    private static let indexKey = "staff.index"

    private static func fileKey(_ key: String, _ scope: String) -> String {
        "staff.\(scope).\(key.replacingOccurrences(of: "/", with: "_"))"
    }

    private static func readIndex() -> Index {
        guard let data = SecureCache.read(key: indexKey),
              let index = try? decoder.decode(Index.self, from: data) else { return Index() }
        return index
    }

    private static func writeIndex(_ index: Index) {
        guard let data = try? encoder.encode(index) else { return }
        SecureCache.write(data, key: indexKey)
    }

    private static func digest(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).prefix(12).map { String(format: "%02x", $0) }.joined()
    }

    private static var encoder: JSONEncoder { JSONEncoder() }
    private static var decoder: JSONDecoder { JSONDecoder() }
}
