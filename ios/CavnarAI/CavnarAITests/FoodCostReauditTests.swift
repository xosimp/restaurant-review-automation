import XCTest
@testable import CavnarAI

/// The blind re-audit (10/8/26) of the Food Cost and nightly-report fix
/// round, the phone's half:
///
///   #1  a count parked offline carries its ledger mark and the time it was
///       taken, so the server applies what was posted after it instead of
///       letting the late row erase it; a parked count the queue gives up
///       comes back still carrying them, so the delivery question is asked.
///   #2  the draft stays until the parked count lands; Discard and an edit
///       take the parked write back; parked counts are kept, not "unsaved".
///   #3  a location switch spares the count drafts.
///   #6  the report switch is per login and location, and goes at sign-out.
///   #7  a parked receive replayed onto a received order is done, not
///       dropped; parked receives and waste are read back from the queue.
///   #8  a waste line's idempotency key belongs to that line only.
///   #9  lines a save left behind stay on the sheet, named.
@MainActor
final class FoodCostReauditTests: XCTestCase {
    private var savedUser = 0
    private var savedRestaurant = 0

    override func setUp() async throws {
        savedUser = SessionScope.userId
        savedRestaurant = SessionScope.restaurantId
        SessionScope.begin(userId: 901, restaurantId: 77)
        CountSheetDraft.clear()
        await PendingWriteQueue.shared.clear()
        await PendingWriteQueue.shared.setActiveRestaurant(77)
    }

    override func tearDown() async throws {
        CountSheetDraft.clear()
        await PendingWriteQueue.shared.clear()
        SessionScope.begin(userId: savedUser, restaurantId: savedRestaurant)
    }

    nonisolated private static let sheet = """
    {"ok": true, "ledger_mark": 41, "count": 2, "source": {"synced": false},
     "items": [{"ingredient_id": 4, "name": "Salmon", "unit": "lb", "expected": 10},
               {"ingredient_id": 5, "name": "Lemons", "unit": "ea", "expected": 8}]}
    """

    /// Answers GET with the sheet (at `mark`), and every POST with `post`.
    private final class Server: @unchecked Sendable {
        private let lock = NSLock()
        private var _mark = 41
        private var _post: (Int, String)? = nil      // nil = no signal
        private var _bodies: [[String: Any]] = []
        var mark: Int { get { lock.withLock { _mark } } set { lock.withLock { _mark = newValue } } }
        var post: (Int, String)? { get { lock.withLock { _post } } set { lock.withLock { _post = newValue } } }
        var bodies: [[String: Any]] { lock.withLock { _bodies } }
        func record(_ b: [String: Any]?) { lock.withLock { if let b { _bodies.append(b) } } }
    }

    private func client(_ server: Server) -> APIClient {
        EdgeHTTP.client { request in
            if request.httpMethod == "GET" {
                return EdgeHTTP.reply(request, 200, Self.sheet.replacingOccurrences(of: "\"ledger_mark\": 41",
                                                                                     with: "\"ledger_mark\": \(server.mark)"))
            }
            server.record(EdgeHTTP.bodyJSON(request))
            guard let (status, json) = server.post else { throw URLError(.notConnectedToInternet) }
            return EdgeHTTP.reply(request, status, json)
        }
    }

    private func parkedBodies() async throws -> [[String: Any]] {
        try await PendingWriteQueue.shared.pending(path: QueuedWrite.countSheetPath).map {
            try XCTUnwrap(JSONSerialization.jsonObject(with: try XCTUnwrap($0.bodyJSON)) as? [String: Any])
        }
    }

    /// Waits for a fire-and-forget queue change (a cancel from a tap).
    private func settle(until done: () async -> Bool) async {
        var tries = 0
        while tries < 200, !(await done()) {
            tries += 1
            await Task.yield()
        }
    }

    private func parkACount(_ server: Server) async throws -> CountSheetViewModel {
        let vm = CountSheetViewModel(client: client(server))
        await vm.load()
        XCTAssertEqual(vm.ledgerMark, 41)
        vm.counts[4] = "6"
        await vm.save()
        return vm
    }

    // MARK: #1, #2 — a parked count

