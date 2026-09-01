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
            } else {
                errorMessage = response.error ?? "Couldn't load notifications."
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load notifications."
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
/// Takes its view model from the caller (HomeView owns one for the whole
/// session) rather than creating its own. HomeView awaits the FIRST load
/// before ever presenting this sheet at all — see its bell button — so by
/// the time this view exists on screen, real content (or a genuine error/
/// empty state) is already sitting in the view model. The loading-skeleton
/// branch below is a fallback for the refresh-an-empty-inbox edge case,
/// not something a normal tap-to-open should ever actually show: no other
/// app flashes a skeleton for something this lightweight, it just shows the
/// list.
struct NotificationsListView: View {
    let viewModel: NotificationsListViewModel
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    @Environment(\.dismiss) private var dismiss

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
                    List(viewModel.notifications) { item in
                        Button {
                            Haptic.light()
                            deepLinkRouter.handleNotificationTap(alertType: item.type, reviewId: item.reviewId)
                            dismiss()
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(item.label)
                                        .font(.cavnarBody(14, weight: 600))
                                        .foregroundStyle(Color.cavnarInk)
                                    Text(item.relativeFiredAt)
                                        .font(.cavnarBody(11))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                                Spacer()
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 12, weight: .semibold))
                                    .foregroundStyle(Color.cavnarInk3.opacity(0.6))
                            }
                        }
                        .buttonStyle(.plain)
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
            .cavnarModuleBackground()
            .cavnarEmberRefreshable { await viewModel.load() }
            .navigationTitle("Notifications")
        }
    }
}
