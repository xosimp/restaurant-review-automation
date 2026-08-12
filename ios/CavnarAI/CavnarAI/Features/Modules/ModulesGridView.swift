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
    // See HomeView's identical navigate(to:)/navHapticTrigger/lastNavigationAt
    // for why tile taps go through a debounced helper instead of appending
    // to path directly from the Button's action closure.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast
    @Environment(DeepLinkRouter.self) private var deepLinkRouter

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                if !viewModel.modules.isEmpty {
                    ScrollView {
                        // Same KPITile/HomeModuleGrid used on Home — one
                        // consistent "orange square" module tile style
                        // across both tabs instead of two different looks.
                        HomeModuleGrid(modules: viewModel.modules) { module in
                            navigate(to: ModuleRoute(key: module.key, label: module.label))
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
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Modules")
            .task {
                await viewModel.load()
                pushToPendingModuleIfDeepLinked()
            }
            .onChange(of: deepLinkRouter.pendingModuleKey) { _, _ in pushToPendingModuleIfDeepLinked() }
        }
    }

    /// A tapped push notification or in-app Notifications row sets
    /// pendingModuleKey (see DeepLinkRouter) — push straight to that module
    /// screen, which then opens the specific review itself (if any) via its
    /// own DeepLinkRouter-reading logic (see ReviewsListView).
    private func pushToPendingModuleIfDeepLinked() {
        // Consumed (not just read) right here — unlike pendingReviewID,
        // nothing downstream needs pendingModuleKey after this point, so
        // leaving it set would push a duplicate ModuleRoute the next time
        // this view reappears or .task reruns.
        guard let moduleKey = deepLinkRouter.consumePendingModuleKey() else { return }
        let label = viewModel.modules.first(where: { $0.key == moduleKey })?.label ?? moduleKey.capitalized
        path.append(ModuleRoute(key: moduleKey, label: label))
    }

    // See HomeView.navigate(to:) for why this is debounced rather than
    // appending to path directly from the tile's action closure.
    private func navigate(to route: ModuleRoute) {
        let now = Date()
        guard now.timeIntervalSince(lastNavigationAt) > 0.35 else { return }
        lastNavigationAt = now
        navHapticTrigger += 1
        path.append(route)
    }
}
