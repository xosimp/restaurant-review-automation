import SwiftUI

struct HomeView: View {
    @Environment(SessionStore.self) private var sessionStore
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    // Owned by RootView (see its homeViewModel) so the loaded summary
    // survives the Face ID lock/unlock swap instead of reloading from
    // scratch on every unlock — and RootView fetches it while the lock
    // screen is still up, so a cold launch lands straight on the hero.
    let viewModel: HomeViewModel
    @State private var showingLocationSwitcher = false
    @State private var showingNotifications = false
    @State private var notificationsBadge = NotificationsBadgeViewModel()
    // Owned here (not by NotificationsListView) so the fetch can start the
    // instant the bell is tapped, and so a second open in the same session
    // shows the already-loaded list immediately instead of resetting to a
    // fresh loading state — see NotificationsListView's own doc comment.
    @State private var notificationsList = NotificationsListViewModel()
    // Bound from RootView, not owned here — see ModulesGridView.path's doc
    // comment (the identical pattern there) for why: RootView.body swaps
    // this whole view out for LockedView across a Face ID lock/unlock
    // cycle, and a locally-owned @State path would reset to empty on every
    // unlock, silently discarding whatever module screen (e.g. Labor,
    // mid-viewing a just-generated schedule) the user had pushed to from a
    // Home tile.
    @Binding var path: NavigationPath
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
                            if summary.quietHoursActive {
                                quietHoursBanner(summary)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 14)
                            }
                            // spacing: 0 with an explicit .padding(.bottom,
                            // 38) on just the carousel below (instead of a
                            // uniform VStack spacing) — this is what lets
                            // the carousel move closer to the greeting
                            // without dragging the chart up with it: the
                            // carousel's own top offset shrinks by the same
                            // amount the gap below it grows, so the chart's
                            // absolute position on the page never moves. A
                            // negative top padding here is intentional — it
                            // pulls the carousel up past where zero would
                            // sit, tight against the hero's own bottom
                            // margin, since the previous +2 was too small a
                            // move to actually read as "closer."
                            VStack(alignment: .leading, spacing: 0) {
                                needsAttentionSection(summary)
                                    .padding(.bottom, 38)
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
                            .padding(.top, -10)
                        } else if viewModel.isLoading {
                            heroSkeleton
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
                .cavnarEmberRefreshable { await viewModel.load() }
            }
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(moduleKey: route.key, moduleLabel: route.label)
            }
            .sensoryFeedback(.impact(weight: .light), trigger: navHapticTrigger)
            // The base color behind everything — where the background
            // layer's own bottom fade ends, and for any content below it.
            .background(Color.cavnarPaper)
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
                // The leading side was empty — .navigationTitle("") on
                // purpose (see above), so this reserved space never showed
                // anything at all. A static, non-interactive seal mark
                // here matches the everyday-logo-in-the-corner convention
                // most apps use, on the one screen every session opens on.
                cavnarToolbarItem(placement: .topBarLeading) {
                    // Matching the SAME NUMBER as the bell's font size
                    // ("17pt each") turned out not to mean matching visual
                    // size at all, and measuring the actual rendered pixels
                    // proved it: the bell glyph (a bold SF Symbol) rendered
                    // at ~22pt tall, the seal's ring (custom-drawn geometry
                    // with built-in internal padding — see CavnarSealMark's
                    // own doc comment on its proportional 120x120 source)
                    // rendered at only ~13pt tall from the same declared
                    // "17". 28 empirically measured to produce the same
                    // ~22pt glyph height as the bell. The shared background
                    // circle stays identical either way — both go through
                    // cavnarToolbarIconGlass()'s own fixed 34pt default,
                    // which doesn't depend on the icon's declared size.
                    CavnarSealMark(size: 28)
                        .cavnarToolbarIconGlass()
                }
                // Only shown when quiet hours is both enabled and the
                // current time actually falls inside the window — computed
                // server-side by the same check notify.py's own alert
                // dispatch gates on (see HomeSummary.quietHoursActive), so
                // this can never claim alerts are silenced when they
                // aren't. Sits to the left of the bell, since the bell is
                // exactly what it's telling you is muted right now.
                if viewModel.summary?.quietHoursActive == true {
                    cavnarToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Haptic.light()
                            deepLinkRouter.pendingTab = .account
                        } label: {
                            CavnarQuietMark(size: 34)
                        }
                        .buttonStyle(.plain)
                        .tint(nil)
                    }
                }
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        Task {
                            // The FIRST open this session waits for the real
                            // fetch to land before the sheet ever appears —
                            // no other app flashes a loading skeleton for
                            // something this lightweight, it just shows the
                            // list, so neither should this. Every open after
                            // that presents instantly against the already-
                            // cached list (still real content, just from a
                            // few minutes ago) and refreshes it silently in
                            // the background rather than making the tap wait
                            // on a network round-trip every single time.
                            if notificationsList.hasLoadedOnce {
                                showingNotifications = true
                                Task { await notificationsList.load() }
                            } else {
                                await notificationsList.load()
                                showingNotifications = true
                            }
                        }
                    } label: {
                        // Badge dot is its own overlay on top of the
                        // already glass-wrapped bell, not inside the same
                        // ZStack — cavnarToolbarIconGlass() clips the icon
                        // to a tight circle, and the badge sits offset
                        // past that icon's own bounds, so it needs to
                        // composite outside that clip, not get cut off by it.
                        Image(systemName: "bell")
                            .font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarToolbarIconGlass()
                            .overlay(alignment: .topTrailing) {
                                if notificationsBadge.unreadCount > 0 {
                                    // "Alert Fired" — pops in with one
                                    // ember ripple the moment there's
                                    // something unread (see CavnarMotion).
                                    CavnarAlertBadge(diameter: 8)
                                        .offset(x: 2, y: -2)
                                }
                            }
                    }
                    // .plain strips the default toolbar-button chrome that
                    // was stacking its own automatic tap feedback on top of
                    // our manual Haptic.light() above — same fix as the FAB
                    // and module tiles, which never had this problem because
                    // they already use a stripped-chrome button style.
                    .buttonStyle(.plain)
                    .tint(nil)
                }
                if sessionStore.currentUser?.isOwner == true {
                    cavnarToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Haptic.light()
                            showingLocationSwitcher = true
                        } label: {
                            Image(systemName: "building.2")
                                .font(.system(size: 17, weight: .semibold))
                                .foregroundStyle(Color.cavnarEmber)
                                .cavnarToolbarIconGlass()
                        }
                        .buttonStyle(.plain)
                        .tint(nil)
                    }
                }
            }
            .sheet(isPresented: $showingLocationSwitcher) {
                LocationSwitcherView { Task { await viewModel.load() } }
            }
            .sheet(isPresented: $showingNotifications) {
                NotificationsListView(viewModel: notificationsList)
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
            // Reopening the app after a shift should not show morning's
            // numbers as if they were current (audit 4.2).
            .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) { await viewModel.load() }
        }
    }

    /// What Home shows while its first summary is still in flight — only
    /// reached when RootView's lock-screen prefetch hasn't landed yet: a
    /// ghost of the hero's own shape (eyebrow, two headline lines, a
    /// subtitle) with the ember line working under it. Never the seal: a
    /// lone big "C" at the top of an empty Home read as a stray logo flash
    /// for the length of the fetch.
    private var heroSkeleton: some View {
        VStack(alignment: .leading, spacing: 12) {
            ghostLine(width: 92, height: 10)
            ghostLine(width: 268, height: 24)
            ghostLine(width: 214, height: 24)
            ghostLine(width: 180, height: 12)
                .padding(.top, 2)
            CavnarShimmerLine(height: 3)
                .frame(width: 120)
                .padding(.top, 10)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 20)
        .padding(.top, 16 + Self.heroContentDownShift * 2 + 24)
    }

    private func ghostLine(width: CGFloat, height: CGFloat) -> some View {
        Capsule()
            .fill(Color.cavnarInk.opacity(0.08))
            .frame(width: width, height: height)
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
                    .font(.cavnarBody(14, weight: 700))
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
            // Same duration/delay as valueChartSection and
            // needsAttentionSection below — all three used to animate on
            // different schedules (this one inherited RootView's ambient
            // 0.7s/0.15s transaction, the other two overrode it with their
            // own, different, timings), so the page read as pieces arriving
            // independently rather than one coordinated reveal. Bottom
            // content was finishing before the hero above it.
            .animation(Self.introAnimation, value: heroAppeared)
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

    // The second of the two quiet-hours surfaces (see CavnarQuietMark's own
    // doc comment) — a plain-language line for the first time someone
    // actually sees this, since a lone toolbar glyph doesn't explain
    // itself. Same trigger condition as the toolbar badge.
    private func quietHoursBanner(_ summary: HomeSummary) -> some View {
        HStack(spacing: 9) {
            CavnarQuietMark(size: 26)
            (Text("Notifications quiet").font(.cavnarBody(13.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                + Text(quietHoursEndText(summary)).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk2))
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .background(Color.white.opacity(0.06))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.1), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 14))
    }

    private func quietHoursEndText(_ summary: HomeSummary) -> String {
        guard let end = summary.alertQuietEnd else {
            return " — text, email, and push are all holding for now."
        }
        let parser = DateFormatter()
        parser.dateFormat = "HH:mm"
        parser.locale = Locale(identifier: "en_US_POSIX")
        guard let date = parser.date(from: end) else {
            return " — text, email, and push are all holding for now."
        }
        let display = DateFormatter()
        display.dateFormat = "h:mm a"
        display.locale = Locale(identifier: "en_US")
        return " until \(display.string(from: date)) — text, email, and push are all holding until then."
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
        return text.font(.cavnarBody(14.5))
    }

    // The value chart's own eyebrow/number/delta/sparkline carry no card
    // chrome of their own (see ValueChartCard) — just fades + rises in with
    // the same `heroAppeared` flip everything else below the hero uses.
    private func valueChartSection(_ summary: HomeSummary) -> some View {
        ValueChartCard(totalValue: summary.totalValueDelivered, history: summary.valueHistory)
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 20)
            .animation(Self.introAnimation, value: heroAppeared)
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
                .animation(Self.introAnimation, value: heroAppeared)
        } else {
            NeedsAttentionCarousel(items: summary.needsAttention) { item in
                navigate(to: ModuleRoute(key: item.module, label: item.module.capitalized))
            }
            .opacity(heroAppeared ? 1 : 0)
            .offset(y: heroAppeared ? 0 : 20)
            .animation(Self.introAnimation, value: heroAppeared)
        }
    }

    // Single shared timing for every heroAppeared-driven fade-in on this
    // screen (hero, value chart, needs-attention) — previously each had its
    // own duration/delay, so the sections landed at different moments and
    // the page read as pieces arriving independently instead of one
    // coordinated reveal.
    private static let introAnimation: Animation = .easeOut(duration: 0.55).delay(0.15)

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
