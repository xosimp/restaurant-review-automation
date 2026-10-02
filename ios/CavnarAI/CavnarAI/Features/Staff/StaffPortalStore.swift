import Foundation
import Observation

// The staff app's frame state (employee audit M1, wave 2 I2): which tab is
// up, every Today section's load state, the two badges, and the deep-link
// hand-off. One store for the portal, owned by StaffPortalView and handed
// to every tab as `store:` — so switching tabs loses nothing (UX-12) and a
// screen on one tab can move the app to another.
//
// CONTRACT for the screens that plug in (I3's Me / Inbox / message thread,
// I4's Tasks, the Requests tab):
//   store.selectedTab = .requests          switch tabs (Checklist → .tasks)
//   store.showingInbox = true              present the Inbox sheet
//   store.messageShiftDate = "2026-10-02"  present the manager thread about
//                                          a shift (nil-with-showingMessages
//                                          for no context)
//   store.focus                            the StaffDeepLink (I1's) that
//                                          opened the app; the tab it names
//                                          reads focus.itemID, then calls
//                                          store.consumeFocus()
//   await store.reloadBadges()             after accepting / declining a
//                                          swap or offer, after reading the
//                                          inbox
//   await store.reloadShifts()             after a drop / swap / claim
//   await store.reloadTasks()              Tasks' pull: I4's StaffTasksStore
//                                          reads /tasks itself (version 2,
//                                          its cache and offline queue) — the
//                                          one /tasks read path
//   store.shifts / store.profile …         StaffSection<T> values
//   store.session                          the StaffSessionStore (authed calls)

/// The four tabs of the bar (Part C): Today · Tasks · Requests · Me.
/// `StaffTab` itself is I1's (Push/StaffDeepLink.swift — the server's
/// names, with `.inbox`); the inbox is not a tab here but a sheet over
/// Today, so `selectedTab` is never `.inbox` (`apply(_:)` maps it).
extension StaffTab {
    static let bar: [StaffTab] = [.today, .tasks, .requests, .me]

    var portalTitle: String {
        switch self {
        case .today: return "Today"
        case .tasks: return "Tasks"
        case .requests: return "Requests"
        case .me: return "Me"
        case .inbox: return "Inbox"
        }
    }

    var portalSymbol: String {
        switch self {
        case .today: return "calendar"
        case .tasks: return "checklist"
        case .requests: return "arrow.left.arrow.right"
        case .me: return "person.crop.circle"
        case .inbox: return "tray"
        }
    }
}

/// One section's load state. A failed load is a failure with the server's
/// sentence, never an empty list (C7); a value painted from StaffCache says
/// so (`fromCache`) until the live answer replaces it (H6).
struct StaffSection<Value> {
    var value: Value?
    var error: String?
    var isLoading = false
    var fromCache = false
    var loadedAt: Date?

    enum Phase: Equatable { case loading, ready, failed }

    /// What to draw: the value whenever there is one (even if a refresh
    /// then failed), the failure when there is none, else the pulse.
    var phase: Phase {
        if value != nil { return .ready }
        if error != nil { return .failed }
        return .loading
    }

    /// A refresh failed while an older value stays on screen.
    var isStale: Bool { value != nil && error != nil }
}

@Observable
@MainActor
final class StaffPortalStore {
    // Frame
    var selectedTab: StaffTab = .today
    var showingInbox = false
    var showingMessages = false
    var messageShiftDate: String?
    /// The link that opened the app (I1's StaffDeepLink): its tab is
    /// selected, and the screen on it reads `itemID`, then calls
    /// consumeFocus().
    var focus: StaffDeepLink?

    // Sections
    var profile = StaffSection<StaffProfile>()
    var shifts = StaffSection<StaffShiftsResponse>()
    var waiting = StaffSection<StaffWaitingResponse>()
    var inbox = StaffSection<StaffInboxBadge>()
    var brief = StaffSection<StaffPersonalBrief>()
    var coworkers = StaffSection<StaffCoworkersResponse>()
    var stats = StaffSection<StaffStats>()
    var earnings = StaffSection<StaffEarnings>()
    var recognition = StaffSection<StaffRecognition>()
    var late = StaffSection<StaffRunningLateList>()

    /// The last time a full refresh finished — "Updated 3:42pm", and the
    /// foreground rule's clock (reload after 5 minutes away).
    private(set) var lastRefresh: Date?
    private(set) var memberScope: String?

    private(set) var session: StaffSessionStore?

    init() {}

    func attach(_ session: StaffSessionStore) {
        guard self.session !== session else { return }
        self.session = session
        warmFromSessionCache()
    }

    // MARK: Badges

    /// Swaps asked of me + shifts offered me (the red count on Requests).
    var requestsBadge: Int { waiting.value?.count ?? 0 }
    /// Unread announcements + unread manager replies.
    var inboxBadge: Int { inbox.value?.total ?? 0 }

    // MARK: Deep links

