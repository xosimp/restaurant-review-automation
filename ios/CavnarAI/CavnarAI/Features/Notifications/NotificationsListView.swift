import SwiftUI
import Observation

@Observable
@MainActor
final class NotificationsListViewModel {
    var notifications: [NotificationItem] = []
    var isLoading = false
    var errorMessage: String?
    // Distinct from notifications.isEmpty — an inbox with genuinely zero
    // alerts is still "loaded." This is what HomeView checks to decide
    // whether the bell tap needs to wait for the fetch before presenting
    // the sheet (first open) or can present immediately against whatever's
    // already cached (every open after that).
    var hasLoadedOnce = false

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Response: Decodable {
        let ok: Bool
        let notifications: [NotificationItem]
        let error: String?
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer {
            isLoading = false
            hasLoadedOnce = true
        }
        do {
            // mark=0: opening the list no longer reads every row — a row is
            // read when it is opened (markOpened / the row's tap), as on the
            // web (parity audit #7). scope=group: every location this login
            // may switch between; the server answers with the one location
            // for anyone else.
            let response: Response = try await client.send("/mobile/api/notifications",
                                                            query: Self.listQuery)
            // The backend always answers with HTTP 200, even on internal
            // failure (ok:false, notifications:[]) — APIClient.send() only
            // throws on actual HTTP-level errors, so `ok` has to be checked
            // explicitly here or a real failure looks identical to "no
            // notifications yet."
            if response.ok {
                notifications = response.notifications
                // Reading the list is what clears the app icon's number.
                // The backend sends an unread count with every push now and
                // nothing was clearing it, so it could only ever go up.
                PushManager.shared.clearBadge()
                await loadActionables()
            } else {
                errorMessage = response.error ?? "Couldn't load notifications."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
            // The screen went away mid-load — not a failure (CLIENT-49).
        } catch {
            errorMessage = "Couldn't load notifications."
        }
    }

    static let listQuery = ["mark": "0", "scope": "group"]

    /// Where a row's open is recorded: flipped read here at once, and the
    /// server told by the router's tap (it posts notifications/opened with
    /// the row's alert_log id) — so the phone and the web agree.
    func noteOpened(_ item: NotificationItem) {
        guard let i = notifications.firstIndex(where: { $0.id == item.id }) else { return }
        notifications[i].unread = false
        // An urgent row the server can't read a subject for is handled once
        // opened — the server's own rule for the bell's red count.
        if notifications[i].resolvesOnOpen == true { notifications[i].resolved = true }
    }

    private struct OpenedBody: Encodable {
        let type: String
        let alertId: Int?
        enum CodingKeys: String, CodingKey {
            case type
            case alertId = "alert_id"
        }
    }

    /// A row acted on in place (approved) is read too — recorded here, since
    /// no tap went through the router.
    func markOpened(_ item: NotificationItem) async {
        guard item.isUnread else { return }
        noteOpened(item)
        let _: APIClient.OKResponse? = try? await client.send(
            "/mobile/api/notifications/opened", method: .post,
            body: OpenedBody(type: item.type, alertId: item.alertId), hapticOnError: false)
    }

    /// "Mark all read": the read mark moves over every location the list
    /// reads (the list without mark=0 is the server's mark-all).
    func markAllRead() async {
        let r: Response? = try? await client.send("/mobile/api/notifications", query: ["scope": "group"],
                                                   hapticOnError: false)
        guard r?.ok == true else { return }
        for i in notifications.indices { notifications[i].unread = false }
    }

    var unreadCount: Int { notifications.filter(\.isUnread).count }

    // MARK: - Acting from a row (friction audit #22)

    /// Queued automatic sends still pending, and the replies that may be
    /// published unread — what lets a row carry Undo or Approve. Read with
    /// the list; silent on failure (a row then just opens).
    private(set) var pendingActions: [PendingAction] = []
    private(set) var approvableReviewIds: Set<Int> = []
    /// Rows answered here, by id, with what happened ("Undone", "Posted").
    private(set) var answered: [String: String] = [:]
    private(set) var busyRowId: String?
    var rowError: (id: String, message: String)?

    private struct PendingResponse: Decodable { let ok: Bool; let actions: [PendingAction]? }
    private struct ReviewsResponse: Decodable { let ok: Bool; let reviews: [Review] }

    func loadActionables() async {
        if notifications.contains(where: { $0.undoableKind != nil }),
           let r: PendingResponse = try? await client.send("/mobile/api/actions/pending", hapticOnError: false) {
            pendingActions = r.actions ?? []
        }
        // A server that says `can_approve` per row is the rule; the reviews
        // read below is only for an older one.
        if notifications.contains(where: { $0.reviewId != nil && $0.canApprove == nil }),
           let r: ReviewsResponse = try? await client.send(
               "/mobile/api/reviews", query: ["filter": "pending", "limit": "50", "offset": "0"],
               hapticOnError: false) {
            approvableReviewIds = Set(r.reviews.filter(ReviewsListViewModel.canQuickApprove).map(\.id))
        }
    }

    /// The queued send a "going out" row can undo in place (F3-10). The row's
    /// own `delayed_action_id` when the server sends it, and never when the
    /// server says `can_undo: false`. Without an id, only a row at the
    /// location the session is on (the pending list is that location's
    /// only — the list is every location's, so location B's old row used to
    /// cancel location A's send), only the NEWEST row of its kind there,
    /// only when a single pending send of that kind exists, and only when
    /// that send goes out after the row fired — yesterday's "order going
    /// out" row (long sent) matched by kind used to stop TODAY's order.
    func undoable(_ item: NotificationItem) -> PendingAction? {
        Self.undoTarget(for: item, among: notifications, pending: pendingActions,
                        answered: answered[item.id] != nil,
                        activeRestaurantId: SessionScope.activeRestaurantId)
    }

    nonisolated static func undoTarget(for item: NotificationItem, among rows: [NotificationItem],
                                       pending: [PendingAction], answered: Bool,
                                       activeRestaurantId: Int) -> PendingAction? {
        guard !answered, item.canUndo != false, let kind = item.undoableKind else { return nil }
        if let id = item.delayedActionId {
            if let match = pending.first(where: { $0.id == id && ($0.status ?? "pending") == "pending" }) {
                return match
            }
            // The server's rule already checked pending, this location and
            // the permission; the pending read may simply have failed.
            return item.canUndo == true
                ? PendingAction(id: id, kind: kind, label: nil, executeAt: nil, status: "pending") : nil
        }
        // A row with no restaurant is from a server that lists one location.
        let here = { (row: NotificationItem) in (row.restaurantId ?? activeRestaurantId) == activeRestaurantId }
        guard activeRestaurantId > 0, here(item) else { return nil }
        let newest = rows.filter { $0.type == item.type && here($0) }
            .max { ($0.firedAtDate ?? .distantPast) < ($1.firedAtDate ?? .distantPast) }
        guard newest?.id == item.id, let fired = item.firedAtDate else { return nil }
        let matches = pending.filter { $0.kind == kind }
        guard matches.count == 1, let only = matches.first,
              let goesOut = CavnarISODate.parse(only.executeAt), goesOut > fired else { return nil }
        return only
    }

    /// Approve in place only where the server says it may (`can_approve`)
    /// AND the draft came with the row to be read first — never a reply
    /// the owner hasn't seen (F3-10).
    func approvable(_ item: NotificationItem) -> Bool {
        guard answered[item.id] == nil, let id = item.reviewId,
              let draft = item.draft, !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return false
        }
        if let can = item.canApprove { return can }
        return approvableReviewIds.contains(id)
    }

