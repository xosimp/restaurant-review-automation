import SwiftUI

struct HomeView: View {
    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = HomeViewModel()
    @State private var showingLocationSwitcher = false
    @State private var showingNotifications = false
    @State private var notificationsBadge = NotificationsBadgeViewModel()
    @State private var path = NavigationPath()
    // Ticked on every accepted tile/row tap instead of calling Haptic.light()
    // directly in the action closure, paired with .sensoryFeedback below.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast
    // Drives the hero's one-time landing reveal (opacity + upward offset),
    // and the same flip staggers the Needs Attention rows in below it.
    // Owned and animated by RootView, not here — the Ask Cavnar FAB (a
    // sibling in a different subtree, overlaid on the whole TabView) needs
    // to fade in on the exact same withAnimation call for the two to land
    // in perfect sync, which isn't possible if each view times its own
    // onAppear independently. See RootView.playIntroSequenceIfNeeded.
    var heroAppeared: Bool
    // Fires once hero(_:) actually mounts — i.e. once `summary` has loaded
    // and the hero is genuinely on screen — so RootView can start the
    // shared fade-in transaction at that moment instead of on a fixed timer
    // that races the network call. On a slow load, a timer-only trigger
    // could flip `heroAppeared` to true before the hero view even existed,
    // so it would mount already-revealed with nothing to animate from.
    var onHeroAppear: () -> Void = {}

    // How far down the Needs Attention wrapper's top padding shifts (see
    // its call site) — the hero uses its own derived shift below, since a
    // VStack's top padding and two Spacers redistributing leftover height
    // don't move by the same math for the same input number. Pulled way
    // down from 40 — the greeting, chart, and carousel all sit inside/below
    // this same hero frame, so a small shift here is what lifts the whole
    // page up.
    private static let heroContentDownShift: CGFloat = 8

    // How tall the animated background itself is — independent of the hero
    // content's own layout. Previously the background was applied via
    // .background() on the hero's content VStack, nested inside the
    // ScrollView; a background modifier on a view INSIDE a ScrollView is
    // clipped to that view's own bounds; no ignoresSafeArea() on it can
    // escape that clipping and reach the status bar/nav bar above the
    // scroll view. That's why the nav bar strip stayed solid black no
    // matter what was tried on the background itself — the fix has to move
    // the background OUT of the scrollable content and into its own layer
    // behind the whole screen (see body below), which is the standard
    // SwiftUI pattern for a hero background that bleeds behind a
    // translucent nav bar. Shrunk in proportion with the hero content frame
    // below so its fade still tails off roughly where the content ends,
    // instead of leaving a stretch of vivid, un-faded aurora behind the
    // (now higher-up) chart and carousel.
    private static let heroBackgroundHeight: CGFloat = 460

