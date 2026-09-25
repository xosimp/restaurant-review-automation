import ActivityKit
import Foundation
import UIKit
import WidgetKit

/// Keeps what the system shows about Cavnar — the widget, the "Approve
/// replies" quick action, the auto-publish countdown — in step with the
/// server, each time the app becomes active (Friction audit #31, #47).
///
/// Reads with the stored owner token directly rather than waiting on the
/// SessionStore: at a cold launch the store installs its token on a Task,
/// and this runs from the scene's first activation. A staff session never
/// has an owner token, so a staff phone's widget stays empty.
@MainActor
final class WidgetSnapshotService {
    static let shared = WidgetSnapshotService()

    private let client: APIClient
    private var lastRefresh: Date?
    private var inFlight = false
    /// Often enough for a morning glance; rare enough that flicking between
    /// apps doesn't re-read four routes every time.
    static let minInterval: TimeInterval = 5 * 60

    init(client: APIClient = .shared) { self.client = client }

    private struct ActionsResponse: Decodable {
        struct Item: Decodable { let key: String; let count: Int? }
        let ok: Bool
        let items: [Item]?
    }

    func refresh(force: Bool = false) async {
        guard let token = Keychain.get(Keychain.Key.sessionToken), !token.isEmpty else {
            // Signed out (or a staff-only phone): nothing of the
            // restaurant's may stay on the Lock Screen.
            WidgetSnapshot.clear()
            WidgetCenter.shared.reloadTimelines(ofKind: WidgetSnapshot.widgetKind)
            UIApplication.shared.shortcutItems = []
            PendingSendActivities.endAll()
            return
        }
        if inFlight { return }
        if !force, let last = lastRefresh, Date().timeIntervalSince(last) < Self.minInterval { return }
        inFlight = true
        defer { inFlight = false }

        async let actions: ActionsResponse? = try? client.sendWithBearer("/mobile/api/actions", bearer: token)
        async let pending: PendingSendActivities.PendingResponse? =
            try? client.sendWithBearer("/mobile/api/actions/pending", bearer: token)
        async let night = Self.lastNight(client: client, bearer: token)
        let (a, p, n) = await (actions, pending, night)
        lastRefresh = Date()

        var snapshot = n ?? WidgetSnapshot.empty
        let items = a?.items ?? []
        snapshot.waitingCount = items.count
        snapshot.pendingReplies = items.first(where: { $0.key == "no_response" })?.count ?? 0
        snapshot.updatedAt = Date()
        if a != nil || n != nil {
            WidgetSnapshot.save(snapshot)
            WidgetCenter.shared.reloadTimelines(ofKind: WidgetSnapshot.widgetKind)
        }
        if a != nil {
            UIApplication.shared.shortcutItems =
                [QuickAction.approveRepliesItem(waiting: snapshot.pendingReplies)].compactMap { $0 }
        }
        if let p { PendingSendActivities.sync(p.actions ?? []) }
    }

    /// Last night's net and its measured change, from the same two routes
    /// Home's Last night card reads. Nil when this login has no report.
    private static func lastNight(client: APIClient, bearer: String) async -> WidgetSnapshot? {
        guard let list: DSRListResponse = try? await client.sendWithBearer("/mobile/api/dsr", bearer: bearer),
              let latest = list.reports.first else { return nil }
        let report: DSRReport? = try? await client.sendWithBearer("/mobile/api/dsr/\(latest.businessDate)",
                                                                 bearer: bearer)
        var snap = WidgetSnapshot.empty
        snap.nightLabel = latest.displayDate
        guard let sales = report?.facts.blocks["sales"], sales.isReady else { return snap }
        if let net = sales.metric("net") { snap.netLabel = DSRFormat.money(net) }
        let change = WidgetSnapshotService.change(lastWeek: sales.metric("vs_last_week_pct"),
                                                  yesterday: sales.metric("vs_yesterday_pct"),
                                                  businessDate: latest.businessDate)
        snap.changeLabel = change?.label
        snap.changeBasis = change?.basis
        snap.changeIsUp = change?.up
        return snap
    }

    /// The same weekday last week when the report measured it — the fair
    /// comparison for a restaurant — else yesterday. Nil when neither was
    /// measured: a missing comparison is never shown as "±0%".
    nonisolated static func change(lastWeek: Double?, yesterday: Double?,
                                   businessDate: String) -> (label: String, basis: String, up: Bool)? {
        if let v = lastWeek {
            let weekday = DSRFormat.weekday(businessDate).map { "vs last \($0)" } ?? "vs last week"
            return (DSRFormat.signedPct(v), weekday, v >= 0)
        }
        if let v = yesterday { return (DSRFormat.signedPct(v), "vs yesterday", v >= 0) }
        return nil
    }
}

// MARK: - Live Activity countdown

/// Starts, updates and ends the countdown for each pending send the server
/// holds (delayed.py). The server is the truth: a row that is no longer
/// pending ends its activity, whoever cancelled it.
@MainActor
enum PendingSendActivities {
    struct PendingAction: Decodable {
        let id: Int
        let kind: String
        let label: String?
        let executeAt: String?
        let status: String?
        enum CodingKeys: String, CodingKey { case id, kind, label, status; case executeAt = "execute_at" }
    }

