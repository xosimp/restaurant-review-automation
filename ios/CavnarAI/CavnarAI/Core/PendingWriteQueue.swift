import Foundation

/// Durable outbox for mutations attempted while offline.
///
/// A restaurant's worst signal is exactly where the work happens — the
/// walk-in, the basement prep area, the back office — and before this every
/// one of the app's write endpoints simply discarded the user's work on
/// failure (audit 6.1). A manager editing a review response on one bar of
/// signal lost the edit outright: the debounced autosave failed silently and
/// the approve failed after it.
///
/// Queued writes survive relaunch (persisted to SecureCache) and drain
/// oldest-first when NetworkMonitor sees the connection return.
actor PendingWriteQueue {
    static let shared = PendingWriteQueue()

    struct PendingWrite: Codable, Identifiable, Sendable {
        let id: UUID
        let path: String
        let method: String
        let bodyJSON: Data?
        let createdAt: Date
        /// Shown in the "waiting to sync" UI, so a queued item is something
        /// the user can recognise rather than an opaque row.
        let label: String
        /// The restaurant that was active when this was queued.
        ///
        /// The server resolves a write's restaurant from the session, which
        /// for a multi-location owner is whatever location is active AT
        /// REPLAY TIME. Queue an approval in Chicago, go offline, switch to
        /// Dallas, come back online — and the queued write drains against
        /// Dallas. Today both queued kinds are addressed by review id and
        /// scoped server-side, so that particular replay fails closed rather
        /// than writing to the wrong location; the first write queued that is
        /// addressed by name (ingredient supplier, say) would not. Stamping
        /// the restaurant here makes the queue refuse the replay itself
        /// instead of relying on every future endpoint to catch it.
        /// Optional so entries persisted before this existed still decode.
        var restaurantId: Int?
    }

    /// A write older than this is dropped rather than replayed. Approving a
    /// response drafted two days ago, against data that has since changed, is
    /// worse than failing — see the audit's note on conflict handling.
    private static let maxAge: TimeInterval = 24 * 60 * 60
    private static let storeKey = "pending-writes.json"

    private var queue: [PendingWrite] = []
    private var isDraining = false
    /// Set by SessionStore on sign-in and on every location switch, so the
    /// drain can tell "this write belongs here" from "this write belongs to
    /// the location we just left".
    private var activeRestaurantId: Int?

    func setActiveRestaurant(_ id: Int?) {
        activeRestaurantId = id
    }

    init() {
        if let data = SecureCache.read(key: Self.storeKey),
           let restored = try? JSONDecoder().decode([PendingWrite].self, from: data) {
            queue = restored.filter { Date().timeIntervalSince($0.createdAt) < Self.maxAge }
        }
    }

    var pendingCount: Int { queue.count }
    var pendingLabels: [String] { queue.map(\.label) }

    /// Stamped with whatever location is active right now — the queue owns
    /// that fact (setActiveRestaurant), so no call site has to remember to
    /// pass it and none can forget.
    func enqueue(path: String, method: String, bodyJSON: Data?, label: String) {
        queue.append(PendingWrite(
            id: UUID(), path: path, method: method,
            bodyJSON: bodyJSON, createdAt: Date(), label: label,
            restaurantId: activeRestaurantId
        ))
        persist()
    }

    /// Called when the active location changes. Anything queued for the
    /// location being left is dropped rather than replayed against the new
    /// one — see PendingWrite.restaurantId.
    func dropWrites(notFor restaurantId: Int) -> Int {
        let before = queue.count
        queue.removeAll { $0.restaurantId != nil && $0.restaurantId != restaurantId }
        if queue.count != before { persist() }
        return before - queue.count
    }

    /// Drains oldest-first and stops at the first failure so ordering holds —
    /// an approve must not overtake the draft save it depends on. Re-entrant
    /// calls are ignored: NetworkMonitor and scenePhase can both fire on the
    /// same reconnect.
    func drain() async {
        guard !isDraining else { return }
        isDraining = true
        defer { isDraining = false }

        // Expired entries go before anything is sent, not after a failure.
        queue.removeAll { Date().timeIntervalSince($0.createdAt) >= Self.maxAge }
        persist()

        while let next = queue.first {
            // A write stamped with a location other than the one now active
            // would land on the wrong restaurant. Drop it rather than send it.
            if let stamped = next.restaurantId, let active = activeRestaurantId, stamped != active {
                queue.removeFirst()
                persist()
                continue
            }
            do {
                try await APIClient.shared.sendQueuedWrite(
                    path: next.path, method: next.method, bodyJSON: next.bodyJSON
                )
                queue.removeFirst()
                persist()
            } catch {
                // Still offline, or the server rejected it — leave the queue
                // intact and try again on the next reconnect.
                return
            }
        }
    }

    func clear() {
        queue.removeAll()
        persist()
    }

    private func persist() {
        guard let data = try? JSONEncoder().encode(queue) else { return }
        SecureCache.write(data, key: Self.storeKey)
    }
}
