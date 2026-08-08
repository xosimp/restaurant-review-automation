import SwiftUI
import Observation

@Observable
@MainActor
final class ModulesGridViewModel {
    var modules: [ModuleSummary] = []
    var isLoading = false
    var errorMessage: String?

    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    func load() async {
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let summary: HomeSummary = try await client.send("/mobile/api/home")
            modules = summary.modules
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn't load your modules."
        }
    }
}


/// Every module the client is entitled to, one tile each — scales to any
/// count with no per-module-count layout code (1 module = 1 tile, 6 = a
/// 2-3 column grid). This is where Reviews/Food Cost/Labor/Marketing/Intel
/// now live instead of as separate tabs.
struct ModulesGridView: View {
    @State private var viewModel = ModulesGridViewModel()
    @State private var path = NavigationPath()
    @Environment(DeepLinkRouter.self) private var deepLinkRouter

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if !viewModel.modules.isEmpty {
                    ScrollView {
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 12)], spacing: 12) {
                            ForEach(viewModel.modules) { module in
                                // A Button driving the path directly, not a
                                // NavigationLink — the haptic fires from a
                                // deterministic action closure instead of a
                                // simultaneousGesture racing NavigationLink's
                                // own tap handling (that combo was the
                                // source of reported delayed/duplicate
                                // haptics on rapid back-and-forth taps).
                                Button {
                                    Haptic.light()
                                    path.append(ModuleRoute(key: module.key, label: module.label))
                                } label: {
                                    ModuleTile(module: module)
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        .padding(20)
                    }
                } else if viewModel.isLoading {
                    ProgressView()
                } else if let error = viewModel.errorMessage {
                    VStack(spacing: 8) {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        Button("Retry") { Task { await viewModel.load() } }
                    }
                }
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Modules")
            .task {
                await viewModel.load()
                pushToReviewsIfDeepLinked()
            }
            .onChange(of: deepLinkRouter.pendingReviewID) { _, _ in pushToReviewsIfDeepLinked() }
        }
    }

    /// A tapped push notification always points at a review (v1's only push
    /// categories are review-related) — push straight to the Reviews module
    /// screen, which then opens the specific review itself via its own
    /// DeepLinkRouter-reading logic (see ReviewsListView).
    private func pushToReviewsIfDeepLinked() {
        guard deepLinkRouter.pendingReviewID != nil else { return }
        path.append(ModuleRoute(key: "reviews", label: "Reviews"))
    }
}

private struct ModuleTile: View {
    let module: ModuleSummary

    var body: some View {
        VStack(spacing: 10) {
            GlowBadge(systemImage: ModuleIcon.symbolName(for: module.icon), size: 48)
            Text(module.label)
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            if !module.isAvailable {
                Text("Coming soon")
                    .font(.cavnarBody(9, weight: 700))
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
        .cavnarCard()
    }
}
