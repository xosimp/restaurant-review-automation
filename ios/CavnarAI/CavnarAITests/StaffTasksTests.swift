import UIKit
import XCTest
@testable import CavnarAI

/// The staff app's task sheets (employee audit I4): the tick's answer
/// replaces its sheet in place by id, a tick parked offline replays in order
/// under the session that made it, a proof photo is shrunk on the phone, and
/// the "Next shift" widget says only what the last read supports.
final class StaffTasksTests: XCTestCase {

    // MARK: Fixtures (the server's own shapes: task_sheets._assignment_view)

    private static let sheetJSON = """
    {"id": 41, "sheet_id": 3, "task_date": "2026-10-02", "job_code": "Kitchen", "shift_kind": "opening",
     "shift_label": "Opening", "title": "Opening — Kitchen", "assignees": ["Ana"], "unassigned": false,
     "shift_start": "2026-10-02T09:00:00", "shift_end": "2026-10-02T15:00:00", "status": "open",
     "done": 1, "total": 2, "overdue": 0, "critical_open": 1, "late": 0, "flagged": 1, "manager": false,
     "lines": [
       {"line_id": 7, "label": "Walk-in temp", "section": "Cold", "due_offset_min": 30,
        "due_at": "2026-10-02T09:30:00", "proof": "number", "proof_label": "Walk-in °F",
        "min_value": 33, "max_value": 41, "critical": 1, "done": true, "overdue": false,
        "completed_by": "Ana", "completed_at": "2026-10-02T09:12:00", "late": false, "flagged": true,
        "proof_value": 46, "photo": null},
       {"line_id": 8, "label": "Sanitizer buckets", "section": "Cold", "due_offset_min": null,
        "due_at": null, "proof": "none", "proof_label": null, "min_value": null, "max_value": null,
        "critical": false, "done": false, "overdue": false, "completed_by": null, "completed_at": null,
        "late": false, "flagged": false, "proof_value": null, "photo": null}
     ]}
    """

    private func sheet(_ json: String = sheetJSON) throws -> StaffSheet {
        try JSONDecoder().decode(StaffSheet.self, from: Data(json.utf8))
    }

    // MARK: Decoding

    func testTheTickAnswerCarriesTheSheetAndTheAlert() throws {
        let json = """
        {"ok": true, "late": false, "flagged": true,
         "alert": {"critical": true, "title": "Tell your manager now",
                   "message": "Tell your manager now: Walk-in °F read 46, outside 33–41.", "manager_alerted": false},
         "sheet": \(Self.sheetJSON), "task_date": "2026-10-02"}
        """
        let r = try JSONDecoder().decode(StaffTickResponse.self, from: Data(json.utf8))
        XCTAssertTrue(r.ok)
        XCTAssertEqual(r.alert?.title, "Tell your manager now")
        XCTAssertEqual(r.alert?.managerAlerted, false)
        XCTAssertEqual(r.sheet?.id, 41)
        XCTAssertEqual(r.sheet?.taskDate, "2026-10-02")
        let line = try XCTUnwrap(r.sheet?.lines.first)
        XCTAssertEqual(line.critical, true, "a 0/1 critical from an older snapshot reads as a Bool")
        XCTAssertEqual(line.proofValue, "46", "a numeric proof value reads as text")
        XCTAssertEqual(line.minValue, 33)
        XCTAssertEqual(line.maxValue, 41)
        XCTAssertEqual(StaffLineNoteView.body(of: r.alert?.message ?? "", title: "Tell your manager now"),
                       "Walk-in °F read 46, outside 33–41.")
    }

