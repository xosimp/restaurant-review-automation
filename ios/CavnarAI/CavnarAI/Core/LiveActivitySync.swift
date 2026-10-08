import ActivityKit
import Foundation
import UIKit

// MARK: - Push tokens for the Live Activities (parity audit #38, #61, #94)

/// The body POST /mobile/api/live-activity-tokens reads — every key
/// live_activities.register_token takes.
struct LiveActivityTokenBody: Encodable, Equatable {
    let activityType: String
    let kind: String
    let token: String
    let environment: String
    let activityKey: String

    enum CodingKeys: String, CodingKey {
        case kind, token, environment
        case activityType = "activity_type"
        case activityKey = "activity_key"
    }
}

/// DELETE /mobile/api/live-activity-tokens {activity_type, kind}.
struct LiveActivityTokenRemoval: Encodable, Equatable {
    let activityType: String
    let kind: String?

    enum CodingKeys: String, CodingKey {
        case kind
        case activityType = "activity_type"
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(activityType, forKey: .activityType)
        try c.encodeIfPresent(kind, forKey: .kind)
    }
}

/// Files this phone's Live Activity push tokens with the server, under the
/// owner session that is signed in:
///
/// - a push-to-start token per activity type (iOS 17.2+) — the queued-send
///   countdown, which the server starts the moment a send is queued from
///   anywhere (#61), and "Tonight's service" when this phone opted in (#94);
/// - each running activity's own update token, so the server's pushes move
///   it with the app closed — the countdown's end, the generation's days
///   drafted (#38), the night's pulse.
///
/// The server files each under this sign-in's session: signing out stops
/// every push to the phone with no second clean-up (push.live_activity_tokens).
@MainActor
final class LiveActivitySync {
    static let shared = LiveActivitySync()

    static let pendingSend = "pending_send"
    static let scheduleBuild = "schedule_build"
    static let service = "service"

    private let client: APIClient
    /// What this session has already filed ("type|kind|key" -> token), so a
    /// refresh re-sends only what changed.
    private var filed: [String: String] = [:]
    private var filedFor: String?
    /// The newest push-to-start token per type, as iOS hands them over.
    private var startTokens: [String: String] = [:]
    private var watched: Set<String> = []
    private var observing = false

    init(client: APIClient = .shared) { self.client = client }

