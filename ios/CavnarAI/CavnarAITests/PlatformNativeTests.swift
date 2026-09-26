import XCTest
@testable import CavnarAI

/// The platform-native round: the offline queue's new kinds (rec answers,
/// count sheets), the warm-start cache every module now shares, and the
/// "Last night" widget + "How was last night?" intent, both of which read
/// only the widget snapshot.
final class PlatformNativeTests: XCTestCase {

    private func json(_ data: Data?) throws -> [String: Any] {
        let data = try XCTUnwrap(data)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Queued writes

    func testDoneQueuesExactlyTheBodyTheLiveAnswerSends() throws {
        let body = APIClient.recEventBody(key: "food:reorder:12", answer: .completed, surface: "food")
        let write = try XCTUnwrap(QueuedWrite.recAnswer(body))
        XCTAssertEqual(write.path, "/mobile/api/recs/event")
        XCTAssertEqual(write.method, "POST")
        XCTAssertEqual(write.label, "Mark a recommendation done")
        let sent = try json(write.bodyJSON)
        XCTAssertEqual(sent["key"] as? String, "food:reorder:12")
        XCTAssertEqual(sent["event"] as? String, "completed")
        XCTAssertEqual(sent["surface"] as? String, "food")
        XCTAssertEqual(sent["module"] as? String, "food", "module defaults to the surface")
        XCTAssertNil(sent["kind"], "only a dismissal carries a kind")
        XCTAssertNil(sent["reason_code"])
    }

    func testPassQueuesAsNotForUsWithItsReason() throws {
        let body = APIClient.recEventBody(key: "reviews:k1", answer: .notForUs, surface: "home",
                                          module: "reviews", reasonCode: "too_costly")
        let write = try XCTUnwrap(QueuedWrite.recAnswer(body))
        XCTAssertEqual(write.label, "Pass on a recommendation")
        let sent = try json(write.bodyJSON)
        XCTAssertEqual(sent["event"] as? String, "dismissed")
        XCTAssertEqual(sent["kind"] as? String, "not_for_us")
        XCTAssertEqual(sent["reason_code"] as? String, "too_costly")
        XCTAssertEqual(sent["module"] as? String, "reviews")
    }

    func testMeasureItIsNeverQueued() {
        // Its answer is the tracker's own sentence; that only exists live.
        let body = APIClient.recEventBody(key: "labor:k", answer: .accepted, surface: "labor")
        XCTAssertNil(QueuedWrite.recAnswer(body))
        let viewed = APIClient.RecEventBody(key: "k", event: "evidence_viewed", surface: "home",
                                            module: "home", kind: nil)
        XCTAssertNil(QueuedWrite.recAnswer(viewed), "only Done and Pass are on the list")
    }

    func testACountSheetQueuesItsLinesPinnedToTheDayCounted() throws {
        let body = CountSheetViewModel.SaveBody(items: [.init(ingredientId: 4, counted: 12.5),
                                                        .init(ingredientId: 9, counted: 0)],
                                                date: "2026-09-24")
        let write = try XCTUnwrap(QueuedWrite.countSheet(body))
        XCTAssertEqual(write.path, "/mobile/api/food-cost/count-sheet")
        XCTAssertEqual(write.label, "Save 2 recounts")
        let sent = try json(write.bodyJSON)
        XCTAssertEqual(sent["date"] as? String, "2026-09-24")
        let items = try XCTUnwrap(sent["items"] as? [[String: Any]])
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0]["ingredient_id"] as? Int, 4)
        XCTAssertEqual(items[0]["counted"] as? Double, 12.5)
    }

    func testACountSheetWithoutAKnownDaySendsNoDate() throws {
        let body = CountSheetViewModel.SaveBody(items: [.init(ingredientId: 4, counted: 3)])
        let write = try XCTUnwrap(QueuedWrite.countSheet(body))
        XCTAssertEqual(write.label, "Save 1 recount")
        XCTAssertNil(try json(write.bodyJSON)["date"], "no guessed date: the server dates it on arrival")
        XCTAssertNil(QueuedWrite.countSheet(CountSheetViewModel.SaveBody(items: [])))
    }

    func testTheNewKindsSurviveTheQueuesOwnPersistence() async throws {
        let q = PendingWriteQueue.shared
        await q.clear()
        await q.setActiveRestaurant(3)
        let rec = try XCTUnwrap(QueuedWrite.recAnswer(
            APIClient.recEventBody(key: "k", answer: .completed, surface: "food")))
        let count = try XCTUnwrap(QueuedWrite.countSheet(
            CountSheetViewModel.SaveBody(items: [.init(ingredientId: 1, counted: 2)])))
        await q.enqueue(rec)
        await q.enqueue(count)
        let labels = await q.pendingLabels
        XCTAssertEqual(labels, ["Mark a recommendation done", "Save 1 recount"])

        // The persisted form — what a relaunch reads back.
        let entry = PendingWriteQueue.PendingWrite(id: UUID(), path: rec.path, method: rec.method,
                                                   bodyJSON: rec.bodyJSON, createdAt: Date(),
                                                   label: rec.label, restaurantId: 3)
        let decoded = try JSONDecoder().decode([PendingWriteQueue.PendingWrite].self,
                                               from: JSONEncoder().encode([entry]))
        XCTAssertEqual(decoded.first?.path, rec.path)
        XCTAssertEqual(decoded.first?.bodyJSON, rec.bodyJSON)
        XCTAssertEqual(decoded.first?.restaurantId, 3)
        await q.clear()
    }

    func testOnlyTheServersOwnNoDropsAWriteAndOnlyOneNothingDependsOn() {
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 400))
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 404))
        XCTAssertTrue(PendingWriteQueue.isRefusal(status: 409))
        for passing in [nil, 401, 402, 408, 425, 429, 500, 502, 503] {
            XCTAssertFalse(PendingWriteQueue.isRefusal(status: passing), "\(String(describing: passing))")
        }
        XCTAssertTrue(QueuedWrite.standsAlone(path: "/mobile/api/recs/event"))
        XCTAssertTrue(QueuedWrite.standsAlone(path: "/mobile/api/food-cost/count-sheet"))
        // A refused draft save must still hold the approve behind it.
        XCTAssertFalse(QueuedWrite.standsAlone(path: "/mobile/api/reviews/5/save-draft"))
        XCTAssertFalse(QueuedWrite.standsAlone(path: "/mobile/api/reviews/5/approve"))
    }

    // MARK: - Warm-start cache

    func testTheCachedNoticeReadsAsHomesDoes() {
        let now = Date()
        XCTAssertNil(CacheFreshness.notice(savedAt: nil, now: now), "live data says nothing")
        XCTAssertNil(CacheFreshness.notice(savedAt: now.addingTimeInterval(-120), now: now))
        XCTAssertEqual(CacheFreshness.notice(savedAt: now.addingTimeInterval(-12 * 60), now: now),
                       "Showing data from 12m ago")
        XCTAssertEqual(CacheFreshness.notice(savedAt: now.addingTimeInterval(-3 * 3600 - 60), now: now),
                       "Showing data from 3h ago")
    }

    private struct Parts: Decodable {
        struct A: Decodable { let x: Int }
        let n: Int?
        let a: A?
        let b: [Int]?
        let missing: A?
    }

    func testAnEnvelopeOfServerBodiesIsJSONAndLeavesOutWhatFailed() throws {
        let data = CacheEnvelope.make([("a", Data("{\"x\":7}".utf8)), ("b", Data("[1,2]".utf8)),
                                       ("missing", nil)],
                                      numbers: [("n", 30)])
        let parts = try JSONDecoder.cavnar.decode(Parts.self, from: data)
        XCTAssertEqual(parts.n, 30)
        XCTAssertEqual(parts.a?.x, 7)
        XCTAssertEqual(parts.b, [1, 2])
        XCTAssertNil(parts.missing)
        XCTAssertNoThrow(try JSONDecoder.cavnar.decode(Parts.self, from: CacheEnvelope.make([])))
    }

    @MainActor
    func testACacheWriteFromAnEndedSessionNeverLands() async {
        SessionScope.begin(userId: 41, restaurantId: 9)
        let cache = ResponseCache<[Int]>("tests.platform-native")
        let started = SessionScope.generation
        cache.save(Data("[1,2,3]".utf8), generation: started)
        let hit = await cache.load()
        XCTAssertEqual(hit?.value, [1, 2, 3])

        // Signed out (or switched location) while a fetch was in flight:
        // the purge ran, then the same account signed back in.
        SecureCache.delete(key: SessionScope.key("tests.platform-native"))
        SessionScope.begin(userId: 41, restaurantId: 9)
        cache.save(Data("[4]".utf8), generation: started)
        let after = await cache.load()
        XCTAssertNil(after, "the old session's answer must not come back after the purge")
        SessionScope.begin(userId: nil, restaurantId: nil)
    }

    // MARK: - Last night: widget snapshot and intent

    private func lastNight(net: String? = "$4,210", budget: Double? = 525) -> WidgetSnapshot? {
        var night = WidgetSnapshotService.NightPart(date: "2026-09-24", label: "9/24/26", net: net,
                                                    change: "+8%", basis: "vs last Thursday", up: true,
                                                    verdict: "Good day", tone: "good", score: 82)
        night.setBudget(budget)
        return WidgetSnapshotService.merge(previous: nil, restaurantId: 9, restaurantName: nil, waiting: nil,
                                           night: night, now: Date())
    }

    func testTheSnapshotCarriesTheNightAgainstBudget() throws {
        let snap = try XCTUnwrap(lastNight())
        XCTAssertEqual(snap.budgetLabel, "+$525 vs budget")
        XCTAssertEqual(snap.budgetIsUp, true)
        XCTAssertEqual(snap.nightLink, "cavnarai://nav/dsr/night/2026-09-24")
        let under = try XCTUnwrap(lastNight(budget: -135))
        XCTAssertEqual(under.budgetIsUp, false)
        XCTAssertEqual(under.budgetLabel?.hasSuffix("$135 vs budget"), true)
        XCTAssertNil(try XCTUnwrap(lastNight(budget: nil)).budgetLabel, "a manager's view has no budget")

        let old = try JSONDecoder().decode(WidgetSnapshot.self, from: Data("""
            {"waitingCount": 0, "pendingReplies": 0, "updatedAt": 0}
            """.utf8))
        XCTAssertNil(old.budgetLabel, "a snapshot written before the budget still decodes")
        XCTAssertTrue(WidgetSnapshot.allWidgetKinds.contains(WidgetSnapshot.widgetKind))
        XCTAssertTrue(WidgetSnapshot.allWidgetKinds.contains(WidgetSnapshot.lastNightWidgetKind))
    }

    func testLastNightIsOneSpokenSentence() throws {
        let snap = try XCTUnwrap(lastNight())
        XCTAssertEqual(snap.lastNightSentence(),
                       "Last night, 9/24/26: $4,210 in net sales, +8% vs last Thursday, +$525 vs budget. "
                       + "Good day, 82 out of 100.")
        XCTAssertNil(try XCTUnwrap(lastNight(net: nil)).lastNightSentence(), "no net, no sentence")
        XCTAssertNil(snap.lastNightSentence(now: Date().addingTimeInterval(WidgetSnapshot.staleAfter + 60)),
                     "an old night is never read out as last night")
    }

    func testTheIntentAnswersOnlyForThisPhonesLocation() throws {
        let snap = try XCTUnwrap(lastNight())
        XCTAssertTrue(LastNightSummaryIntent.answer(snap, restaurantId: 9).hasPrefix("Last night, 9/24/26: $4,210"))
        XCTAssertEqual(LastNightSummaryIntent.answer(snap, restaurantId: 12),
                       "Open Cavnar AI to refresh last night's numbers.")
        XCTAssertEqual(LastNightSummaryIntent.answer(nil, restaurantId: 9),
                       "Sign in to Cavnar AI to hear last night's numbers.")
        XCTAssertEqual(LastNightSummaryIntent.answer(try XCTUnwrap(lastNight(net: nil)), restaurantId: 9),
                       "There's no report for last night yet. Open Cavnar AI to check on it.")
    }
}