    struct PendingResponse: Decodable {
        let ok: Bool
        let actions: [PendingAction]?
    }

    /// The rows that earn a countdown: an outward send still ahead of us.
    nonisolated static func countdowns(_ rows: [PendingAction], now: Date = Date()) -> [(PendingAction, Date)] {
        rows.compactMap { row in
            guard PendingSendAttributes.countdownKinds.contains(row.kind),
                  (row.status ?? "pending") == "pending",
                  let fire = CavnarISODate.parse(row.executeAt), fire > now else { return nil }
            return (row, fire)
        }
    }

    static func sync(_ rows: [PendingAction]) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let wanted = countdowns(rows)
        let wantedIds = Set(wanted.map { $0.0.id })
        for activity in Activity<PendingSendAttributes>.activities
        where !wantedIds.contains(activity.attributes.actionId) {
            // Gone from the pending list: it went out, or someone undid it.
            let state = PendingSendAttributes.ContentState(fireAt: activity.content.state.fireAt,
                                                           status: "ended", note: nil)
            Task { await activity.end(ActivityContent(state: state, staleDate: nil), dismissalPolicy: .immediate) }
        }
        for (row, fire) in wanted {
            let state = PendingSendAttributes.ContentState(fireAt: fire, status: "pending", note: nil)
            let content = ActivityContent(state: state, staleDate: fire.addingTimeInterval(60))
            if let existing = Activity<PendingSendAttributes>.activities
                .first(where: { $0.attributes.actionId == row.id }) {
                if existing.content.state != state { Task { await existing.update(content) } }
                continue
            }
            let title = (row.label?.isEmpty == false ? row.label! : PendingSendAttributes.plainTitle(kind: row.kind))
            let attributes = PendingSendAttributes(actionId: row.id, kind: row.kind, title: title)
            _ = try? Activity.request(attributes: attributes, content: content, pushType: nil)
        }
    }

    /// Ends one countdown after an Undo, showing the outcome for a moment.
    static func finish(actionId: Int, status: String, note: String?) {
        for activity in Activity<PendingSendAttributes>.activities where activity.attributes.actionId == actionId {
            let state = PendingSendAttributes.ContentState(fireAt: activity.content.state.fireAt,
                                                           status: status, note: note)
            Task {
                await activity.end(ActivityContent(state: state, staleDate: nil),
                                   dismissalPolicy: .after(Date().addingTimeInterval(8)))
            }
        }
    }

    static func endAll() {
        for activity in Activity<PendingSendAttributes>.activities {
            Task { await activity.end(nil, dismissalPolicy: .immediate) }
        }
    }
}

/// The Undo behind the Live Activity button and the "Undo the pending
/// publish" shortcut: the same route the Home activity sheet's Undo posts.
enum PendingSendCanceller {
    struct Outcome {
        let stopped: Bool
        let sentence: String
    }

    private struct CancelBody: Encodable {}

    static func cancel(actionId: Int, client: APIClient = .shared) async -> Outcome {
        guard let token = Keychain.get(Keychain.Key.sessionToken), !token.isEmpty else {
            return Outcome(stopped: false, sentence: "Sign in to Cavnar AI to undo this.")
        }
        let outcome: Outcome
        do {
            let r: APIClient.OKResponse = try await client.sendWithBearer(
                "/mobile/api/actions/\(actionId)/cancel", method: .post, body: CancelBody(), bearer: token)
            outcome = r.ok
                ? Outcome(stopped: true, sentence: "Stopped. Nothing went out.")
                : Outcome(stopped: false, sentence: r.error ?? "That already went out, or was already undone.")
        } catch let error as APIClient.APIError {
            outcome = Outcome(stopped: false, sentence: error.message)
        } catch {
            outcome = Outcome(stopped: false, sentence: "Couldn\u{2019}t reach Cavnar AI. Open the app to undo it.")
        }
        await MainActor.run {
            PendingSendActivities.finish(actionId: actionId, status: outcome.stopped ? "stopped" : "failed",
                                         note: outcome.stopped ? nil : outcome.sentence)
        }
        return outcome
    }

    /// The soonest pending outward send, for the shortcut that names none.
    static func soonestPending(client: APIClient = .shared) async -> PendingSendActivities.PendingAction? {
        guard let token = Keychain.get(Keychain.Key.sessionToken), !token.isEmpty,
              let r: PendingSendActivities.PendingResponse = try? await client.sendWithBearer(
                "/mobile/api/actions/pending", bearer: token) else { return nil }
        return PendingSendActivities.countdowns(r.actions ?? []).first?.0
    }
}

/// execute_at arrives as "2026-09-25T15:00:00Z" (delayed._row), sometimes
/// with fractional seconds.
enum CavnarISODate {
    static func parse(_ raw: String?) -> Date? {
        guard let raw, !raw.isEmpty else { return nil }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let d = plain.date(from: raw) { return d }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: raw)
    }
}
