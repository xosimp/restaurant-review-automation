import Foundation
import Observation
import UIKit

/// The Tasks screen's state, kept across tab switches: today's sheets (from
/// the phone's copy first, then the server), each line's in-flight or
/// parked tick, what each line has to say (an alert, a refusal, "will
/// send"), proof thumbnails, and photos waiting for a connection.
///
/// One per app (`shared`), bound to the staff session that loaded it: a new
/// session (another person on a shared phone) starts from nothing.
@Observable
@MainActor
final class StaffTasksStore {
    static let shared = StaffTasksStore()

    /// What a line says under itself, where the person tapped (UX-13).
    enum LineNote: Equatable {
        /// A critical reading out of range: "Tell your manager now". `offline`
        /// when the phone worked it out itself and nobody has been told.
        case alert(StaffTaskAlert, offline: Bool)
        case error(String)
        /// A neutral word: saved late, flagged, waiting to send.
        case info(String)
        /// Parked offline; sends on reconnect.
        case queued(String)
    }

    private(set) var payload: StaffTasksPayload?
    /// When `payload` was read from the server.
    private(set) var asOf: Date?
    /// The sheets on screen are the phone's copy, not a fresh read.
    private(set) var showingCached = false
    /// The last read failed (said beside the cached copy, or alone).
    private(set) var loadError: String?
    private(set) var isLoading = false
    private(set) var overlays: [String: StaffLineOverlay] = [:]
    private(set) var notes: [String: LineNote] = [:]
    private(set) var busy: Set<String> = []
    private(set) var thumbnails: [String: UIImage] = [:]
    /// Downscaled proof photos that couldn't go for want of a connection,
    /// by line key. Memory only — sent on reconnect while the app is open.
    private(set) var heldPhotos: [String: Data] = [:]
    private(set) var queuedCount = 0
    /// The screen-level confirmation (sign-off), shown as the posted moment.
    var posted: String?
    /// A screen-level failure that belongs to no line (sign-off).
    var banner: String?

    private let api: StaffTasksAPI
    private let queue: StaffOfflineQueue
    private weak var staff: StaffSessionStore?
    private var owner = ""
    private var watching = false
    private var loadingThumbs: Set<String> = []

    init(api: StaffTasksAPI = .shared, queue: StaffOfflineQueue = .shared) {
        self.api = api
        self.queue = queue
    }

    // MARK: What the screen draws

    var sheets: [StaffSheet] { StaffSheetMerge.applying(overlays, to: payload?.sheets ?? []) }
    var floor: [StaffSheet] { payload?.floor ?? [] }

    /// Last night's note, shown on the opening sheet only.
    var lastNightNote: StaffLastNightNote? {
        guard let note = payload?.lastNightNote, sheets.contains(where: { $0.shiftKind == "opening" }) else { return nil }
        return note
    }

    func note(_ key: String) -> LineNote? { notes[key] }
    func overlay(_ key: String) -> StaffLineOverlay? { overlays[key] }
    func isBusy(_ key: String) -> Bool { busy.contains(key) }
    func dismissNote(_ key: String) { notes[key] = nil }

    // MARK: Session

    /// Binds the store to the signed-in staff session. A different session
    /// than last time starts empty, and its own copy is read from disk.
    func attach(_ staff: StaffSessionStore) {
        self.staff = staff
        let fp = StaffOfflineQueue.fingerprint(token: staff.token)
        if fp != owner {
            reset()
            owner = fp
        }
        watch()
    }

    /// Everything of the last session, gone from memory.
    func reset() {
        payload = nil
        asOf = nil
        seededWithoutNote = false
        lastSeed = nil
        showingCached = false
        loadError = nil
        overlays = [:]
        notes = [:]
        busy = []
        thumbnails = [:]
        heldPhotos = [:]
        queuedCount = 0
        posted = nil
        banner = nil
        loadingThumbs = []
        owner = ""
    }

