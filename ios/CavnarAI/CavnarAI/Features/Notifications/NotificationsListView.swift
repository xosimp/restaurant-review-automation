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
            let response: Response = try await client.send("/mobile/api/notifications")
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
        if notifications.contains(where: { $0.reviewId != nil }),
           let r: ReviewsResponse = try? await client.send(
               "/mobile/api/reviews", query: ["filter": "pending", "limit": "50", "offset": "0"],
               hapticOnError: false) {
            approvableReviewIds = Set(r.reviews.filter(ReviewsListViewModel.canQuickApprove).map(\.id))
        }
    }

    /// The one queued send a "going out" row can undo in place: only when a
    /// single pending send of its kind exists — with several, the row opens
    /// them instead of guessing which one it meant.
    func undoable(_ item: NotificationItem) -> PendingAction? {
        guard answered[item.id] == nil, let kind = item.undoableKind else { return nil }
        let matches = pendingActions.filter { $0.kind == kind }
        return matches.count == 1 ? matches[0] : nil
    }

    func approvable(_ item: NotificationItem) -> Bool {
        guard answered[item.id] == nil, let id = item.reviewId else { return false }
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
            } else {
                rowError = (item.id, r.error ?? "That already went out, or was already undone.")
            }
        } catch let error as APIClient.APIError {
            rowError = (item.id, error.message)
        } catch {
            rowError = (item.id, "Couldn\u{2019}t reach Cavnar AI.")
        }
    }

    private struct ApproveResponse: Decodable {
        let ok: Bool
        let autoPosted: Bool?
        let postError: String?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, error
            case autoPosted = "auto_posted"
            case postError = "post_error"
        }
    }

    func approve(_ item: NotificationItem) async {
        guard let reviewId = item.reviewId else { return }
        busyRowId = item.id
        rowError = nil
        defer { busyRowId = nil }
        do {
            let r: ApproveResponse = try await client.send("/mobile/api/reviews/\(reviewId)/approve", method: .post)
            if r.ok {
                approvableReviewIds.remove(reviewId)
                if let postError = r.postError {
                    answered[item.id] = "Approved"
                    rowError = (item.id, postError)
                } else {
                    answered[item.id] = r.autoPosted == true ? "Posted" : "Approved"
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

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct CountResponse: Decodable {
        let ok: Bool
        let count: Int
    }

    func refresh() async {
        if let response: CountResponse = try? await client.send("/mobile/api/notifications/unread-count") {
            unreadCount = response.count
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
    @State private var urgentOnly = false

    private var shown: [NotificationItem] {
        urgentOnly ? viewModel.notifications.filter(\.isUrgent) : viewModel.notifications
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
                        if viewModel.notifications.contains(where: \.isUrgent) {
                            Picker("", selection: $urgentOnly) {
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
                    deepLinkRouter.handleNotificationTap(alertType: item.type, reviewId: item.reviewId,
                                                         module: item.module, restaurantId: item.restaurantId,
                                                         nav: item.nav)
                    dismiss()
                } label: {
                    HStack(spacing: 10) {
                        // The ember is spent on P0/P1 only — the things that
                        // stop being worth anything once the shift they were
                        // about is over.
                        Circle()
                            .fill(item.isUrgent ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.25))
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
            if let error = viewModel.rowError, error.id == item.id {
                Text(error.message)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .swipeActions(edge: .trailing, allowsFullSwipe: true) {
            if !busy, let undo {
                Button(role: .destructive) {
                    Task { await viewModel.undo(item, action: undo) }
                } label: { Label("Undo", systemImage: "arrow.uturn.backward") }
            } else if !busy, approve {
                Button {
                    Task { await viewModel.approve(item) }
                } label: { Label("Approve", systemImage: "checkmark") }
                .tint(Color.cavnarGreen)
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
