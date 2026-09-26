import Foundation
import Observation

/// Where a Daily Report screen is pushed from — Home's "Last night" card,
/// the list of nights, and a tapped `dsr` push all append one of these to
/// Home's NavigationPath.
enum DailyReportRoute: Hashable {
    /// A night's report; nil = the latest night there is. `follow` when the
    /// screen opens right after Close day, so it waits for the run it just
    /// started instead of reading the night as not started (or as the
    /// version before it).
    case report(date: String?, follow: DSRFollow? = nil)
    case list
    case week(date: String?)
    /// The fiscal period holding `date`, one row per week — the web's
    /// Night / Week / Period third view.
    case period(date: String?)
}

/// What a just-started run will look like on /status, from the answer to
/// POST /dsr/close (dsr.pipeline.start_manual + run_night):
///   * no row yet (version nil)            → wait for the first row;
///   * a finished night (final / provisional / failed) re-run → a NEW
///     version, so the old one's terminal status must not end the wait;
///   * a night still in flight             → the same version moves on.
struct DSRFollow: Hashable {
    var expectingRow: Bool
    var newerThan: Int?

    /// nil when nothing was started (the night was already final).
    static func after(_ close: DSRCloseResponse) -> DSRFollow? {
        guard close.ok, close.started == true else { return nil }
        return DSRFollow(expectingRow: close.version == nil,
                         newerThan: DSRPhase(status: close.status).isTerminal ? close.version : nil)
    }
}

// MARK: - Polling

/// When the progressive screen stops asking /status. Pure, so the stop
/// conditions are tested without a clock or a network.
enum DSRPollPolicy {
    static let interval: Duration = .seconds(5)
    /// A night waiting on the POS can sit in awaiting_close for hours;
    /// nobody should hold a phone open for that. After this the screen says
    /// so and a pull-to-refresh looks again.
    static let maxDuration: TimeInterval = 15 * 60
    /// Ticks to wait for a row that Close day just asked for (the run starts
    /// on a background thread) or for a re-run's new version to appear.
    static let maxWaitingTicks = 24
    /// A dead connection stops the loop instead of retrying forever.
    static let maxConsecutiveErrors = 5

    enum Decision: Equatable {
        case keepPolling
        /// The night reached Final / Provisional / Couldn't finish — load the
        /// whole report once and stop.
        case reload
        /// Nothing is running and nothing was asked for.
        case stop
        /// Waited as long as the screen should; the night may still finish.
        case gaveUp
    }

    struct Tick {
        /// nil when this tick's request failed.
        var exists: Bool?
        var phase: DSRPhase?
        var version: Int?
        var elapsed: TimeInterval
        var waitingTicks: Int
        var consecutiveErrors: Int
        /// Close day was just pressed for a night with no row yet.
        var expectingRow: Bool = false
        /// A re-run (or a Close day on a provisional/failed night) makes a
        /// new version; the old one's terminal status must not end the wait.
        var expectNewerThan: Int?
    }

    static func decide(_ t: Tick) -> Decision {
        if t.consecutiveErrors >= maxConsecutiveErrors || t.elapsed >= maxDuration { return .gaveUp }
        guard let exists = t.exists else { return .keepPolling }
        if !exists {
            guard t.expectingRow else { return .stop }
            return t.waitingTicks < maxWaitingTicks ? .keepPolling : .gaveUp
        }
        if let base = t.expectNewerThan, (t.version ?? 0) <= base {
            return t.waitingTicks < maxWaitingTicks ? .keepPolling : .gaveUp
        }
        if let phase = t.phase, phase.isTerminal { return .reload }
        return .keepPolling
    }
}

// MARK: - One night

@Observable
@MainActor
final class DailyReportViewModel {
    private(set) var businessDate: String?
    private(set) var report: DSRReport?
    /// The freshest stage list: the report's own, then each /status tick.
    private(set) var checklist: DSRChecklist?
    /// "owner" / "manager" — from whichever response said so last.
    private(set) var view: String?
    /// No row for this night yet (or no nights at all).
    private(set) var nothingYet = false
    private(set) var isLoading = false
    private(set) var errorMessage: String?
    /// The server's own sentence for a refused Close day or re-run.
    var actionError: String?
    private(set) var isSubmitting = false
    private(set) var isPolling = false
    /// Polling hit its cap with the night still running.
    private(set) var pollGaveUp = false
    /// nil = the latest version.
    private(set) var selectedVersion: Int?
    /// Bumped to (re)start the view's `.task(id:)` that runs followProgress.
    private(set) var pollGeneration = 0
    /// Close day / Re-run was accepted and the night hasn't answered yet.
    private(set) var runStarted = false

