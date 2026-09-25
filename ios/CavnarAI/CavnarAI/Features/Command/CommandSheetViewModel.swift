import Foundation
import Observation

/// The phone's command sheet (Friction audit #47, U3-24 / U4-21): what's
/// waiting, the places and locations one tap away, and a field that finds a
/// place, a person or an item — and hands anything else to Ask Cavnar.
///
/// Nothing here decides anything. The registry, the search and the confirm
/// card all come from the server's permission-filtered routes; navigation is
/// a `.cavnarOpenNav` post the router handles; an action's confirm is the
/// same AskProposal Ask renders and confirms through Ask's own route. When
/// the command routes are missing (an older server) the sheet still works
/// as navigation + Ask, from the local list of places.
@Observable
@MainActor
final class CommandSheetViewModel {
    var query = ""
    private(set) var registry: [CommandEntry] = []
    private(set) var registryLive = false
    private(set) var waiting: [CommandWaitingItem] = []
    private(set) var pendingSends: [PendingSendActivities.PendingAction] = []
    private(set) var searchResults: [CommandSearchResult] = []
    private(set) var isSearching = false
    private(set) var searchUnavailable = false
    private(set) var isLoading = true
    /// The confirm card for the action tapped last, from /command/propose.
    private(set) var proposal: AskProposal?
    private(set) var proposing: String?
    var errorLine: String?
    /// Per-row outcome of an inline answer or undo ("Approved", "Stopped").
    private(set) var rowOutcome: [String: String] = [:]
    private(set) var rowBusy: Set<String> = []

    /// The same view model Ask confirms with — so a proposal confirmed here
    /// runs the same route, waits on the same job, and writes the same
    /// ask_cavnar_actions line as it would in Ask.
    let askViewModel = AskCavnarViewModel()

    private let client: APIClient
    private var searchTask: Task<Void, Never>?

    init(client: APIClient = .shared) { self.client = client }

    /// Places matched locally — the registry's, or the built-in list when
    /// the registry route isn't there.
    var matchedCommands: [CommandEntry] {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else { return [] }
        let source = registryLive ? registry : CommandMatch.fallbackPlaces
        return CommandMatch.rank(source, query: q)
    }

    var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    // MARK: Load

    func load() async {
        isLoading = true
        defer { isLoading = false }
        async let reg: CommandRegistryResponse? = try? client.send("/mobile/api/command/registry", hapticOnError: false)
        async let wait: CommandWaitingResponse? = try? client.send("/mobile/api/actions", hapticOnError: false)
        async let sends: PendingSendActivities.PendingResponse? =
            try? client.send("/mobile/api/actions/pending", hapticOnError: false)
        let (r, w, s) = await (reg, wait, sends)
        if let r, r.ok, let commands = r.commands {
            registry = commands
            registryLive = true
        } else {
            registry = []
            registryLive = false
        }
        waiting = w?.items ?? []
        pendingSends = PendingSendActivities.countdowns(s?.actions ?? []).map { $0.0 }
    }

    // MARK: Search

    /// Debounced: the server search runs once the owner pauses typing.
    func queryChanged() {
        searchTask?.cancel()
        proposal = nil
        let q = trimmedQuery
        guard q.count >= 2, !searchUnavailable else {
            searchResults = []
            isSearching = false
            return
        }
        isSearching = true
        searchTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            await self?.search(q)
        }
    }

    private func search(_ q: String) async {
        defer { if trimmedQuery == q { isSearching = false } }
        do {
            let r: CommandSearchResponse = try await client.send("/mobile/api/command/search", query: ["q": q],
                                                                 hapticOnError: false)
            guard trimmedQuery == q else { return }
            searchResults = r.results ?? []
        } catch let error as APIClient.APIError where error.status == 404 {
            // An older server: no search yet. Places and Ask still work.
            searchUnavailable = true
            searchResults = []
        } catch {
            if trimmedQuery == q { searchResults = [] }
        }
    }

    // MARK: Actions

    private struct ProposeBody: Encodable {
        let action: String
        let args: [String: AnyCodableValue]
    }

    /// An action command: ask the server for the confirm card. Nothing runs
    /// until the owner confirms on the card.
    func propose(_ entry: CommandEntry) async {
        guard let action = entry.action else { return }
        proposing = entry.id
        errorLine = nil
        defer { proposing = nil }
        do {
            let r: CommandProposeResponse = try await client.send(
                "/mobile/api/command/propose", method: .post,
                body: ProposeBody(action: action, args: entry.args ?? [:]))
            if r.ok, let p = r.proposal {
                proposal = p
            } else {
                errorLine = r.error ?? "Cavnar AI couldn't prepare that."
            }
        } catch let error as APIClient.APIError {
            errorLine = error.message
        } catch is CancellationError {
        } catch {
            errorLine = "Couldn't reach Cavnar AI."
        }
    }

    func clearProposal() { proposal = nil }

    // MARK: Inline answers

    private struct DecideBody: Encodable { let decision: String }

    /// Approve / Deny a time-off or shift request in place — the same decide
    /// routes the Labor rows post. A drop approved here opens the shift for
    /// anyone to claim; naming a replacement stays in Labor.
    func decide(_ item: CommandWaitingItem, approve: Bool) async {
        guard let req = item.request else { return }
        let path = req.kind == "time_off"
            ? "/mobile/api/labor/time-off/\(req.id)/decide"
            : "/mobile/api/labor/shift-requests/\(req.id)/decide"
        rowBusy.insert(item.key)
        defer { rowBusy.remove(item.key) }
        do {
            let r: APIClient.OKResponse = try await client.send(path, method: .post, body: DecideBody(decision: approve ? "approve" : "deny"))
            if r.ok {
                rowOutcome[item.key] = approve ? "Approved" : "Denied"
                Haptic.success()
            } else {
                rowOutcome[item.key] = r.error ?? "That didn't go through."
            }
        } catch let error as APIClient.APIError {
            rowOutcome[item.key] = error.message
        } catch {}
    }

    /// Undo a pending send — reversible the other way round: nothing has
    /// gone out yet, so it acts at once.
    func undo(_ send: PendingSendActivities.PendingAction) async {
        let key = "send:\(send.id)"
        rowBusy.insert(key)
        defer { rowBusy.remove(key) }
        let outcome = await PendingSendCanceller.cancel(actionId: send.id, client: client)
        rowOutcome[key] = outcome.stopped ? "Stopped — nothing went out" : outcome.sentence
    }
}