    nonisolated static func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }

    /// Called from every widget refresh with the owner session's bearer.
    func refresh(bearer: String) async {
        if filedFor != bearer {
            filed = [:]
            filedFor = bearer
        }
        observeIfNeeded()
        if #available(iOS 17.2, *) {
            if let t = Activity<PendingSendAttributes>.pushToStartToken { startTokens[Self.pendingSend] = Self.hex(t) }
            if let t = Activity<ServiceAttributes>.pushToStartToken { startTokens[Self.service] = Self.hex(t) }
        }
        for (type, token) in startTokens {
            if type == Self.service && !ServiceActivities.optedIn { continue }
            await file(type: type, kind: "start", token: token, key: "", bearer: bearer)
        }
        for a in Activity<PendingSendAttributes>.activities {
            if let t = a.pushToken {
                await file(type: Self.pendingSend, kind: "update", token: Self.hex(t),
                           key: String(a.attributes.actionId), bearer: bearer)
            }
        }
        for a in Activity<ScheduleBuildAttributes>.activities {
            if let t = a.pushToken {
                await file(type: Self.scheduleBuild, kind: "update", token: Self.hex(t), key: a.attributes.jobId,
                           bearer: bearer)
            }
        }
        for a in Activity<ServiceAttributes>.activities {
            if let t = a.pushToken {
                await file(type: Self.service, kind: "update", token: Self.hex(t), key: a.attributes.businessDate,
                           bearer: bearer)
            }
        }
        await ServiceActivities.sync(bearer: bearer)
    }

    /// Turning "Tonight's service" off: the server forgets this phone's
    /// push-to-start token for it, so it is never started here again.
    func forget(type: String) async {
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty else { return }
        filed = filed.filter { !$0.key.hasPrefix(type + "|start") }
        let _: APIClient.OKResponse? = try? await client.sendWithBearer(
            "/mobile/api/live-activity-tokens", method: .delete,
            body: LiveActivityTokenRemoval(activityType: type, kind: "start"), bearer: bearer)
    }

    /// Re-files the service push-to-start token at once (turned on).
    func fileServiceStartToken() async {
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty else { return }
        if let token = startTokens[Self.service] {
            await file(type: Self.service, kind: "start", token: token, key: "", bearer: bearer)
        }
    }

    private func file(type: String, kind: String, token: String, key: String, bearer: String) async {
        let slot = "\(type)|\(kind)|\(key)"
        guard filed[slot] != token else { return }
        let body = LiveActivityTokenBody(activityType: type, kind: kind, token: token,
                                         environment: PushManager.apnsEnvironment, activityKey: key)
        do {
            let r: APIClient.OKResponse = try await client.sendWithBearer(
                "/mobile/api/live-activity-tokens", method: .post, body: body, bearer: bearer)
            if r.ok { filed[slot] = token }
        } catch {
            // Offline, or this login may not hold it: the next refresh tries again.
        }
    }

    private func fileNow(type: String, kind: String, token: String, key: String) {
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty else { return }
        if filedFor != bearer {
            filed = [:]
            filedFor = bearer
        }
        Task { await self.file(type: type, kind: kind, token: token, key: key, bearer: bearer) }
    }

    /// Follows the tokens iOS hands over: a push-to-start token (and each
    /// rotation of it), and every activity started — by the app or by a
    /// push — with its own update token.
    private func observeIfNeeded() {
        guard !observing else { return }
        observing = true
        if #available(iOS 17.2, *) {
            Task {
                for await data in Activity<PendingSendAttributes>.pushToStartTokenUpdates {
                    self.startTokens[Self.pendingSend] = Self.hex(data)
                    self.fileNow(type: Self.pendingSend, kind: "start", token: Self.hex(data), key: "")
                }
            }
            Task {
                for await data in Activity<ServiceAttributes>.pushToStartTokenUpdates {
                    self.startTokens[Self.service] = Self.hex(data)
                    if ServiceActivities.optedIn {
                        self.fileNow(type: Self.service, kind: "start", token: Self.hex(data), key: "")
                    }
                }
            }
        }
        Task {
            for await activity in Activity<PendingSendAttributes>.activityUpdates {
                self.watch(activity, type: Self.pendingSend, key: String(activity.attributes.actionId))
            }
        }
        Task {
            for await activity in Activity<ScheduleBuildAttributes>.activityUpdates {
                self.watch(activity, type: Self.scheduleBuild, key: activity.attributes.jobId)
            }
        }
        Task {
            for await activity in Activity<ServiceAttributes>.activityUpdates {
                self.watch(activity, type: Self.service, key: activity.attributes.businessDate)
                // A push-started night and one the app started: one stays.
                ServiceActivities.endDuplicates()
            }
        }
        Activity<PendingSendAttributes>.activities.forEach {
            watch($0, type: Self.pendingSend, key: String($0.attributes.actionId))
        }
        Activity<ScheduleBuildAttributes>.activities.forEach {
            watch($0, type: Self.scheduleBuild, key: $0.attributes.jobId)
        }
        Activity<ServiceAttributes>.activities.forEach {
            watch($0, type: Self.service, key: $0.attributes.businessDate)
        }
    }

    private func watch<A: ActivityAttributes>(_ activity: Activity<A>, type: String, key: String) {
        guard watched.insert(activity.id).inserted else { return }
        Task {
            for await data in activity.pushTokenUpdates {
                self.fileNow(type: type, kind: "update", token: Self.hex(data), key: key)
            }
        }
    }

    /// Signed out: every Live Activity of the owner's ends on this phone.
    static func endAll() {
        for a in Activity<ScheduleBuildAttributes>.activities {
            Task { await a.end(nil, dismissalPolicy: .immediate) }
        }
        for a in Activity<ServiceAttributes>.activities {
            Task { await a.end(nil, dismissalPolicy: .immediate) }
        }
        shared.filed = [:]
        shared.filedFor = nil
    }
}