    private var expectingRow: Bool
    private var expectNewerThan: Int?
    private let client: APIClient
    var pollInterval: Duration = DSRPollPolicy.interval

    init(businessDate: String?, follow: DSRFollow? = nil, client: APIClient = .shared) {
        self.businessDate = DSRFormat.isISODate(businessDate) ? businessDate : nil
        self.expectingRow = follow?.expectingRow ?? false
        self.expectNewerThan = follow?.newerThan
        self.runStarted = follow != nil
        self.client = client
    }

    var phase: DSRPhase {
        if selectedVersion != nil, let report { return report.phase }
        if runStarted { return .running }
        if let checklist, checklist.status != nil { return checklist.phase }
        return report?.phase ?? .notStarted
    }

    var isOwner: Bool { view == "owner" }

    /// The stage that is running now, e.g. "awaiting_close".
    var currentStage: String? { checklist?.status ?? report?.status }

    /// Close day: nothing has started, the night is still waiting on the
    /// POS to close (Close day forces it), or it ended failed or provisional
    /// — "Try again now", which the server takes from any console login as a
    /// new version (failed) or an upgrade (provisional), as the web offers it
    /// (D3-9). Before, a manager on the phone had no button on a night that
    /// couldn't finish. Anyone with the console may.
    var canCloseDay: Bool {
        guard selectedVersion == nil, !runStarted, !isSubmitting else { return false }
        if phase == .notStarted || phase == .failed || phase == .provisional { return true }
        return phase == .running && (currentStage == "scheduled" || currentStage == "awaiting_close")
    }

    /// The Close day button's words: "Try again now" on a night that ended
    /// failed or provisional, as on the web.
    var closeDayLabel: String {
        (phase == .failed || phase == .provisional) ? "Try again now" : "Close day"
    }

    /// Re-run: the owner's, on a finished night. The server enforces the
    /// window (tonight or the night before) and says so when it refuses.
    var canRerun: Bool { isOwner && report != nil && phase.isTerminal && !isPolling && !isSubmitting }

    var displayDate: String {
        report?.displayDate ?? checklist?.label ?? businessDate.map(CavnarDate.mdy) ?? "Tonight"
    }

    // MARK: Loading

