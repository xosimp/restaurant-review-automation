import SwiftUI

struct HomeView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = HomeViewModel()
    @State private var showingLocationSwitcher = false
    @State private var showingNotifications = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if let summary = viewModel.summary {
                        header(summary)
                        HomeModuleGrid(modules: summary.modules)
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
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            .navigationTitle("Home")
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        showingNotifications = true
                    } label: {
                        Image(systemName: "bell")
                    }
                }
                if sessionStore.currentUser?.isOwner == true {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button {
                            showingLocationSwitcher = true
                        } label: {
                            Image(systemName: "building.2")
                        }
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
                        NavigationLink {
                            ModuleDestinationView(
                                moduleKey: item.module,
                                moduleLabel: item.module.capitalized
                            )
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
}