    func testAParkedCountCarriesItsMarkAndWhenItWasTakenAndKeepsItsDraft() async throws {
        let server = Server()
        let vm = try await parkACount(server)
        let parked = try await parkedBodies()
        XCTAssertEqual(parked.count, 1)
        let body = try XCTUnwrap(parked.first)
        XCTAssertEqual(body["ledger_mark"] as? Int, 41, "the mark the sheet was opened on rides with the parked count")
        let stamp = try XCTUnwrap(body["counted_at"] as? String)
        XCTAssertNotNil(ISO8601DateFormatter().date(from: stamp), "counted_at is ISO 8601 with a zone: \(stamp)")
        XCTAssertEqual((body["items"] as? [[String: Any]])?.first?["counted"] as? Double, 6)

        XCTAssertEqual(vm.queuedCount, 1)
        XCTAssertNotNil(vm.queuedWriteId)
        XCTAssertFalse(vm.hasUnsaved, "kept counts close the sheet without a warning")
        let draft = try XCTUnwrap(CountSheetDraft.load(), "the draft stays until the server has the count")
        XCTAssertEqual(draft.queuedWriteId, vm.queuedWriteId)
        XCTAssertEqual(draft.ledgerMark, 41)
        XCTAssertNotNil(draft.countedAt)
        XCTAssertEqual(draft.counts[4], "6")
    }

