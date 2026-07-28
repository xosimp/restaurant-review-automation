import SwiftUI

struct RootView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var deepLinkRouter = DeepLinkRouter()
    @State private var selectedTab: AppTab = .home

    var body: some View {
        Group {
            if !sessionStore.isAuthenticated {
                LoginView(sessionStore: sessionStore)
            } else if sessionStore.isLocked {
                LockedView()
            } else {
                mainTabs
            }
        }
        .onAppear {
            PushManager.shared.router = deepLinkRouter
        }
        .onChange(of: scenePhase) { _, newPhase in
            if newPhase == .background {
                sessionStore.lockIfNeeded()
            }
        }
        .onChange(of: deepLinkRouter.pendingTab) { _, tab in
            if let tab { selectedTab = tab }
        }
    }

    private var mainTabs: some View {
        TabView(selection: $selectedTab) {
            HomeView(onSelectTab: { selectedTab = $0 })
                .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
                .tag(AppTab.home)

            ReviewsListView(deepLinkReviewID: deepLinkRouter.pendingReviewID)
                .tabItem { Label(AppTab.reviews.title, systemImage: AppTab.reviews.systemImage) }
                .tag(AppTab.reviews)

            AskCavnarView()
                .tabItem { Label(AppTab.askCavnar.title, systemImage: AppTab.askCavnar.systemImage) }
                .tag(AppTab.askCavnar)

            FoodCostQuickEntryView()
                .tabItem { Label(AppTab.foodCost.title, systemImage: AppTab.foodCost.systemImage) }
                .tag(AppTab.foodCost)
        }
        .tint(Color.cavnarEmber)
        .task {
            PushManager.shared.requestAuthorizationAndRegister()
        }
    }
}

/// Face ID re-entry gate shown whenever the app returns to the foreground
/// with an active session — see SessionStore's doc comment for why iOS
/// sessions rely on this instead of the web's 8-hour inactivity timeout.
struct LockedView: View {
    @Environment(SessionStore.self) private var sessionStore

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(spacing: 20) {
                Image(systemName: "lock.fill")
                    .font(.system(size: 40))
                    .foregroundStyle(Color.cavnarEmber)
                Text("Cavnar AI")
                    .font(.cavnarHeadline(24))
                    .foregroundStyle(Color.cavnarInk)
                Button("Unlock") {
                    Task { await sessionStore.unlockWithBiometrics() }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .padding(.horizontal, 60)
            }
        }
        .task {
            _ = await sessionStore.unlockWithBiometrics()
        }
    }
}
