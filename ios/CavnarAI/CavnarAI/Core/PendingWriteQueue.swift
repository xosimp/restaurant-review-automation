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
        /// Dallas. The writes queued today are a review's draft save and
        /// approve (addressed by review id, scoped server-side), a
        /// recommendation's Done / Pass (addressed by its key) and a count
        /// sheet's recounts (addressed by ingredient id) — see QueuedWrite.
        /// A review-id replay fails closed on the wrong location, but a
        /// recount or a recommendation key need not. Stamping the restaurant
        /// here makes the queue refuse the replay itself instead of relying
        /// on every endpoint to catch it.
        /// Optional so entries persisted before this existed still decode.
        var restaurantId: Int?
    }

    /// A write older than this is dropped rather than replayed. Approving a
    /// response drafted two days ago, against data that has since changed, is
    /// worse than failing — see the audit's note on conflict handling.
    private static let maxAge: TimeInterval = 24 * 60 * 60
    /// Internal so a location switch can spare this file from
    /// SecureCache.purgeAll (see SessionStore.didSwitchLocation).
    static let storeKey = "pending-writes.json"

    private var queue: [PendingWrite] = []
    private var isDraining = false

    /// A queued change the app gave up on, and why — shown to the owner
    /// (RootView's pill) until they dismiss it. A write the server refused
    /// used to sit at the head of the queue for a day and then vanish with
    /// everything behind it, and nobody was told.
    struct DroppedWrite: Equatable, Sendable {
        let label: String
        let reason: String
    }
    private(set) var dropped: [DroppedWrite] = []
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
            dropped = restored.filter { Date().timeIntervalSince($0.createdAt) >= Self.maxAge }
                .map { DroppedWrite(label: $0.label, reason: Self.expiredReason) }
        }
    }

    static let expiredReason = "It waited more than a day for a connection, so it wasn't sent."
    static let otherLocationReason = "It was for the location you switched away from."

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
        let leaving = queue.filter { $0.restaurantId != nil && $0.restaurantId != restaurantId }
        queue.removeAll { $0.restaurantId != nil && $0.restaurantId != restaurantId }
        // Said on screen (RootView's pill): SessionStore's lastError, where
        // this used to go, is only drawn on the sign-in screen.
        dropped += leaving.map {
            DroppedWrite(label: $0.label, reason: Self.otherLocationReason)
        }
        if queue.count != before { persist() }
        return before - queue.count
    }

    /// Drains oldest-first. A failure that may pass (offline, a 5xx, an
    /// expired session) stops the drain with the queue intact, so ordering
    /// holds — an approve never overtakes the draft save it depends on.
    ///
    /// A refusal (isRefusal: the server read the write and said no) can
    /// never succeed on replay, so the write is dropped, and with it every
    /// later write that depends on it — the same review's approve behind a
    /// refused draft save (QueuedWrite.dependencyKey). Independent writes
    /// behind it keep draining, and the owner is told (`dropped`). A 2xx
    /// answering `ok: false` is a refusal too (sendQueuedWrite).
    ///
    /// Re-entrant calls are ignored: NetworkMonitor and scenePhase can both
    /// fire on the same reconnect. Every removal is by id: the actor is
    /// re-entrant across the send's `await`, and a sign-out (clear) or a
    /// location switch (dropWrites) in between changes what the head is.
    func drain(client: APIClient? = nil) async {
        guard !isDraining else { return }
        isDraining = true
        defer { isDraining = false }
        let api = client ?? APIClient.shared

        // Expired entries go before anything is sent, not after a failure.
        let expired = queue.filter { Date().timeIntervalSince($0.createdAt) >= Self.maxAge }
        if !expired.isEmpty {
            let ids = Set(expired.map(\.id))
            queue.removeAll { ids.contains($0.id) }
            dropped += expired.map { DroppedWrite(label: $0.label, reason: Self.expiredReason) }
            persist()
        }

        var tried: Set<UUID> = []
        while let next = queue.first(where: { !tried.contains($0.id) }) {
            tried.insert(next.id)
            // A write stamped with a location other than the one now active
            // would land on the wrong restaurant. Drop it rather than send it.
            if let stamped = next.restaurantId, let active = activeRestaurantId, stamped != active {
                dropped.append(DroppedWrite(label: next.label, reason: Self.otherLocationReason))
                remove(id: next.id)
                continue
            }
            do {
                try await api.sendQueuedWrite(
                    path: next.path, method: next.method, bodyJSON: next.bodyJSON
                )
                remove(id: next.id)
            } catch let error as APIClient.APIError where Self.isRefusal(status: error.status) {
                refuse(next, reason: error.message)
            } catch {
                // Still offline, or the server is unwell — leave the queue
                // intact and try again on the next reconnect.
                return
            }
        }
    }

    /// Drops a refused write and every later write that depends on it, and
    /// records why. A write already gone (a sign-out cleared the queue while
    /// it was in flight) is not reported: its session is over.
    private func refuse(_ write: PendingWrite, reason: String) {
        guard let at = queue.firstIndex(where: { $0.id == write.id }) else { return }
        let key = QueuedWrite.dependencyKey(path: write.path)
        let dependents = key == nil ? [] : queue[(at + 1)...].filter {
            QueuedWrite.dependencyKey(path: $0.path) == key
        }
        let gone = Set([write.id] + dependents.map(\.id))
        queue.removeAll { gone.contains($0.id) }
        print("[PendingWriteQueue] dropped refused write \(write.path): \(reason)")
        dropped.append(DroppedWrite(label: write.label, reason: reason))
        for d in dependents {
            dropped.append(DroppedWrite(label: d.label,
                                        reason: "It depended on \"\(write.label)\", which wasn't accepted."))
        }
        persist()
    }

    private func remove(id: UUID) {
        guard let at = queue.firstIndex(where: { $0.id == id }) else { return }
        queue.remove(at: at)
        persist()
    }

    /// The pill's sentence for what was dropped: the first change and its
    /// reason, and how many more. Nil when nothing was.
    static func droppedNote(_ dropped: [DroppedWrite]) -> String? {
        guard let first = dropped.first else { return nil }
        let n = dropped.count
        let head = "\(n) change\(n == 1 ? "" : "s") couldn't be sent"
        let more = n > 1 ? " (and \(n - 1) more)" : ""
        var reason = first.reason.trimmingCharacters(in: .whitespacesAndNewlines)
        if let last = reason.last, !".!?".contains(last) { reason += "." }
        return "\(head): \(first.label)\(more). \(reason) Tap to dismiss."
    }

    /// The owner has read the dropped-change note.
    func dismissDropped() {
        guard !dropped.isEmpty else { return }
        dropped.removeAll()
        notifyChange()
    }

    /// A status that is the server's final answer about this write, not a
    /// passing condition: any 4xx except an expired session (401), a paused
    /// account (402 — billing can be fixed within the day), a timeout (408),
    /// too-early (425) and rate limiting (429).
    ///
    /// A 2xx carried by an error is a refusal as well: some routes answer
    /// 200 `{ok: false, error}` (an empty or over-long draft save), and
    /// sendQueuedWrite throws those with their status.
    static func isRefusal(status: Int?) -> Bool {
        guard let status else { return false }
        if (200..<300).contains(status) { return true }
        guard (400..<500).contains(status) else { return false }
        return ![401, 402, 408, 425, 429].contains(status)
    }

    /// Parks one of the app's queueable writes (QueuedWrite).
    func enqueue(_ write: QueuedWrite) {
        enqueue(path: write.path, method: write.method, bodyJSON: write.bodyJSON, label: write.label)
    }

    func clear() {
        queue.removeAll()
        dropped.removeAll()
        persist()
    }

    /// Posted (on the main queue) whenever the queue's contents change, so
    /// the screen showing unsent work can re-read it.
    static let didChange = Notification.Name("ai.cavnar.pendingWritesChanged")

    private func persist() {
        if let data = try? JSONEncoder().encode(queue) {
            SecureCache.write(data, key: Self.storeKey)
        }
        notifyChange()
    }

    private func notifyChange() {
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: Self.didChange, object: nil)
        }
    }
}