    /// The container's own /tasks read (StaffPortalStore.tasks). It counts
    /// as a fresh read — so the screen doesn't read again within the minute
    /// — except that it can't carry `last_night_note`, so a manager with an
    /// opening sheet still gets this screen's read (refreshIfNeeded).
    /// A newer container read (its pull to refresh) replaces what is shown;
    /// the note already read is kept.
    func seed(_ response: StaffTasksResponse) {
        let seeded = StaffTasksPayload(seed: response)
        // The same container read handed in again (the tab coming back into
        // view) is older than what this screen has since ticked: ignored.
        guard seeded != lastSeed else { return }
        lastSeed = seeded
        if let current = payload, !showingCached,
           current.sheets == seeded.sheets, current.floor == seeded.floor,
           current.signoffs == seeded.signoffs { return }
        var next = seeded
        next.lastNightNote = payload?.lastNightNote
        next.taskDate = payload?.taskDate
        payload = next
        asOf = Date()
        showingCached = false
        loadError = nil
        seededWithoutNote = payload?.lastNightNote == nil
        if let token = staff?.token {
            StaffReadCache.save(next, path: StaffTasksAPI.tasksPath, token: token)
        }
    }

    /// The payload on screen came from the container, which can't carry
    /// last night's note.
    private var seededWithoutNote = false
    private var lastSeed: StaffTasksPayload?

    private var needsNote: Bool {
        guard seededWithoutNote, let p = payload else { return false }
        return p.manager && p.sheets.contains { $0.shiftKind == "opening" }
    }

    /// A read when the screen comes back into view, unless the last one is
    /// under a minute old — a tab switch is not a reason to read again.
    func refreshIfNeeded(maxAge: TimeInterval = 60) async {
        if payload != nil, !showingCached, !needsNote, let asOf, Date().timeIntervalSince(asOf) < maxAge {
            await syncQueueOverlays()
            return
        }
        await load()
    }

    /// Sends what waits, then reads /tasks. With no connection the phone's
    /// copy stays up, labelled with its time; with no copy either, the
    /// screen says it failed and offers Try again — never an endless pulse.
    func load() async {
        guard let staff, let token = staff.token else { return }
        if payload == nil, let cached = StaffReadCache.load(StaffTasksPayload.self, path: StaffTasksAPI.tasksPath,
                                                             token: token) {
            payload = cached.value
            asOf = cached.savedAt
            showingCached = true
        }
        await syncQueueOverlays()
        if queuedCount > 0 { await drain() }
        isLoading = true
        defer { isLoading = false }
        do {
            let fresh = try await api.tasks(bearer: token)
            guard staff.token == token else { return }
            payload = fresh
            asOf = Date()
            showingCached = false
            loadError = nil
            seededWithoutNote = false
            StaffReadCache.save(fresh, path: StaffTasksAPI.tasksPath, token: token)
            await syncQueueOverlays()
        } catch is APIClient.SessionExpiredError {
            staff.signOut()
        } catch {
            loadError = Self.describe(error, haveCopy: payload != nil)
            if payload != nil { showingCached = true }
        }
    }

    // MARK: Ticks

