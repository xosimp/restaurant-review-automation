import XCTest
@testable import CavnarAI

/// The offline outbox is one queue for the whole app, and the server resolves
/// a write's restaurant from the session AT REPLAY TIME. For a multi-location
/// owner that means a write queued in Chicago, drained after switching to
/// Dallas, is aimed at the wrong location. Each entry carries the location it
/// was queued for so the queue refuses that itself, rather than relying on
/// every current and future endpoint to catch it.
final class PendingWriteQueueTests: XCTestCase {

    private func freshQueue() async -> PendingWriteQueue {
        let q = PendingWriteQueue.shared
        await q.clear()
        return q
    }

    func testAQueuedWriteIsStampedWithTheActiveLocation() async {
        let q = await freshQueue()
        await q.setActiveRestaurant(7)
        await q.enqueue(path: "/mobile/api/reviews/1/approve", method: "POST",
                        bodyJSON: nil, label: "Approve")

        let count = await q.pendingCount
        XCTAssertEqual(count, 1)
        let dropped = await q.dropWrites(notFor: 7)
        XCTAssertEqual(dropped, 0, "a write for the active location must survive")
    }

    func testSwitchingLocationDropsWritesQueuedForTheOldOne() async {
        let q = await freshQueue()
        await q.setActiveRestaurant(7)
        await q.enqueue(path: "/mobile/api/reviews/1/approve", method: "POST",
                        bodyJSON: nil, label: "Chicago approve")
        await q.enqueue(path: "/mobile/api/reviews/2/save-draft", method: "POST",
                        bodyJSON: nil, label: "Chicago draft")

        let dropped = await q.dropWrites(notFor: 9)
        XCTAssertEqual(dropped, 2, "both Chicago writes must be discarded, not replayed at Dallas")
        let remaining = await q.pendingCount
        XCTAssertEqual(remaining, 0)
        await q.clear()
    }

    func testWritesForTheLocationBeingSwitchedToAreKept() async {
        let q = await freshQueue()
        await q.setActiveRestaurant(9)
        await q.enqueue(path: "/mobile/api/reviews/3/approve", method: "POST",
                        bodyJSON: nil, label: "Dallas approve")
        await q.setActiveRestaurant(7)
        await q.enqueue(path: "/mobile/api/reviews/4/approve", method: "POST",
                        bodyJSON: nil, label: "Chicago approve")

        let dropped = await q.dropWrites(notFor: 9)
        XCTAssertEqual(dropped, 1, "only the Chicago one goes")
        let labels = await q.pendingLabels
        XCTAssertEqual(labels, ["Dallas approve"])
        await q.clear()
    }

    func testAnUnstampedLegacyEntryIsNeverDropped() async {
        // Entries persisted before restaurantId existed decode with nil.
        // Discarding someone's unsent work on upgrade would be worse than
        // the ambiguity it resolves.
        let q = await freshQueue()
        await q.setActiveRestaurant(nil)
        await q.enqueue(path: "/mobile/api/reviews/5/approve", method: "POST",
                        bodyJSON: nil, label: "Legacy")
        let dropped = await q.dropWrites(notFor: 7)
        XCTAssertEqual(dropped, 0)
        await q.clear()
    }
}
