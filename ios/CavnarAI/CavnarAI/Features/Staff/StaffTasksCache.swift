import Foundation
import WidgetKit

/// The staff app's last good read of a route, kept on the phone so a walk-in
/// with no signal still shows today's sheets, labelled "as of 4:05pm"
/// (PERF-09, MISS-10): `save(value, path:, token:)` after a read,
/// `load(_:path:token:)` before one.
///
/// One store for the staff app: this is I2's StaffCache (SecureCache,
/// complete file protection; `StaffCache.purgeAll()` on an explicit
/// sign-out clears it with everything else) under the PIN session's scope
/// (`StaffCache.sessionScope(token:)`), so the next person signed in on a
/// shared phone never sees the last one's sheets, and a new session simply
/// starts empty. Writing drops any other session's copy of the same route.
@MainActor
enum StaffReadCache {
    static func save<T: Encodable>(_ value: T, path: String, token: String?) {
        guard let token, !token.isEmpty else { return }
        let scope = StaffCache.sessionScope(token: token)
        StaffCache.purge(keys: { $0 == path }, except: scope)
        StaffCache.save(value, key: path, scope: scope)
    }

    static func load<T: Decodable>(_ type: T.Type, path: String, token: String?) -> (value: T, savedAt: Date)? {
        guard let token, !token.isEmpty,
              let hit = StaffCache.load(type, key: path, scope: StaffCache.sessionScope(token: token)) else { return nil }
        return (hit.value, hit.savedAt)
    }

    /// Every route read kept this way (the /staff/api/… keys), every session.
    static func clear() {
        StaffCache.purge(keys: { $0.hasPrefix("/staff/api/") })
    }
}

/// Everything of the staff session the phone keeps between launches: the
/// read cache, unsent ticks, the task screen's state and the next-shift
/// widget. RootView runs it (with `StaffCache.purgeAll()`) from
/// `StaffSessionStore.onExplicitSignOut` — Sign out, "Not you?", a deleted
/// account. When the token goes any other way (an ended session),
/// StaffTasksStore runs it too, since that session's ticks can no longer be
/// sent; the idle lock keeps the token, so it keeps everything.
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