    func undo(_ item: NotificationItem, action: PendingAction) async {
        busyRowId = item.id
        rowError = nil
        defer { busyRowId = nil }
        do {
            let r: APIClient.OKResponse = try await client.send(
                "/mobile/api/actions/\(action.id)/cancel", method: .post, body: [String: String]())
            if r.ok {
                answered[item.id] = "Undone"
                pendingActions.removeAll { $0.id == action.id }
                Haptic.success()
                // The Lock Screen countdown ends with it (F3-9).
                PendingSendActivities.finish(actionId: action.id, status: "stopped", note: nil)
            } else {
                rowError = (item.id, r.error ?? "That already went out, or was already undone.")
            }
        } catch let error as APIClient.APIError {
            rowError = (item.id, error.message)
        } catch {
            rowError = (item.id, "Couldn\u{2019}t reach Cavnar AI.")
        }
    }


    func approve(_ item: NotificationItem) async {
        guard let reviewId = item.reviewId else { return }
        busyRowId = item.id
        rowError = nil
        defer { busyRowId = nil }
        do {
            let r: ReviewPostOutcome = try await client.send("/mobile/api/reviews/\(reviewId)/approve", method: .post)
            if r.ok {
                approvableReviewIds.remove(reviewId)
                await markOpened(item)
                if let why = r.shortfall {
                    // Approved, not live — said, never shown as "Posted".
                    answered[item.id] = "Approved"
                    rowError = (item.id, why)
                } else {
                    answered[item.id] = r.posted ? "Posted" : "Approved"
                    Haptic.success()
                }
            } else {
                rowError = (item.id, r.error ?? "Couldn\u{2019}t approve \u{2014} open it to try again.")
            }
        } catch let error as APIClient.APIError {
            rowError = (item.id, error.message)
        } catch {
            rowError = (item.id, "Couldn\u{2019}t reach Cavnar AI.")
        }
    }
}