    var body: some View {
        NavigationStack(path: $path) {
            ZStack(alignment: .top) {
                HomeHeroBackground()
                    .frame(height: Self.heroBackgroundHeight)
                    .ignoresSafeArea(edges: .top)

                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if let summary = viewModel.summary {
                            // Full-bleed, unpadded — the greeting sits on
                            // top of the background layer above, not inside
                            // the 20pt content margin the rest of the page
                            // uses.
                            hero(summary)
                            // spacing: 0 with an explicit .padding(.bottom,
                            // 26) on just the carousel below (instead of a
                            // uniform VStack spacing) — this is what lets
                            // the carousel move closer to the greeting
                            // without dragging the chart up with it: the
                            // carousel's own top offset shrinks by the same
                            // 6pt the gap below it grows, so the chart's
                            // absolute position on the page never moves.
                            VStack(alignment: .leading, spacing: 0) {
                                needsAttentionSection(summary)
                                    .padding(.bottom, 26)
                                valueChartSection(summary)
                            }
                            // Independent of heroContentDownShift now — that
                            // constant governs the greeting text's own
                            // position inside the tall hero, which stayed
                            // put; this is just Needs Attention's own small
                            // top margin below where hero(_:) ends. Bottom
                            // padding bumped well past the FAB's own
                            // reserved band (70pt above the tab bar, plus its
                            // own ~50pt footprint) so the chart never sits
                            // directly behind the fixed FAB circle on first
                            // load.
                            .padding(.horizontal, 20)
                            .padding(.bottom, 120)
                            .padding(.top, 2)
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
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            // The base color behind everything — where the background
            // layer's own bottom fade ends, and for any content below it.
            .background(Color.cavnarPaper)
            .refreshable { await viewModel.load() }
            // No title text — "Home" was redundant with the hero's own
            // greeting right below it. Still .inline (not omitted) so the
            // bell/building toolbar icons keep a compact bar instead of
            // reserving large-title space for nothing.
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            // The system's default translucent nav bar material was dimming
            // and blurring the hero gradient wherever it sat behind the
            // status bar/toolbar row. Hiding that material lets the
            // background layer show through unobstructed behind the bell/
            // building icons instead of a flat bar sitting over it.
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        showingNotifications = true
                    } label: {
                        ZStack(alignment: .topTrailing) {
                            Image(systemName: "bell")
                            if notificationsBadge.unreadCount > 0 {
                                Circle()
                                    .fill(Color.cavnarEmber)
                                    .frame(width: 8, height: 8)
                                    .offset(x: 5, y: -3)
                            }
                        }
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
            // Opening the sheet marks alert_log seen server-side (see
            // NotificationsListViewModel.load()), so refreshing again right
            // as it's dismissed is what actually clears the bell's dot.
            .onChange(of: showingNotifications) { wasShowing, isShowing in
                if wasShowing && !isShowing {
                    Task { await notificationsBadge.refresh() }
                }
            }
            .task { await viewModel.load() }
            .task { await notificationsBadge.refresh() }
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
            // Two Spacers with EQUAL minLength (as this was originally)
            // vertically center the content block — SwiftUI splits whatever
            // height is left over between them evenly, regardless of their
            // minLength, so a fixed top padding instead of a matched pair
            // doesn't "start lower," it just drops the min floor and lets
            // the content float up toward the top. To move the block down
            // FROM that centered position by an exact amount without
            // fighting that redistribution, only the top Spacer's minLength
            // grows, and by DOUBLE the desired shift — half of any increase
            // here gets redistributed back to the bottom Spacer, so growing
            // it by 2x nets exactly +1x at the content's actual position.
            Spacer(minLength: 16 + Self.heroContentDownShift * 2)
            VStack(spacing: 10) {
                Text(todayDateString)
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(2)
                    .textCase(.uppercase)
                    // cavnarEmber (deep, dark-mode brand orange) was reading
                    // as roughly the same tone as the aurora blooms sitting
                    // right behind it — cavnarEmber2 (the lighter peach
                    // token) plus a real drop shadow gives it a defined edge
                    // regardless of exactly which part of the moving
                    // background happens to be behind it at any moment.
                    .foregroundStyle(Color.cavnarEmber2)
                    .shadow(color: .black.opacity(0.5), radius: 3, x: 0, y: 1)

                heroHeadline(summary)

                subtitleText(summary)
            }
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 26)
            Spacer(minLength: 16)
        }
        // Deliberately shorter than the background behind it — this governs
        // where the value chart starts. Shrunk from 380 so the greeting,
        // chart, and carousel all sit noticeably higher on the page instead
        // of leaving a tall stretch of empty hero space above them.
        .frame(minHeight: 260)
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.horizontal, 20)
        .onAppear { onHeroAppear() }
    }

    private func heroHeadline(_ summary: HomeSummary) -> some View {
        // cavnarEmber2 instead of cavnarEmber for the name, same reasoning
        // as the date eyebrow above — plus a shadow on the whole line
        // (Text concatenation only carries font/color per segment, not
        // per-segment view modifiers like .shadow, so it applies to both
        // halves; harmless on the already-high-contrast cream half, and
        // exactly what the ember half needed).
        (Text(greetingName(summary)).foregroundStyle(Color.cavnarEmber2)
            + Text(" — your restaurant is running on AI.").foregroundStyle(Color.cavnarInk))
            .font(.cavnarHeadline(26))
            .lineSpacing(3)
            .shadow(color: .black.opacity(0.45), radius: 4, x: 0, y: 2)
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

    // The value chart's own eyebrow/number/delta/sparkline carry no card
    // chrome of their own (see ValueChartCard) — just fades + rises in with
    // the same `heroAppeared` flip everything else below the hero uses.
    private func valueChartSection(_ summary: HomeSummary) -> some View {
        ValueChartCard(totalValue: summary.totalValueDelivered, history: summary.valueHistory)
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 20)
            .animation(.easeOut(duration: 0.5).delay(0.3), value: heroAppeared)
    }

    // No "Needs attention" header and no enclosing gray .cavnarCard() around
    // the carousel anymore — each card carries its own light, uniform
    // surface (see NeedsAttentionFloatCard), so a boxy outer container plus
    // a label restating what the cards themselves already make obvious just
    // added visual noise. The whole carousel fades + rises in as one unit
    // off the same `heroAppeared` flip the hero uses — a horizontal-scroll
    // row doesn't read as a top-to-bottom sequence the way the old vertical
    // list did, so a per-card stagger no longer made sense.
    @ViewBuilder
    private func needsAttentionSection(_ summary: HomeSummary) -> some View {
        if summary.needsAttention.isEmpty {
            AllClearRow()
                .opacity(heroAppeared ? 1 : 0)
                .offset(y: heroAppeared ? 0 : 20)
                .animation(.easeOut(duration: 0.5).delay(0.1), value: heroAppeared)
        } else {
            NeedsAttentionCarousel(items: summary.needsAttention) { item in
                navigate(to: ModuleRoute(key: item.module, label: item.module.capitalized))
            }
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 20)
            .animation(.easeOut(duration: 0.55).delay(0.1), value: heroAppeared)
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
