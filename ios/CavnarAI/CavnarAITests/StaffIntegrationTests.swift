import XCTest
@testable import CavnarAI

/// Employee audit iOS integration: the seams between I1 (session, links),
/// I2 (the container), I3 (Requests, Me, Inbox) and I4 (tasks, offline) —
/// a query that reaches its route, one tasks transport rule, the request a
/// push names, translated announcements, the overtime note, "Message your
/// manager" under a flagged reading and one staff read cache.
@MainActor
final class StaffIntegrationTests: XCTestCase {
    private var defaults: UserDefaults!
    private var suite = ""

    override func setUp() {
        super.setUp()
        suite = "staff-int-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(T.self, from: Data(json.utf8))
    }

    // MARK: - A query reaches its route

    func testTheBuiltUrlKeepsTheQuery() throws {
        let base = try XCTUnwrap(URL(string: "https://example.com"))
        let url = APIClient.composeURL(base: base, path: "/staff/api/colleagues", query: ["date": "2026-10-02"])
        XCTAssertEqual(url.absoluteString, "https://example.com/staff/api/colleagues?date=2026-10-02")
        let two = APIClient.composeURL(base: base, path: "/staff/api/colleagues",
                                       query: ["shift_start": "4:00pm", "shift_date": "2026-10-02"])
        XCTAssertEqual(two.path, "/staff/api/colleagues")
        XCTAssertEqual(two.query, "shift_date=2026-10-02&shift_start=4:00pm", "items sorted, the path never escaped")
        XCTAssertEqual(APIClient.composeURL(base: base, path: "/staff/api/me", query: [:]).absoluteString,
                       "https://example.com/staff/api/me")
    }