    func testTheDraftIsClearedOnlyWhenTheParkedCountLands() async throws {
        let server = Server()
        _ = try await parkACount(server)
        server.post = (200, #"{"ok": true, "written": 1, "skipped": 0, "skipped_ids": [], "superseded": 0}"#)
        await PendingWriteQueue.shared.drain(client: client(server))
        let left = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(left, 0)
        XCTAssertNil(CountSheetDraft.load(), "landed: the kept counts go")
        let dropped = await PendingWriteQueue.shared.dropped
        XCTAssertTrue(dropped.isEmpty)
    }

    func testARefusedParkedCountComesBackAndAsksTheDeliveryQuestion() async throws {
        let server = Server()
        let vm = try await parkACount(server)
        let firstParked = try await parkedBodies().first
        let stamp = try XCTUnwrap(firstParked?["counted_at"] as? String)
        // The server, at replay: a delivery came in before the count was taken.
        server.post = (409, """
        {"ok": false, "needs_confirm": true,
         "deliveries": [{"ingredient_id": 4, "qty": 40, "name": "Salmon", "unit": "lb"}],
         "error": "A delivery was received after this count was started \\u2014 open the count sheet and say whether your count includes it."}
        """)
        await PendingWriteQueue.shared.drain(client: client(server))
        let dropped = await PendingWriteQueue.shared.dropped
        XCTAssertEqual(dropped.count, 1)
        XCTAssertTrue(dropped.first?.reason.contains("open the count sheet") == true)
        XCTAssertNotNil(CountSheetDraft.load(), "given up by the queue: the counts are not lost")

        // The next open finds them back on the sheet...
        server.mark = 55          // the ledger has moved on since
        let reopened = CountSheetViewModel(client: client(server))
        await reopened.load()
        XCTAssertTrue(reopened.returnedFromQueue)
        XCTAssertEqual(reopened.counts[4], "6")
        XCTAssertTrue(reopened.hasUnsaved)
        XCTAssertNil(CountSheetDraft.load()?.queuedWriteId)
        // ...and a sheet left open hears it from the queue.
        await vm.queueChanged()
        XCTAssertTrue(vm.returnedFromQueue)
        XCTAssertNil(vm.queuedWriteId)
        XCTAssertTrue(vm.hasUnsaved)

        await reopened.save()
        let sent = try XCTUnwrap(server.bodies.last)
        XCTAssertEqual(sent["ledger_mark"] as? Int, 41, "asked against the mark the count was started on, not the newer one")
        XCTAssertEqual(sent["counted_at"] as? String, stamp, "still the time it was taken")
        XCTAssertNotNil(reopened.deliveryQuestion)
        XCTAssertEqual(reopened.pendingDeliveries.first?.qty, 40)
    }

    func testDiscardTakesTheParkedCountBack() async throws {
        let vm = try await parkACount(Server())
        vm.discard()
        await settle { await PendingWriteQueue.shared.pendingCount == 0 }
        let left = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(left, 0, "discarded counts never send later")
        XCTAssertNil(CountSheetDraft.load())
        XCTAssertNil(vm.queuedCount)
    }

    func testEditingAParkedCountTakesItOffTheQueueAndKeepsTheEdit() async throws {
        let vm = try await parkACount(Server())
        vm.counts[4] = "7"
        XCTAssertNil(vm.queuedWriteId)
        XCTAssertTrue(vm.hasUnsaved)
        await settle { await PendingWriteQueue.shared.pendingCount == 0 }
        let left = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(left, 0, "the stale parked copy never lands over the newer figure")
        let draft = try XCTUnwrap(CountSheetDraft.load())
        XCTAssertEqual(draft.counts[4], "7")
        XCTAssertNil(draft.queuedWriteId)
        XCTAssertNil(draft.countedAt, "an edit is a count taken now")
        XCTAssertEqual(draft.ledgerMark, 41)
    }

    func testASecondSaveOnlineCancelsTheParkedCopy() async throws {
        let server = Server()
        let vm = try await parkACount(server)
        server.post = (200, #"{"ok": true, "written": 1, "skipped": 0, "skipped_ids": []}"#)
        await vm.save()
        let left = await PendingWriteQueue.shared.pendingCount
        XCTAssertEqual(left, 0)
        XCTAssertEqual(vm.savedCount, 1)
        XCTAssertNil(CountSheetDraft.load())
    }

    func testAnOldDraftWithoutTheNewFieldsStillDecodes() throws {
        let old = #"{"counts": {"4": "6"}, "savedAt": 781000000}"#
        let d = try JSONDecoder().decode(CountSheetDraft.self, from: Data(old.utf8))
        XCTAssertEqual(d.counts[4], "6")
        XCTAssertNil(d.queuedWriteId)
        XCTAssertNil(d.ledgerMark)
    }

    // MARK: #9 — what a save left behind

    func testLinesASaveLeftBehindStayOnTheSheetNamed() async throws {
        let server = Server()
        server.post = (200, #"{"ok": true, "written": 1, "skipped": 0, "skipped_ids": [], "superseded": 0}"#)
        let vm = CountSheetViewModel(client: client(server))
        await vm.load()
        vm.counts[4] = "6"
        vm.counts[5] = "a few"
        XCTAssertEqual(vm.unusable.map(\.name), ["Lemons"])
        await vm.save()
        XCTAssertEqual(vm.savedCount, 1)
        XCTAssertEqual(vm.counts[5], "a few", "never dropped from the sheet unnamed")
        let line = try XCTUnwrap(vm.notSavedLine)
        XCTAssertTrue(line.contains("Lemons") && line.hasPrefix("1 line not saved"), line)
        XCTAssertEqual(CountSheetDraft.load()?.counts[5], "a few")
    }

    func testTheNotSavedSentence() {
        XCTAssertNil(CountSheetViewModel.notSavedSentence([]))
        XCTAssertEqual(CountSheetViewModel.notSavedSentence(["A", "B", "C", "D", "E"]),
                       "5 lines not saved \u{2014} A, B, C and 2 more. Each needs a number of 0 or more; they stay on the sheet.")
    }

    // MARK: #3 — a location switch spares the drafts

    func testALocationSwitchSparesCountDraftsAndSignOutDoesNot() {
        let draftKey = "\(CountSheetDraft.keyPrefix)u901.r12.json"
        SecureCache.write(Data("{}".utf8), key: draftKey)
        SecureCache.write(Data("{}".utf8), key: "labor.cachedStats.12")
        SecureCache.purgeAll(keeping: [PendingWriteQueue.storeKey], keepingPrefixes: [CountSheetDraft.keyPrefix])
        XCTAssertNotNil(SecureCache.read(key: draftKey), "the other location's walk-in count survives the switch")
        XCTAssertNil(SecureCache.read(key: "labor.cachedStats.12"))
        SecureCache.purgeAll()
        XCTAssertNil(SecureCache.read(key: draftKey), "sign-out takes every draft")
    }

    // MARK: #6 — the report switch per login and location

    func testTheReportSwitchIsPerLoginAndLocationAndGoesAtSignOut() {
        DSRAvailability.clearAll()
        SessionScope.begin(userId: 901, restaurantId: 2)
        DSRAvailability.record(false)
        XCTAssertFalse(DSRAvailability.isEnabled)
        SessionScope.begin(userId: 901, restaurantId: 3)
        XCTAssertTrue(DSRAvailability.isEnabled, "another location's switch is its own")
        SessionScope.begin(userId: 902, restaurantId: 2)
        XCTAssertTrue(DSRAvailability.isEnabled, "another login starts from on")
        SessionScope.begin(userId: 901, restaurantId: 2)
        XCTAssertFalse(DSRAvailability.isEnabled)
        DSRAvailability.clearAll()
        XCTAssertTrue(DSRAvailability.isEnabled)
    }

    // MARK: #7 — parked receives and waste

    func testAParkedReceiveLandingOnAReceivedOrderIsDoneNotDropped() async {
        let q = PendingWriteQueue.shared
        await q.enqueue(path: QueuedWrite.receivePath(31), method: "POST", bodyJSON: Data("{}".utf8), label: "Receive PO-31")
        await q.enqueue(path: QueuedWrite.receivePath(32), method: "POST", bodyJSON: Data("{}".utf8), label: "Receive PO-32")
        let parked = await q.pending(pathPrefix: QueuedWrite.receivePathPrefix).compactMap { QueuedWrite.receivePoId($0.path) }
        XCTAssertEqual(parked, [31, 32], "the deliveries list reads these back after a relaunch")
        let api = EdgeHTTP.client { request in
            if request.url?.path == QueuedWrite.receivePath(31) {
                return EdgeHTTP.reply(request, 404, """
                {"ok": false, "code": "already_received", "po_number": "PO-31", "error": "That order is already received, or isn't yours."}
                """)
            }
            return EdgeHTTP.reply(request, 404, #"{"ok": false, "error": "That order is already received, or isn't yours."}"#)
        }
        await q.drain(client: api)
        let left = await q.pendingCount
        XCTAssertEqual(left, 0)
        let dropped = await q.dropped
        XCTAssertEqual(dropped.map(\.label), ["Receive PO-32"], "only the one that is not someone's received order is reported")
    }

    func testAReceivePathReadsBackItsOrder() {
        XCTAssertEqual(QueuedWrite.receivePoId(QueuedWrite.receivePath(77)), 77)
        XCTAssertNil(QueuedWrite.receivePoId(QueuedWrite.wastePath))
        XCTAssertNil(QueuedWrite.receivePoId("/mobile/api/food-cost/purchase-orders/x/received"))
    }

    func testParkedWasteIsReadBackFromTheQueue() async throws {
        let body = WasteLogViewModel.Body(ingredientId: 4, qty: 2, reason: "spoiled", idempotencyKey: "waste:k")
        await PendingWriteQueue.shared.enqueue(try XCTUnwrap(QueuedWrite.waste(body)))
        let vm = WasteLogViewModel()
        await vm.refreshParked()
        XCTAssertEqual(vm.parked, [body])
        let items = try JSONDecoder.cavnar.decode([CountSheetItem].self, from: Data("""
        [{"ingredient_id": 4, "name": "Salmon", "unit": "lb", "expected": 10}]
        """.utf8))
        XCTAssertEqual(WasteLogForm.parkedLine(body, items: items), "2 lb Salmon \u{00B7} Spoiled")
    }

    // MARK: #8 — one key per waste line

    func testAWasteKeyIsKeptForTheSameLineAndNeverReusedForAnother() {
        let vm = WasteLogViewModel()
        let line = WasteLogViewModel.Body(ingredientId: 4, qty: 2, reason: "spoiled")
        let first = vm.key(for: line)
        XCTAssertEqual(vm.key(for: line), first, "a retry of the same line is the same write")
        let other = WasteLogViewModel.Body(ingredientId: 4, qty: 3, reason: "spoiled")
        XCTAssertNotEqual(vm.key(for: other), first, "a different line after a failed send gets its own key")
        XCTAssertNotEqual(vm.key(for: line), first, "and the first line's key is not resurrected")
    }
}
