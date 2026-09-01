import SwiftUI

struct RootView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var deepLinkRouter = DeepLinkRouter()
    @State private var selectedTab: AppTab = .home
    // Owned here rather than by HomeView/ModulesGridView themselves — see
    // ModulesGridView.path's doc comment for why: these need to survive
    // the LockedView swap in body below, which discards and recreates
    // mainTabs (and everything nested in it) on every Face ID lock/unlock.
    @State private var homePath = NavigationPath()
    @State private var modulesPath = NavigationPath()
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
        // Mobile's in-app interface is dark-only by design — what's
        // switchable is the home-screen APP ICON (Account > More), not
        // this. See AppIconManager for that.
        .preferredColorScheme(.dark)
        // Single app-wide source for tint — covers button/control tint AND
        // text field cursor color (a TextField's blinking caret follows the
        // environment's tint, not a color you set on the field itself).
        // Applied at the very root, above the Login/Locked/mainTabs branch,
        // so it's inherited by every screen and every .sheet presented from
        // any of them — previously only some screens set this locally
        // (MarketingView, AccountView, TwoFactorView), which is why cursors
        // elsewhere (like Ask Cavnar's compose field) still showed the
        // system's default blue.
        .tint(Color.cavnarEmber)
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
            HomeView(path: $homePath, heroAppeared: introAppeared, onHeroAppear: startIntroSequence)
                .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
                .tag(AppTab.home)

            ModulesGridView(path: $modulesPath)
                .tabItem { Label(AppTab.modules.title, systemImage: AppTab.modules.systemImage) }
                .tag(AppTab.modules)

            AccountView()
                .tabItem { Label(AppTab.account.title, systemImage: AppTab.account.systemImage) }
                .tag(AppTab.account)
        }
        .sensoryFeedback(.selection, trigger: selectedTab)
        // True-black tab bar chrome, distinct from the warm near-black
        // content background — mirrors the web dashboard's own two-tier
        // black system (pure #000 nav chrome vs #1a1714 content).
        .toolbarBackground(Color.cavnarChrome, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .task {
            PushManager.shared.requestAuthorizationAndRegister()
        }
        // Fallback only — the real trigger is HomeView's onHeroAppear
        // (fires the moment Home's data has actually loaded and the hero is
        // on screen). Without this, a failed/very slow Home load would
        // leave the FAB invisible for the rest of the session with no way
        // to reach Ask Cavnar at all.
        .task {
            try? await Task.sleep(nanoseconds: 4_000_000_000)
            startIntroSequence()
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

    /// Single entry point for the landing intro — called from HomeView's
    /// onHeroAppear (the real trigger: fires once Home's data has actually
    /// loaded and the hero is genuinely on screen) and from the fallback
    /// timeout .task above, whichever comes first. Guarded on
    /// SessionStore.hasShownHomeIntro (reset on logout, see
    /// SessionStore.clearLocalSession) so only the very first call this
    /// sign-in animates anything — a second call in the same session (the
    /// fallback firing after onHeroAppear already did, or a remount after
    /// the Face ID lock screen) just snaps everything straight to its
    /// settled state instead of replaying. This is also why the guard lives
    /// on the SessionStore flag rather than a RootView-local one: a local
    /// flag wouldn't reset on logout, and a client who signs out and back
    /// in should get the intro again.
    private func startIntroSequence() {
        guard !sessionStore.hasShownHomeIntro else {
            introAppeared = true
            fabIconSpun = true
            fabCollapsed = true
            return
        }
        sessionStore.hasShownHomeIntro = true
        Task { await playIntroSequence() }
    }

    /// Hero + FAB fade/rise in together (one withAnimation call drives
    /// both, so they're pixel-synced) → a short pop → the FAB's icon spins
    /// once → the FAB collapses down to an icon-only button, since a
    /// permanently full-width "Ask Cavnar AI" pill was sitting over tap
    /// targets on the rest of the screen. Only ever reached once per
    /// sign-in — see startIntroSequence()'s guard above.
    private func playIntroSequence() async {
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
/// session (see RootView.startIntroSequence), then pops, spins its
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

                    // Only the badge's outer star rotates (GlowBadge's own
                    // `rotation:` param) — the sparkles icon on top of it
                    // stays fixed, per the ask that just the orange star
                    // spin, not the whole badge including its icon.
                    GlowBadge(
                        systemImage: "sparkles", size: 30,
                        // Intro's one-time spin, then the ambient loop keeps
                        // turning from wherever that left off — both are
                        // full 360s so the handoff between them never jumps.
                        rotation: .degrees((iconSpun ? 360 : 0) + (ambientRotation ? 360 : 0))
                    )
                }
                if !collapsed {
                    Text("Ask Cavnar AI")
                        .font(.cavnarBody(14.5, weight: 700))
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
            //
            // Deliberately NOT using CavnarPremiumButtonSurface/the shared
            // button system — this is a one-of-one custom-animated control
            // (GlowBadge rotation, ambient halo pulse, expand/collapse),
            // not a generic button, and doesn't need to track the shared
            // style's changes.
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
///
/// Deliberately NOT the multi-second seal-draws-in/letters-stamp-in entrance
/// LoginView uses — Face ID fires the instant this appears (see .task below)
/// and can resolve in well under a second, tearing this whole view back down
/// before a ~2s one-shot animation ever finishes. That's why the seal draw-in
/// never visibly played on a real device: the screen was gone before it got
/// there. This uses CavnarLoadingSeal's continuous breathing loop instead —
/// it starts immediately and reads correctly no matter how long the screen
/// is actually up for, a few frames or several seconds. Simplified overall
/// (one mark, one wordmark, one button, generous vertical rhythm) — the
/// previous version stacked a separate SF Symbol lock glyph above a full
/// seal+wordmark lockup, two different "this is secured" cues competing for
/// the same read.
struct LockedView: View {
    @Environment(SessionStore.self) private var sessionStore

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(spacing: 32) {
                Spacer()
                VStack(spacing: 22) {
                    CavnarLoadingSeal(size: 84)
                    Image("BrandLockup")
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(width: 172)
                }
                Spacer()
                Button("Unlock") {
                    Task { await sessionStore.unlockWithBiometrics() }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .padding(.horizontal, 60)
                .padding(.bottom, 56)
            }
        }
        .task {
            _ = await sessionStore.unlockWithBiometrics()
        }
    }
}
