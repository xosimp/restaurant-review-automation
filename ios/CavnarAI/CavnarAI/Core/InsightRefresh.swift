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
    ///
    /// A read the server could not write (a budget stop, an outage, a
    /// refused read, with no older read to fall back on) is answered once
    /// with the route's error — an error status, or `ok: false` / `error`
    /// on a 200 — and the job is never retried on a poll. That answer ends
    /// the wait at once and hands the server's own sentence to `failed`
    /// (re-audit 10/8/26 #3): it was treated as a poll lost in transit and
    /// re-asked for three minutes, the skeleton up and nothing said.
    @discardableResult
    static func follow<T: Decodable>(
        _ path: String, from state: APIClient.InsightRefreshState?, client: APIClient,
        failed: ((String) -> Void)? = nil,
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
            } catch is APIClient.SessionExpiredError {
                // Signed out: the session's own handler takes it from here.
                return showedRead
            } catch {
                // The server answered, and said the read could not be
                // written: said once, never re-asked.
                if let message = failureMessage(error) {
                    failed?(message)
                    return showedRead
                }
                // A poll lost in transit (a deploy, a weak signal) is waited
                // out; the read is still being written server-side.
                continue
            }
            if answer.refresh?.isWaiting != true, let message = failureMessage(body: answer.body) {
                failed?(message)
                return showedRead
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

    /// The server's sentence when a poll was answered with an error status
    /// (the read could not be written); nil for a poll that never reached
    /// an answer (offline, timed out, cut off), which is waited out.
    nonisolated static func failureMessage(_ error: Error) -> String? {
        guard let apiError = error as? APIClient.APIError, apiError.status != nil else { return nil }
        let message = apiError.message.trimmingCharacters(in: .whitespacesAndNewlines)
        return message.isEmpty ? fallbackMessage : message
    }

    /// The route's error on a 200 (`ok: false`, or an `error`), else nil.
    nonisolated static func failureMessage(body: Data) -> String? {
        struct Envelope: Decodable { let ok: Bool?; let error: String?; let insight: String? }
        guard let envelope = try? JSONDecoder.cavnar.decode(Envelope.self, from: body) else { return nil }
        let error = envelope.error?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !error.isEmpty { return error }
        guard envelope.ok == false else { return nil }
        let insight = envelope.insight?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return insight.isEmpty ? fallbackMessage : insight
    }

    /// Said when the server gave no sentence of its own.
    nonisolated static let fallbackMessage = "Cavnar AI couldn\u{2019}t write this read just now."
}