/// A write the app may park while offline, and exactly what it replays —
/// built here so the whole set is one list, and testable.
///
/// What qualifies: a write an owner makes on the floor, on one bar of
/// signal, that is safe to land late and safe to land twice. Queued only
/// when the request certainly never left the phone
/// (`APIError.mayHaveReachedServer == false`), so a replay is the first
/// send, not a second one.
///
/// Deliberately NOT here: anything that sends to someone outside the app —
/// a supplier order, a campaign, a published schedule — and time-off /
/// shift-request decisions. Those two routes are idempotent (a second
/// decide finds the request no longer pending and answers 404), but every
/// decision messages the employee (people.tell / shift_requests._notify:
/// push, text or email), and a decision that reaches staff hours late, with
/// the owner no longer looking, is not a low-risk write.
struct QueuedWrite: Equatable {
    let path: String
    let method: String
    let bodyJSON: Data?
    let label: String

    static let recEventPath = "/mobile/api/recs/event"
    static let countSheetPath = "/mobile/api/food-cost/count-sheet"

    /// Writes no later write depends on — so one the server refuses can be
    /// dropped without breaking an order the queue exists to keep.
    static func standsAlone(path: String) -> Bool {
        path == recEventPath || path == countSheetPath
    }

    /// Which later writes fall with this one when the server refuses it:
    /// every write with the same key. A review's writes share one key (its
    /// approve depends on the draft save queued ahead of it); a write that
    /// stands alone has none; anything else is keyed by its own path, so
    /// only a repeat of the same write falls with it.
    static func dependencyKey(path: String) -> String? {
        if standsAlone(path: path) { return nil }
        let parts = path.split(separator: "/")
        if let i = parts.firstIndex(of: "reviews"), i + 1 < parts.count, Int(parts[i + 1]) != nil {
            return "review:\(parts[i + 1])"
        }
        return path
    }

