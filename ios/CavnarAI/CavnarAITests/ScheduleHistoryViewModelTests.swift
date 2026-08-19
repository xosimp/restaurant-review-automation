import XCTest
@testable import CavnarAI

final class ScheduleHistoryViewModelTests: XCTestCase {
    private func makeClient(handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)) -> APIClient {
        MockURLProtocol.requestHandler = handler
        return APIClient(baseURL: URL(string: "https://example.com")!, session: MockURLProtocol.makeSession())
    }

    /// Reproduces the exact shape GET /mobile/api/labor/schedule-history/<id>
    /// really returns (confirmed directly against the live backend) --
    /// jsonify(ok=True, **detail, preview_rows=preview_rows), where detail
    /// carries id/restaurant_id/generated_at/week_start/week_end on top of
    /// the GeneratedSchedule-shaped fields.
    private static let realDetailJSON = """
    {"ok": true, "id": 79, "restaurant_id": 2, "generated_at": "2026-08-19 19:24:51",
     "week_start": "2026-08-24", "week_end": "2026-08-30",
     "hours_scheduled": 1293.9, "hours_budget": 1314.4, "labor_target": 23.0,
     "schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\\n2026-08-24,Monday,Amy C.,Prep Cook,8:30am,3:30pm,7,morning floor",
     "summary": ["Added servers/cooks daily to close 30 percent PAR hours gap."],
     "preview_rows": [{"date": "2026-08-24", "day": "Monday", "employee": "Amy C.",
       "role": "Prep Cook", "shift_start": "8:30am", "shift_end": "3:30pm",
       "scheduled_hours": "7", "notes": "morning floor"}]}
    """

    @MainActor
    func testLoadPopulatesDetailFromRealBackendShape() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data(Self.realDetailJSON.utf8))
        }
        let viewModel = ScheduleHistoryDetailViewModel(client: client)

        await viewModel.load(id: 79)

        XCTAssertNil(viewModel.errorMessage, "load() should not surface an error for a well-formed response")
        XCTAssertNotNil(viewModel.detail, "detail must be populated — a nil detail with no error is what renders as a blank page")
        XCTAssertEqual(viewModel.detail?.hoursScheduled, 1293.9)
        XCTAssertEqual(viewModel.detail?.previewRows?.first?.employee, "Amy C.")
        XCTAssertEqual(viewModel.detail?.summary?.first, "Added servers/cooks daily to close 30 percent PAR hours gap.")
    }

    /// Regression test for the "blank page" bug: .task/.onAppear on the
    /// destination view have repeatedly proven unreliable in this app for
    /// a view pushed from a List row's NavigationLink. The fix was to
    /// have the view model start loading the moment it's constructed with
    /// an id, rather than depending on a view lifecycle callback firing
    /// at all — this confirms that path actually populates detail on its
    /// own, with no separate call to load(id:).
    @MainActor
    func testInitWithIdAutomaticallyLoadsWithoutAnExplicitLoadCall() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data(Self.realDetailJSON.utf8))
        }
        let viewModel = ScheduleHistoryDetailViewModel(id: 79, client: client)

        // init fires a detached Task rather than something this test can
        // await directly (see its own doc comment) — poll briefly instead
        // of asserting immediately.
        for _ in 0..<50 where viewModel.detail == nil && viewModel.errorMessage == nil {
            await Task.yield()
        }

        XCTAssertNil(viewModel.errorMessage)
        XCTAssertNotNil(viewModel.detail, "constructing with an id should load without a separate .load(id:) call")
        XCTAssertEqual(viewModel.detail?.hoursScheduled, 1293.9)
    }

    @MainActor
    func testDeleteRemovesTheEntryLocallyOnSuccess() async {
        let client = makeClient { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (response, Data("""
            {"ok": true}
            """.utf8))
        }
        let viewModel = ScheduleHistoryViewModel(client: client)
        viewModel.history = [
            ScheduleHistoryEntry(id: 1, generatedAt: "2026-08-17 10:00:00", weekStart: "2026-08-17",
                                  weekEnd: "2026-08-23", hoursScheduled: 100, hoursBudget: 100, laborTarget: 23),
            ScheduleHistoryEntry(id: 2, generatedAt: "2026-08-24 10:00:00", weekStart: "2026-08-24",
                                  weekEnd: "2026-08-30", hoursScheduled: 100, hoursBudget: 100, laborTarget: 23),
        ]

        await viewModel.delete(id: 1)

        XCTAssertEqual(viewModel.history.map(\.id), [2])
    }
}