    func load() async {
        isLoading = report == nil
        errorMessage = nil
        defer { isLoading = false }
        do {
            if businessDate == nil {
                let list: DSRListResponse = try await client.send("/mobile/api/dsr", query: ["limit": "1"],
                                                                  hapticOnError: false)
                view = list.view ?? view
                guard let latest = list.reports.first else {
                    nothingYet = true
                    return
                }
                businessDate = latest.businessDate
            }
            try await fetchReport()
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is APIClient.SessionExpiredError {
        } catch {
            errorMessage = "Couldn\u{2019}t load the daily report."
        }
        if selectedVersion == nil, runStarted || phase == .running { startPolling() }
    }

    /// The report for `businessDate` (at `selectedVersion`); a 404 falls
    /// back to /status, since a night that is running may not have a
    /// report row readable yet.
    private func fetchReport() async throws {
        guard let date = businessDate else { return }
        var query: [String: String] = [:]
        if let selectedVersion { query["version"] = String(selectedVersion) }
        do {
            let fetched: DSRReport = try await client.send("/mobile/api/dsr/\(date)", query: query, hapticOnError: false)
            report = fetched
            view = fetched.view ?? view
            if selectedVersion == nil { checklist = fetched.checklist }
            nothingYet = false
        } catch let error as APIClient.APIError where error.status == 404 {
            report = nil
            let status: DSRStatusResponse = try await client.send("/mobile/api/dsr/\(date)/status", hapticOnError: false)
            view = status.view ?? view
            checklist = status.checklist
            nothingYet = !status.exists
        }
    }

    func selectVersion(_ version: Int?) async {
        let latest = report?.versions.map(\.version).max()
        selectedVersion = (version == latest) ? nil : version
        do { try await fetchReport() } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {}
    }

    // MARK: Progress

    func startPolling() {
        pollGaveUp = false
        pollGeneration += 1
    }

    /// Asks /status every five seconds until the night stops moving, then
    /// loads the finished report once. Run from the view's `.task(id:)`, so
    /// leaving the screen cancels it.
    func followProgress() async {
        // Also re-entered when the screen reappears (a `.task` restarts on
        // appear); a night that has already stopped isn't asked about again.
        guard pollGeneration > 0, selectedVersion == nil, let date = businessDate,
              runStarted || phase == .running else { return }
        isPolling = true
        defer { isPolling = false }
        let started = Date()
        var waiting = 0
        var errors = 0
        while !Task.isCancelled {
            try? await Task.sleep(for: pollInterval)
            if Task.isCancelled { return }
            var tick = DSRPollPolicy.Tick(elapsed: Date().timeIntervalSince(started), waitingTicks: waiting,
                                          consecutiveErrors: errors, expectingRow: expectingRow,
                                          expectNewerThan: expectNewerThan)
            do {
                let status: DSRStatusResponse = try await client.send("/mobile/api/dsr/\(date)/status",
                                                                      hapticOnError: false)
                errors = 0
                view = status.view ?? view
                tick.exists = status.exists
                tick.consecutiveErrors = 0
                if let fresh = status.checklist {
                    tick.phase = fresh.phase
                    tick.version = fresh.version
                    let stale = expectNewerThan.map { (fresh.version ?? 0) <= $0 } ?? false
                    if !stale {
                        checklist = fresh
                        nothingYet = false
                        runStarted = false
                    }
                    if stale { waiting += 1 }
                } else {
                    waiting += 1
                }
            } catch is CancellationError {
                return
            } catch {
                errors += 1
                tick.consecutiveErrors = errors
            }
            tick.waitingTicks = waiting
            switch DSRPollPolicy.decide(tick) {
            case .keepPolling:
                continue
            case .reload:
                runStarted = false
                expectingRow = false
                expectNewerThan = nil
                try? await fetchReport()
                return
            case .stop:
                runStarted = false
                expectingRow = false
                return
            case .gaveUp:
                runStarted = false
                expectingRow = false
                expectNewerThan = nil
                pollGaveUp = true
                return
            }
        }
    }

    // MARK: Close day / re-run

    private struct CloseBody: Encodable {
        let date: String?
        let rerun: Bool?
        var early: Bool? = nil
    }

    /// The POS hasn't closed the day: the view asks, and a yes re-posts
    /// with `early: true` (DSRCloseGate).
    var confirmingEarlyClose = false

    /// POST /mobile/api/dsr/close, then follow the night on /status. The
    /// run happens on the server's own thread; this only starts it.
    func closeDay(rerun: Bool = false, early: Bool = false) async {
        guard !isSubmitting else { return }
        isSubmitting = true
        actionError = nil
        defer { isSubmitting = false }
        do {
            let r: DSRCloseResponse = try await client.send(
                "/mobile/api/dsr/close", method: .post,
                body: CloseBody(date: businessDate, rerun: rerun ? true : nil, early: early ? true : nil),
                retryTransient: false)
            guard r.ok else {
                actionError = r.error ?? "That didn\u{2019}t start."
                return
            }
            if let d = r.businessDate, DSRFormat.isISODate(d) { businessDate = d }
            selectedVersion = nil
            if let follow = DSRFollow.after(r) {
                expectingRow = follow.expectingRow
                expectNewerThan = follow.newerThan
                // Until the first tick lands, the night reads as running.
                runStarted = true
                startPolling()
            } else {
                // Already final — nothing to run; show it.
                try? await fetchReport()
            }
        } catch let error as APIClient.APIError where !early && DSRCloseGate.isBeforeClose(error) {
            // Not a failure: the POS day is still open. Ask first.
            confirmingEarlyClose = true
        } catch let error as APIClient.APIError {
            actionError = error.message
        } catch is CancellationError {
        } catch {
            actionError = "Couldn\u{2019}t reach Cavnar AI. Nothing was started."
        }
    }

    /// True between pressing Close day / Re-run and the night's first answer.
    var isStarting: Bool { runStarted && (expectingRow || expectNewerThan != nil) }
}

// MARK: - The list of nights

@Observable
@MainActor
final class DailyReportListViewModel {
    private(set) var reports: [DSRSummary] = []
    private(set) var view: String?
    private(set) var isLoading = false
    private(set) var isLoadingMore = false
    private(set) var reachedEnd = false
    private(set) var errorMessage: String?
    private(set) var noAccess = false
    /// When the list last loaded — the foreground-refresh clock.
    private(set) var lastLoadedAt: Date?
    private let client: APIClient
    static let pageSize = 30

    init(client: APIClient = .shared) { self.client = client }

    /// The last good list, painted before the fetch (ResponseCache).
    @ObservationIgnored private let cache = ResponseCache<DSRListResponse>("dsr.list")
    /// When the cached list on screen was stored; nil once a live load lands.
    private(set) var cachedAt: Date?
    var stalenessNotice: String? { CacheFreshness.notice(savedAt: cachedAt) }

