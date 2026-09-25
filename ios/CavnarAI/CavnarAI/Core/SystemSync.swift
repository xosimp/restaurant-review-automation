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

    private struct LocationsResponse: Decodable {
        let ok: Bool
        let locations: [LocationOption]
    }

    /// Signed out (or a staff-only phone): nothing of the restaurant's may
    /// stay on the Lock Screen, the icon's quick actions or a countdown.
    /// SessionStore calls this the moment it signs out — not at the next
    /// activation, which on a shared back-office phone could be hours away
    /// (F3-8).
    static func clearForSignOut() {
        WidgetSnapshot.clear()
        WidgetCenter.shared.reloadTimelines(ofKind: WidgetSnapshot.widgetKind)
        UIApplication.shared.shortcutItems = []
        PendingSendActivities.endAll()
    }

    /// The waiting half of one read: the open count and the replies count.
    struct WaitingPart: Equatable {
        let count: Int
        let replies: Int
    }

    /// The night half of one read. `.none` (all nil) is a real answer — this
    /// login has no report — which clears the figures; a FAILED read is nil
    /// and keeps the last good ones.
    struct NightPart: Equatable {
        var date: String?
        var label: String?
        var net: String?
        var change: String?
        var basis: String?
        var up: Bool?
        static let none = NightPart()
    }

    /// The next snapshot from the last one and whichever halves this read
    /// got. A half that failed keeps its last value AND its own timestamp, so
    /// it goes stale on its own clock rather than reading "Nothing waiting"
    /// or losing last night's net (F3-8). A snapshot from another location
    /// is never carried over. Nil when there is nothing new to save.
    nonisolated static func merge(previous: WidgetSnapshot?, restaurantId: Int, restaurantName: String?,
                                  waiting: WaitingPart?, night: NightPart?, now: Date) -> WidgetSnapshot? {
        guard waiting != nil || night != nil else { return nil }
        let sameStore = restaurantId > 0 && previous?.restaurantId == restaurantId
        var snap = sameStore ? (previous ?? .empty) : .empty
        snap.restaurantId = restaurantId > 0 ? restaurantId : nil
        snap.restaurantName = restaurantName ?? (sameStore ? snap.restaurantName : nil)
        if let waiting {
            snap.waitingCount = waiting.count
            snap.pendingReplies = waiting.replies
            snap.waitingUpdatedAt = now
        } else if !sameStore {
            snap.waitingUpdatedAt = .distantPast
        }
        if let night {
            snap.nightDate = night.date
            snap.nightLabel = night.label
            snap.netLabel = night.net
            snap.changeLabel = night.change
            snap.changeBasis = night.basis
            snap.changeIsUp = night.up
            snap.nightUpdatedAt = now
        } else if !sameStore {
            snap.nightUpdatedAt = .distantPast
        }
        snap.updatedAt = now
        return snap
    }

    func refresh(force: Bool = false) async {
        guard let token = Keychain.get(Keychain.Key.sessionToken), !token.isEmpty else {
            Self.clearForSignOut()
            return
        }
        if inFlight { return }
        if !force, let last = lastRefresh, Date().timeIntervalSince(last) < Self.minInterval { return }
        inFlight = true
        defer { inFlight = false }

        // `peek=1`: a background read for the widget is not the owner seeing
        // the queue — the server skips presenting its recommendations, as it
        // does for the report below (F3-6).
        async let actions: ActionsResponse? = try? client.sendWithBearer(
            "/mobile/api/actions", query: ["peek": "1"], bearer: token)
        async let pending: PendingSendActivities.PendingResponse? =
            try? client.sendWithBearer("/mobile/api/actions/pending", bearer: token)
        async let night = Self.lastNight(client: client, bearer: token)
        async let locations: LocationsResponse? = try? client.sendWithBearer(
            "/mobile/api/group-locations", bearer: token)
        let (a, p, n, l) = await (actions, pending, night, locations)
        lastRefresh = Date()

        let items = a?.items ?? []
        let waiting = a.map { _ in
            WaitingPart(count: items.count,
                        replies: items.first(where: { $0.key == "no_response" })?.count ?? 0)
        }
        // A store's name only means something beside another one.
        let name = l.flatMap { r in r.locations.count > 1 ? r.locations.first(where: \.active)?.name : nil }
        if let snapshot = Self.merge(previous: WidgetSnapshot.load(), restaurantId: SessionScope.activeRestaurantId,
                                     restaurantName: name, waiting: waiting, night: n, now: Date()) {
            WidgetSnapshot.save(snapshot)
            WidgetCenter.shared.reloadTimelines(ofKind: WidgetSnapshot.widgetKind)
        }
        if let waiting {
            UIApplication.shared.shortcutItems =
                [QuickAction.approveRepliesItem(waiting: waiting.replies)].compactMap { $0 }
        }
        if let p { PendingSendActivities.sync(p.actions ?? []) }
    }

    /// Last night's net and its measured change. `.none` when this login has
    /// no report; nil when the read failed. The report is read with
    /// `peek=1`: a widget refresh is not someone opening the report, so its
    /// actions are not recorded as shown (F3-6 / D3-8).
    private static func lastNight(client: APIClient, bearer: String) async -> NightPart? {
        let list: DSRListResponse
        do {
            list = try await client.sendWithBearer("/mobile/api/dsr", query: ["limit": "1"], bearer: bearer)
        } catch let error as APIClient.APIError where error.status == 403 {
            return NightPart.none
        } catch {
            return nil
        }
        guard let latest = list.reports.first else { return NightPart.none }
        var part = NightPart(date: latest.businessDate, label: latest.displayDate)
        guard let report: DSRReport = try? await client.sendWithBearer(
            "/mobile/api/dsr/\(latest.businessDate)", query: ["peek": "1"], bearer: bearer) else {
            // The list answered and the report didn't: the list's own net.
            part.net = latest.net.map { DSRFormat.money($0) }
            return part
        }
        guard let sales = report.facts.blocks["sales"], sales.isReady else { return part }
        if let net = sales.metric("net") { part.net = DSRFormat.money(net) }
        let change = WidgetSnapshotService.change(lastWeek: sales.metric("vs_last_week_pct"),
                                                  yesterday: sales.metric("vs_yesterday_pct"),
                                                  businessDate: latest.businessDate)
        part.change = change?.label
        part.basis = change?.basis
        part.up = change?.up
        return part
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
