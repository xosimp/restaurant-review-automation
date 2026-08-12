import SwiftUI
import Observation

@Observable
@MainActor
final class NotificationsListViewModel {
    var notifications: [NotificationItem] = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private struct Response: Decodable {
        let ok: Bool
        let notifications: [NotificationItem]
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let response: Response = try await client.send("/mobile/api/notifications")
            notifications = response.notifications
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load notifications."
        }
    }
}

/// Pull-based recent-alerts history (alert_log) — distinct from the push
/// notifications APNs delivers; this is "what's happened lately," always
/// available even before push permission is granted.
struct NotificationsListView: View {
    @State private var viewModel = NotificationsListViewModel()

    var body: some View {
        NavigationStack {
            Group {
                if viewModel.notifications.isEmpty && !viewModel.isLoading {
                    ContentUnavailableView("No alerts yet", systemImage: "bell")
                } else {
                    List(viewModel.notifications) { item in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(item.label)
                                .font(.cavnarBody(14, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text(item.firedAt)
                                .font(.cavnarBody(11))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                }
            }
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Notifications")
            .task { await viewModel.load() }
        }
        .cavnarSheetTopRim()
    }
}
