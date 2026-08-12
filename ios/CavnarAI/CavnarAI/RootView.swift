import SwiftUI

struct RootView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var deepLinkRouter = DeepLinkRouter()
    @State private var selectedTab: AppTab = .home
    @State private var showingAskCavnar = false
    // Single shared flip that drives BOTH Home's hero fade-in and the FAB's
    // — owned up here (not by HomeView, which lives in a separate subtree
    // from the FAB overlay) so one withAnimation call moves both at once
    // instead of two separately-timed onAppear checks that could drift out
    // of sync. The three that follow are the FAB's own post-fade stages.
    @State private var introAppeared = false
    @State private var fabPopped = false
    @State private var fabIconSpun = false
    @State private var fabCollapsed = false

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
        // Mobile is dark-only by design — the light/dark toggle stays a
        // desktop-only feature (see the web dashboard's theme switcher).
        .preferredColorScheme(.dark)
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

    // .sensoryFeedback(trigger:) instead of .onChange(of: selectedTab) {
    // Haptic.light() } — the onChange version produced inconsistent results
    // (silent on some tabs, doubled on others). .sensoryFeedback is Apple's
    // own trigger-driven, race/duplicate-resistant mechanism (see
    // CavnarPrimaryButtonStyle for the same fix applied to button presses),
    // and .selection (not .impact) matches the system's own tab/segmented-
    // control haptic convention for a discrete-choice change.
    private var mainTabs: some View {
        TabView(selection: $selectedTab) {
            HomeView(heroAppeared: introAppeared)
                .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
                .tag(AppTab.home)

            ModulesGridView()
                .tabItem { Label(AppTab.modules.title, systemImage: AppTab.modules.systemImage) }
                .tag(AppTab.modules)

            AccountView()
                .tabItem { Label(AppTab.account.title, systemImage: AppTab.account.systemImage) }
                .tag(AppTab.account)
        }
        .sensoryFeedback(.selection, trigger: selectedTab)
        .tint(Color.cavnarEmber)
        // True-black tab bar chrome, distinct from the warm near-black
        // content background — mirrors the web dashboard's own two-tier
        // black system (pure #000 nav chrome vs #1a1714 content).
        .toolbarBackground(Color.cavnarChrome, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .task {
            PushManager.shared.requestAuthorizationAndRegister()
        }
        .task {
            await playIntroSequenceIfNeeded()
        }
        // .overlay (not a ZStack sibling) so the FAB actually receives taps —
        // a ZStack sibling next to TabView silently lost hit-testing to the
        // tab content underneath it.
        .overlay(alignment: .bottomTrailing) {
            AskCavnarFAB(
                appeared: introAppeared,
                popped: fabPopped,
                iconSpun: fabIconSpun,
                collapsed: fabCollapsed
            ) {
                Haptic.light()
                showingAskCavnar = true
            }
                .padding(.trailing, 20)
                .padding(.bottom, 70)  // clears the tab bar
        }
        .sheet(isPresented: $showingAskCavnar) {
            AskCavnarView()
        }
    }

    /// Plays exactly once per sign-in — gated on SessionStore.hasShownHomeIntro
    /// (reset on logout, see SessionStore.clearLocalSession), read here
    /// rather than from inside HomeView or AskCavnarFAB themselves, since
    /// this is the one place both of those views' triggers come from.
    /// Sequence: hero + FAB fade/rise in together (same withAnimation call
    /// drives both, so they're pixel-synced) → a short pop → the FAB's icon
    /// spins once → the FAB collapses down to an icon-only button, since a
    /// permanently full-width "Ask Cavnar AI" pill was sitting over tap
    /// targets on the rest of the screen. Re-running this .task (e.g. after
    /// the Face ID lock screen dismisses) is safe: the guard below just
    /// snaps everything to its already-settled final state with no replay.
    private func playIntroSequenceIfNeeded() async {
        guard !sessionStore.hasShownHomeIntro else {
            introAppeared = true
            fabIconSpun = true
            fabCollapsed = true
            return
        }
        sessionStore.hasShownHomeIntro = true

        withAnimation(.easeOut(duration: 0.7).delay(0.15)) {
            introAppeared = true
        }
        try? await Task.sleep(nanoseconds: 1_300_000_000)

        withAnimation(.easeOut(duration: 0.32)) { fabPopped = true }
        withAnimation(.easeInOut(duration: 0.9)) { fabIconSpun = true }
        try? await Task.sleep(nanoseconds: 320_000_000)
        withAnimation(.spring(response: 0.5, dampingFraction: 0.55)) { fabPopped = false }
        try? await Task.sleep(nanoseconds: 650_000_000)

        withAnimation(.easeInOut(duration: 0.6)) {
            fabCollapsed = true
        }
    }
}