/// Drives the small dot on Home's bell icon — same shape as
/// ChangelogBadgeViewModel. Opening NotificationsListView marks alert_log
/// as seen server-side (GET /mobile/api/notifications stamps
/// notifications_seen_at), so refreshing this again right after the sheet
/// is dismissed is what actually clears the dot.
@Observable
@MainActor
final class NotificationsBadgeViewModel {
    var unreadCount = 0
    /// The urgent rows nobody has handled (P0/P1, unresolved) — the red
    /// count on the bell and the Home tab, as on the web bell.
    var urgentCount = 0

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct CountResponse: Decodable {
        let ok: Bool
        let count: Int
        let urgent: Int?
    }

    func refresh() async {
        // The same locations the list reads (scope=group), so the badge and
        // the list agree for an owner with several.
        if let response: CountResponse = try? await client.send("/mobile/api/notifications/unread-count",
                                                                query: ["scope": "group"]) {
            unreadCount = response.count
            urgentCount = response.urgent ?? 0
        }
    }
}

/// Pull-based recent-alerts history (alert_log) — distinct from the push
/// notifications APNs delivers; this is "what's happened lately," always
/// available even before push permission is granted.
///
/// Takes its view model from the caller (AppChrome owns one for the whole
/// session) rather than creating its own. The sheet opens the instant the
/// bell is tapped (friction audit #32) — it used to wait for the network
/// first — so a first open shows the skeleton below for the moment the
/// fetch takes; every later open shows the list it already has while it
/// refreshes.
struct NotificationsListView: View {
    let viewModel: NotificationsListViewModel
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    @Environment(\.dismiss) private var dismiss
    @State private var clock = CavnarEntranceClock()
    /// Twenty rows in one flat list is an audit log. A busy week is mostly
    /// things that have already been dealt with, and the two or three that
    /// still need someone are what an owner opened this for.
    /// nil until the owner picks: then "Needs you" whenever something is
    /// urgent, "Everything" otherwise (density #39) — the bell used to open
    /// on the chronological feed with the urgent items mixed into the FYIs.
    @State private var urgentChoice: Bool?

    /// Urgent AND not yet handled, as on the web bell — an answered review
    /// or a resolved issue no longer opens the list on "Needs you".
    private var hasUrgent: Bool { viewModel.notifications.contains(where: \.needsYou) }
    private var urgentOnly: Bool { Self.defaultsToUrgent(choice: urgentChoice, hasUrgent: hasUrgent) }

    static func defaultsToUrgent(choice: Bool?, hasUrgent: Bool) -> Bool {
        guard hasUrgent else { return false }
        return choice ?? true
    }

