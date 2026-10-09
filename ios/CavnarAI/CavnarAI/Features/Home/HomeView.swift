import SwiftUI

/// Home — "Web explains. iPhone decides." (iOS readability round, 10/8/26).
///
/// Top to bottom, "decide, then more": the hero (the date, the brief's
/// headline and one overnight line), anything about to go out on its own
/// with Undo, the night's own card (last night's report before noon, the
/// close-out after 8pm), the glance (three fixed tiles and data health in
/// words), Find or ask, Today's focus, ONE ranked "Needs you" list, the
/// brief's reads, Restaurant DNA — and one closed "More" group holding the
/// recommendations, the measured results and how the restaurant compares.
/// No module tiles here — that's the Modules tab's job. Everything sits on
/// HomeObsidianField — black stone with light moving across it.
struct HomeView: View {
    @State private var followThrough = HomeFollowThroughViewModel()
    @State private var aiActivity = AIActivityViewModel()
    /// The brief and open issues — one read feeds the brief card and the
    /// issue rows in Needs you.
    @State private var day = HomeDayViewModel()
    /// Last night's report — one read feeds its card and the glance row.
    @State private var lastNight = HomeLastNightViewModel()
    /// The recommendation record, with the kinds on hold and the quieter
    /// kinds (moved off Home, #96).
    @State private var showingRecord = false
    @Environment(SessionStore.self) private var sessionStore
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    // Owned by RootView (see its homeViewModel) so the loaded summary
    // survives the Face ID lock/unlock swap instead of reloading from
    // scratch on every unlock — and RootView fetches it while the lock
    // screen is still up, so a cold launch lands straight on the hero.
    let viewModel: HomeViewModel
    @State private var showingValueDetail = false
    @State private var showingDataHealth = false
    // The inbox and the location switcher are RootView's now (AppChrome):
    // one of each, reachable from every screen, not just Home's corner
    // (friction audit #32).
    @Environment(AppChrome.self) private var chrome
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
    /// The publish's confirm card — every reply that would post, in its own
    /// words (/command/propose, parity audit #2) — while it is read, and the
    /// Ask view model its Confirm runs through (the same route as Ask's).
    @State private var publishProposal: AskProposal?
    @State private var publishProposalLoading = false
    @State private var publishAsk = AskCavnarViewModel()
    /// A cross-module link's Evidence, opened from its Needs-attention row.
    @State private var evidenceLink: HomeFollowThroughViewModel.CrossModule.Link?
    // A second hide on a needs-attention item asks why (#42): the item,
    // held apart from the dialog's own flag so the answer still has it
    // after the dialog has closed itself.
    @State private var attentionAskingWhy: NeedsAttentionItem?
    @State private var showingAttentionWhy = false
    /// The issue an issue push named (DeepLinkRouter.pendingIssueId), handed
    /// to the day card to scroll to and pulse (parity audit #11).
    @State private var issueFocus: HomeIssueFocus?
    /// Restaurant DNA (redesigned 10/8/26): a card under the day card that
    /// opens the full DNA screen. One read feeds both.
    @State private var showingDNA = false
    @State private var dnaModel = RestaurantDNAViewModel()
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
        showingValueDetail || chrome.showingNotifications || chrome.showingLocationSwitcher || showingDataHealth || !tabVisible
            // Also frozen until the landing is done. Home mounts during
            // RootView's own crossfade out of the sign-in screen, and the
            // field's three Canvas layers used to start ticking right then
            // — competing with that transition, the first data render, and
            // every section's reveal, all in the same handful of frames.
            // It's slow ambient drift; starting it once the page has
            // settled is invisible, and it takes real work out of exactly
            // the moment that was skipping.
            || !heroAppeared
    }
    // False while another tab is selected. TabView keeps Home mounted (and
    // its TimelineViews ticking) behind the other tabs — three Canvas
    // layers repainting at 30fps under the Ask Cavnar chat was pure wasted
    // main-thread time on the exact screen that needs it for scrolling.
    var tabVisible: Bool = true

    var body: some View {
        NavigationStack(path: $path) {
            ZStack(alignment: .top) {
                HomeObsidianField(paused: backgroundMotionPaused)

                // A custom card, not .confirmationDialog — moving that
                // modifier down from the screen root onto the deck card's
                // own frame (an earlier attempt at this) had zero effect on
                // where it actually rendered, confirming its on-screen
                // position isn't governed by tree attachment the way an
                // .overlay's is. This is fully our own view, so its
                // position, its haptics, and its own "Working…" state are
                // all things we actually control rather than delegating to
                // system chrome that wasn't behaving as documented.
                if let item = pendingPublish {
                    publishConfirmCard(item)
                        .zIndex(3)
                        .transition(.opacity.combined(with: .scale(scale: 0.95)))
                }

                // The reader lets an issue push scroll Home to the issue
                // (parity audit #11), and the Home tab's badge to Needs you.
                ScrollViewReader { scrollProxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        if let summary = viewModel.summary {
                            // "Decide, then more" (iOS readability round,
                            // 10/8/26, #4): the glance, what is about to go
                            // out, the night's own card, the one thing, ONE
                            // ranked Needs you, the brief, DNA — then one
                            // closed More group. The chips, the kind holds
                            // and the activity ticker are gone from Home.
                            hero(summary)

                            // What's on screen came from the device cache
                            // and is old enough to say so (audit 6.5).
                            if let notice = viewModel.stalenessNotice {
                                HomeMixedText.make(notice, role: .secondary, color: .cavnarAmber)
                                    .frame(maxWidth: .infinity)
                                    .multilineTextAlignment(.center)
                                    .padding(.horizontal, 24)
                                    .padding(.top, 10)
                            }

                            // Something Cavnar AI is about to send on its
                            // own, with Undo — only while something is
                            // queued (#40).
                            if aiActivity.activity?.queued?.isEmpty == false || aiActivity.undoneNote != nil {
                                HomeQueuedBanner(viewModel: aiActivity)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 18)
                                    .belowFold(heroAppeared, delay: 0.08)
                            }

                            // The day's slot: the close-out after 8pm, last
                            // night's report before noon (#37) — directly
                            // under the hero, what the owner opened the app
                            // for at that hour.
                            if summary.localIsEvening {
                                HomeCloseOutCard(viewModel: followThrough)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 24)
                                    .belowFold(heroAppeared, delay: 0.1)
                            } else if Self.isMorning(summary), lastNight.night != nil {
                                HomeLastNightCard(viewModel: lastNight, open: { path.append($0) },
                                                  localNow: summary.localNow)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 24)
                                    .belowFold(heroAppeared, delay: 0.1)
                            }

                            // The glance (#94): three fixed tiles and data
                            // health in words. The net tile leaves when the
                            // report's own card is right above it.
                            HomeKPIRow(
                                tiles: HomeKPIRow.tiles(
                                    modules: summary.modules, charts: summary.charts,
                                    night: Self.isMorning(summary) ? nil : lastNight.night,
                                    nightKicker: lastNight.kicker(localNow: summary.localNow)),
                                health: HomeKPIRow.healthLine(
                                    health: summary.dataHealth, unavailable: summary.freshnessUnavailable,
                                    entries: summary.freshness?.entries ?? []),
                                onOpen: { target in openGlance(target, in: summary) },
                                onOpenDataHealth: { showingDataHealth = true })
                                .padding(.horizontal, 20)
                                .padding(.top, 18)
                                .belowFold(heroAppeared, delay: 0.12)

                            // Find or ask — the command sheet, on Home too.
                            findOrAsk
                                .padding(.horizontal, 20)
                                .padding(.top, 10)
                                .belowFold(heroAppeared, delay: 0.12)

                            if summary.quietHoursActive {
                                quietHoursBanner(summary)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 16)
                                    .belowFold(heroAppeared, delay: 0.14)
                            }

                            // The updated Privacy Policy and Terms, owed an
                            // account holder for 30 days (policy_notice) —
                            // dismissed once, for this login everywhere.
                            if let notice = summary.policyNotice {
                                HomePolicyNoticeCard(notice: notice,
                                                     onDismissed: { Task { await viewModel.load() } })
                                    .id(notice.key)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 16)
                                    .belowFold(heroAppeared, delay: 0.14)
                            }

                            // Monday: the weekly receipts, short, in the
                            // day's slot (the rest of the week they are
                            // proof, in More).
                            if summary.localIsMonday, let receipts = summary.weeklyReceipts, !receipts.isEmpty {
                                HomeWeeklyReceipts(receipts: receipts)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 24)
                                    .belowFold(heroAppeared, delay: 0.16)
                            }

                            // Today's focus (#6): the finding, else the most
                            // urgent item, else the top recommendation — the
                            // web's order (parity #1) — never before the
                            // day's reads have landed.
                            let lead = focusLead(summary)
                            if let lead {
                                HomeOneThingCard(viewModel: followThrough, lead: lead,
                                                 busy: viewModel.isPublishingReplies,
                                                 onPrimary: { item in primaryAction(item, in: summary) },
                                                 onChanged: { Task { await viewModel.load() } })
                                    .padding(.horizontal, 20)
                                    .padding(.top, 28)
                                    .belowFold(heroAppeared, delay: 0.18)
                            }

                            // Needs you (#5): every decision in one ranked
                            // list — attention items and links, issues,
                            // what is still open, the brief's actions,
                            // goals, check-ins, flags and shortcuts.
                            attentionSection(summary, items: attentionItems(summary, lead: lead), lead: lead,
                                             scrollProxy: scrollProxy)
                                .padding(.horizontal, 20)
                                .padding(.top, 30)
                                .belowFold(heroAppeared, delay: 0.22)

                            if summary.isFresh {
                                freshStart(summary)
                            }

                            // Last night's report after noon (before noon it
                            // leads, above); it shows nothing for a login or
                            // location without one.
                            if !Self.isMorning(summary), lastNight.night != nil {
                                HomeLastNightCard(viewModel: lastNight, open: { path.append($0) },
                                                  localNow: summary.localNow)
                                    .padding(.horizontal, 20)
                                    .padding(.top, 30)
                                    .belowFold(heroAppeared, delay: 0.3)
                            }
                            // The brief's reads; its actions are Needs you
                            // rows, and it leaves out what the page already
                            // says (web `hbShownKeys`, parity #3).
                            HomeDayCard(viewModel: day,
                                        shownKeys: briefShownKeys(summary, lead: lead),
                                        onOpenNav: { nav in open(nav: nav, module: "home", in: summary) })
                                .padding(.horizontal, 20)
                                .padding(.top, 30)
                                .belowFold(heroAppeared, delay: 0.32)

                            // Restaurant DNA under the day's read (owner,
                            // 10/8/26: it sat at the foot of Results where
                            // no one saw it). Opens the full DNA screen.
                            DNAHomeCard(model: dnaModel) { showingDNA = true }
                                .padding(.horizontal, 20)
                                .padding(.top, 30)
                                .belowFold(heroAppeared, delay: 0.36)

                            // MORE — the recommendations, the measured
                            // results and How you compare, closed, with the
                            // figure still on the closed row.
                            moreGroup(summary, lead: lead)
                                .padding(.top, 34)
                                // Its four reads (what worked, the value
                                // figures, what got better, last month) wait
                                // until More nears the screen (parity #20).
                                .onScrolledNear { Task { await followThrough.loadResults() } }
                                .belowFold(heroAppeared, delay: 0.42)

                            // The tab bar's safe area is the ScrollView's;
                            // this is breathing room under the last row.
                            Color.clear.frame(height: CavnarSpace.l)
                        } else if viewModel.isLoading {
                            heroSkeleton
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).cavnarText(.body)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 80)
                            .frame(maxWidth: .infinity)
                        }
                    }
                    // One readable column on an iPad, centred (#99) — §11b's
                    // order holds at every width.
                    .cavnarReadableWidth()
                    // The day's reads — the follow-through queue, the brief
                    // and open issues, last night's report, what is about
                    // to go out — are read only after Home's own fetch,
                    // and again after each one (the queue drops what Home
                    // already showed today, which it learns from that
                    // fetch, H-23).
                    .task(id: viewModel.lastLoadedAt) {
                        guard viewModel.lastLoadedAt != nil else { return }
                        async let ft: Void = followThrough.load()
                        async let d: Void = day.load()
                        async let n: Void = lastNight.load()
                        async let a: Void = aiActivity.load()
                        _ = await (ft, d, n, a)
                    }
                }
                // A pull rebuilds the brief (fresh=1) instead of the
                // server's 60-second copy — the web's hbLoad(true) (#90).
                .cavnarEmberRefreshable { await viewModel.load(fresh: true) }
                // The Home tab's badge leads to Needs you: arriving on Home
                // with something urgent scrolls to the list.
                .onChange(of: tabVisible) { _, visible in
                    guard visible, chrome.notificationsBadge.urgentCount > 0 else { return }
                    withAnimation(.easeInOut(duration: 0.35)) {
                        scrollProxy.scrollTo(HomeNeedsYou.anchor, anchor: .top)
                    }
                }
                // Anchored to the whole screen, not to HomeActionDeck —
                // publishing can be the LAST needs-attention item, and the
                // reload that follows a successful publish (inside
                // publishAllReplies(), before this label is even set) can
                // swap the deck out for AllClearRow the instant the list
                // empties. The overlay used to live on the deck itself, so
                // it lost its home in the view tree at the exact moment it
                // needed to appear — this was the "nothing happened" bug:
                // the publish worked, but its own confirmation had nowhere
                // left to render.
                .cavnarPostedOverlay(postedLabel) { postedLabel = nil }
                // The milestone moment. Screen-level for the same reason
                // the posted overlay is: the card that triggered it can be
                // swapped out by the reload underneath. It appears at most
                // once ever per milestone — the server row is the
                // guarantee — and dismissing it marks it seen everywhere,
                // including the web.
                .overlay {
                    if let milestone = followThrough.pendingMilestone {
                        MilestoneMoment(milestone: milestone) {
                            Task { await followThrough.markMilestoneSeen(milestone) }
                        }
                        .transition(.opacity)
                    }
                }
                }
            }
            .animation(.easeOut(duration: 0.2), value: pendingPublish != nil)
            .animation(.easeOut(duration: 0.25), value: followThrough.pendingMilestone?.key)
            .navigationDestination(for: ModuleRoute.self) { route in
                ModuleDestinationView(route: route)
            }
            .navigationDestination(for: DailyReportRoute.self) { route in
                switch route {
                case .report(let date, let follow):
                    DailyReportView(date: date, follow: follow)
                case .list:
                    DailyReportListView(open: { path.append($0) })
                case .week(let date):
                    DailyReportWeekView(date: date, open: { path.append($0) })
                case .period(let date):
                    DailyReportWeekView(date: date, period: true, open: { path.append($0) })
                }
            }
            // A tapped `dsr` push (or its row in the notification list)
            // opens that night's report on Home's stack — on a fresh
            // stack, so Back lands on Home rather than wherever it was.
            // One hop later, so RootView's own observer (which switches to
            // this tab) sees the route before it is consumed here.
            .onChange(of: deepLinkRouter.pendingDailyReport) { _, route in
                if route != nil { Task { @MainActor in openPendingDailyReport() } }
            }
            .onAppear { openPendingDailyReport(); takePendingIssue() }
            // An issue push (or a row naming one): back to Home's top
            // level, then the day card scrolls to the issue and pulses it.
            .onChange(of: deepLinkRouter.pendingIssueId) { _, id in
                if id != nil { Task { @MainActor in takePendingIssue() } }
            }
            .sensoryFeedback(.impact(weight: .medium), trigger: navHapticTrigger) { _, _ in AppPreferences.hapticsEnabledSnapshot }
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
                    // The one bell (AppChrome): the sheet opens at once on
                    // its own skeleton — the first open used to wait for the
                    // network before anything appeared (#32).
                    CavnarBellButton()
                }
                if sessionStore.currentUser?.isOwner == true {
                    cavnarToolbarItem(placement: .topBarTrailing) {
                        Button {
                            Haptic.light()
                            chrome.showingLocationSwitcher = true
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
            .sheet(isPresented: $showingValueDetail) {
                valueDetailSheet
            }
            .sheet(isPresented: $showingDataHealth) {
                DataHealthSheet(summary: viewModel.summary?.dataHealth)
            }
            .sheet(isPresented: $showingRecord, onDismiss: { Task { await viewModel.load() } }) {
                RecommendationHistoryView(kindHolds: viewModel.summary?.kindHolds?.items ?? [],
                                          quieter: viewModel.summary?.quieter ?? [],
                                          onRestoreKind: { kind in await followThrough.restoreKind(kind) })
            }
            .fullScreenCover(isPresented: $showingDNA) {
                RestaurantDNAScreen(model: dnaModel)
            }
            .sheet(item: $evidenceLink, onDismiss: { Task { await followThrough.load() } }) { link in
                HomeLinkEvidenceSheet(link: link)
            }
            .task { await viewModel.load() }
            // A tapped push about another location switched to it
            // (DeepLinkRouter), or the switcher did — Home shows that
            // location now, not the old one. RootView re-reads the badge.
            .onChange(of: deepLinkRouter.locationSwitches) { _, _ in
                Task { await viewModel.load() }
            }
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
            Text(Self.heroDate(localNow: summary.localNow))
                .cavnarText(.kicker)
                .shadow(color: .black.opacity(0.5), radius: 3, x: 0, y: 1)

            heroHeadline(summary)
                .fixedSize(horizontal: false, vertical: true)

            overnightLine(summary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)

            // No "Since your last visit" line: the web removed it from the
            // header on 9/26/26 at the owner's call, and the phone follows
            // (parity audit #90). The changes still reach the brief.
        }
        .frame(maxWidth: .infinity)
        .multilineTextAlignment(.center)
        .padding(.horizontal, 24)
        .padding(.top, 38)
        .opacity(heroAppeared ? 1 : 0)
        .offset(y: heroAppeared ? 0 : 26)
        .animation(Self.introAnimation, value: heroAppeared)
        .onAppear { DebugFrameWatchdog.mark("hero onAppear"); onHeroAppear() }
    }

    @ViewBuilder
    private func heroHeadline(_ summary: HomeSummary) -> some View {
        if let headline = Self.briefHeadline(summary) {
            // The 3-second answer (density #1): the brief's own headline —
            // web Home's H1 — in its tone, under the owner's name. The
            // slogan it replaced said nothing about the restaurant.
            VStack(spacing: 6) {
                Text(greetingName(summary))
                    .cavnarText(.lead, color: .cavnarEmber2)
                Text(headline)
                    .cavnarText(.title, color: Self.briefToneColor(summary.brief?.tone))
                    // The 3-second answer reads at the phone's own size, past
                    // the app's xxxLarge cap: one centred, wrapping line
                    // with nothing beside it. accessibility2, not 3 — at 27pt
                    // a title2-relative headline is already the largest
                    // type on the screen.
                    .cavnarReadingSize(upTo: .accessibility2)
            }
            .shadow(color: .black.opacity(0.45), radius: 4, x: 0, y: 2)
            .accessibilityElement(children: .combine)
        } else {
            // cavnarEmber2 for the name (the deeper cavnarEmber sank into the
            // old aurora; on the field it's about the glow, not contrast) plus
            // a shadow on the whole line — Text concatenation only carries
            // font/colour per segment, not per-segment view modifiers.
            (Text(greetingName(summary)).foregroundStyle(Color.cavnarEmber2)
                + Text(Self.heroTail(liveSources: summary.monitoring?.countLive)).foregroundStyle(Color.cavnarInk))
                .cavnarText(.title)
                .shadow(color: .black.opacity(0.45), radius: 4, x: 0, y: 2)
        }
    }

    /// The brief's headline, trimmed; nil when the server sent none (an
    /// older server, or a brief that couldn't be built) — the hero then
    /// keeps its greeting line, which only claims AI over a live source.
    static func briefHeadline(_ summary: HomeSummary) -> String? {
        guard let h = summary.brief?.headline?.trimmingCharacters(in: .whitespacesAndNewlines),
              !h.isEmpty else { return nil }
        return h
    }

    /// home_brief's headline tones: "bad" red, "warn" amber, "good" green;
    /// "neutral" (and anything unknown) reads in ink — a state, not an alarm.
    static func briefToneColor(_ tone: String?) -> Color {
        switch tone {
        case "bad", "critical": return .cavnarRed
        case "warn": return .cavnarAmber
        case "good": return .cavnarGreen
        default: return .cavnarInk
        }
    }

    /// The hero's claim about the restaurant is conditional on a live
    /// source under it (NS1 #14): with nothing connected yet, or nothing
    /// current, it says what comes next instead.
    static func heroTail(liveSources: Int?) -> String {
        (liveSources ?? 0) > 0 ? " \u{2014} your restaurant is running on AI."
                               : " \u{2014} connect a source and Cavnar AI starts reading it."
    }

    private func greetingName(_ summary: HomeSummary) -> String {
        guard let username = summary.username, !username.isEmpty else { return "Welcome back" }
        return username.prefix(1).uppercased() + username.dropFirst()
    }

    /// "Overnight: 3 replies drafted · 2 flagged" — Ink2 at body size,
    /// figures in ember; no "{Restaurant} · " prefix (the switcher and the
    /// hero already say where). Before noon it's "Overnight"; after, "Since
    /// yesterday" (the window is the last 24h either way). `answered` counts
    /// drafts written (mobile_api._home_overnight), so the line says
    /// drafted — never posted. With nothing to report it says so.
    private func overnightLine(_ summary: HomeSummary) -> Text {
        // The restaurant's own hour (local_now), not the phone's clock.
        let hour = summary.localHour ?? Calendar.current.component(.hour, from: Date())
        return HomeMixedText.make(Self.overnightText(summary.overnight, hour: hour), role: .body,
                                  numberColor: .cavnarEmber2)
    }

    static func overnightText(_ overnight: HomeOvernight?, hour: Int) -> String {
        guard let o = overnight, o.answered + o.flagged > 0 else {
            return "All quiet since yesterday."
        }
        var bits: [String] = []
        if o.answered > 0 { bits.append("\(o.answered) \(o.answered == 1 ? "reply" : "replies") drafted") }
        if o.flagged > 0 { bits.append("\(o.flagged) flagged") }
        return (hour < 12 ? "Overnight: " : "Since yesterday: ") + bits.joined(separator: " \u{00B7} ")
    }

    /// "MONDAY · 9/21/26" — the weekday for orientation, the date in the
    /// one owner-facing form, M/D/YY (CLIENT-45) — on the restaurant's own
    /// clock (the server's local_now, parity audit #90): an owner in another
    /// time zone, or a phone set wrong, saw a different day from the brief
    /// under it. The phone's clock only when the server sent none.
    static func heroDate(localNow: String?) -> String {
        if let day = localDay(localNow) {
            return "\(day.weekday) · \(CavnarDate.mdy(day.iso))".uppercased()
        }
        let now = Date()
        let weekday = now.formatted(Date.FormatStyle(locale: Locale(identifier: "en_US")).weekday(.wide))
        return "\(weekday) · \(CavnarDate.mdy(now))".uppercased()
    }

    /// The restaurant's calendar day from local_now ("2026-09-21T07:12:00-05:00")
    /// and its weekday name, read without any time-zone conversion.
    static func localDay(_ localNow: String?) -> (iso: String, weekday: String)? {
        guard let s = localNow, s.count >= 10 else { return nil }
        let iso = String(s.prefix(10))
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(secondsFromGMT: 0)
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: iso) else { return nil }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(secondsFromGMT: 0)!
        let symbols = DateFormatter()
        symbols.locale = Locale(identifier: "en_US")
        let names = symbols.weekdaySymbols ?? []
        let i = cal.component(.weekday, from: d) - 1
        guard names.indices.contains(i) else { return nil }
        return (iso, names[i])
    }

    // MARK: - Sections

    /// The first look and readiness — above the day, for an account with
    /// nothing connected only. Readiness hides itself once complete.
    @ViewBuilder
    private func freshStart(_ summary: HomeSummary) -> some View {
        if let look = summary.firstLook, !look.isEmpty {
            HomeFirstLook(lines: look)
                .padding(.horizontal, 20)
                .padding(.top, 30)
                .belowFold(heroAppeared, delay: 0.6)
        }
        if let ready = summary.readiness {
            HomeReadinessCard(readiness: ready) { module in
                navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
            }
            .padding(.horizontal, 20)
            .padding(.top, 30)
            .belowFold(heroAppeared, delay: 0.66)
        }
    }

    /// M/D/YY from the restaurant's own clock — the page's one date format.
    private func dayLabel(_ summary: HomeSummary) -> String? {
        guard let s = summary.localNow, s.count >= 10 else { return nil }
        let p = s.prefix(10).split(separator: "-")
        guard p.count == 3, let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        return "\(m)/\(d)/\(p[0].suffix(2))"
    }

    /// What the one-thing card leads with (HomeFocusLead.pick, the web's
    /// `renderFocus` order): the finding, else the first attention item,
    /// else the top recommendation; nil until the day's reads land.
    private func focusLead(_ summary: HomeSummary) -> HomeFocusLead? {
        HomeFocusLead.pick(loaded: followThrough.crossLoaded,
                           hasFinding: !(followThrough.fixFirst?.what ?? "").isEmpty,
                           attention: summary.needsAttention,
                           recommendations: summary.recommendations ?? [])
    }

    /// Needs attention under the lead: without the item the one-thing card
    /// took (it is not said twice), with every cross-module link as a row.
    private func attentionItems(_ summary: HomeSummary, lead: HomeFocusLead?) -> [NeedsAttentionItem] {
        var items = summary.needsAttention
        if case .attention? = lead { items = Array(items.dropFirst()) }
        return items + followThrough.linkItems
    }

    /// The recommendations grid, less the one the one-thing card leads with.
    private func recommendationsShown(_ summary: HomeSummary, lead: HomeFocusLead?) -> [HomeRecommendation] {
        let recs = summary.recommendations ?? []
        if case .recommendation? = lead { return Array(recs.dropFirst()) }
        return recs
    }

    static func attentionTitle(hasOneThing: Bool) -> String {
        hasOneThing ? "Then these" : "Start here"
    }

    /// Before noon on the restaurant's clock — last night's report leads.
    static func isMorning(_ summary: HomeSummary) -> Bool {
        (summary.localHour ?? Calendar.current.component(.hour, from: Date())) < 12
    }

    /// What the page above the brief already says, by job (web
    /// `hbShownKeys`): every attention item and link, and the lead's key.
    private func briefShownKeys(_ summary: HomeSummary, lead: HomeFocusLead?) -> Set<String> {
        HomeBriefFilter.shownKeys(attention: summary.needsAttention + followThrough.linkItems,
                                  focusKey: followThrough.fixFirst?.answerKey ?? lead?.key)
    }

    /// Needs you (#5): the attention items less the one the focus card
    /// leads with (the server records the focus plus the first rows as
    /// shown, home_brief HOME_ATTENTION_SHOWN), the links, the shortcuts no
    /// row carries, the brief's own actions, issues, what is still open,
    /// goals, check-ins and flags — one ranked list. "Start here" is gone:
    /// the focus card above says it.
    @ViewBuilder
    private func attentionSection(_ summary: HomeSummary, items: [NeedsAttentionItem], lead: HomeFocusLead?,
                                  scrollProxy: ScrollViewProxy) -> some View {
        let shown = briefShownKeys(summary, lead: lead)
        let leadIsAttention: Bool = { if case .attention? = lead { return true } else { return false } }()
        HomeNeedsYou(
            items: items,
            leadTookAttention: leadIsAttention,
            quick: HomeQuickAction.unsaid(summary.quickActions?.items ?? [], attention: summary.needsAttention),
            briefActions: HomeBriefFilter.split(day.lines, shown: shown, hasIssues: !day.issues.isEmpty).act,
            day: day,
            followThrough: followThrough,
            busyPublishing: viewModel.isPublishingReplies,
            // Nothing flagged on stale or no data is not a clean bill (NS1 #8).
            notClearReason: OwnerCopy.allClear(attentionEmpty: true, monitoring: summary.monitoring).reason,
            leadTookOnlyItem: items.isEmpty && !summary.needsAttention.isEmpty,
            onPrimary: { item in primaryAction(item, in: summary) },
            onSecondary: { item in
                // "Read them first" is the queue to read, not the inbox.
                open(nav: item.isPublishAction ? (item.nav ?? "reviews?filter=pending") : item.nav,
                     module: item.module, in: summary)
            },
            // Not today / hide, recorded server-side so the same item is
            // quiet in the brief and the queue too. Never offered for a
            // critical item (the server sends dismissable=false). A second
            // hide asks why first, as the web does (#42).
            onDismiss: { item, kind in
                if kind == "recommendation", (item.timesHidden ?? 0) >= 1 {
                    attentionAskingWhy = item
                    showingAttentionWhy = true
                    return
                }
                Task {
                    // The server's sentence for what the answer does (memory
                    // round 9/29/26) in the screen's posted check — the row
                    // leaves on the reload under it.
                    if let said = await followThrough.answerAttention(item, kind: kind) {
                        postedLabel = said
                        await viewModel.load()
                    }
                }
            },
            onQuick: { q in quickAction(q, in: summary) },
            onOpenNav: { nav in open(nav: nav, module: "home", in: summary) },
            onOpenPath: { nav in open(nav: nav.raw, module: "home", in: summary) },
            onOpenModule: { module in
                navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
            },
            onChanged: { Task { await viewModel.load() } },
            focusIssue: issueFocus,
            onFocus: { id in
                withAnimation(.easeInOut(duration: 0.35)) {
                    scrollProxy.scrollTo(HomeDayCard.issueAnchor(id), anchor: .center)
                }
            }
        )
        .recReasonDialog(isPresented: $showingAttentionWhy,
                         title: "You\u{2019}ve hidden this before \u{2014} why?",
                         message: "Tell Cavnar AI why, so it stops raising it.",
                         skipLabel: "Just hide it for two weeks",
                         onSkip: { answerAttentionWhy(kind: "recommendation", reason: nil) },
                         onPick: { reason in answerAttentionWhy(kind: "not_for_us", reason: reason) })
    }

    /// Find or ask anything — the command sheet, one tap from Home.
    private var findOrAsk: some View {
        Button {
            Haptic.light()
            CommandSheetRequest.request()
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                Image(systemName: "magnifyingglass")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityHidden(true)
                Text("Find or ask anything")
                    .cavnarText(.body)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, CavnarSpace.m)
            .frame(maxWidth: .infinity, minHeight: 44)
            .background(Color.cavnarPaper2.opacity(0.85), in: Capsule())
            .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Find or ask")
    }

    /// A glance tile's place: the night's report, or the module.
    private func openGlance(_ target: HomeKPIRow.Target, in summary: HomeSummary) {
        switch target {
        case .report(let date): path.append(DailyReportRoute.report(date: date))
        case .module(let key): navigate(to: ModuleRoute(key: key, label: moduleLabel(key, in: summary)))
        }
    }

    /// MORE (#4): the recommendations, then Results — the measured band,
    /// How you compare (#96), the trends, the receipts off Monday and what
    /// the owner's changes did — and, before 8pm, the handoff.
    private func moreGroup(_ summary: HomeSummary, lead: HomeFocusLead?) -> some View {
        HomeMoreDisclosure(
            line: HomeResultsSummary.line(
                headline: summary.valueHeadline,
                improved: followThrough.value?.delivered?.wins,
                worse: summary.value?.worsened?.count ?? followThrough.value?.delivered?.worsened?.count),
            tone: HomeResultsSummary.tone(headline: summary.valueHeadline)
        ) {
            VStack(alignment: .leading, spacing: 0) {
                // What Cavnar AI recommends, less the one the focus card
                // leads with, and the undo for the last one hidden.
                let recs = recommendationsShown(summary, lead: lead)
                let hidden = summary.dismissed?.items.first
                if !recs.isEmpty || hidden != nil {
                    HomeRecommendations(recommendations: recs,
                                        viewModel: followThrough,
                                        assignees: summary.assignees ?? [],
                                        onOpenModule: { module in
                        navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
                    }, onChanged: { Task { await viewModel.load() } },
                                        restorable: hidden,
                                        onRestore: { rec in await viewModel.restoreHidden(rec) })
                    .padding(.horizontal, 20)
                }
                // The record, with the kinds held back and the quieter kinds
                // (moved off Home, #96).
                recordRow(summary)
                    .padding(.horizontal, 20)
                    .padding(.top, 8)

                Text("Results")
                    .cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
                    .padding(.horizontal, 20)
                    .padding(.top, 30)
                    .padding(.bottom, 12)
                HomeValueBand(
                    total: summary.totalValueDelivered,
                    history: summary.valueHistory,
                    measuredOn: summary.valueByModule ?? [],
                    headline: summary.valueHeadline,
                    scope: summary.value?.scope,
                    revealed: heroAppeared
                ) {
                    Haptic.light()
                    showingValueDetail = true
                }

                // How you compare (Benchmarking #23) — inside Results.
                HomeBenchmarkStrip(onOpenModule: { module in
                    navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
                })
                .padding(.horizontal, 20)
                .padding(.top, 22)

                // The trend behind each glance tile (web `renderSignals`).
                if let charts = summary.charts, !charts.isEmpty {
                    HomeSignals(charts: charts) { module in
                        navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 22)
                }

                // The weekly receipts, outside Monday: proof, so they sit
                // with the results.
                if !summary.localIsMonday, let receipts = summary.weeklyReceipts, !receipts.isEmpty {
                    HomeWeeklyReceipts(receipts: receipts)
                        .padding(.horizontal, 20)
                        .padding(.top, 30)
                }

                if HomeFollowThrough.hasResults(followThrough) {
                    HomeFollowThrough(viewModel: followThrough, part: .results) { module in
                        navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 30)
                }

                // Before 8pm the handoff waits here; after, it leads Home.
                if !summary.localIsEvening {
                    HomeCloseOutCard(viewModel: followThrough)
                        .padding(.horizontal, 20)
                        .padding(.top, 30)
                }
            }
        }
    }

    /// "Recommendation history · 2 kinds on hold ›" — the record, where the
    /// "Keep suggesting these?" questions and the quieter kinds live now.
    private func recordRow(_ summary: HomeSummary) -> some View {
        let holds = summary.kindHolds?.items.count ?? 0
        let quieter = summary.quieter?.count ?? 0
        var bits: [String] = []
        if holds > 0 { bits.append("\(holds) to answer") }
        if quieter > 0 { bits.append("\(quieter) quieter") }
        return Button {
            Haptic.light()
            showingRecord = true
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                (Text("Recommendation history").font(.cavnar(.label)).foregroundColor(.cavnarInk)
                 + Text(bits.isEmpty ? "" : " \u{00B7} " + bits.joined(separator: ", "))
                    .font(.cavnar(.secondary)).foregroundColor(.cavnarInk2))
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk2)
                    .accessibilityHidden(true)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens what Cavnar AI suggested, what you did, and the kinds it is holding back")
    }

    /// A shortcut row's tap: a publish asks first, as an attention row's
    /// does; anything else opens its place.
    private func quickAction(_ q: HomeQuickAction, in summary: HomeSummary) {
        Haptic.light()
        if q.kind == "publish_replies" {
            pendingPublish = NeedsAttentionItem(
                type: "reviews_awaiting_approval", module: q.module ?? "reviews",
                title: q.label, detail: "", cta: q.label, secondary: nil,
                action: "publish_replies", recKey: nil, dismissable: false,
                timesHidden: nil, count: q.count, evidence: nil, confidence: nil)
            loadPublishProposal()
        } else {
            open(nav: q.nav, module: q.module ?? "home", in: summary)
        }
    }

    /// The second hide's answer: a reason (sent as not_for_us with its
    /// code) or "just hide it" (the ordinary two-week hide).
    private func answerAttentionWhy(kind: String, reason: RecReason?) {
        guard let item = attentionAskingWhy else { return }
        attentionAskingWhy = nil
        Task {
            if let said = await followThrough.answerAttention(item, kind: kind, reasonCode: reason?.code) {
                postedLabel = said
                await viewModel.load()
            }
        }
    }

    private var valueDetailSheet: some View {
        NavigationStack {
            ScrollView {
                if let summary = viewModel.summary {
                    ValueChartCard(totalValue: summary.totalValueDelivered, history: summary.valueHistory,
                                   headline: summary.valueHeadline)
                        .padding(20)
                    // Worth — what was measured, estimated and surfaced,
                    // never added together — lives here now, beside the
                    // chart it explains, instead of restating the band on
                    // Home (density #4). No homeLoadedAt: Home's own
                    // follow-through instance already loaded it.
                    HomeFollowThrough(viewModel: followThrough, part: .worth) { module in
                        showingValueDetail = false
                        navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
                    }
                    .padding(.horizontal, 20)
                    .padding(.bottom, 24)
                }
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                cavnarTitleToolbar("Measured results")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        showingValueDetail = false
                    } label: {
                        Text("Done")
                            .cavnarText(.label, color: .cavnarEmber2)
                            .cavnarHitTarget()
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
            (Text("Notifications quiet").font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                + Text(quietHoursEndText(summary)).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk2))
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
        // "6:45pm", the house time (DESIGN_SYSTEM → Dates and times); the
        // parser's zone is the phone's, so reading it back on the same zone
        // gives the stored wall-clock time.
        return " until \(CavnarDate.time(date)) — text, email, and push are all holding until then."
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
            loadPublishProposal()
        } else if item.action == "link_evidence" {
            // A cross-module link's row: its Evidence, To confirm and Could
            // also be, with Done / Not for us (the web's "Evidence" panel).
            Haptic.light()
            evidenceLink = followThrough.link(for: item)
        } else {
            open(nav: item.nav, module: item.module, in: summary)
        }
    }

    /// The confirm card lists every reply that would post (parity #2):
    /// read it while the card shows its shimmer; with no proposal (an older
    /// server) the card falls back to the count.
    private func loadPublishProposal() {
        publishProposal = nil
        publishProposalLoading = true
        publishAsk = AskCavnarViewModel()
        Task {
            let p = await viewModel.proposePublish()
            guard pendingPublish != nil else { return }
            publishProposal = p
            publishProposalLoading = false
        }
    }

    /// The proposal's Confirm went through: the card closes, Home re-reads.
    private func publishConfirmed() {
        pendingPublish = nil
        publishProposal = nil
        Haptic.success()
        postedLabel = "Replies approved"
        Task { await viewModel.load() }
    }

    private func closePublish() {
        pendingPublish = nil
        publishProposal = nil
        publishProposalLoading = false
    }

    /// A card's destination: its nav path when the server sent one — the
    /// filter, section or item it is about, pushed on Home's own stack when
    /// it is a module screen, handed to the router otherwise (Ask, the daily
    /// report, a queued send) — else the module's top as before (#3).
    private func open(nav raw: String?, module: String, in summary: HomeSummary) {
        if let nav = NavPath(raw) {
            if let route = ModuleRoute.from(nav, labelFor: { moduleLabel($0, in: summary) }) {
                navigate(to: route)
            } else {
                deepLinkRouter.open(nav)
            }
            return
        }
        navigate(to: ModuleRoute(key: module, label: moduleLabel(module, in: summary)))
    }

    /// The card STAYS open (showing "Working…" via viewModel
    /// .isPublishingReplies) for the whole network round trip now, instead
    /// of dismissing the instant the button is tapped — a tap that then
    /// waits in silence for however long the request takes was the
    /// "nothing happened" complaint just as much as the missing overlay
    /// was. It closes only once there's an actual outcome, at which point
    /// the success checkmark overlay (cavnarPostedOverlay, screen-level)
    /// takes over.
    private func publishReplies() async {
        guard pendingPublish != nil else { return }
        // A nil result means the call failed — APIClient has already played
        // the error haptic, and the deck stays exactly as it was.
        guard let result = await viewModel.publishAllReplies(limit: pendingPublish?.count), result.approved > 0 else {
            pendingPublish = nil
            return
        }
        pendingPublish = nil
        Haptic.success()
        if result.posted > 0 {
            postedLabel = "Published \(result.posted) to Google"
        } else {
            postedLabel = "Approved \(result.approved) \(result.approved == 1 ? "reply" : "replies")"
        }
    }

    /// The confirm step itself — a plain custom card, centered, dimming
    /// the screen behind it. Shows "Working…" for the duration of the
    /// publish call (see publishReplies()) rather than disappearing the
    /// instant it's tapped.
    private func publishConfirmCard(_ item: NeedsAttentionItem) -> some View {
        ZStack {
            Color.black.opacity(0.55)
                .ignoresSafeArea()
                .onTapGesture {
                    guard !viewModel.isPublishingReplies else { return }
                    closePublish()
                }
            if publishProposalLoading || publishProposal != nil {
                publishProposalCard
            } else {
                publishCountCard(item)
            }
        }
    }

    /// The same confirm card the web and Ask render: what would post, each
    /// reply in its own words, and how many the check holds back; Confirm
    /// runs the approve through Ask's route (parity #2).
    private var publishProposalCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let p = publishProposal {
                ScrollView {
                    ProposalCard(proposal: p, viewModel: publishAsk, onDone: { publishConfirmed() })
                }
                .frame(maxHeight: 460)
            } else {
                CavnarShimmerText(text: "Reading the replies\u{2026}", color: Color.cavnarInk)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 18)
            }
            Button {
                Haptic.light()
                closePublish()
            } label: {
                Text("Close")
                    .cavnarText(.label, color: .cavnarInk2)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.plain)
        }
        .padding(20)
        .frame(maxWidth: 380)
        .background(Color.cavnarPaper2)
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        .shadow(color: .black.opacity(0.45), radius: 24, y: 12)
        .padding(.horizontal, 20)
    }

    /// The count-only confirm — an older server without /command/propose.
    private func publishCountCard(_ item: NeedsAttentionItem) -> some View {
        ZStack {
            VStack(spacing: 18) {
                Text(item.cta ?? "Publish replies")
                    .cavnarText(.headline)
                    .multilineTextAlignment(.center)
                Text("Each reply was drafted in your voice. Google-connected replies post right away; the rest are marked approved.")
                    .cavnarText(.body)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)

                if viewModel.isPublishingReplies {
                    CavnarShimmerText(text: "Publishing…", color: Color.cavnarInk)
                        .padding(.top, 2)
                } else {
                    VStack(spacing: 10) {
                        Button {
                            // Fires the instant the tap lands, before the
                            // network call even starts — the confirm never
                            // reads as "did that register?" regardless of
                            // how long the request takes.
                            Haptic.medium()
                            Task { await publishReplies() }
                        } label: {
                            Text(item.cta ?? "Publish")
                                .cavnarText(.label, color: .white)
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 13)
                                .background(
                                    LinearGradient(colors: [Color.cavnarEmber2, Color.cavnarEmber], startPoint: .top, endPoint: .bottom)
                                )
                                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                        }
                        .buttonStyle(.plain)

                        Button {
                            Haptic.light()
                            pendingPublish = nil
                        } label: {
                            Text("Cancel")
                                .cavnarText(.label, color: .cavnarInk2)
                                .frame(maxWidth: .infinity)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .padding(24)
            .frame(maxWidth: 340)
            .background(Color.cavnarPaper2)
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
            .shadow(color: .black.opacity(0.45), radius: 24, y: 12)
            .padding(.horizontal, 36)
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
    /// The router's pending issue becomes the day card's focus; Home pops
    /// back to its own top level first so the issue is on screen.
    private func takePendingIssue() {
        guard let id = deepLinkRouter.pendingIssueId else { return }
        deepLinkRouter.pendingIssueId = nil
        if !path.isEmpty { path = NavigationPath() }
        issueFocus = HomeIssueFocus(id: id)
    }

    private func openPendingDailyReport() {
        guard let route = deepLinkRouter.consumePendingDailyReport() else { return }
        var fresh = NavigationPath()
        fresh.append(route)
        path = fresh
    }

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
            // 0.5s each, spaced ~0.2s apart by the call sites. They used to
            // be 0.25/0.25/0.35/0.45/0.5 against a 0.55s duration, which
            // overlapped so heavily that five sections read as one
            // simultaneous fade rather than a sequence landing top to
            // bottom — two of them were on the same delay outright.
            .animation(.easeOut(duration: 0.5).delay(delay), value: appeared)
    }
}

private extension View {
    func belowFold(_ appeared: Bool, delay: Double) -> some View {
        modifier(BelowFoldReveal(appeared: appeared, delay: delay))
    }
}


/// The web Home's "Let's get set up" card: real completion state per
/// step, tap a step to go do it, ✕ to retire it for good.
