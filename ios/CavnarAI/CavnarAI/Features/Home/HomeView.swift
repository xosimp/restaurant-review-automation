import SwiftUI

struct HomeView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = HomeViewModel()
    @State private var showingLocationSwitcher = false
    @State private var showingNotifications = false
    @State private var path = NavigationPath()
    // Ticked on every accepted tile/row tap instead of calling Haptic.light()
    // directly in the action closure, paired with .sensoryFeedback below.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let summary = viewModel.summary {
                        header(summary)
                        HomeModuleGrid(modules: summary.modules) { module in
                            navigate(to: ModuleRoute(key: module.key, label: module.label))
                        }
                        needsAttentionSection(summary)
                    } else if viewModel.isLoading {
                        ProgressView().padding(.top, 80)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 80)
                        .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Home")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        showingNotifications = true
                    } label: {
                        Image(systemName: "bell")
                    }
                    // .plain strips the default toolbar-button chrome that
                    // was stacking its own automatic tap feedback on top of
                    // our manual Haptic.light() above — same fix as the FAB
                    // and module tiles, which never had this problem because
                    // they already use a stripped-chrome button style.
                    .buttonStyle(.plain)
                }
                if sessionStore.currentUser?.isOwner == true {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Haptic.light()
                            showingLocationSwitcher = true
                        } label: {
                            Image(systemName: "building.2")
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
            .sheet(isPresented: $showingNotifications) {
                NotificationsListView()
            }
            .task { await viewModel.load() }
        }
    }

    @ViewBuilder
    private func header(_ summary: HomeSummary) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(summary.restaurantName)
                .font(.cavnarHeadline(24))
                .foregroundStyle(Color.cavnarInk)
            if let locationName = summary.locationName {
                Text(locationName)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    @ViewBuilder
    private func needsAttentionSection(_ summary: HomeSummary) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Needs attention")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            if summary.needsAttention.isEmpty {
                AllClearRow().cavnarCard()
            } else {
                VStack(spacing: 8) {
                    ForEach(summary.needsAttention) { item in
                        Button {
                            navigate(to: ModuleRoute(key: item.module, label: item.module.capitalized))
                        } label: {
                            NeedsAttentionRow(item: item)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .cavnarCard()
            }
        }
    }

    // A Button inside a ScrollView/LazyVGrid has to let the ScrollView's own
    // pan gesture "race" its tap gesture to tell a scroll from a tap — under
    // a fast swipe-off-one-tile-and-tap-another, that disambiguation can
    // resolve the FIRST tile's tap late, after the second tile's tap has
    // already gone through, landing a stale extra navigation (and haptic)
    // moments after the real one. Rather than trust the timing of Button's
    // action closure at all, ignore any tap that lands within 350ms of the
    // last one we accepted — short enough that no legitimate back-to-back
    // navigation reads as "the same gesture settling twice," long enough to
    // absorb the stale-resolution window this class of bug produces.
    private func navigate(to route: ModuleRoute) {
        let now = Date()
        guard now.timeIntervalSince(lastNavigationAt) > 0.35 else { return }
        lastNavigationAt = now
        navHapticTrigger += 1
        path.append(route)
    }
}