// MARK: - "Building next week" (#38)

/// The generation's `typical` from the start answer (mobile_api's
/// generate-schedule: schedule_engine.typical_generation_seconds) — the
/// measured median, or nothing until two have run.
struct ScheduleTypical: Decodable, Equatable {
    let seconds: Int?
    let n: Int?
    let basis: String?
}

/// Started, updated and ended from LaborViewModel's generation flow; the
/// server's pushes carry the same ContentState when the app is closed.
@MainActor
enum ScheduleBuildActivities {
    /// "Next week" / "Week of 10/12/26" / "Redoing 2 days" from the week the
    /// owner picked (GenerateWeek.label) and the days being redone.
    nonisolated static func weekLabel(pickerLabel: String, redoCount: Int) -> String {
        if redoCount > 0 { return redoCount == 1 ? "Redoing 1 day" : "Redoing \(redoCount) days" }
        if pickerLabel.first?.isNumber == true { return "Week of \(pickerLabel)" }
        return pickerLabel
    }

    static func start(jobId: String, weekLabel: String, typicalSeconds: Int?, startedAt: Date = Date()) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        if Activity<ScheduleBuildAttributes>.activities.contains(where: { $0.attributes.jobId == jobId }) { return }
        let end = typicalSeconds.flatMap { $0 > 0 ? startedAt.addingTimeInterval(TimeInterval($0)) : nil }
        let state = ScheduleBuildAttributes.ContentState(status: "building", estimatedEnd: end)
        let attributes = ScheduleBuildAttributes(jobId: jobId, weekLabel: weekLabel)
        // Stale an hour on: past any generation's own deadline.
        _ = try? Activity.request(attributes: attributes,
                                  content: ActivityContent(state: state, staleDate: Date().addingTimeInterval(3600)),
                                  pushType: .token)
    }

    static func update(jobId: String, daysDrafted: Int, daysTotal: Int) {
        for a in Activity<ScheduleBuildAttributes>.activities where a.attributes.jobId == jobId {
            var state = a.content.state
            guard state.status == "building", state.daysDrafted != daysDrafted || state.daysTotal != daysTotal
            else { continue }
            state.daysDrafted = daysDrafted
            state.daysTotal = daysTotal
            Task { await a.update(ActivityContent(state: state, staleDate: a.content.staleDate)) }
        }
    }

    static func end(jobId: String, ok: Bool, note: String?) {
        for a in Activity<ScheduleBuildAttributes>.activities where a.attributes.jobId == jobId {
            var state = a.content.state
            state.status = ok ? "done" : "failed"
            state.note = ok ? nil : note
            if ok, state.daysTotal > 0 { state.daysDrafted = state.daysTotal }
            Task {
                await a.end(ActivityContent(state: state, staleDate: nil),
                            dismissalPolicy: .after(Date().addingTimeInterval(ok ? 15 * 60 : 30 * 60)))
            }
        }
    }
}

// MARK: - "Tonight's service" (#94)

/// GET /mobile/api/intraday/tonight — the attributes and content state the
/// server's pushes carry, built by the same function (service_content).
struct ServiceTonightResponse: Decodable {
    let ok: Bool
    let inService: Bool
    let businessDate: String?
    let attributes: ServiceAttributes?
    let contentState: ServiceAttributes.ContentState?
    let closesAtUnix: Double?