    func load() async {
        let generation = SessionScope.generation
        if reports.isEmpty, let hit = await cache.load() {
            reports = hit.value.reports
            view = hit.value.view
            reachedEnd = hit.value.reports.count < Self.pageSize
            cachedAt = hit.savedAt
        }
        isLoading = reports.isEmpty
        errorMessage = nil
        defer { isLoading = false }
        do {
            let fetched: (value: DSRListResponse, body: Data) = try await client.sendKeepingBody(
                "/mobile/api/dsr", query: ["limit": String(Self.pageSize)], hapticOnError: false)
            let r = fetched.value
            cache.save(fetched.body, generation: generation)
            cachedAt = nil
            reports = r.reports
            view = r.view
            reachedEnd = r.reports.count < Self.pageSize
            lastLoadedAt = Date()
        } catch let error as APIClient.APIError {
            noAccess = error.status == 403
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t load the daily reports."
        }
    }

    private(set) var isClosing = false
    var closeError: String?

    private struct CloseBody: Encodable { var early: Bool? = nil }

    /// The POS hasn't closed the day: the list asks, and a yes re-posts with
    /// `early: true` (DSRCloseGate).
    var confirmingEarlyClose = false

    /// Close day for tonight (the server picks tonight's business date).
    /// Returns the night to open — following its progress when it started —
    /// or nil with `closeError` set to the server's sentence.
    func closeTonight(early: Bool = false) async -> DailyReportRoute? {
        guard !isClosing else { return nil }
        isClosing = true
        closeError = nil
        defer { isClosing = false }
        do {
            let r: DSRCloseResponse = try await client.send("/mobile/api/dsr/close", method: .post,
                                                            body: CloseBody(early: early ? true : nil),
                                                            retryTransient: false)
            guard r.ok, let date = r.businessDate, DSRFormat.isISODate(date) else {
                closeError = r.error ?? "That didn\u{2019}t start."
                return nil
            }
            return .report(date: date, follow: DSRFollow.after(r))
        } catch let error as APIClient.APIError where !early && DSRCloseGate.isBeforeClose(error) {
            confirmingEarlyClose = true
        } catch let error as APIClient.APIError {
            closeError = error.message
        } catch is CancellationError {
        } catch {
            closeError = "Couldn\u{2019}t reach Cavnar AI. Nothing was started."
        }
        return nil
    }

    func loadMore() async {
        guard !isLoadingMore, !reachedEnd, let last = reports.last else { return }
        isLoadingMore = true
        defer { isLoadingMore = false }
        let r: DSRListResponse? = try? await client.send(
            "/mobile/api/dsr", query: ["limit": String(Self.pageSize), "before": last.businessDate], hapticOnError: false)
        guard let r else { return }
        let seen = Set(reports.map(\.businessDate))
        reports += r.reports.filter { !seen.contains($0.businessDate) }
        reachedEnd = r.reports.count < Self.pageSize
    }
}

// MARK: - The week

@Observable
@MainActor
final class DailyReportWeekViewModel {
    private(set) var grid: DSRGrid?
    private(set) var isLoading = false
    private(set) var errorMessage: String?
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    /// The restaurant's week holding `date` (nil = the latest night's), or
    /// with `period` the fiscal period holding it (/dsr/period — the web's
    /// Period view; the app never read it).
    func load(date: String?, period: Bool = false) async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        var query: [String: String] = [:]
        if let date, DSRFormat.isISODate(date) { query["date"] = date }
        // A switch between week and period must not leave the other's grid
        // on screen under the new title.
        if let g = grid, (g.kind == "period") != period { grid = nil }
        do {
            if period {
                let r: DSRPeriodResponse = try await client.send("/mobile/api/dsr/period", query: query, hapticOnError: false)
                grid = r.period
            } else {
                let r: DSRWeekResponse = try await client.send("/mobile/api/dsr/week", query: query, hapticOnError: false)
                grid = r.week
            }
        } catch let error as APIClient.APIError {
            // 409 without a fiscal calendar: the server's sentence says so.
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = period ? "Couldn\u{2019}t load the period." : "Couldn\u{2019}t load the week."
        }
    }

    /// A day inside the week (or period) before / after the one showing.
    func neighbour(_ step: Int) -> String? {
        guard let g = grid else { return nil }
        if g.kind == "period" {
            // A period is four or five weeks: step off its first or last day.
            return step < 0 ? DSRFormat.isoAdding(days: -1, to: g.start) : DSRFormat.isoAdding(days: 1, to: g.end)
        }
        return DSRFormat.isoAdding(days: 7 * step, to: g.start)
    }
}