    func apply(_ link: StaffDeepLink) {
        // Requests and the inbox read `focus` (the request / announcement /
        // thread it names) and consume it; the other tabs only switch.
        focus = (link.tab == .inbox || link.tab == .requests) ? link : nil
        if link.tab == .inbox {
            selectedTab = .today
            showingInbox = true
        } else {
            selectedTab = link.tab
        }
    }

    func consumeFocus() { focus = nil }

    // MARK: Loading

    /// Every Today section, each landing as it arrives (a slow earnings
    /// read never holds the week back).
    func refreshAll() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.reloadProfile() }
            group.addTask { await self.reloadShifts() }
            group.addTask { await self.refreshTasks() }
            group.addTask { await self.reloadBadges() }
            group.addTask { await self.reloadBrief() }
            group.addTask { await self.reloadStats() }
            group.addTask { await self.reloadEarnings() }
            group.addTask { await self.reloadRecognition() }
            group.addTask { await self.reloadLate() }
        }
        lastAttempt = Date()
        // "Updated 3:42pm" only for a week that really came in.
        if shifts.error == nil, !shifts.fromCache { lastRefresh = lastAttempt }
    }

    /// When the last refresh was tried, whatever came of it.
    private(set) var lastAttempt: Date?

    /// The foreground rule's clock (H6): five minutes after the last try;
    /// at once when the week never loaded, so a phone that came back from a
    /// dead spot doesn't sit on "Couldn't load" until a pull.
    var foregroundClock: Date? {
        guard let lastAttempt else { return nil }
        return shifts.phase == .failed ? .distantPast : lastAttempt
    }

    /// The same rule, callable: reload when the last try is older than
    /// `maxAge`, or never happened.
    func refreshIfStale(maxAge: TimeInterval = 300, now: Date = Date()) async {
        if let clock = foregroundClock, now.timeIntervalSince(clock) < maxAge { return }
        await refreshAll()
    }

    func reloadProfile() async {
        await load("/staff/api/me", into: \.profile, cacheKey: "profile") { (r: StaffProfileResponse) in r.employee }
        if let p = profile.value, !profile.fromCache, let token = session?.token {
            let member = StaffCache.memberScope(restaurant: p.restaurant, name: p.name)
            StaffCache.link(session: StaffCache.sessionScope(token: token), to: member)
            if memberScope != member {
                memberScope = member
                warm(from: member)
                saveLive(to: member)
            }
        }
    }

    /// Sections that landed live before /me said whose they are.
    private func saveLive(to scope: String) {
        if let v = shifts.value, !shifts.fromCache { StaffCache.save(v, key: "shifts", scope: scope) }
        if let v = stats.value, !stats.fromCache { StaffCache.save(v, key: "stats", scope: scope) }
        if let v = earnings.value, !earnings.fromCache { StaffCache.save(v, key: "earnings", scope: scope) }
        if let v = profile.value, !profile.fromCache { StaffCache.save(v, key: "profile", scope: scope) }
    }

    func reloadShifts() async {
        await load("/staff/api/shifts", into: \.shifts, cacheKey: "shifts") { (r: StaffShiftsResponse) in r }
        await reloadCoworkers()
    }

    /// Tasks has one read path: I4's StaffTasksStore, which sends
    /// `X-Staff-Tasks-Version: 2`, keeps last night's note, the phone's
    /// "as of" copy and the offline tick queue. A pull forces a read.
    func reloadTasks() async {
        guard let session else { return }
        let tasks = StaffTasksStore.shared
        tasks.attach(session)
        await tasks.load()
    }

    /// The portal's refresh (start, foreground, Today's pull): Tasks reads
    /// only when its copy is over a minute old, so the tab's own appearance
    /// right after doesn't read twice.
    func refreshTasks() async {
        guard let session else { return }
        let tasks = StaffTasksStore.shared
        tasks.attach(session)
        await tasks.refreshIfNeeded()
    }

    func reloadBadges() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.reloadWaiting() }
            group.addTask { await self.reloadInboxCount() }
        }
    }

    func reloadWaiting() async {
        await load("/staff/api/shift-requests", into: \.waiting) { (r: StaffWaitingResponse) in r }
    }

    func reloadInboxCount() async {
        await load("/staff/api/inbox", into: \.inbox) { (r: StaffInboxBadge) in r }
    }

    func reloadBrief() async {
        await load("/staff/api/preshift", into: \.brief) { (r: StaffPersonalBrief) in r }
    }

    func reloadStats() async {
        await load("/staff/api/stats", into: \.stats, cacheKey: "stats") { (r: StaffStats) in r }
    }

    func reloadEarnings() async {
        await load("/staff/api/earnings", query: ["days": "14"], into: \.earnings, cacheKey: "earnings") { (r: StaffEarnings) in r }
    }

    func reloadRecognition() async {
        await load("/staff/api/recognition", into: \.recognition) { (r: StaffRecognition) in r }
    }

    func reloadLate() async {
        await load("/staff/api/running-late", into: \.late) { (r: StaffRunningLateList) in r }
    }

    /// Who's on with me, for the hero's day (the next working day, today
    /// when today has a shift left).
    func reloadCoworkers(now: Date = Date()) async {
        guard let week = shifts.value?.week,
              let hero = StaffTodayPlan.hero(week: week, now: now) else {
            coworkers = StaffSection()
            return
        }
        let date = hero.day.date
        if coworkers.value?.date != date { coworkers.value = nil }
        await load("/staff/api/colleagues", query: ["date": date], into: \.coworkers) { (r: StaffCoworkersResponse) in r }
    }

    // MARK: Running late (H1)

    enum LateOutcome: Equatable {
        case told(managersTold: Bool, created: Bool)
        case refused(String)
    }

    func reportLate(date: String, shiftStart: String, eta: Int, note: String) async -> LateOutcome {
        guard let session else { return .refused("You're signed out.") }
        let trimmed = note.trimmingCharacters(in: .whitespacesAndNewlines)
        let body = StaffRunningLateBody(date: date, shiftStart: shiftStart, etaMinutes: eta,
                                        note: trimmed.isEmpty ? nil : String(trimmed.prefix(200)))
        do {
            let r: StaffRunningLateResult = try await session.authed("/staff/api/running-late", method: .post,
                                                                      body: body)
            guard r.ok else { return .refused(r.error ?? "That didn't go through.") }
            await reloadLate()
            return .told(managersTold: r.managersTold ?? false, created: r.created ?? true)
        } catch let error as APIClient.APIError {
            return .refused(error.message)
        } catch {
            return .refused("That didn't go through. Check your connection, or call your manager.")
        }
    }

    /// Today's report on a leg, if one was sent ("You told them 20 min").
    func lateReport(date: String, shiftStart: String?) -> StaffRunningLateReport? {
        guard let start = shiftStart.flatMap(StaffTime.minutes) else { return nil }
        return late.value?.reports?.first {
            $0.date == date && StaffTime.minutes($0.shiftStart) == start
        }
    }

    // MARK: Plumbing

    private func load<Response: Decodable & StaffRefusable, Value>(
        _ path: String,
        query: [String: String] = [:],
        into keyPath: ReferenceWritableKeyPath<StaffPortalStore, StaffSection<Value>>,
        cacheKey: String? = nil,
        _ pick: (Response) -> Value?
    ) async {
        guard let session else { return }
        self[keyPath: keyPath].isLoading = true
        do {
            let response: Response = try await session.authed(path, query: query)
            if !response.ok {
                fail(keyPath, response.error ?? "That didn't load.")
                return
            }
            guard let value = pick(response) else {
                fail(keyPath, response.error ?? "That didn't load.")
                return
            }
            self[keyPath: keyPath] = StaffSection(value: value, error: nil, isLoading: false,
                                                  fromCache: false, loadedAt: Date())
            if let cacheKey, let scope = memberScope, let encodable = value as? Encodable {
                StaffCache.save(AnyEncodable(encodable), key: cacheKey, scope: scope)
            }
        } catch let error as APIClient.APIError {
            fail(keyPath, error.message)
        } catch is APIClient.SessionExpiredError {
            fail(keyPath, "Your session ended. Sign in again.")
        } catch {
            fail(keyPath, "That didn't load. Check your connection.")
        }
    }

    private func fail<Value>(_ keyPath: ReferenceWritableKeyPath<StaffPortalStore, StaffSection<Value>>,
                             _ message: String) {
        self[keyPath: keyPath].error = message
        self[keyPath: keyPath].isLoading = false
    }

    /// A relaunch inside the same PIN session: the session is already known
    /// to be this employee's, so their copies paint before any network.
    private func warmFromSessionCache() {
        guard let token = session?.token,
              let member = StaffCache.memberScope(forSession: StaffCache.sessionScope(token: token)) else { return }
        memberScope = member
        warm(from: member)
    }

    /// Paints each cached section that hasn't loaded live yet, marked as
    /// "as of" its save.
    private func warm(from scope: String) {
        warmOne(\.profile, StaffProfile.self, "profile", scope)
        warmOne(\.shifts, StaffShiftsResponse.self, "shifts", scope)
        warmOne(\.stats, StaffStats.self, "stats", scope)
        warmOne(\.earnings, StaffEarnings.self, "earnings", scope)
    }

    private func warmOne<Value: Decodable>(_ keyPath: ReferenceWritableKeyPath<StaffPortalStore, StaffSection<Value>>,
                                           _ type: Value.Type, _ key: String, _ scope: String) {
        guard self[keyPath: keyPath].value == nil,
              let hit = StaffCache.load(type, key: key, scope: scope) else { return }
        self[keyPath: keyPath].value = hit.value
        self[keyPath: keyPath].fromCache = true
        self[keyPath: keyPath].loadedAt = hit.savedAt
    }

    /// The oldest "as of" among the sections on screen from the cache.
    var cachedSince: Date? {
        [shifts.fromCache ? shifts.loadedAt : nil, profile.fromCache ? profile.loadedAt : nil]
            .compactMap { $0 }.min()
    }
}

/// Lets `load` cache whichever Encodable value it picked.
private struct AnyEncodable: Encodable {
    let base: Encodable
    init(_ base: Encodable) { self.base = base }
    func encode(to encoder: Encoder) throws { try base.encode(to: encoder) }
}