    /// Ticks or un-ticks one line, at once on screen. The server's answer
    /// replaces the sheet in place; a refusal puts the line back and says
    /// why under it; no connection parks the tick (it sends on reconnect).
    func tick(_ sheet: StaffSheet, _ line: StaffSheetLine, done: Bool, value: String? = nil) async {
        let key = StaffSheetMerge.key(sheet.id, line.lineID)
        guard !busy.contains(key), let staff, let token = staff.token else { return }
        let trimmed = value.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        let body = StaffTasksAPI.TickBody(assignment_id: sheet.id, line_id: line.lineID, done: done,
                                          value: (trimmed?.isEmpty ?? true) ? nil : trimmed)
        notes[key] = nil
        overlays[key] = StaffLineOverlay(done: done, value: body.value, state: .sending)
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            let r = try await api.complete(body, bearer: token)
            overlays[key] = nil
            guard r.ok else {
                notes[key] = .error(r.error ?? "That didn't save. Try again.")
                return
            }
            await absorb(r, key: key)
        } catch is APIClient.SessionExpiredError {
            overlays[key] = nil
            staff.signOut()
        } catch {
            if StaffOfflineQueue.isTransport(error) {
                await queue.enqueue(body, label: line.label, owner: owner)
                overlays[key] = StaffLineOverlay(done: done, value: body.value, state: .queued)
                notes[key] = Self.offlineNote(line, done: done, value: body.value)
                queuedCount = await queue.count
            } else {
                overlays[key] = nil
                notes[key] = .error((error as? APIClient.APIError)?.message ?? "That didn't save. Try again.")
            }
        }
    }

    /// A tick's answer on screen: its sheet replaced by id (no second read
    /// unless the server couldn't send it), and what the line has to say.
    private func absorb(_ r: StaffTickResponse, key: String) async {
        if let sheet = r.sheet, let current = payload, let merged = StaffSheetMerge.merging(sheet, into: current) {
            payload = merged
            if let token = staff?.token {
                StaffReadCache.save(merged, path: StaffTasksAPI.tasksPath, token: token)
            }
        } else {
            Task { await self.load() }
        }
        if let alert = r.alert {
            notes[key] = .alert(alert, offline: false)
        } else if r.flagged == true {
            notes[key] = .info("Saved. It's outside its range, so your manager will see it flagged.")
        } else if r.late == true {
            notes[key] = .info("Saved after its due time.")
        } else {
            notes[key] = nil
        }
    }

    nonisolated static func offlineNote(_ line: StaffSheetLine, done: Bool, value: String?) -> LineNote {
        if done, line.critical == true, StaffSheetMerge.outOfRange(value, line: line) {
            let what = line.proofLabel ?? line.label
            let range = StaffSheetMerge.rangeLabel(line).map { ", outside \($0)" } ?? ""
            let alert = StaffTaskAlert(
                critical: true, title: "Tell your manager now",
                message: "\(what) read \(value ?? "")\(range). Your phone is offline, so nobody has been told yet "
                    + "— tell your manager in person.",
                managerAlerted: false)
            return .alert(alert, offline: true)
        }
        return .queued("Will send when you're back online.")
    }

    // MARK: Offline replay

    /// Sends the parked ticks in order and puts each answer on screen.
    func drain() async {
        // Only under the session this store was attached to: a sign-in that
        // hasn't reached the screen yet has no owner to replay as.
        guard let staff, let token = staff.token, !owner.isEmpty,
              StaffOfflineQueue.fingerprint(token: token) == owner else { return }
        let api = self.api
        let replay = await queue.drain(owner: owner) { body in
            try await api.complete(body, bearer: token)
        }
        for sent in replay.sent {
            if overlays[sent.tick.key]?.state == .queued { overlays[sent.tick.key] = nil }
            await absorb(sent.response, key: sent.tick.key)
        }
        for refused in replay.refused {
            overlays[refused.tick.key] = nil
            notes[refused.tick.key] = .error("Not sent: \(refused.reason)")
        }
        if replay.sessionEnded { staff.signOut() }
        await syncQueueOverlays()
    }

    /// The parked ticks of this session as overlays on their lines.
    private func syncQueueOverlays() async {
        let items = await queue.items.filter { $0.owner == owner }
        queuedCount = items.count
        let queuedKeys = Set(items.map(\.key))
        for (key, o) in overlays where o.state == .queued && !queuedKeys.contains(key) {
            overlays[key] = nil
            if case .queued = notes[key] { notes[key] = nil }
        }
        for item in items where overlays[item.key]?.state != .sending {
            overlays[item.key] = StaffLineOverlay(done: item.body.done, value: item.body.value, state: .queued)
            if notes[item.key] == nil { notes[item.key] = .queued("Will send when you're back online.") }
        }
    }

    /// Reconnects drain the queue and send held photos; a sign-out (the
    /// staff token gone) clears this session's copy from the phone.
    private func watch() {
        guard !watching else { return }
        watching = true
        observe()
    }

    private func observe() {
        withObservationTracking {
            _ = NetworkMonitor.shared.isOnline
            _ = staff?.token
        } onChange: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.staff?.token == nil, !self.owner.isEmpty {
                    StaffLocalData.clearForSignOut()
                } else if NetworkMonitor.shared.isOnline {
                    await self.drain()
                    await self.sendHeldPhotos()
                }
                self.observe()
            }
        }
    }

    // MARK: Photos

    /// Downscales on the phone (PERF-06), then ticks the line with it.
    func sendPhoto(_ image: UIImage, sheet: StaffSheet, line: StaffSheetLine) async {
        let key = StaffSheetMerge.key(sheet.id, line.lineID)
        busy.insert(key)
        notes[key] = nil
        let jpeg = await Task.detached(priority: .userInitiated) { StaffPhotoPrep.jpeg(from: image) }.value
        busy.remove(key)
        guard let jpeg else {
            notes[key] = .error("That photo couldn't be read. Try another.")
            return
        }
        await upload(jpeg, sheetID: sheet.id, lineID: line.lineID)
    }

    private func upload(_ jpeg: Data, sheetID: Int, lineID: Int) async {
        let key = StaffSheetMerge.key(sheetID, lineID)
        guard !busy.contains(key), let staff, let token = staff.token else { return }
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            let r = try await api.photo(jpeg: jpeg, assignmentID: sheetID, lineID: lineID, bearer: token)
            guard r.ok else {
                notes[key] = .error(r.error ?? "That photo didn't save. Try again.")
                return
            }
            heldPhotos[key] = nil
            if let token = r.sheet?.lines.first(where: { $0.lineID == lineID })?.photo,
               let image = UIImage(data: jpeg) {
                thumbnails[token] = image
            }
            await absorb(r, key: key)
        } catch is APIClient.SessionExpiredError {
            staff.signOut()
        } catch {
            if StaffOfflineQueue.isTransport(error) {
                heldPhotos[key] = jpeg
                notes[key] = .queued("A photo needs a connection. It's kept here until you close the app "
                                     + "and sends when you're back online.")
            } else {
                notes[key] = .error((error as? APIClient.APIError)?.message ?? "That photo didn't save. Try again.")
            }
        }
    }

    /// Sends a held photo now (its line's "Send photo").
    func retryHeldPhoto(_ key: String) async {
        guard let jpeg = heldPhotos[key] else { return }
        let parts = key.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 2 else { return }
        await upload(jpeg, sheetID: parts[0], lineID: parts[1])
    }

    private func sendHeldPhotos() async {
        for key in heldPhotos.keys.sorted() { await retryHeldPhoto(key) }
    }

    /// A proof photo's thumbnail, read once per screen and kept in memory
    /// (the server sends it no-store).
    func loadThumbnail(_ token: String) async {
        guard thumbnails[token] == nil, !loadingThumbs.contains(token),
              let bearer = staff?.token else { return }
        loadingThumbs.insert(token)
        defer { loadingThumbs.remove(token) }
        if let data = try? await api.photoData(token: token, bearer: bearer), let image = UIImage(data: data) {
            thumbnails[token] = image
        }
    }

    // MARK: Sign-off

    func signOff(_ kind: String, note: String) async {
        guard let staff, let token = staff.token else { return }
        let key = "signoff-" + kind
        busy.insert(key)
        defer { busy.remove(key) }
        banner = nil
        do {
            let r = try await api.signOff(.init(shift_kind: kind, note: note), bearer: token)
            guard r.ok else { banner = r.error ?? "That didn't sign off."; return }
            posted = "Signed off"
            await load()
        } catch is APIClient.SessionExpiredError {
            staff.signOut()
        } catch {
            banner = StaffOfflineQueue.isTransport(error)
                ? "Signing off needs a connection. Try again when you're back online."
                : ((error as? APIClient.APIError)?.message ?? "That didn't sign off.")
        }
    }

    // MARK: Words

    nonisolated static func describe(_ error: Error, haveCopy: Bool) -> String {
        if StaffOfflineQueue.isTransport(error) {
            return haveCopy ? "You're offline. Ticks you make will send when you're back."
                            : "Couldn't load your sheets. Check your connection and try again."
        }
        return (error as? APIClient.APIError)?.message ?? "Couldn't load your sheets."
    }
}

extension StaffPhotoPrep {
    /// The photo drawn at `targetSize` (orientation applied, 1x) and encoded
    /// as JPEG at `quality`, stepping the quality down if it is still over
    /// `maxBytes`. Nil when it can't be drawn.
    nonisolated static func jpeg(from image: UIImage) -> Data? {
        let pixelSize = CGSize(width: image.size.width * image.scale, height: image.size.height * image.scale)
        let target = targetSize(for: pixelSize)
        guard target.width > 0, target.height > 0 else { return nil }
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        format.opaque = true
        let drawn = UIGraphicsImageRenderer(size: target, format: format).image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        var q = quality
        var data = drawn.jpegData(compressionQuality: q)
        while let d = data, d.count > maxBytes, q > 0.35 {
            q -= 0.15
            data = drawn.jpegData(compressionQuality: q)
        }
        return data
    }
}
