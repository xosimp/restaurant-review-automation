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
    // Drives the hero's one-time landing reveal (opacity + upward offset).
    // Gated by sessionStore.hasShownHomeIntro rather than just this local
    // flag — see hero(_:)'s onAppear for why.
    @State private var heroAppeared = false

    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if let summary = viewModel.summary {
                        // Full-bleed, unpadded — the animated ember background
                        // behind the greeting needs to run edge to edge, not
                        // sit inside the 20pt content margin the rest of the
                        // page uses.
                        hero(summary)
                        VStack(alignment: .leading, spacing: 20) {
                            needsAttentionSection(summary)
                        }
                        .padding(20)
                    } else if viewModel.isLoading {
                        ProgressView().padding(.top, 80).frame(maxWidth: .infinity)
                    } else if let error = viewModel.errorMessage {
                        VStack(spacing: 8) {
                            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                            Button("Retry") { Task { await viewModel.load() } }
                        }
                        .padding(.top, 80)
                        .frame(maxWidth: .infinity)
                    }
                }
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            // No title text — "Home" was redundant with the hero's own
            // greeting right below it. Still .inline (not omitted) so the
            // bell/building toolbar icons keep a compact bar instead of
            // reserving large-title space for nothing.
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
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

    // Mirrors the web dashboard's Home hero (templates/dashboard.html,
    // #home-tw-headline/#home-tw-date/#home-tw-sub): an ember date eyebrow,
    // "{name} — your restaurant is running on AI." with the name in ember,
    // then a subtitle line — all rendered together and revealed as one
    // block, rising up from below and fading in, rather than typed out
    // word by word.
    private func hero(_ summary: HomeSummary) -> some View {
        VStack(spacing: 0) {
            Spacer(minLength: 16)
            VStack(spacing: 10) {
                Text(todayDateString)
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(2)
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarEmber)

                heroHeadline(summary)

                subtitleText(summary)
            }
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 26)
            Spacer(minLength: 16)
        }
        // Short enough that Needs Attention lands well above the fold — the
        // animated background still fades to black before this frame ends,
        // so it reads as one wash rather than a boxed hero banner.
        .frame(minHeight: 340)
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.horizontal, 20)
        .background(HomeHeroBackground())
        .onAppear {
            // Plays once per sign-in: SessionStore.hasShownHomeIntro (not
            // this view's own @State) is the source of truth, since it
            // survives whatever recreates HomeView when the client switches
            // tabs — a plain local flag would just replay the reveal every
            // time they come back to Home.
            guard !sessionStore.hasShownHomeIntro else {
                heroAppeared = true
                return
            }
            sessionStore.hasShownHomeIntro = true
            withAnimation(.easeOut(duration: 0.7).delay(0.15)) {
                heroAppeared = true
            }
        }
    }

    private func heroHeadline(_ summary: HomeSummary) -> some View {
        (Text(greetingName(summary)).foregroundStyle(Color.cavnarEmber)
            + Text(" — your restaurant is running on AI.").foregroundStyle(Color.cavnarInk))
            .font(.cavnarHeadline(26))
            .lineSpacing(3)
    }

    private func greetingName(_ summary: HomeSummary) -> String {
        guard let username = summary.username, !username.isEmpty else { return "Welcome back" }
        return username.prefix(1).uppercased() + username.dropFirst()
    }

    private var todayDateString: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, yyyy"
        formatter.locale = Locale(identifier: "en_US")
        return formatter.string(from: Date()).uppercased()
    }

    // No "N reviews awaiting approval" chip here anymore — Needs Attention
    // right below the hero already says the same thing, so surfacing it
    // twice just read as redundant.
    private func subtitleText(_ summary: HomeSummary) -> some View {
        var text = Text(summary.restaurantName).foregroundStyle(Color.cavnarInk3)
        if let locationName = summary.locationName, !locationName.isEmpty {
            text = text + Text(" — \(locationName)").foregroundStyle(Color.cavnarInk3)
        }
        return text.font(.cavnarBody(13))
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