    enum CodingKeys: String, CodingKey {
        case ok, attributes
        case inService = "in_service"
        case businessDate = "business_date"
        case contentState = "content_state"
        case closesAtUnix = "closes_at_unix"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        inService = (try? c.decode(Bool.self, forKey: .inService)) ?? false
        businessDate = try? c.decodeIfPresent(String.self, forKey: .businessDate)
        attributes = try? c.decodeIfPresent(ServiceAttributes.self, forKey: .attributes)
        contentState = try? c.decodeIfPresent(ServiceAttributes.ContentState.self, forKey: .contentState)
        closesAtUnix = try? c.decodeIfPresent(Double.self, forKey: .closesAtUnix)
    }
}

/// Opt-in per phone (Account → Alerts → On this phone). While on, the app
/// files the service push-to-start token so the server can start tonight's
/// activity at open without the app being opened, and — on any iOS 17 —
/// starts it itself when it is opened during service.
@MainActor
enum ServiceActivities {
    private static let optInKey = "cavnar.pref.service_live_activity"

    static var optedIn: Bool { UserDefaults.standard.bool(forKey: optInKey) }

    static func setOptedIn(_ on: Bool) async {
        UserDefaults.standard.set(on, forKey: optInKey)
        if on {
            await LiveActivitySync.shared.fileServiceStartToken()
            if let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty {
                await sync(bearer: bearer)
            }
        } else {
            await LiveActivitySync.shared.forget(type: LiveActivitySync.service)
            for a in Activity<ServiceAttributes>.activities {
                Task { await a.end(nil, dismissalPolicy: .immediate) }
            }
        }
    }

    /// Starts tonight's activity when the restaurant is open and none is
    /// running, refreshes a running one, and ends one whose service is over.
    static func sync(bearer: String, client: APIClient = .shared) async {
        guard optedIn, ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        guard let r: ServiceTonightResponse = try? await client.sendWithBearer(
            "/mobile/api/intraday/tonight", bearer: bearer), r.ok,
              let attributes = r.attributes, let state = r.contentState else { return }
        let stale = r.closesAtUnix.map { Date(timeIntervalSince1970: $0 + 15 * 60) }
        for a in Activity<ServiceAttributes>.activities
        where !r.inService || a.attributes.businessDate != attributes.businessDate
            || a.attributes.restaurantId != attributes.restaurantId {
            var closed = a.content.state
            closed.status = "closed"
            Task { await a.end(ActivityContent(state: closed, staleDate: nil),
                               dismissalPolicy: .after(Date().addingTimeInterval(15 * 60))) }
        }
        guard r.inService else { return }
        let running = Activity<ServiceAttributes>.activities.filter {
            $0.attributes.businessDate == attributes.businessDate && $0.attributes.restaurantId == attributes.restaurantId
        }
        if running.isEmpty {
            _ = try? Activity.request(attributes: attributes, content: ActivityContent(state: state, staleDate: stale),
                                      pushType: .token)
        } else {
            for a in running where a.content.state != state {
                Task { await a.update(ActivityContent(state: state, staleDate: stale)) }
            }
        }
        endDuplicates()
    }

    /// One activity per night: a second (the server's push-to-start racing
    /// the app's own start) is ended.
    static func endDuplicates() {
        var seen: Set<String> = []
        for a in Activity<ServiceAttributes>.activities {
            let key = "\(a.attributes.restaurantId)|\(a.attributes.businessDate)"
            if seen.insert(key).inserted { continue }
            Task { await a.end(nil, dismissalPolicy: .immediate) }
        }
    }
}

// MARK: - The widget's location list (#96)

/// GET /mobile/api/group-locations, for Siri / Shortcuts' location picker in
/// the app process. The widget extension reads the stored copy instead.
@MainActor
enum WidgetLocationsReader {
    private struct Response: Decodable {
        let ok: Bool
        let locations: [LocationOption]
    }

    static func fetch(client: APIClient = .shared) async -> [WidgetLocationOption]? {
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty,
              let r: Response = try? await client.sendWithBearer("/mobile/api/group-locations", bearer: bearer),
              r.ok else { return nil }
        let list = r.locations.count > 1 ? r.locations.map { WidgetLocationOption(id: $0.id, name: $0.name) } : []
        WidgetLocations.save(list)
        return list
    }
}
