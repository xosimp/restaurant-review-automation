import SwiftUI

/// Home — the first fold has one job: make the owner feel the AI working.
///
/// Top to bottom: the hero line (the date, "{name} — your restaurant is
/// running on AI.", and what Cavnar did overnight), the pulse strip of
/// breathing module chips, the action deck led by the one thing to tap,
/// the value-delivered band, and a "this week" receipt. No module tiles
/// here — that's the Modules tab's job, not Home's. Everything sits on
/// HomeObsidianField — black stone with light moving across it — instead
/// of the old ember aurora.
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
    @State private var showingValueDetail = false
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
    // unlock, silently discarding whatever module screen the user had
    // pushed to from a Home tile.
    @Binding var path: NavigationPath
    // Ticked on every accepted tile/row tap instead of calling Haptic.light()
    // directly in the action closure, paired with .sensoryFeedback below.
    @State private var navHapticTrigger = 0
    @State private var lastNavigationAt = Date.distantPast
    // The action deck's one-tap publish: the card whose CTA was tapped
    // (drives the confirmation dialog), then the "Published N" check.
    @State private var pendingPublish: NeedsAttentionItem?
    @State private var postedLabel: String?
    // Drives the hero's one-time landing reveal (opacity + upward offset),
    // and everything below it rises in off the same flip, a beat later.
    // Owned and animated by RootView, not here — the Ask Cavnar FAB (a
    // sibling in a different subtree, overlaid on the whole TabView) needs
    // to fade in on the exact same withAnimation call for the two to land
    // in perfect sync. See RootView.playIntroSequenceIfNeeded.
    var heroAppeared: Bool
    // Fires once hero(_:) actually mounts — i.e. once `summary` has loaded
    // and the hero is genuinely on screen — so RootView can start the
    // shared fade-in transaction at that moment instead of on a fixed timer
    // that races the network call.
    var onHeroAppear: () -> Void = {}

    // True while any sheet is up over Home — see HomeObsidianField's own
    // doc comment on its `paused` parameter for why this exists: without
    // it, the field's three Canvas layers and the pulse strip's marquee
    // kept compositing every frame through a sheet's presentation and any
    // interactive swipe-to-dismiss, which is what made both feel laggy.
    private var backgroundMotionPaused: Bool {
        showingValueDetail || showingNotifications || showingLocationSwitcher
    }

    var body: some View {
        NavigationStack(path: $path) {
            ZStack(alignment: .top) {
                HomeObsidianField(paused: backgroundMotionPaused)

                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if let summary = viewModel.summary {
                            hero(summary)

                            HomePulseStrip(modules: summary.modules, paused: backgroundMotionPaused) { module in
                                navigate(to: ModuleRoute(key: module.key, label: module.label))
                            }
                            .padding(.top, 18)
                            .belowFold(heroAppeared, delay: 0.25)

                            if summary.quietHoursActive {
                                quietHoursBanner(summary)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 16)
                                    .belowFold(heroAppeared, delay: 0.25)
                            }

                            attentionSection(summary)
                                .padding(.horizontal, 20)
                                .padding(.top, 30)
                                .belowFold(heroAppeared, delay: 0.35)

                            HomeValueBand(
                                total: summary.totalValueDelivered,
                                history: summary.valueHistory,
                                activeModuleKeys: summary.modules.filter(\.isAvailable).map(\.key)
                            ) {
                                Haptic.light()
                                showingValueDetail = true
                            }
                            .padding(.top, 24)
                            .belowFold(heroAppeared, delay: 0.45)

                            if let receipts = summary.weeklyReceipts, !receipts.isEmpty {
                                HomeWeeklyReceipts(receipts: receipts)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 30)
                                    .belowFold(heroAppeared, delay: 0.5)
                            }

                            // Clears the FAB's reserved band above the tab
                            // bar plus its own footprint, so the last
                            // section never sits behind it.
                            Color.clear.frame(height: 120)
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
            .sensoryFeedback(.impact(weight: .medium), trigger: navHapticTrigger)
            // The base colour behind everything — where the field's own
            // bottom fade ends, and for any content below it.
            .background(Color.cavnarPaper)
            // No title text — "Home" was redundant with the hero's own
            // greeting right below it. Still .inline (not omitted) so the
            // bell/building toolbar icons keep a compact bar instead of
            // reserving large-title space for nothing.
            .navigationTitle("")
            .navigationBarTitleDisplayMode(.inline)
            // The system's translucent nav bar material would dim and blur
            // the field wherever it sat behind the status bar/toolbar row.
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                cavnarToolbarItem(placement: .topBarLeading) {
                    // 28 measures to the same ~22pt glyph height as the
                    // bell — see CavnarSealMark's own doc comment on its
                    // built-in internal padding.
                    CavnarSealMark(size: 28)
                        .cavnarToolbarIconGlass()
                }
                // Only shown when quiet hours is both enabled and the
                // current time actually falls inside the window — computed
                // server-side by the same check notify.py's own alert
                // dispatch gates on (see HomeSummary.quietHoursActive).
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
                            // fetch to land before the sheet ever appears;
                            // every open after that presents instantly
                            // against the cached list and refreshes it
                            // silently in the background.
                            if notificationsList.hasLoadedOnce {
                                showingNotifications = true
                                Task { await notificationsList.load() }
                            } else {
                                await notificationsList.load()
                                showingNotifications = true
                            }
                        }
                    } label: {
                        Image(systemName: "bell")
                            .font(.system(size: 17, weight: .semibold))
                            // cavnarEmber2, not the deeper cavnarEmber — this
                            // is Home's header, and the header's other orange
                            // (the greeting name, the section kickers) is all
                            // Ember2; a bell in the darker token read as an
                            // off-brand mismatch next to them.
                            .foregroundStyle(Color.cavnarEmber2)
                            .cavnarToolbarIconGlass()
                            .overlay(alignment: .topTrailing) {
                                if notificationsBadge.unreadCount > 0 {
                                    CavnarAlertBadge(diameter: 8)
                                        .offset(x: 2, y: -2)
                                }
                            }
                    }
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
                                .font(.system(size: 15, weight: .semibold))
                                // See the bell above — matches the header's
                                // other orange (name, kickers), not the
                                // darker cavnarEmber.
                                .foregroundStyle(Color.cavnarEmber2)
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
            .sheet(isPresented: $showingValueDetail) {
                valueDetailSheet
            }
            .confirmationDialog(
                pendingPublish?.cta ?? "Publish replies",
                isPresented: Binding(
                    get: { pendingPublish != nil },
                    set: { if !$0 { pendingPublish = nil } }
                ),
                titleVisibility: .visible
            ) {
                Button(pendingPublish?.cta ?? "Publish") {
                    Task { await publishReplies() }
                }
                Button("Cancel", role: .cancel) { pendingPublish = nil }
            } message: {
                Text("Each reply was drafted in your voice. Google-connected replies post right away; the rest are marked approved.")
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

    // MARK: - Hero

    /// The date, the line, and what Cavnar did while the owner wasn't
    /// looking — centred, alone on the field, revealed as one block.
    private func hero(_ summary: HomeSummary) -> some View {
        VStack(spacing: 10) {
            Text(todayDateString)
                .font(.cavnarBody(12.5, weight: 700))
                .tracking(2.2)
                .foregroundStyle(Color.cavnarEmber2)
                .shadow(color: .black.opacity(0.5), radius: 3, x: 0, y: 1)

            heroHeadline(summary)
                .fixedSize(horizontal: false, vertical: true)

            overnightLine(summary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.horizontal, 24)
        .padding(.top, 38)
        .opacity(heroAppeared ? 1 : 0)
        .offset(y: heroAppeared ? 0 : 26)
        .animation(Self.introAnimation, value: heroAppeared)
        .onAppear { onHeroAppear() }
    }

    private func heroHeadline(_ summary: HomeSummary) -> some View {
        // cavnarEmber2 for the name (the deeper cavnarEmber sank into the
        // old aurora; on the field it's about the glow, not contrast) plus
        // a shadow on the whole line — Text concatenation only carries
        // font/colour per segment, not per-segment view modifiers.
        (Text(greetingName(summary)).foregroundStyle(Color.cavnarEmber2)
            + Text(" — your restaurant is running on AI.").foregroundStyle(Color.cavnarInk))
            .font(.cavnarHeadline(27))
            .lineSpacing(3)
            .shadow(color: .black.opacity(0.45), radius: 4, x: 0, y: 2)
    }

    private func greetingName(_ summary: HomeSummary) -> String {
        guard let username = summary.username, !username.isEmpty else { return "Welcome back" }
        return username.prefix(1).uppercased() + username.dropFirst()
    }

    /// "Gia Mia · Overnight, Cavnar answered 3 reviews and flagged 2 things
    /// for you." — numbers in ember, in Space Grotesk. Before noon it's
    /// "Overnight"; after, "Since yesterday" (the window is the last 24h
    /// either way). With nothing to report it says so instead of padding.
    private func overnightLine(_ summary: HomeSummary) -> Text {
        let size: CGFloat = 14
        let quiet = Color.cavnarInk3
        let lead = Text(verbatim: summary.restaurantName + " · ").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
        guard let overnight = summary.overnight, overnight.answered + overnight.flagged > 0 else {
            return lead + Text(verbatim: "All quiet since yesterday — nothing new for you.")
                .font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
        }
        let when = Calendar.current.component(.hour, from: Date()) < 12 ? "Overnight" : "Since yesterday"
        var line = lead + Text(verbatim: "\(when), Cavnar AI ").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
        var clauses: [Text] = []
        if overnight.answered > 0 {
            clauses.append(
                Text(verbatim: "answered ").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
                + Text(verbatim: "\(overnight.answered)").font(.cavnarNumber(size, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                + Text(verbatim: overnight.answered == 1 ? " review" : " reviews").font(.cavnarBody(size, weight: 700)).foregroundStyle(Color.cavnarEmber2)
            )
        }
        if overnight.flagged > 0 {
            clauses.append(
                Text(verbatim: "flagged ").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
                + Text(verbatim: "\(overnight.flagged)").font(.cavnarNumber(size, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                + Text(verbatim: overnight.flagged == 1 ? " thing" : " things").font(.cavnarBody(size, weight: 700)).foregroundStyle(Color.cavnarEmber2)
            )
        }
        line = line + clauses[0]
        if clauses.count > 1 {
            line = line + Text(verbatim: " and ").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet) + clauses[1]
        }
        return line + Text(verbatim: " for you.").font(.cavnarBody(size, weight: 600)).foregroundStyle(quiet)
    }

    private var todayDateString: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEEE, MMM d"
        formatter.locale = Locale(identifier: "en_US")
        return formatter.string(from: Date()).uppercased()
    }

    // MARK: - Sections

    @ViewBuilder
    private func attentionSection(_ summary: HomeSummary) -> some View {
        if summary.needsAttention.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                HomeSectionHeader(kicker: "Needs attention", title: "Start here")
                AllClearRow()
            }
        } else {
            HomeActionDeck(
                items: summary.needsAttention,
                busy: viewModel.isPublishingReplies,
                onPrimary: { item in primaryAction(item, in: summary) },
                onSecondary: { item in
                    navigate(to: ModuleRoute(key: item.module, label: moduleLabel(item.module, in: summary)))
                }
            )
            .cavnarPostedOverlay(postedLabel) { postedLabel = nil }
        }
    }

    private var valueDetailSheet: some View {
        NavigationStack {
            ScrollView {
                if let summary = viewModel.summary {
                    ValueChartCard(totalValue: summary.totalValueDelivered, history: summary.valueHistory)
                        .padding(20)
                }
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                cavnarTitleToolbar("Value delivered")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        showingValueDetail = false
                    } label: {
                        Text("Done")
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
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

    /// What Home shows while its first summary is still in flight — only
    /// reached when RootView's lock-screen prefetch hasn't landed yet: a
    /// ghost of the hero's own shape with the ember line working under it.
    private var heroSkeleton: some View {
        VStack(spacing: 12) {
            ghostLine(width: 120, height: 10)
            ghostLine(width: 268, height: 24)
            ghostLine(width: 214, height: 24)
            ghostLine(width: 230, height: 12)
                .padding(.top, 2)
            CavnarShimmerLine(height: 3)
                .frame(width: 120)
                .padding(.top, 10)
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 20)
        .padding(.top, 46)
    }

    private func ghostLine(width: CGFloat, height: CGFloat) -> some View {
        Capsule()
            .fill(Color.cavnarInk.opacity(0.08))
            .frame(width: width, height: height)
    }

    // MARK: - Actions

    private func primaryAction(_ item: NeedsAttentionItem, in summary: HomeSummary) {
        if item.isPublishAction {
            Haptic.light()
            pendingPublish = item
        } else {
            navigate(to: ModuleRoute(key: item.module, label: moduleLabel(item.module, in: summary)))
        }
    }

    private func publishReplies() async {
        guard pendingPublish != nil else { return }
        pendingPublish = nil
        // A nil result means the call failed — APIClient has already played
        // the error haptic, and the deck stays exactly as it was.
        guard let result = await viewModel.publishAllReplies(), result.approved > 0 else { return }
        Haptic.success()
        if result.posted > 0 {
            postedLabel = "Published \(result.posted) to Google"
        } else {
            postedLabel = "Approved \(result.approved) \(result.approved == 1 ? "reply" : "replies")"
        }
    }

    private func moduleLabel(_ key: String, in summary: HomeSummary) -> String {
        summary.modules.first { $0.key == key }?.label ?? key.capitalized
    }

    // Single shared timing for every heroAppeared-driven reveal on this
    // screen — the hero first, then each section a beat later (see
    // belowFold), so the page reads as one coordinated landing.
    private static let introAnimation: Animation = .easeOut(duration: 0.55).delay(0.15)

    // A Button inside a ScrollView has to let the ScrollView's own pan
    // gesture "race" its tap gesture to tell a scroll from a tap — under a
    // fast swipe-off-one-tile-and-tap-another, that disambiguation can
    // resolve the FIRST tile's tap late, landing a stale extra navigation
    // moments after the real one. Ignore any tap within 350ms of the last
    // accepted one.
    private func navigate(to route: ModuleRoute) {
        let now = Date()
        guard now.timeIntervalSince(lastNavigationAt) > 0.35 else { return }
        lastNavigationAt = now
        navHapticTrigger += 1
        path.append(route)
    }
}

/// The below-the-hero reveal: fade + rise off the same `heroAppeared` flip
/// the hero uses, delayed by `delay` so sections land top to bottom.
private struct BelowFoldReveal: ViewModifier {
    let appeared: Bool
    let delay: Double

    func body(content: Content) -> some View {
        content
            .opacity(appeared ? 1 : 0)
            .offset(y: appeared ? 0 : 20)
            .animation(.easeOut(duration: 0.55).delay(delay), value: appeared)
    }
}

private extension View {
    func belowFold(_ appeared: Bool, delay: Double) -> some View {
        modifier(BelowFoldReveal(appeared: appeared, delay: delay))
    }
}