    /// "3 need you: 2 replies ready to post, 1 health mention" — the
    /// 3-second answer to "how many need me?", in the web bell's words
    /// (parity audit #7): what still needs someone (urgent and not yet
    /// handled), by kind; "Nothing needs you · 4 unread" otherwise. Nil for
    /// an empty list.
    static func summaryLine(_ items: [NotificationItem]) -> String? {
        guard !items.isEmpty else { return nil }
        let open = items.filter(\.needsYou)
        guard !open.isEmpty else {
            let unread = items.filter(\.isUnread).count
            return "Nothing needs you" + (unread > 0 ? " \u{00B7} \(unread) unread" : "")
        }
        var ready = 0, health = 0
        for n in open {
            if n.canApprove == true, !(n.draft ?? "").isEmpty { ready += 1 } else if n.type == "health" { health += 1 }
        }
        let other = open.count - ready - health
        var bits: [String] = []
        if ready > 0 { bits.append("\(ready) \(ready == 1 ? "reply" : "replies") ready to post") }
        if health > 0 { bits.append("\(health) health mention\(health == 1 ? "" : "s")") }
        if other > 0 { bits.append("\(other) \(bits.isEmpty ? "urgent" : "other") alert\(other == 1 ? "" : "s")") }
        return "\(open.count) need\(open.count == 1 ? "s" : "") you: " + bits.joined(separator: ", ")
    }

    private var shown: [NotificationItem] {
        urgentOnly ? viewModel.notifications.filter(\.needsYou) : viewModel.notifications
    }