    func testAStaffReadWithAQueryArrivesAsAQueryNotAnEscapedPath() async throws {
        let captured = Box<[URL]>([])
        let client = EdgeHTTP.client { request in
            captured.value.append(request.url!)
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "days": []}"#)
        }
        let staff = StaffSessionStore(client: client, storedToken: .some("tok"), defaults: defaults)
        struct Any200: Decodable { let ok: Bool }
        let _: Any200 = try await staff.authed("/staff/api/earnings", query: ["days": "14"])
        let _: Any200 = try await staff.staffGet("/staff/api/colleagues", query: ["date": "2026-10-02"])
        XCTAssertEqual(captured.value.map(\.path), ["/staff/api/earnings", "/staff/api/colleagues"])
        XCTAssertEqual(captured.value.map { $0.query ?? "" }, ["days=14", "date=2026-10-02"])
        XCTAssertFalse(captured.value.contains { $0.absoluteString.contains("%3F") }, "a ? in the path is a 404")
    }

    // MARK: - The tasks transport reads an ended session like every staff call

    func testATasks401WithASentenceIsAnEndedSession() async throws {
        MockURLProtocol.requestHandler = { request in
            EdgeHTTP.reply(request, 401, #"{"ok": false, "error": "Your shift session ended — sign in again."}"#)
        }
        let api = StaffTasksAPI(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
        do {
            _ = try await api.tasks(bearer: "tok")
            XCTFail("a 401 on /staff/api is an ended session")
        } catch let error as APIClient.SessionExpiredError {
            XCTAssertEqual(error.message, "Your shift session ended — sign in again.")
        }
    }

    func testTheTasksReadSendsVersionTwo() async throws {
        let captured = Box<URLRequest?>(nil)
        MockURLProtocol.requestHandler = { request in
            captured.value = request
            return EdgeHTTP.reply(request, 200, #"{"ok": true, "sheets": [], "manager": false}"#)
        }
        let api = StaffTasksAPI(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
        _ = try await api.tasks(bearer: "tok")
        XCTAssertEqual(captured.value?.value(forHTTPHeaderField: StaffTasksAPI.versionHeader), "2")
    }

    func testAnEndedSessionIsNotAnExplicitSignOut() {
        let staff = StaffSessionStore(client: EdgeHTTP.client { EdgeHTTP.reply($0, 200, "{}") },
                                      storedToken: .some("tok"), defaults: defaults)
        var hookRan = false
        staff.onExplicitSignOut = { hookRan = true }
        staff.sessionEnded(sentToken: "older-token", message: "x")
        XCTAssertTrue(staff.isAuthenticated, "a slow answer from an earlier session never ends this one")
        staff.sessionEnded(sentToken: "tok", message: "Your shift session ended — sign in again.")
        XCTAssertFalse(staff.isAuthenticated)
        XCTAssertEqual(staff.signInNotice, "Your shift session ended — sign in again.")
        XCTAssertFalse(hookRan, "an ended session keeps this person's cached screens")
    }

    // MARK: - Deep links: the item a push names

    func testOnlyRequestsAndTheInboxHoldAFocus() throws {
        let store = StaffPortalStore()
        store.apply(try XCTUnwrap(StaffDeepLink.from(nav: "staff/tasks/9")))
        XCTAssertEqual(store.selectedTab, .tasks)
        XCTAssertNil(store.focus, "nothing on Tasks reads it, so it never goes stale")
        store.apply(try XCTUnwrap(StaffDeepLink.from(nav: "staff/requests/41")))
        XCTAssertEqual(store.focus?.itemID, 41)
    }

    func testARequestLinkPointsAtItsRows() throws {
        let board = try decode(StaffRequestsBoard.self, """
            {"ok": true, "requests": [], "asks": [], "open": [],
             "offers": [{"id": 3, "request_id": 41, "status": "offered", "date": "2026-10-03"},
                        {"id": 4, "request_id": 50, "status": "offered", "date": "2026-10-04"}]}
            """)
        let shift = StaffDeepLink(tab: .requests, itemID: 41, alertType: "staff_request", kind: "request",
                                  event: nil, requestKind: "swap")
        XCTAssertEqual(StaffRequestsFocus.keys(for: shift, board: board), ["sr41", "ask41", "open41", "offer3"])
        let off = StaffDeepLink(tab: .requests, itemID: 7, alertType: "staff_request", kind: "request",
                                event: nil, requestKind: "time_off")
        XCTAssertEqual(StaffRequestsFocus.keys(for: off, board: board), ["to7"])
        let none = StaffDeepLink(tab: .requests, itemID: nil, alertType: "staff_request", kind: nil,
                                 event: nil, requestKind: nil)
        XCTAssertEqual(StaffRequestsFocus.keys(for: none, board: board), [])
    }

    func testAManagersReplyOpensTheThreadAndAnAnnouncementDoesNot() {
        let reply = StaffDeepLink(tab: .inbox, itemID: 2, alertType: "staff_message", kind: "message",
                                  event: nil, requestKind: nil)
        let notice = StaffDeepLink(tab: .inbox, itemID: 12, alertType: "staff_announcement", kind: "announcement",
                                   event: nil, requestKind: nil)
        XCTAssertTrue(StaffInboxFocus.opensThread(reply))
        XCTAssertFalse(StaffInboxFocus.opensThread(notice))
    }

    // MARK: - S9: translated announcements, the overtime note

    func testATranslatedAnnouncementOffersTheOriginal() throws {
        let inbox = try decode(StaffInboxPayload.self, """
            {"ok": true, "unread": 1, "unread_messages": 0, "announcements": [
              {"id": 5, "title": "Reunión el lunes", "body": "A las 3pm.", "priority": "normal",
               "created_at": "2026-10-01T15:00:00", "created_by_name": "Erik S", "expires_on": null,
               "acked_at": null, "language": "es", "translated": true,
               "original_title": "Meeting Monday", "original_body": "At 3pm."},
              {"id": 6, "title": "Patio closed", "body": "", "priority": "urgent", "acked_at": null,
               "language": "es", "translated": false, "original_title": null, "original_body": null},
              {"id": 7, "title": "Older build", "body": "No language keys at all."}]}
            """)
        let a = inbox.announcements[0]
        XCTAssertTrue(a.translated)
        XCTAssertEqual(a.language, "es")
        XCTAssertEqual(a.shown(original: false).title, "Reunión el lunes")
        XCTAssertEqual(a.shown(original: true).title, "Meeting Monday")
        XCTAssertEqual(a.shown(original: true).body, "At 3pm.")
        XCTAssertFalse(inbox.announcements[1].translated)
        XCTAssertEqual(inbox.announcements[1].shown(original: true).title, "Patio closed",
                       "nothing to flip to when it wasn't translated")
        XCTAssertFalse(inbox.announcements[2].translated)
        XCTAssertNil(inbox.announcements[2].language)
    }

    func testThePickupOvertimeNoteIsReadOnOpenShiftsAndOffers() throws {
        let board = try decode(StaffRequestsBoard.self, """
            {"ok": true, "requests": [], "asks": [],
             "offers": [{"id": 3, "request_id": 41, "status": "offered", "date": "2026-10-03",
                         "pickup_overtime_note": "This 6h pickup takes you past 40 hours that week (to 43h)."}],
             "open": [{"id": 9, "kind": "drop", "date": "2026-10-04", "can_take": true, "why_not": null,
                       "pickup_overtime_note": "This 5h pickup takes you past 40 hours that week (to 41h)."},
                      {"id": 10, "kind": "drop", "date": "2026-10-05", "can_take": false,
                       "why_not": "You're already on then", "pickup_overtime_note": null},
                      {"id": 11, "kind": "post", "date": "2026-10-06", "pickup_overtime_note": "  "}]}
            """)
        XCTAssertEqual(board.offers[0].overtimeNote, "This 6h pickup takes you past 40 hours that week (to 43h).")
        XCTAssertEqual(board.open[0].overtimeNote, "This 5h pickup takes you past 40 hours that week (to 41h).")
        XCTAssertNil(board.open[1].overtimeNote)
        XCTAssertNil(board.open[2].overtimeNote, "blank is no note")
    }

    // MARK: - Message your manager under a flagged reading

    func testTheMessageNamesTheLineTheReadingAndTheRange() throws {
        let sheet = try decode(StaffSheet.self, """
            {"id": 41, "task_date": "2026-10-02", "title": "Opening — Kitchen", "shift_kind": "opening",
             "assignees": ["Ana"], "unassigned": false, "status": "open", "done": 1, "total": 1,
             "lines": [{"line_id": 7, "label": "Walk-in temp", "proof": "number",
                        "min_value": 33, "max_value": 41, "critical": 1, "done": true, "proof_value": 46}]}
            """)
        let m = StaffLineMessage.about(sheet: sheet, line: sheet.lines[0], reading: sheet.lines[0].proofValue)
        XCTAssertEqual(m.context.shiftDate, "2026-10-02")
        XCTAssertEqual(m.context.label, "Opening — Kitchen · Walk-in temp")
        XCTAssertEqual(m.draft, "Walk-in temp on the Opening — Kitchen sheet read 46 — it should be 33–41. ")
        var open = sheet.lines[0]
        open.minValue = nil
        open.maxValue = nil
        XCTAssertEqual(StaffLineMessage.about(sheet: sheet, line: open, reading: nil).draft,
                       "Walk-in temp on the Opening — Kitchen sheet is out of range. ")
    }

    // MARK: - One staff read cache

    func testTheTasksCopyLivesInStaffCacheAndLeavesWithTheNextSession() {
        StaffCache.purgeAll()
        defer { StaffCache.purgeAll() }
        let payload = StaffTasksPayload(role: "Server")
        StaffReadCache.save(payload, path: "/staff/api/tasks", token: "token-a")
        XCTAssertEqual(StaffCache.load(StaffTasksPayload.self, key: "/staff/api/tasks",
                                       scope: StaffCache.sessionScope(token: "token-a"))?.value, payload,
                       "the same store as the portal's warm start")
        StaffReadCache.save(StaffTasksPayload(role: "Cook"), path: "/staff/api/tasks", token: "token-b")
        XCTAssertNil(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-a"),
                     "a shared phone keeps one person's sheets")
        XCTAssertEqual(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-b")?.value.role,
                       "Cook")
        StaffCache.purgeAll()
        XCTAssertNil(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-b"),
                     "an explicit sign-out's purge takes the sheets too")
    }

    func testClearingTheReadCacheLeavesThePortalsCopies() {
        StaffCache.purgeAll()
        defer { StaffCache.purgeAll() }
        let member = StaffCache.memberScope(restaurant: "Alpha", name: "Ana R")
        StaffCache.save(["x": 1], key: "stats", scope: member)
        StaffReadCache.save(StaffTasksPayload(role: "Server"), path: "/staff/api/tasks", token: "token-a")
        StaffReadCache.clear()
        XCTAssertNil(StaffReadCache.load(StaffTasksPayload.self, path: "/staff/api/tasks", token: "token-a"))
        XCTAssertEqual(StaffCache.load([String: Int].self, key: "stats", scope: member)?.value, ["x": 1])
    }
}
