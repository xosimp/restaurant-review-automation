import SwiftUI
import UIKit
import LocalAuthentication

struct RootView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var deepLinkRouter = DeepLinkRouter()
    @State private var network = NetworkMonitor()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var selectedTab: AppTab = .home
    // Cold-launch handoff. iOS draws the launch screen itself (UILaunchScreen
    // in project.yml: the 128pt LaunchSeal on Paper) and it can't animate —
    // so the app's own first frame redraws that exact seal in that exact
    // spot, breathes the ember, and fades out over whatever screen is
    // underneath. Only ever true on process start: RootView persists across
    // foreground returns, so the Face ID gate never sees this.
    @State private var showLaunchSplash = true
    // Home's landing intro asked to start while the splash was still
    // covering it — replayed the moment the splash lifts instead of being
    // wasted underneath it.
    @State private var introWaitingOnSplash = false
    // True from process start until the first unlock/sign-in — the one
    // window where the wordmark gets traced and filled in by the ember
    // (CavnarWordmarkTraceIn) instead of stamping in. A later re-lock
    // from the background is the same session, and gets the stamp-in.
    @State private var coldLaunchIntroPending = true
    // Owned here rather than by HomeView/ModulesGridView themselves — see
    // ModulesGridView.path's doc comment for why: these need to survive
    // the LockedView swap in body below, which discards and recreates
    // mainTabs (and everything nested in it) on every Face ID lock/unlock.
    @State private var homePath = NavigationPath()
    @State private var modulesPath = NavigationPath()
    // Same reason as the paths above: owned here so Home's already-loaded
    // summary survives the LockedView swap. When HomeView owned this, every
    // unlock recreated it empty, and Home flashed its loading seal (top of
    // the page, before the ScrollView had its width — hence "a big C in the
    // top-left") for the reload before the hero came back.
    @State private var homeViewModel = HomeViewModel()
    @State private var showingAskCavnar = false
    // Owned here, not inside the sheet — a sheet-owned @State view model is
    // destroyed on every dismissal, wiping the whole conversation (audit 5.6).
    // Same rationale as homeViewModel/homePath above.
    @State private var askCavnarViewModel = AskCavnarViewModel()
    // Raised at .inactive, BEFORE iOS takes the app-switcher snapshot, so the
    // thumbnail shows the seal instead of the live dashboard (audit 1.6).
    @State private var privacyShieldUp = false
    // Single shared flip that drives BOTH Home's hero fade-in and the FAB's
    // — owned up here (not by HomeView, which lives in a separate subtree
    // from the FAB overlay) so one withAnimation call moves both at once
    // instead of two separately-timed onAppear checks that could drift out
    // of sync. The three that follow are the FAB's own post-fade stages.
    @State private var introAppeared = false
    @State private var fabPopped = false
    @State private var fabIconSpun = false
    @State private var fabCollapsed = false
    // See SessionStore.pendingPasscodeSetup.
    @State private var showingPasscodeSetup = false

    var body: some View {
        Group {
            if !sessionStore.isAuthenticated {
                LoginView(sessionStore: sessionStore, introReady: !showLaunchSplash, coldLaunch: coldLaunchIntroPending)
                    .transition(.opacity)
            } else if sessionStore.isLocked {
                // introReady: on a cold launch this mounts UNDER the splash;
                // without the gate its draw-in played hidden and the user
                // only ever saw the settled end state once the splash lifted.
                LockedView(introReady: !showLaunchSplash, coldLaunch: coldLaunchIntroPending)
                    // Fetch Home's summary while the gate is up (the
                    // session is signed in, just locked), so the moment the
                    // user unlocks, Home mounts straight onto its hero — no
                    // loading state at all. Without this, every cold-launch
                    // unlock landed on an empty Home whose loading seal
                    // flashed for the length of the fetch.
                    .task {
                        if homeViewModel.summary == nil { await homeViewModel.load() }
                    }
            } else {
                mainTabs
                    .transition(.opacity)
            }
        }
        // Home's own hero/FAB already fade their CONTENT in (see
        // playIntroSequence below) — this covers the screen SWAP itself,
        // which was a hard, unanimated cut straight from the login screen
        // to the fully-built tab bar + nav chrome underneath that content,
        // reading as "it just appears" a beat before the hero's own fade
        // even started. Scoped to isAuthenticated only — the Face ID
        // lock/unlock swap (isLocked) stays an instant cut deliberately,
        // since that's a frequent, security-relevant action where snappy
        // reads as trustworthy and a fade would just feel like lag.
        .animation(.easeOut(duration: 0.35), value: sessionStore.isAuthenticated)
        .environment(deepLinkRouter)
        .environment(network)
        // Mobile's in-app interface is dark-only by design — what's
        // switchable is the home-screen APP ICON (Account > More), not
        // this. See AppIconManager for that.
        .preferredColorScheme(.dark)
        // Dynamic Type is honoured (see Font+Cavnar), capped at xxxLarge —
        // the top of the "standard" range, one notch short of the
        // "accessibility" categories (accessibility1...5). Those use a much
        // steeper multiplier specifically meant for low-vision users, and
        // ~400 call sites across this app were never individually stress-
        // tested against jumps that large: this session's first cap
        // (accessibility2) let short, bold, uppercase-tracked labels like
        // Account's section kickers balloon disproportionately (small text
        // styles scale more steeply than large ones by Apple's own design)
        // while dense KPI/chart screens broke outright. xxxLarge still gives
        // real, meaningful growth over the old frozen-at-any-setting
        // behavior (audit 7.1's actual defect) without reaching into the
        // range this app hasn't been laid out for yet.
        .dynamicTypeSize(...DynamicTypeSize.xxxLarge)
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
        .overlay {
            if privacyShieldUp {
                ZStack {
                    Color.cavnarPaper.ignoresSafeArea()
                    CavnarSealMark(size: 64)
                }
                .transition(.opacity)
            }
        }
        .overlay(alignment: .top) {
            if !network.isOnline {
                Text("Offline — showing your last update")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Color.cavnarAmber.opacity(0.92), in: Capsule())
                    .padding(.top, 8)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(.easeOut(duration: 0.25), value: network.isOnline)
        #if DEBUG
        // Debug-only tripwire for the exact failure that once took a whole
        // live-debugging session to trace: CAVNAR_API_BASE_URL falling back
        // to the unsubstituted "${CAVNAR_DEV_API_BASE_URL}" placeholder
        // (because `xcodegen generate` ran in a shell that hadn't sourced
        // the export) silently drops every request to unreachable
        // localhost, and every screen just shows a generic "connection
        // dropped" error with nothing pointing at the real cause. This
        // can't be missed on screen the way AppEnvironment's console NSLog
        // can be.
        .overlay(alignment: .top) {
            if AppEnvironment.baseURLOverrideIsUnsubstitutedPlaceholder {
                Text("DEV BUILD: API base URL not configured — see Xcode console")
                    .font(.cavnarBody(12, weight: 700))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Color.cavnarRed, in: Capsule())
                    .padding(.top, 8)
            }
        }
        #endif
        .overlay {
            if showLaunchSplash {
                LaunchSplashView {
                    withAnimation(.easeOut(duration: 0.45)) { showLaunchSplash = false }
                    if introWaitingOnSplash {
                        introWaitingOnSplash = false
                        startIntroSequence()
                    }
                }
                .transition(.opacity)
            }
        }
        .onAppear {
            PushManager.shared.router = deepLinkRouter
        }
        .onChange(of: sessionStore.isLocked) { _, locked in
            if !locked, sessionStore.pendingPasscodeSetup {
                sessionStore.pendingPasscodeSetup = false
                showingPasscodeSetup = true
            }
        }
        .sheet(isPresented: $showingPasscodeSetup) {
            AppPasscodeSheet(mode: .create)
        }
        #if DEBUG
        // Debug-only, opt-in auto-login for UI-automation/screenshot
        // verification — a no-op unless BOTH this is a Debug build AND the
        // launching process explicitly set these two env vars, so it can
        // never fire in a TestFlight/release build or an ordinary debug
        // run. Reuses the real login() path (not a fabricated token), so
        // it exercises the exact same code a real sign-in does.
        .task {
            guard !sessionStore.isAuthenticated,
                  let user = ProcessInfo.processInfo.environment["CAVNAR_DEBUG_AUTOLOGIN_USER"],
                  let pass = ProcessInfo.processInfo.environment["CAVNAR_DEBUG_AUTOLOGIN_PASS"] else { return }
            _ = try? await sessionStore.login(username: user, password: pass)
            if ProcessInfo.processInfo.environment["CAVNAR_DEBUG_OPEN_SHEET"] != nil {
                // The onChange(of: sessionStore.isAuthenticated) reset to
                // .home (added to fix the real sign-out/sign-in bug) fires
                // asynchronously off the same login() call and can land
                // after this line, clobbering it back to .home. A beat's
                // delay lets that settle first so this debug override
                // actually sticks.
                try? await Task.sleep(for: .milliseconds(100))
                selectedTab = .account
            }
        }
        #endif
        .onChange(of: scenePhase) { _, newPhase in
            switch newPhase {
            case .inactive:
                // iOS captures the app-switcher thumbnail during the
                // .inactive -> .background transition, so locking only on
                // .background meant revenue figures and review content sat in
                // the switcher card with no authentication (audit 1.6).
                //
                // isAuthenticated alone was wrong: it's true for the entire
                // time the user is sitting on LockedView too, not just once
                // they're actually in the dashboard. Two routine things also
                // flip scenePhase to .inactive and were incorrectly raising
                // this shield as a result — a plain cold launch (iOS always
                // routes app startup through a brief .inactive tick before
                // .active) and presenting the SYSTEM Face ID sheet itself
                // (any system UI on top of the app does this). Both left the
                // user staring at this black seal screen until content
                // finished loading, on every login and every unlock — a real
                // regression this fix introduced. LockedView is already a
                // safe, non-sensitive screen, so it needs no extra shield;
                // only raise this while the actual dashboard is what would be
                // captured.
                if sessionStore.isAuthenticated && !sessionStore.isLocked {
                    privacyShieldUp = true
                }
            case .background:
                sessionStore.lockIfNeeded()
            case .active:
                privacyShieldUp = false
                // Foreground is a reconnect opportunity for anything queued
                // while offline, and for a push token that failed to register.
                Task {
                    await PendingWriteQueue.shared.drain()
                    await PushManager.shared.flushPendingToken()
                }
            @unknown default:
                break
            }
        }
        // The cold-launch trace-in is spent once the user is through the
        // gate — by unlocking, or by signing in on a fresh install.
        .onChange(of: sessionStore.isLocked) { _, locked in
            if !locked { coldLaunchIntroPending = false }
        }
        .onChange(of: sessionStore.isAuthenticated) { _, authenticated in
            if authenticated {
                coldLaunchIntroPending = false
                // selectedTab is @State on RootView, which is never recreated
                // across sign-out/sign-in (only isAuthenticated flips) — so it
                // was remembering whatever tab was active when the user
                // signed out. Signing out from Account and back in landed
                // straight back on Account instead of Home. A fresh sign-in
                // should always start on Home.
                selectedTab = .home
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
            HomeView(viewModel: homeViewModel, path: $homePath, heroAppeared: introAppeared, onHeroAppear: startIntroSequence)
                .tabItem { Label(AppTab.home.title, systemImage: AppTab.home.systemImage) }
                .tag(AppTab.home)

            // Seeded with the modules Home already fetched, so the tab's
            // first open renders the grid instead of a loading seal.
            ModulesGridView(path: $modulesPath, initialModules: homeViewModel.summary?.modules ?? [])
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
                .accessibilityLabel("Ask Cavnar AI")
                .accessibilityHint("Opens a chat with your restaurant intelligence consultant")
        }
        .sheet(isPresented: $showingAskCavnar) {
            AskCavnarView(viewModel: askCavnarViewModel)
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
        // Don't burn the one-time landing reveal while the launch splash is
        // still sitting on top of it — the splash's onFinished replays this.
        guard !showLaunchSplash else {
            introWaitingOnSplash = true
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
/// session (see RootView.startIntroSequence), then pops, runs its halo
/// once, and collapses to the icon-only tile for the rest of the
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var ambientRotation = false
    @State private var ambientGlow = false

    private var pill: RoundedRectangle { RoundedRectangle(cornerRadius: 15, style: .continuous) }

    var body: some View {
        Button(action: action) {
            HStack(spacing: collapsed ? 0 : 8) {
                ZStack {
                    // Soft halo behind the icon, pulsing — only visible once
                    // collapsed (opacity rides the same pulse either way,
                    // but it's negligible while the material pill is still
                    // covering it).
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
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
                        // Only the thin ember halo turns (see GlowBadge);
                        // the tile and its sparkle stay still.
                        rotation: .degrees((iconSpun ? 360 : 0) + (ambientRotation ? 360 : 0)),
                        halo: true
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
            //
            // The pill is a rounded rectangle concentric with the tile
            // (tile radius 9 + 6pt padding = 15), not a Capsule — a capsule
            // shrinking around a rounded-square badge ended the collapse as
            // a circle with a square inside it, two shapes fighting. Now
            // the pill, the badge, and its halo are one family and the
            // collapse ends on the badge's own outline.
            .background(pill.fill(.ultraThinMaterial).opacity(collapsed ? 0 : 1))
            .overlay(
                pill
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
            // Reduce Motion settles this straight to its resting state rather
            // than looping forever — required for accessibility, and it also
            // stops a decorative loop keeping the GPU awake on a phone parked
            // on a pass counter all service (audit 3.6 / 7.6).
            guard !reduceMotion else {
                ambientRotation = false
                ambientGlow = false
                return
            }
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

/// The app's own first frame after the static launch image. iOS draws the
/// launch screen itself and it can't animate, so it now shows only a faint
/// ghost of the ring (LaunchGhost, 128pt, centered on Paper) — this view
/// draws the real ring in over that exact spot, then the ember pops in and
/// flares once, then the whole thing fades out over whatever screen is
/// underneath. The time BEFORE this appears (the static ghost) is the
/// process launching — nothing in the app runs yet, and a debug build with
/// Xcode attached spends several seconds there that a release build doesn't.
private struct LaunchSplashView: View {
    var onFinished: () -> Void

    @State private var ringProgress: CGFloat = 0
    @State private var emberOn = false
    @State private var flare: CGFloat = 0

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            ZStack {
                CavnarSealRingShape()
                    .trim(from: 0, to: ringProgress)
                    .stroke(Color.cavnarInk, style: StrokeStyle(lineWidth: 128 * (19.0 / 120.0), lineCap: .butt))
                    .frame(width: 128, height: 128)
                // Just the ember (ring hidden) — sits in the gap of the ring
                // drawn above, flares via the mark's own emberIntensity.
                CavnarSealMark(size: 128, ringOpacity: 0, emberIntensity: flare)
                    .opacity(emberOn ? 1 : 0)
            }
            .frame(width: 128, height: 128)
        }
        .task {
            withAnimation(.easeInOut(duration: 0.9)) { ringProgress = 1 }
            try? await Task.sleep(for: .seconds(0.85))
            withAnimation(.easeOut(duration: 0.3)) { emberOn = true }
            try? await Task.sleep(for: .seconds(0.25))
            withAnimation(.easeInOut(duration: 0.4)) { flare = 1 }
            try? await Task.sleep(for: .seconds(0.4))
            withAnimation(.easeInOut(duration: 0.45)) { flare = 0 }
            try? await Task.sleep(for: .seconds(0.45))
            onFinished()
        }
    }
}

/// Face ID re-entry gate shown whenever the app returns to the foreground
/// with an active session — see SessionStore's doc comment for why iOS
/// sessions rely on this instead of the web's 8-hour inactivity timeout.
///
/// Biometrics are NOT fired automatically on appear anymore — the owner
/// taps Unlock. That's what lets this screen actually play its entrance
/// (the same seal-draws-in / letters-stamp-in the login screen uses, over
/// the Home hero's own moving ember aurora) instead of Face ID resolving
/// in under a second and tearing the view down mid-animation, which is
/// why the previous auto-firing version never visibly animated at all.
/// The copy, the button glyph, and the caption all name the device's real
/// biometry (Face ID vs Touch ID) rather than assuming.
struct LockedView: View {
    @Environment(SessionStore.self) private var sessionStore
    // False while RootView's launch splash is still covering this on a cold
    // launch — the wordmark isn't mounted (so its stamp-in doesn't start)
    // and the stagger below waits, so nothing plays hidden.
    var introReady: Bool = true
    // Cold launch: the wordmark is traced and filled in by the ember. A
    // warm re-lock (same session) gets the quicker stamp-in.
    var coldLaunch: Bool = false
    @State private var isUnlocking = false
    @State private var unlockFailed = false
    // Staggered reveal after the lockup has drawn itself in: 1 headline,
    // 2 caption, 3 the unlock surface, 4 the line beneath it.
    @State private var stage = 0
    @State private var passcode = ""
    @State private var passcodeError = false
    @State private var passcodeMessage: String?
    @State private var lockoutRemaining = 0

    private var biometry: (name: String, symbol: String) {
        let context = LAContext()
        _ = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        switch context.biometryType {
        case .faceID: return ("Face ID", "faceid")
        case .touchID: return ("Touch ID", "touchid")
        default: return ("your passcode", "lock.fill")
        }
    }

    private var biometricAvailable: Bool {
        sessionStore.biometricLockEnabled && !sessionStore.biometricsUnavailable
    }
    private var hasPasscode: Bool { sessionStore.appPasscodeSet }

    // One screen, both ways in — like the phone's own lock screen. With a
    // passcode on file the pad is always up and Face ID is the key in its
    // bottom-left slot; without one, the Face ID button plus a prompt to
    // add a passcode. The top half (wordmark, headline) never moves
    // between the two, so the screen reads the same either way.
    private var caption: String {
        if let passcodeMessage { return passcodeMessage }
        if unlockFailed {
            return hasPasscode ? "Couldn't verify you — use your passcode instead." : "Couldn't verify you — try again."
        }
        switch (biometricAvailable, hasPasscode) {
        case (true, true): return "Unlock with \(biometry.name) or your passcode."
        case (false, true): return "Enter your app passcode to pick up where you left off."
        default: return "Unlock with \(biometry.name) to pick up right where you left off."
        }
    }
    private var captionIsError: Bool { passcodeMessage != nil || unlockFailed }

    var body: some View {
        let biometry = biometry
        ZStack(alignment: .top) {
            // Exactly the sign-in screen's background — same aurora, same
            // constellation, same vignette — so locking and signing in read
            // as the same place. (Was Home's hero band at 0.8, which looked
            // like a third variant of the brand backdrop.)
            LoginBackground()

            VStack(spacing: 0) {
                // Two equal Spacers — one above the wordmark, one between it
                // and the headline — so the wordmark sits centered in
                // whatever room is left above the unlock surface. With the
                // pad up that lands it near the top; with just the Face ID
                // button it settles around the upper third, which is where
                // banking-app lock screens put their mark. The unlock
                // controls stay anchored at the bottom either way: that's
                // the thumb zone, and it's the convention (Apple's own lock
                // screen, Chase, Revolut) — the gap above them is by design.
                Spacer(minLength: 40)

                // Wordmark only — no seal beside it here (it read as a
                // stray "C" off to the side of the word). Placeholder keeps
                // the layout stable until the splash lifts and it mounts.
                // aiTagOverhangs: the six letters are what's centered above
                // "Welcome back"; the small AI tag hangs off to the right.
                Group {
                    if introReady && coldLaunch {
                        CavnarWordmarkTraceIn(width: 300, aiTagOverhangs: true)
                    } else if introReady {
                        CavnarWordmarkStampIn(width: 300, aiTagOverhangs: true)
                    } else {
                        Color.clear.frame(width: 300, height: 300 * (100 / CavnarWordmarkLetterShape.boxWidth))
                    }
                }

                // The headline sits with the unlock surface, not with the
                // wordmark.
                Spacer(minLength: 28)

                VStack(spacing: 12) {
                    Text("Welcome back")
                        .font(.cavnarHeadline(28))
                        .foregroundStyle(Color.cavnarInk)
                        .lockReveal(stage >= 1)
                    Text(caption)
                        .font(.cavnarBody(16, weight: captionIsError ? 600 : 400))
                        .foregroundStyle(captionIsError ? Color.cavnarRed : Color.cavnarInk3)
                        .multilineTextAlignment(.center)
                        .lineSpacing(3)
                        .lockReveal(stage >= 2)
                }
                .padding(.horizontal, 40)
                .padding(.bottom, 30)

                if hasPasscode {
                    passcodeBlock(biometry)
                } else {
                    biometricBlock(biometry)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .animation(.easeOut(duration: 0.3), value: unlockFailed)
        .animation(.easeOut(duration: 0.2), value: passcodeMessage)
        .onAppear { if hasPasscode { refreshLockout() } }
        .task(id: lockoutRemaining > 0) {
            // Live countdown while a lockout is active.
            while lockoutRemaining > 0, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(1))
                refreshLockout()
            }
        }
        .task(id: introReady) {
            guard introReady, stage == 0 else { return }
            // Let the wordmark finish arriving first — traced and filled
            // (cold launch) or stamped in — then bring the rest up in order.
            try? await Task.sleep(for: .seconds(coldLaunch ? CavnarWordmarkTraceIn.duration : CavnarWordmarkStampIn.duration))
            for step in 1...4 {
                withAnimation(.easeOut(duration: 0.45)) { stage = step }
                try? await Task.sleep(for: .seconds(0.14))
            }
        }
    }

    // MARK: - Passcode on file: the pad (Face ID lives in its corner key)

    private func passcodeBlock(_ biometry: (name: String, symbol: String)) -> some View {
        VStack(spacing: 24) {
            CavnarPasscodePad(
                code: $passcode,
                isError: passcodeError,
                isVerifying: isUnlocking,
                disabled: lockoutRemaining > 0,
                biometrySymbol: biometricAvailable ? biometry.symbol : nil,
                onBiometry: biometricAvailable ? { Task { await unlock() } } : nil
            ) { entered in
                submitPasscode(entered)
            }
            .lockReveal(stage >= 3)

            // The escape hatch for a forgotten passcode: signing out clears
            // it (see SessionStore.clearLocalSession), and the account
            // password gets them back in.
            Button {
                Haptic.light()
                Task { await sessionStore.logout() }
            } label: {
                Text("Forgot your passcode? Sign out")
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(.plain)
            .lockReveal(stage >= 4)
        }
        .padding(.bottom, 34)
    }

    // MARK: - No passcode yet: Face ID, plus the prompt to add one

    private func biometricBlock(_ biometry: (name: String, symbol: String)) -> some View {
        VStack(spacing: 18) {
            // No manual Haptic.light() here — CavnarPrimaryButtonStyle
            // already fires its own press haptic via .sensoryFeedback,
            // so this was buzzing twice per tap.
            Button {
                Task { await unlock() }
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: biometry.symbol)
                        .font(.system(size: 21, weight: .semibold))
                    if isUnlocking {
                        CavnarShimmerText(text: "Verifying…")
                    } else {
                        Text("Unlock")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: isUnlocking))
            .disabled(isUnlocking)
            .lockReveal(stage >= 3)

            // Setting a passcode is never allowed from behind the gate —
            // this passes Face ID first, then RootView opens the setup
            // sheet the moment the lock drops (SessionStore.pendingPasscodeSetup).
            Button {
                Haptic.light()
                Task { await unlockThenSetUpPasscode() }
            } label: {
                HStack(spacing: 12) {
                    Image(systemName: "circle.grid.3x3.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("No app passcode yet")
                            .font(.cavnarBody(14.5, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        Text("Add one to unlock without \(biometry.name)")
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 8)
                    Text("Set one")
                        .font(.cavnarBody(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 13)
                .background(
                    RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                        .fill(.ultraThinMaterial)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                        .strokeBorder(Color.cavnarEmber.opacity(0.32), lineWidth: 1)
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(isUnlocking)
            .lockReveal(stage >= 4)
        }
        .padding(.horizontal, 44)
        .padding(.bottom, 44)
    }

    // MARK: - Actions

    private func unlock() async {
        isUnlocking = true
        unlockFailed = false
        let unlocked = await sessionStore.unlockWithBiometrics()
        isUnlocking = false
        if !unlocked { unlockFailed = true }
    }

    private func unlockThenSetUpPasscode() async {
        sessionStore.pendingPasscodeSetup = true
        await unlock()
        // Still locked (cancelled or failed) — don't leave the intent armed
        // for some later, unrelated unlock.
        if sessionStore.isLocked { sessionStore.pendingPasscodeSetup = false }
    }

    private func submitPasscode(_ entered: String) {
        isUnlocking = true
        unlockFailed = false
        // A beat of "verifying" so the row's breathe is actually seen and
        // the success lands as a moment, not a flicker.
        Task { @MainActor in
            try? await Task.sleep(for: .seconds(0.35))
            let result = sessionStore.unlockWithPasscode(entered)
            isUnlocking = false
            switch result {
            case .unlocked:
                break   // RootView swaps to mainTabs on isLocked = false
            case .wrong(let remaining):
                Haptic.error()
                passcodeMessage = remaining > 0 && remaining <= 3
                    ? "Wrong passcode · \(remaining) attempt\(remaining == 1 ? "" : "s") left"
                    : "Wrong passcode — try again"
                passcodeError = true
                try? await Task.sleep(for: .seconds(0.5))
                passcode = ""
                passcodeError = false
            case .lockedOut:
                Haptic.error()
                passcodeError = true
                try? await Task.sleep(for: .seconds(0.5))
                passcode = ""
                passcodeError = false
                refreshLockout()
            }
        }
    }

    private func refreshLockout() {
        lockoutRemaining = sessionStore.passcodeLockoutRemaining
        if lockoutRemaining > 0 {
            let text = lockoutRemaining >= 60
                ? "\(Int((Double(lockoutRemaining) / 60).rounded(.up))) min"
                : "\(lockoutRemaining)s"
            passcodeMessage = "Too many attempts. Try again in \(text)."
        } else if passcodeMessage?.hasPrefix("Too many") == true {
            passcodeMessage = nil
        }
    }
}

private extension View {
    /// One step of LockedView's staggered reveal — fades and rises in.
    func lockReveal(_ shown: Bool) -> some View {
        opacity(shown ? 1 : 0).offset(y: shown ? 0 : 14)
    }
}
