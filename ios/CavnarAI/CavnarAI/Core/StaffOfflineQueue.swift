import CryptoKit
import Foundation

/// A tick on a task sheet made with no connection — the walk-in, the
/// basement prep room (PERF-09, MISS-10).
struct StaffQueuedTick: Codable, Equatable, Identifiable, Sendable {
    let id: UUID
    let body: StaffTasksAPI.TickBody
    /// The line's own words, for "1 tick couldn't be sent: Walk-in temp".
    let label: String
    let createdAt: Date
    /// Whose session queued it (StaffOfflineQueue.fingerprint of the staff
    /// token). A tick is replayed only under the session that made it: the
    /// server records the tick under the signed-in name, so a tick queued
    /// by one person must never land as another's on a shared phone.
    let owner: String

    var key: String { StaffSheetMerge.key(body.assignment_id, body.line_id) }
}

/// The staff tier's offline outbox for task-sheet ticks and readings.
///
/// Not PendingWriteQueue: that queue replays through APIClient with the
/// OWNER token (`sendQueuedWrite`), and a staff token is deliberately never
/// installed there (StaffSessionStore.authed). Same shape otherwise —
/// persisted in SecureCache (encrypted at rest), drained oldest-first when
/// the connection returns, a day's limit, and a refusal is never silent.
///
/// Why a tick is safe to replay: task_sheets.complete_line appends a
/// completion row and the newest row per (assignment, line) is the line's
/// state, so sending the same tick twice leaves the same state, and ticks
/// replayed in order end where the person left them. What a replay can't
/// keep: the server stamps the time (and so "late") when it receives the
/// tick, and it refuses a sheet that has closed or a day outside today ± 1 —
/// those come back as refusals and are said on the line.
///
/// Photos are not queued here (see StaffTasksStore.heldPhotos).
actor StaffOfflineQueue {
    static let shared = StaffOfflineQueue()
    static let defaultStoreKey = "staff-task-queue.v1"
    /// A tick older than this is not sent: the sheet has long closed.
    static let maxAge: TimeInterval = 24 * 60 * 60
    static let expiredReason = "It waited more than a day for a connection, so it wasn't sent."
    static let otherSessionReason = "It was ticked before a sign-out, so it wasn't sent."

    struct Refused: Equatable, Sendable {
        let tick: StaffQueuedTick
        let reason: String
    }

    struct Sent: Sendable {
        let tick: StaffQueuedTick
        let response: StaffTickResponse
    }

    /// What one drain did, for the screen to apply: each answer's sheet,
    /// each refusal's reason on its line.
    struct Replay: Sendable {
        var sent: [Sent] = []
        var refused: [Refused] = []
        /// The connection (or the server) failed partway: the rest wait.
        var stopped = false
        /// The staff session ended; the store signs out.
        var sessionEnded = false
    }

    private let storeKey: String
    private var queue: [StaffQueuedTick] = []
    private var isDraining = false

    init(storeKey: String = StaffOfflineQueue.defaultStoreKey) {
        self.storeKey = storeKey
        if let data = SecureCache.read(key: storeKey),
           let restored = try? JSONDecoder().decode([StaffQueuedTick].self, from: data) {
            queue = restored
        }
    }

    var items: [StaffQueuedTick] { queue }
    var count: Int { queue.count }

    /// Parks a tick. A later tick of the same line replaces an earlier one
    /// still waiting: a line's state is its newest tick, so only the last
    /// needs to travel.
    @discardableResult
    func enqueue(_ body: StaffTasksAPI.TickBody, label: String, owner: String,
                 now: Date = Date()) -> StaffQueuedTick {
        let tick = StaffQueuedTick(id: UUID(), body: body, label: label, createdAt: now, owner: owner)
        queue.removeAll { $0.key == tick.key }
        queue.append(tick)
        persist()
        return tick
    }

    func clear() {
        queue.removeAll()
        persist()
    }

    /// Sends what is waiting, oldest first, through `send` (the live tick
    /// call). Stops with the rest intact at the first failure that may pass
    /// (offline, a timeout, a 5xx, a rate limit) or an ended session; a
    /// refusal (the server read it and said no) is dropped and reported.
    /// Re-entrant calls (a reconnect and the screen appearing together)
    /// return an empty replay.
    func drain(owner: String, now: Date = Date(),
               send: @Sendable (StaffTasksAPI.TickBody) async throws -> StaffTickResponse) async -> Replay {
        var replay = Replay()
        guard !isDraining else { return replay }
        isDraining = true
        defer { isDraining = false }

        for tick in queue {
            if now.timeIntervalSince(tick.createdAt) >= Self.maxAge {
                replay.refused.append(Refused(tick: tick, reason: Self.expiredReason))
                remove(tick.id)
            } else if tick.owner != owner {
                replay.refused.append(Refused(tick: tick, reason: Self.otherSessionReason))
                remove(tick.id)
            }
        }

        var tried: Set<UUID> = []
        while let next = queue.first(where: { !tried.contains($0.id) }) {
            tried.insert(next.id)
            do {
                let answer = try await send(next.body)
                // Removed by id: the actor is re-entrant across the await,
                // and a newer tick of the same line may have replaced it.
                remove(next.id)
                if answer.ok {
                    replay.sent.append(Sent(tick: next, response: answer))
                } else {
                    replay.refused.append(Refused(tick: next, reason: answer.error ?? "The server didn't accept it."))
                }
            } catch is APIClient.SessionExpiredError {
                replay.sessionEnded = true
                replay.stopped = true
                return replay
            } catch {
                if let reason = Self.refusalReason(error) {
                    remove(next.id)
                    replay.refused.append(Refused(tick: next, reason: reason))
                    continue
                }
                if let api = error as? APIClient.APIError, api.kind == .decoding,
                   let status = api.status, (200..<300).contains(status) {
                    // Saved, answered with something unreadable: sent.
                    remove(next.id)
                    continue
                }
                replay.stopped = true
                return replay
            }
        }
        return replay
    }

    private func remove(_ id: UUID) {
        guard let i = queue.firstIndex(where: { $0.id == id }) else { return }
        queue.remove(at: i)
        persist()
    }

    private func persist() {
        if queue.isEmpty {
            SecureCache.delete(key: storeKey)
        } else if let data = try? JSONEncoder().encode(queue) {
            SecureCache.write(data, key: storeKey)
        }
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: Self.didChange, object: nil)
        }
    }

    static let didChange = Notification.Name("ai.cavnar.staffTaskQueueChanged")

    // MARK: Classifying a failure

    /// The attempt died in transit or the server is passing through a bad
    /// moment — worth parking the tick (it is safe to send twice).
    nonisolated static func isTransport(_ error: Error) -> Bool {
        // Any URLError: no answer came back. Even a refused certificate (a
        // venue Wi-Fi's sign-in page) never reached the server.
        if error is URLError { return true }
        if let api = error as? APIClient.APIError {
            if let status = api.status { return [408, 429, 500, 502, 503, 504].contains(status) }
            return api.kind == .offline || api.kind == .timedOut
        }
        return false
    }

    /// The server's own "no" (a 4xx it answered with words), or nil. 401 is
    /// a session matter and 408/429 pass; everything else in 4xx is final.
    nonisolated static func refusalReason(_ error: Error) -> String? {
        guard let api = error as? APIClient.APIError, let status = api.status,
              (400..<500).contains(status), ![401, 408, 429].contains(status) else { return nil }
        return api.message
    }

    /// A short, one-way fingerprint of the staff token: enough to tell this
    /// session's ticks from another's without keeping the token itself.
    nonisolated static func fingerprint(token: String?) -> String {
        guard let token, !token.isEmpty else { return "" }
        let digest = SHA256.hash(data: Data(token.utf8))
        return digest.prefix(12).map { String(format: "%02x", $0) }.joined()
    }
}