/// Persistent floating action button reachable from any tab — matches the
/// web dashboard's own Ask Cavnar bubble (a FAB there too, not a tab), and
/// frees a permanent tab slot as more modules ship (see the architecture plan).
/// Lands as a labeled glass pill on the client's first landing this
/// session (see RootView.playIntroSequenceIfNeeded), then pops, spins its
/// icon once, and collapses to an icon-only circle for the rest of the
/// session — the permanently-labeled pill was wide enough to sit over tap
/// targets on the screen behind it.
private struct AskCavnarFAB: View {
    var appeared: Bool
    var popped: Bool
    var iconSpun: Bool
    var collapsed: Bool
    var action: () -> Void

    // Ambient "alive" loop for the collapsed icon-only state — a slow
    // continuous spin plus a breathing glow pulse, so it reads as an always-
    // listening presence instead of a static leftover icon. Self-contained
    // here rather than driven from RootView since it's purely cosmetic and
    // has no one-time/session-scoped requirement the way the intro does.
    @State private var ambientRotation = false
    @State private var ambientGlow = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: collapsed ? 0 : 8) {
                ZStack {
                    // Soft halo behind the icon, pulsing — only visible once
                    // collapsed (opacity rides the same pulse either way,
                    // but it's negligible while the material pill is still
                    // covering it).
                    Circle()
                        .fill(Color.cavnarEmber)
                        .frame(width: 30, height: 30)
                        .blur(radius: 10)
                        .opacity(collapsed ? (ambientGlow ? 0.55 : 0.2) : 0)

                    GlowBadge(systemImage: "sparkles", size: 30)
                        // Intro's one-time spin, then the ambient loop keeps
                        // turning from wherever that left off — both are
                        // full 360s so the handoff between them never jumps.
                        .rotationEffect(.degrees((iconSpun ? 360 : 0) + (ambientRotation ? 360 : 0)))
                }
                if !collapsed {
                    Text("Ask Cavnar AI")
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize()
                        .transition(.opacity.combined(with: .scale(scale: 0.01, anchor: .leading)))
                }
            }
            .padding(.leading, 6)
            .padding(.trailing, collapsed ? 6 : 14)
            .padding(.vertical, 6)
            // Faded out (not removed) as `collapsed` flips, in the same
            // container as the text above — keeps the pill's shrink and the
            // chrome's fade as one continuous collapse instead of a jump
            // cut, ending with nothing left behind the icon but its own
            // built-in glow, per the "just the icon itself" ask.
            .background(Capsule().fill(.ultraThinMaterial).opacity(collapsed ? 0 : 1))
            .overlay(
                Capsule()
                    .strokeBorder(Color.cavnarEmber.opacity(0.35), lineWidth: 1)
                    .opacity(collapsed ? 0 : 1)
            )
            .shadow(color: .black.opacity(collapsed ? 0 : 0.25), radius: 8, y: 4)
        }
        .buttonStyle(FABPressStyle())
        // Same opacity/offset curve as HomeView's hero, driven by the same
        // `introAppeared` flip from RootView, so the two reveal in lockstep.
        .opacity(appeared ? 1 : 0)
        .offset(y: appeared ? 0 : 26)
        .scaleEffect(popped ? 1.15 : 1.0)
        .task(id: collapsed) {
            guard collapsed else { return }
            withAnimation(.linear(duration: 16).repeatForever(autoreverses: false)) {
                ambientRotation = true
            }
            withAnimation(.easeInOut(duration: 2.2).repeatForever(autoreverses: true)) {
                ambientGlow = true
            }
        }
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
