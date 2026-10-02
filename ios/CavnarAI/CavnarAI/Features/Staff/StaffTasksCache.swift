import Foundation
import WidgetKit

/// The staff app's last good read of a route, kept on the phone so a walk-in
/// with no signal still shows today's sheets, labelled "as of 4:05pm"
/// (PERF-09, MISS-10). Small and generic so the schedule and the brief can
/// use it too: `save(value, path:, token:)` after a read, `load(_:path:token:)`
/// before one.
///
/// Encrypted at rest (SecureCache: complete file protection). Each entry is
/// bound to the staff session that read it — a one-way fingerprint of its
/// token — so the next person signed in on a shared phone never sees the
/// last one's sheets, and a new session simply starts empty.
@MainActor
enum StaffReadCache {
    static let storeKey = "staff-read-cache.v1"

    private struct Entry: Codable {
        let owner: String
        let savedAt: Date
        let body: Data
    }

    static func save<T: Encodable>(_ value: T, path: String, token: String?, now: Date = Date()) {
        let owner = StaffOfflineQueue.fingerprint(token: token)
        guard !owner.isEmpty, let body = try? JSONEncoder().encode(value) else { return }
        // Another session's entries go as this one writes.
        var all = entries().filter { $0.value.owner == owner }
        all[path] = Entry(owner: owner, savedAt: now, body: body)
        write(all)
    }

    static func load<T: Decodable>(_ type: T.Type, path: String, token: String?) -> (value: T, savedAt: Date)? {
        let owner = StaffOfflineQueue.fingerprint(token: token)
        guard !owner.isEmpty, let entry = entries()[path], entry.owner == owner,
              let value = try? JSONDecoder().decode(T.self, from: entry.body) else { return nil }
        return (value, entry.savedAt)
    }

    static func clear() {
        SecureCache.delete(key: storeKey)
    }

    private static func entries() -> [String: Entry] {
        guard let data = SecureCache.read(key: storeKey),
              let all = try? JSONDecoder().decode([String: Entry].self, from: data) else { return [:] }
        return all
    }

    private static func write(_ all: [String: Entry]) {
        if all.isEmpty { clear(); return }
        if let data = try? JSONEncoder().encode(all) { SecureCache.write(data, key: storeKey) }
    }
}

/// Everything of the staff session the phone keeps between launches: the
/// read cache, unsent ticks, the task screen's state and the next-shift
/// widget. One call for a staff sign-out (StaffSessionStore.signOut should
/// call it; until it does, StaffTasksStore calls it when it sees the token
/// go, and WidgetSnapshotService clears the widget on the next activation).
@MainActor
enum StaffLocalData {
    static func clearForSignOut() {
        StaffReadCache.clear()
        Task { await StaffOfflineQueue.shared.clear() }
        StaffTasksStore.shared.reset()
        StaffShiftSnapshot.clear()
        WidgetCenter.shared.reloadTimelines(ofKind: StaffShiftSnapshot.widgetKind)
    }
}
