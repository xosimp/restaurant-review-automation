import SwiftUI

struct RootView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var deepLinkRouter = DeepLinkRouter()
    @State private var selectedTab: AppTab = .home
    @State private var showingAskCavnar = false

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
        .environment(deepLinkRouter)
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
            HomeView()
                .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
                .tag(AppTab.home)

            ModulesGridView()
                .tabItem { Label(AppTab.modules.title, systemImage: AppTab.modules.systemImage) }
                .tag(AppTab.modules)

            AccountView()
                .tabItem { Label(AppTab.account.title, systemImage: AppTab.account.systemImage) }
                .tag(AppTab.account)
        }
        .tint(Color.cavnarEmber)
        .task {
            PushManager.shared.requestAuthorizationAndRegister()
        }
        // .overlay (not a ZStack sibling) so the FAB actually receives taps —
        // a ZStack sibling next to TabView silently lost hit-testing to the
        // tab content underneath it.
        .overlay(alignment: .bottomTrailing) {
            AskCavnarFAB { showingAskCavnar = true }
                .padding(.trailing, 20)
                .padding(.bottom, 70)  // clears the tab bar
        }
        .sheet(isPresented: $showingAskCavnar) {
            AskCavnarView()
        }
    }
}

/// Persistent floating action button reachable from any tab — matches the
/// web dashboard's own Ask Cavnar bubble (a FAB there too, not a tab), and
/// frees a permanent tab slot as more modules ship (see the architecture plan).
private struct AskCavnarFAB: View {
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "sparkles")
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 56, height: 56)
                .background(Color.cavnarEmber)
                .clipShape(Circle())
                .shadow(color: .black.opacity(0.25), radius: 8, y: 4)
        }
        .buttonStyle(FABPressStyle())
    }
}

/// A separate simultaneousGesture(DragGesture(minimumDistance: 0)) used to
/// power the press-scale animation previously competed with the Button's own
/// tap recognition and could swallow the tap entirely. configuration.isPressed
/// gets the same visual feedback without a second gesture in the mix.
private struct FABPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .scaleEffect(configuration.isPressed ? 0.9 : 1)
            .animation(.easeOut(duration: 0.12), value: configuration.isPressed)
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