    /// Done or Pass on a recommendation (POST /mobile/api/recs/event). The
    /// ledger ignores the same answer twice (rec_ledger.record: the episode
    /// is already closed, `recorded: false`), and a Done's tracker start is
    /// refused while anything in its family is being measured. "Measure it"
    /// (accepted) is not queued: the owner starts a tracker to be told which
    /// metric it measures and until when, and that answer only exists live.
    static func recAnswer(_ body: APIClient.RecEventBody) -> QueuedWrite? {
        let label: String
        if body.event == RecAnswer.completed.event {
            label = "Mark a recommendation done"
        } else if body.event == RecAnswer.notForUs.event, body.kind == RecAnswer.notForUs.kind {
            label = "Pass on a recommendation"
        } else {
            return nil
        }
        guard let data = try? JSONEncoder().encode(body) else { return nil }
        return QueuedWrite(path: recEventPath, method: "POST", bodyJSON: data, label: label)
    }

    /// A count sheet's recounts (POST /mobile/api/food-cost/count-sheet).
    /// A recount is an absolute figure, not a change: the same count landing
    /// twice anchors the ledger at the same number, and the second finds no
    /// gap to call waste. `body.date` pins it to the day it was taken, so a
    /// replay after midnight is not filed under the next day.
    static func countSheet(_ body: CountSheetViewModel.SaveBody) -> QueuedWrite? {
        guard !body.items.isEmpty, let data = try? JSONEncoder().encode(body) else { return nil }
        let n = body.items.count
        return QueuedWrite(path: countSheetPath, method: "POST", bodyJSON: data,
                           label: "Save \(n) recount\(n == 1 ? "" : "s")")
    }
}
