import Foundation

/// Following a module read the server is writing in the background
/// (iOS parity audit 10/7/26 #37; server: insight_refresh.py).
///
/// The Labor, Reviews and Marketing reads are asked with `async=1`. A read
/// already stored for the current figures answers as it always did. One
/// that is not answers at once — `pending` (nothing stored yet: the screen
/// keeps its loading state) or the last read with its age (`refreshing`:
/// shown, with its "older read" note, until the new one lands) — and the
/// model call runs on the server's AI pool instead of holding a request
/// thread. `follow` re-reads the route with the job it named, on the
/// shared poll cadence (1.5 → 3 → 5 s), handing each answer worth showing
/// to `apply`, until the new read arrives or three minutes pass.
///
/// Run from the screen's own Task, so leaving the screen cancels the wait
/// (the read is still written, and the next open serves it).
@MainActor
enum InsightRefresh {
    /// How long a screen keeps polling before it leaves what it has up.
    static let followLimit: TimeInterval = 180

    /// Polls `path` from `state` until the read is written. `apply` gets
    /// every answer that is not a placeholder (a stale read while waiting,
    /// then the new one); returns false when the wait ended with nothing
    /// but a placeholder on screen.
    @discardableResult
    static func follow<T: Decodable>(
        _ path: String, from state: APIClient.InsightRefreshState?, client: APIClient,
        apply: (T) -> Void
    ) async -> Bool {
        guard var job = state?.refreshJob, state?.isWaiting == true else { return true }
        let started = Date()
        var polls = 0
        var showedRead = state?.isPending == false
        while Date().timeIntervalSince(started) < followLimit {
            do {
                try await Task.sleep(for: APIClient.pollDelay(after: polls))
            } catch {
                return showedRead
            }
            polls += 1
            let answer: (value: T, body: Data, refresh: APIClient.InsightRefreshState?)
            do {
                answer = try await client.sendInsight(path, refreshJob: job)
            } catch is CancellationError {
                return showedRead
            } catch {
                // A poll lost in transit (a deploy, a weak signal) is waited
                // out; the read is still being written server-side.
                continue
            }
            if answer.refresh?.isPending != true {
                apply(answer.value)
                showedRead = true
            }
            guard let next = answer.refresh, next.isWaiting, let nextJob = next.refreshJob else {
                return showedRead
            }
            job = nextJob
        }
        return showedRead
    }
}