    /// Day headings, newest first, preserving the server's ordering within
    /// each day.
    private var grouped: [(String, [NotificationItem])] {
        var order: [String] = []
        var byDay: [String: [NotificationItem]] = [:]
        for item in shown {
            if byDay[item.dayGroup] == nil { order.append(item.dayGroup) }
            byDay[item.dayGroup, default: []].append(item)
        }
        return order.map { ($0, byDay[$0] ?? []) }
    }

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.isLoading && viewModel.notifications.isEmpty {
                    // A skeleton, not a spinner — matches the loading
                    // language used everywhere else in the app (see
                    // CavnarSkeletonLines).
                    VStack(spacing: 14) {
                        ForEach(0..<4, id: \.self) { _ in
                            CavnarSkeletonLines(widths: [0.55, 0.85], lineHeight: 12, spacing: 8)
                        }
                    }
                    .padding(20)
                } else if let error = viewModel.errorMessage {
                    VStack(spacing: 12) {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        Button("Retry") { Task { await viewModel.load() } }
                    }
                    .padding(.top, 60)
                    .frame(maxWidth: .infinity)
                } else if viewModel.notifications.isEmpty {
                    CavnarEmptyHearth(
                        title: "No alerts yet",
                        message: "Low ratings, labor overages, and anything else worth knowing about will show up here."
                    )
                    .padding(.top, 40)
                } else {
                    List {
                        if let summary = Self.summaryLine(viewModel.notifications) {
                            HomeMixedText.make(summary, size: CavnarType.body, weight: 700,
                                               color: hasUrgent ? .cavnarInk : .cavnarInk2,
                                               numberColor: hasUrgent ? .cavnarRed : nil)
                                .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 8, trailing: 0))
                                .listRowBackground(Color.clear)
                                .listRowSeparator(.hidden)
                        }
                        if hasUrgent {
                            Picker("", selection: Binding(get: { urgentOnly },
                                                          set: { urgentChoice = $0 })) {
                                Text("Everything").tag(false)
                                Text("Needs you").tag(true)
                            }
                            .pickerStyle(.segmented)
                            .listRowInsets(EdgeInsets(top: 4, leading: 0, bottom: 10, trailing: 0))
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                        }
                        ForEach(grouped, id: \.0) { day, items in
                            Section {
                                ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                    row(item)
                                        .cavnarRowEntrance(index: index, clock: clock)
                                }
                            } header: {
                                Text(day)
                                    .font(.cavnarBody(13, weight: 700))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .textCase(nil)
                            }
                        }
                        // A row is read when it is opened; this reads the
                        // rest at once (the web bell's "Mark all read").
                        if viewModel.unreadCount > 0 {
                            Button {
                                Haptic.light()
                                Task { await viewModel.markAllRead() }
                            } label: {
                                Text("Mark all read")
                                    .font(.cavnarBody(14, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .frame(maxWidth: .infinity, minHeight: 44)
                            }
                            .buttonStyle(.plain)
                            .listRowBackground(Color.clear)
                            .listRowSeparator(.hidden)
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            // Without this, the Group (and the .background it carries)
            // only sizes itself to whichever branch's own intrinsic
            // content height — the loading skeleton and error states are
            // both just a short VStack, not something that naturally fills
            // the screen the way List does. That let the sheet's own
            // default background show through above and below a small
            // island of cavnarPaper wherever the skeleton/error content
            // happened to land — the "gray top and bottom, black band with
            // skeleton bars floating in the middle" look. Forcing the
            // Group itself to fill the available space means every branch
            // gets the same full-bleed background regardless of how little
            // content it has.
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            .cavnarEmberRefreshable { await viewModel.load() }
            // Module background, inline styled title, and the ember chevron
            // — the same chrome every Account sheet uses.
            .accountSheetChrome("Notifications")
        }
    }

    /// One notification: the tap opens its own place (the review, the
    /// queued send, the request — `nav`), and an actionable one carries its
    /// action on the row itself — Undo on a "going out" send, Approve on a
    /// reply that may be published unread — as a button and a swipe
    /// (friction audit #22). Answered rows say what happened.
    @ViewBuilder
    private func row(_ item: NotificationItem) -> some View {
        let undo = viewModel.undoable(item)
        let approve = viewModel.approvable(item)
        let busy = viewModel.busyRowId == item.id
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    // Opening a row is what reads it (the router records the
                    // open against its alert_log id), as on the web.
                    viewModel.noteOpened(item)
                    deepLinkRouter.handleNotificationTap(alertType: item.type, reviewId: item.reviewId,
                                                         alertId: item.alertId,
                                                         module: item.module, restaurantId: item.restaurantId,
                                                         nav: item.nav)
                    dismiss()
                } label: {
                    HStack(spacing: 10) {
                        // The ember is spent on P0/P1 only — the things that
                        // stop being worth anything once the shift they were
                        // about is over.
                        Circle()
                            .fill(item.needsYou ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.25))
                            .frame(width: 6, height: 6)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.label)
                                .font(.cavnarBody(14, weight: item.isUnread ? 700 : 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text(item.relativeFiredAt)
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer(minLength: 8)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)

                if let done = viewModel.answered[item.id] {
                    Text(done)
                        .font(.cavnarBody(13.5, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                } else if busy {
                    CavnarShimmerText(text: "Working…")
                } else if let undo {
                    rowAction("Undo", tint: .cavnarRed) { Task { await viewModel.undo(item, action: undo) } }
                } else if approve {
                    rowAction("Approve", tint: .cavnarEmber2) { Task { await viewModel.approve(item) } }
                } else {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.6))
                }
            }
            // The reply Approve would publish, in full, before it can be
            // approved here (F3-10).
            if approve, let draft = item.draft {
                Text(draft)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(10)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                    .padding(.leading, 16)
            }
            if let error = viewModel.rowError, error.id == item.id {
                Text(error.message)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        // Undo only — the safe direction. Publishing a reply is a tap on the
        // row's own Approve, under the draft it publishes; a full swipe used
        // to post a reply nobody had read (F3-10).
        .swipeActions(edge: .trailing, allowsFullSwipe: false) {
            if !busy, let undo {
                Button(role: .destructive) {
                    Task { await viewModel.undo(item, action: undo) }
                } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
            }
        }
    }

    private func rowAction(_ title: String, tint: Color, _ action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Text(title)
                .font(.cavnarBody(13.5, weight: 700))
                .foregroundStyle(tint)
                .padding(.horizontal, 12)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