    func testAnOlderTickAnswerWithoutTheSheetStillDecodes() throws {
        let r = try JSONDecoder().decode(StaffTickResponse.self,
                                         from: Data(#"{"ok": true, "late": true, "flagged": false}"#.utf8))
        XCTAssertNil(r.sheet)
        XCTAssertNil(r.alert)
        XCTAssertEqual(r.late, true)
    }

    func testTheTasksPayloadReadsLastNightsNoteAndRoundTrips() throws {
        let json = """
        {"ok": true, "role": "Manager", "task_date": "2026-10-02", "sheets": [\(Self.sheetJSON)],
         "manager": true, "floor": [], "signoffs": [], "can_sign_off": ["opening"],
         "last_night_note": {"note": "Ice machine is leaking", "signed_by": "Erik",
                             "signed_at": "2026-10-02T03:10:00Z", "shift_kind": "closing",
                             "task_date": "2026-10-01", "date_label": "10/1/26"}}
        """
        let p = try JSONDecoder().decode(StaffTasksPayload.self, from: Data(json.utf8))
        XCTAssertEqual(p.lastNightNote?.note, "Ice machine is leaking")
        XCTAssertEqual(p.lastNightNote?.dateLabel, "10/1/26")
        XCTAssertEqual(p.canSignOff, ["opening"])
        // The phone's copy (StaffReadCache) is this payload re-encoded.
        let again = try JSONDecoder().decode(StaffTasksPayload.self, from: JSONEncoder().encode(p))
        XCTAssertEqual(again, p)
    }

    func testAPayloadWithoutTheNewFieldsStillDecodes() throws {
        let p = try JSONDecoder().decode(StaffTasksPayload.self,
                                         from: Data(#"{"ok": true, "role": "Server", "tasks": []}"#.utf8))
        XCTAssertTrue(p.sheets.isEmpty)
        XCTAssertNil(p.lastNightNote)
        XCTAssertFalse(p.manager)
    }

    // MARK: Merge by id

    func testTheTicksSheetReplacesItsOwnSheetInPlace() throws {
        let original = try sheet()
        let other = StaffSheet(id: 52, title: "Closing — Bar", shiftKind: "closing", shiftStart: nil, shiftEnd: nil,
                           assignees: ["Ana"], unassigned: false, status: "open", done: 0, total: 0,
                           overdue: 0, lines: [])
        var updated = original
        updated.done = 2
        updated.lines[1].done = true
        let payload = StaffTasksPayload(sheets: [original, other])

        let merged = try XCTUnwrap(StaffSheetMerge.merging(updated, into: payload))
        XCTAssertEqual(merged.sheets.map(\.id), [41, 52], "order kept")
        XCTAssertEqual(merged.sheets[0].done, 2)
        XCTAssertEqual(merged.sheets[1], other, "the other sheet is untouched")
    }

    func testAFloorSheetMergesIntoTheFloorAndAStrangerAsksForARead() throws {
        let s = try sheet()
        var updated = s
        updated.done = 2
        let floorPayload = StaffTasksPayload(manager: true, floor: [s])
        XCTAssertEqual(StaffSheetMerge.merging(updated, into: floorPayload)?.floor.first?.done, 2)
        XCTAssertNil(StaffSheetMerge.merging(updated, into: StaffTasksPayload()),
                     "a sheet the screen doesn't have means read /tasks again")
    }

    func testOverlaysShowTheTickAtOnceAndTheCountFollows() throws {
        let s = try sheet()
        let key = StaffSheetMerge.key(41, 8)
        let ticked = StaffSheetMerge.applying([key: StaffLineOverlay(done: true, value: nil, state: .sending)], to: [s])
        XCTAssertEqual(ticked[0].done, 2)
        XCTAssertTrue(ticked[0].lines[1].done)

        let unticked = StaffSheetMerge.applying(
            [StaffSheetMerge.key(41, 7): StaffLineOverlay(done: false, value: nil, state: .queued)], to: [s])
        XCTAssertEqual(unticked[0].done, 0)
        XCTAssertNil(unticked[0].lines[0].proofValue, "an un-tick clears the reading it showed")
        XCTAssertEqual(StaffSheetMerge.applying([:], to: [s]), [s])
    }

    func testAReadingOutOfRangeIsSeenOfflineAsTheServerWouldSeeIt() throws {
        let line = try sheet().lines[0]
        XCTAssertTrue(StaffSheetMerge.outOfRange("46", line: line))
        XCTAssertTrue(StaffSheetMerge.outOfRange("32.5", line: line))
        XCTAssertFalse(StaffSheetMerge.outOfRange("41", line: line), "bounds are inclusive")
        XCTAssertFalse(StaffSheetMerge.outOfRange("cold", line: line))
        XCTAssertEqual(StaffSheetMerge.rangeLabel(line), "33–41")

        guard case let .alert(alert, offline) = StaffTasksStore.offlineNote(line, done: true, value: "46") else {
            return XCTFail("a critical reading out of range offline must say tell your manager")
        }
        XCTAssertTrue(offline)
        XCTAssertEqual(alert.title, "Tell your manager now")
        XCTAssertEqual(StaffTasksStore.offlineNote(line, done: true, value: "38"),
                       .queued("Will send when you're back online."))
    }

    // MARK: Photo

    func testAPhotoIsSizedDownToItsLongEdgeAndNeverUp() {
        XCTAssertEqual(StaffPhotoPrep.targetSize(for: CGSize(width: 4032, height: 3024)), CGSize(width: 1600, height: 1200))
        XCTAssertEqual(StaffPhotoPrep.targetSize(for: CGSize(width: 3024, height: 4032)), CGSize(width: 1200, height: 1600))
        XCTAssertEqual(StaffPhotoPrep.targetSize(for: CGSize(width: 8064, height: 6048)), CGSize(width: 1600, height: 1200))
        XCTAssertEqual(StaffPhotoPrep.targetSize(for: CGSize(width: 1000, height: 800)), CGSize(width: 1000, height: 800))
        XCTAssertEqual(StaffPhotoPrep.targetSize(for: .zero), .zero)
    }

    func testAPhotoIsEncodedSmallEnoughForTheServer() throws {
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let big = UIGraphicsImageRenderer(size: CGSize(width: 4000, height: 3000), format: format).image { ctx in
            UIColor.orange.setFill()
            ctx.fill(CGRect(x: 0, y: 0, width: 4000, height: 3000))
        }
        let jpeg = try XCTUnwrap(StaffPhotoPrep.jpeg(from: big))
        XCTAssertLessThanOrEqual(jpeg.count, StaffPhotoPrep.maxBytes)
        let decoded = try XCTUnwrap(UIImage(data: jpeg))
        XCTAssertEqual(max(decoded.size.width * decoded.scale, decoded.size.height * decoded.scale), 1600)
    }

    func testThePhotoGoesAsMultipartWithItsSheetAndLine() {
        let body = StaffTasksAPI.multipart(boundary: "B", fields: ["assignment_id": "41", "line_id": "7"],
                                           file: Data([0xFF, 0xD8]), filename: "proof.jpg", mime: "image/jpeg")
        let text = String(decoding: body, as: UTF8.self)
        XCTAssertTrue(text.contains("name=\"assignment_id\"\r\n\r\n41\r\n"))
        XCTAssertTrue(text.contains("name=\"line_id\"\r\n\r\n7\r\n"))
        XCTAssertTrue(text.contains("name=\"file\"; filename=\"proof.jpg\""))
        XCTAssertTrue(text.hasSuffix("--B--\r\n"))
    }

    // MARK: Offline queue

    private actor Recorder {
        var sent: [StaffTasksAPI.TickBody] = []
        func record(_ b: StaffTasksAPI.TickBody) { sent.append(b) }
    }

    private func freshQueue() async -> StaffOfflineQueue {
        let q = StaffOfflineQueue(storeKey: "test-staff-task-queue-\(UUID().uuidString)")
        await q.clear()
        return q
    }

    private func body(_ a: Int, _ l: Int, _ done: Bool = true, _ value: String? = nil) -> StaffTasksAPI.TickBody {
        StaffTasksAPI.TickBody(assignment_id: a, line_id: l, done: done, value: value)
    }

    private static let okAnswer = StaffTickResponse(ok: true, error: nil, late: false, flagged: false)

    func testParkedTicksReplayInOrderAndALaterTickOfTheSameLineReplacesTheEarlier() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7, true, "38"), label: "Walk-in temp", owner: "me")
        await q.enqueue(body(41, 8), label: "Sanitizer", owner: "me")
        await q.enqueue(body(41, 7, true, "39"), label: "Walk-in temp", owner: "me")
        let queued = await q.items
        XCTAssertEqual(queued.map(\.key), ["41-8", "41-7"])
        XCTAssertEqual(queued.last?.body.value, "39", "only the newest state of a line travels")

        let rec = Recorder()
        let replay = await q.drain(owner: "me") { b in
            await rec.record(b)
            return Self.okAnswer
        }
        let sent = await rec.sent
        XCTAssertEqual(sent.map(\.line_id), [8, 7])
        XCTAssertEqual(replay.sent.count, 2)
        XCTAssertFalse(replay.stopped)
        let left = await q.count
        XCTAssertEqual(left, 0)
    }

    func testNoConnectionStopsTheReplayWithTheRestIntact() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7), label: "A", owner: "me")
        await q.enqueue(body(41, 8), label: "B", owner: "me")
        let replay = await q.drain(owner: "me") { _ in throw URLError(.notConnectedToInternet) }
        XCTAssertTrue(replay.stopped)
        XCTAssertTrue(replay.sent.isEmpty)
        let left = await q.items.map(\.label)
        XCTAssertEqual(left, ["A", "B"], "nothing lost, order kept")
        await q.clear()
    }

    func testARefusalIsDroppedAndSaidWhileTheRestStillSend() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7), label: "Closed one", owner: "me")
        await q.enqueue(body(52, 1), label: "Open one", owner: "me")
        let replay = await q.drain(owner: "me") { b in
            if b.assignment_id == 41 {
                throw APIClient.APIError(kind: .server, message: "That sheet closed at the end of the shift.", status: 400)
            }
            return Self.okAnswer
        }
        XCTAssertEqual(replay.refused.map(\.tick.label), ["Closed one"])
        XCTAssertEqual(replay.refused.first?.reason, "That sheet closed at the end of the shift.")
        XCTAssertEqual(replay.sent.map(\.tick.label), ["Open one"])
        let left = await q.count
        XCTAssertEqual(left, 0)
    }

    func testAnotherSessionsTicksAreNeverSentAsThisOnes() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7), label: "Ana's tick", owner: "ana")
        let rec = Recorder()
        let replay = await q.drain(owner: "ben") { b in
            await rec.record(b)
            return Self.okAnswer
        }
        let sent = await rec.sent
        XCTAssertTrue(sent.isEmpty)
        XCTAssertEqual(replay.refused.first?.reason, StaffOfflineQueue.otherSessionReason)
    }

    func testADayOldTickIsNotSent() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7), label: "Old", owner: "me", now: Date().addingTimeInterval(-25 * 3600))
        let replay = await q.drain(owner: "me") { _ in Self.okAnswer }
        XCTAssertEqual(replay.refused.first?.reason, StaffOfflineQueue.expiredReason)
        XCTAssertTrue(replay.sent.isEmpty)
    }

    func testAnEndedSessionStopsTheReplay() async {
        let q = await freshQueue()
        await q.enqueue(body(41, 7), label: "A", owner: "me")
        let replay = await q.drain(owner: "me") { _ in throw APIClient.SessionExpiredError() }
        XCTAssertTrue(replay.sessionEnded)
        let left = await q.count
        XCTAssertEqual(left, 1)
        await q.clear()
    }

    func testWhatCountsAsNoConnectionAndWhatIsTheServersNo() {
        XCTAssertTrue(StaffOfflineQueue.isTransport(URLError(.timedOut)))
        XCTAssertTrue(StaffOfflineQueue.isTransport(APIClient.APIError(message: "x", status: 503)))
        XCTAssertFalse(StaffOfflineQueue.isTransport(APIClient.APIError(message: "x", status: 400)))
        XCTAssertEqual(StaffOfflineQueue.refusalReason(APIClient.APIError(message: "That sheet isn't yours today.",
                                                                           status: 403)),
                       "That sheet isn't yours today.")
        XCTAssertNil(StaffOfflineQueue.refusalReason(APIClient.APIError(message: "x", status: 429)))
        XCTAssertNil(StaffOfflineQueue.refusalReason(URLError(.notConnectedToInternet)))
        XCTAssertEqual(StaffOfflineQueue.fingerprint(token: "abc"), StaffOfflineQueue.fingerprint(token: "abc"))
        XCTAssertNotEqual(StaffOfflineQueue.fingerprint(token: "abc"), StaffOfflineQueue.fingerprint(token: "abd"))
        XCTAssertEqual(StaffOfflineQueue.fingerprint(token: nil), "")
    }

    // MARK: The container's read (I2's StaffPortalStore.tasks)

    @MainActor
    func testTheContainersReadPaintsOnceAndANewerOneReplacesIt() throws {
        let store = StaffTasksStore()
        store.attach(StaffSessionStore(storedToken: "seed-test-token"))
        defer { StaffReadCache.clear() }
        let first = try JSONDecoder().decode(StaffTasksResponse.self, from: Data("""
            {"ok": true, "role": "Cook", "sheets": [\(Self.sheetJSON)], "manager": false}
            """.utf8))
        store.seed(first)
        XCTAssertEqual(store.sheets.first?.done, 1)
        XCTAssertFalse(store.showingCached)

        // A newer container read (its pull to refresh) replaces it.
        let newer = Self.sheetJSON.replacingOccurrences(of: #""done": 1, "total": 2"#, with: #""done": 2, "total": 2"#)
        let second = try JSONDecoder().decode(StaffTasksResponse.self, from: Data("""
            {"ok": true, "role": "Cook", "sheets": [\(newer)], "manager": false}
            """.utf8))
        store.seed(second)
        XCTAssertEqual(store.sheets.first?.done, 2)

        // The same read handed in again (the tab back in view) changes
        // nothing — it would otherwise undo ticks made since.
        let before = store.payload
        store.seed(second)
        XCTAssertEqual(store.payload, before)
    }

    // MARK: The phone's copy

    @MainActor
    func testTheCopyBelongsToTheSessionThatReadIt() {
        StaffReadCache.clear()
        let payload = StaffTasksPayload(role: "Server")
        StaffReadCache.save(payload, path: "/staff/api/tasks", token: "token-a")
        XCTAssertEqual(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-a")?.value,
                       payload)
        XCTAssertNil(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-b"),
                     "the next person on a shared phone never sees the last one's sheets")
        XCTAssertNil(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: nil))
        StaffReadCache.clear()
    }

    func testAsOfSaysTheTimeTodayAndTheDateOtherwise() throws {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = try XCTUnwrap(TimeZone(identifier: "America/Chicago"))
        let now = try XCTUnwrap(cal.date(from: DateComponents(year: 2026, month: 10, day: 2, hour: 18)))
        let read = try XCTUnwrap(cal.date(from: DateComponents(year: 2026, month: 10, day: 2, hour: 16, minute: 5)))
        XCTAssertEqual(StaffSheetFormat.asOf(read, now: now, calendar: cal), "4:05pm")
        let yesterday = try XCTUnwrap(cal.date(from: DateComponents(year: 2026, month: 9, day: 30, hour: 16)))
        XCTAssertEqual(StaffSheetFormat.asOf(yesterday, now: now, calendar: cal), "9/30/26 4pm")
    }

    // MARK: Next-shift widget

    private func chicago() throws -> Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = try XCTUnwrap(TimeZone(identifier: "America/Chicago"))
        return cal
    }

    private func at(_ cal: Calendar, _ day: Int, _ hour: Int, _ minute: Int = 0) throws -> Date {
        try XCTUnwrap(cal.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute)))
    }

    private func snapshot(_ shifts: [StaffShiftSnapshot.Shift], published: Bool = true,
                          from start: String = "2026-10-01") -> StaffShiftSnapshot {
        StaffShiftSnapshot(published: published, shifts: shifts, windowStart: start, updatedAt: Date())
    }

    func testTheWidgetSaysTheNextShiftInTheShortForm() throws {
        let cal = try chicago()
        let snap = snapshot([.init(date: "2026-10-02", start: "4:00pm", end: "10:00pm", role: "Bartender")])
        XCTAssertEqual(snap.line(now: try at(cal, 1, 9), calendar: cal), "Next shift: Tomorrow 4pm – 10pm · Bartender")
        XCTAssertEqual(snap.line(now: try at(cal, 2, 12), calendar: cal), "Next shift: Today 4pm – 10pm · Bartender")
        let later = snapshot([.init(date: "2026-10-03", start: "16:30", end: "23:00", role: "Server")])
        XCTAssertEqual(later.line(now: try at(cal, 1, 9), calendar: cal), "Next shift: Sat 4:30pm – 11pm · Server")
    }

    func testAShiftThatHasEndedGivesWayToTheNextOne() throws {
        let cal = try chicago()
        let snap = snapshot([
            .init(date: "2026-10-01", start: "11:00am", end: "3:00pm", role: "Server"),
            .init(date: "2026-10-04", start: "5pm", end: "11pm", role: "Server"),
        ])
        XCTAssertEqual(snap.answer(now: try at(cal, 1, 14), calendar: cal), .next(snap.shifts[0]))
        XCTAssertEqual(snap.answer(now: try at(cal, 1, 16), calendar: cal), .next(snap.shifts[1]))
    }

    func testACloseThatEndsAfterMidnightIsStillTonightsShift() throws {
        let cal = try chicago()
        let snap = snapshot([.init(date: "2026-10-01", start: "6:00pm", end: "1:00am", role: "Bartender")])
        XCTAssertEqual(snap.answer(now: try at(cal, 1, 23), calendar: cal), .next(snap.shifts[0]))
        XCTAssertEqual(snap.answer(now: try at(cal, 2, 0, 30), calendar: cal), .next(snap.shifts[0]),
                       "half past midnight, the close is still on")
        XCTAssertEqual(snap.answer(now: try at(cal, 2, 1, 30), calendar: cal), .noneThrough("Wed"))
    }

    func testNoShiftsNoScheduleAndAnOldReadEachSayTheirOwnThing() throws {
        let cal = try chicago()
        XCTAssertEqual(snapshot([]).line(now: try at(cal, 1, 9), calendar: cal), "No shifts in the next 7 days")
        XCTAssertEqual(snapshot([]).line(now: try at(cal, 3, 9), calendar: cal), "No shifts through Wed",
                       "a read from two days ago only knows through its seventh day")
        XCTAssertEqual(snapshot([], published: false).line(now: try at(cal, 1, 9), calendar: cal),
                       "No schedule posted yet")
        XCTAssertEqual(snapshot([]).line(now: try at(cal, 9, 9), calendar: cal),
                       "Open Cavnar AI to see your next shift", "past the read's window nothing is claimed")
    }

    func testTimesReadTheWayTheScheduleWritesThem() {
        XCTAssertEqual(StaffShiftSnapshot.compactTime("4:00pm"), "4pm")
        XCTAssertEqual(StaffShiftSnapshot.compactTime("4:30 PM"), "4:30pm")
        XCTAssertEqual(StaffShiftSnapshot.compactTime("16:00"), "4pm")
        XCTAssertEqual(StaffShiftSnapshot.compactTime("12:00am"), "12am")
        XCTAssertEqual(StaffShiftSnapshot.compactTime("12pm"), "12pm")
        XCTAssertEqual(StaffShiftSnapshot.compactTime("close"), "close")
    }

    func testTheShiftsReadBecomesTheWidgetsSnapshot() throws {
        let json = """
        {"ok": true, "published": true, "week": [
          {"date": "2026-10-01", "weekday": "Thursday", "is_today": true, "off": true, "shift": null, "shifts": []},
          {"date": "2026-10-02", "weekday": "Friday", "is_today": false, "off": false,
           "shift": {"date": "2026-10-02", "role": "Bartender", "start": "11:00am", "end": "3:00pm"},
           "shifts": [{"date": "2026-10-02", "role": "Bartender", "start": "11:00am", "end": "3:00pm"},
                      {"date": "2026-10-02", "role": "Bartender", "start": "6:00pm", "end": "11:00pm"}]},
          {"date": "2026-10-03", "weekday": "Saturday", "is_today": false, "off": false,
           "shift": {"role": "Server", "shift_start": "5:00pm", "shift_end": "10:00pm"}}
        ]}
        """
        let read = try JSONDecoder().decode(WidgetSnapshotService.StaffShiftsRead.self, from: Data(json.utf8))
        let snap = try XCTUnwrap(WidgetSnapshotService.staffSnapshot(from: read, now: Date()))
        XCTAssertEqual(snap.windowStart, "2026-10-01")
        XCTAssertEqual(snap.shifts.map(\.start), ["11:00am", "6:00pm", "5:00pm"], "every leg of a double, then the next day")
        XCTAssertEqual(snap.shifts.last?.role, "Server")

        let never = try JSONDecoder().decode(WidgetSnapshotService.StaffShiftsRead.self,
                                             from: Data(#"{"ok": true, "published": false, "week": []}"#.utf8))
        XCTAssertEqual(WidgetSnapshotService.staffSnapshot(from: never, now: Date())?.published, false)
    }
}
