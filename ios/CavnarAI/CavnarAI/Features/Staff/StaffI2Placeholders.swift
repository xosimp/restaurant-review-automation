import Foundation
import Observation

// TEMPORARY — delete this whole file when I2's container wave merges.
//
// Me, the Inbox and the manager thread take I2's `StaffPortalStore` (the
// container's frame state) and Sign out purges I2's `StaffCache`. Neither
// exists on this branch, so these stand-ins carry exactly the members the
// I3 views call — `reloadBadges()`, `reloadShifts()` and
// `StaffCache.purgeAll()` — and do nothing. I2's real
// Features/Staff/StaffPortalStore.swift and Core/StaffCache.swift replace
// them one for one; with both present the compiler reports the duplicates.

@Observable
@MainActor
final class StaffPortalStore {
    init() {}

    func reloadBadges() async {}
    func reloadShifts() async {}
}

enum StaffCache {
    static func purgeAll() {}
}
