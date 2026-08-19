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
    // Bound from RootView, not owned here — RootView.body swaps this
    // entire view out for LockedView (and back) across a Face ID
    // lock/unlock cycle, which tears down and recreates ModulesGridView
    // from scratch. A locally-owned @State path would reset to empty on
    // every unlock, silently popping the user back to the grid and
    // discarding whatever module screen (e.g. Labor, mid-viewing a
    // just-generated schedule) they'd pushed to — binding to a path RootView
    // itself owns means it survives that swap, so unlocking lands back on
    // the exact same pushed screen instead of the grid.
    @Binding var path: NavigationPath
    // See HomeView's identical navigate(to:)/navHapticTrigger/lastNavigationAt
    // for why tile taps go through a debounced helper instead of appending
    // to path directly from the Button's action closure.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast
    @Environment(DeepLinkRouter.self) private var deepLinkRouter

    // Static, not backend-driven — these aren't real, shipping features
    // gated by entitlement the way the modules above are, so every client
    // sees them regardless of what /mobile/api/home actually returns for
    // their account. Non-interactive (see ComingSoonModuleTile — no Button
    // wrapper at all), so there's nothing to route to on tap.
    private let comingSoonModules: [ModuleSummary] = [
        ModuleSummary(key: "waitlist", label: "Waitlist & Reservations", icon: "waitlist", status: "coming_soon", kpi: nil),
        ModuleSummary(key: "bar", label: "Bar & Alcohol", icon: "bar", status: "coming_soon", kpi: nil),
    ]

    var body: some View {
        NavigationStack(path: $path) {
            Group {
                // Loading/error only take priority while there's nothing
                // else to show yet — once the real fetch has settled
                // (success OR failure), the grid always renders, since
                // comingSoonModules alone guarantees it's never actually
                // empty (a client with zero active real modules used to
                // hit a blank screen here).
                if viewModel.isLoading && viewModel.modules.isEmpty && viewModel.errorMessage == nil {
                    // See AccountView's identical fix for why the frame
                    // matters, not just centering: .cavnarModuleBackground()
                    // sizes to this Group, and a bare ProgressView hugging
                    // its own tiny size made the wash flash as a narrow
                    // rectangle instead of full-screen.
                    ProgressView()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let error = viewModel.errorMessage, viewModel.modules.isEmpty {
                    VStack(spacing: 8) {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        Button("Retry") { Task { await viewModel.load() } }
                    }
                } else {
                    ScrollView {
                        // Same KPITile/HomeModuleGrid used on Home — one
                        // consistent "orange square" module tile style
                        // across both tabs instead of two different looks.
                        HomeModuleGrid(modules: viewModel.modules, comingSoon: comingSoonModules) { module in
                            navigate(to: ModuleRoute(key: module.key, label: module.label))
                        }
                        .padding(20)
                    }
                }
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            // Same ember-to-black wash every module screen (Reviews, Labor,
            // etc.) already has behind its tiles, instead of flat black —
            // this is the grid that leads into those screens, so it reads
            // as one continuous look rather than a plain tab standing apart.
            .cavnarModuleBackground()
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
