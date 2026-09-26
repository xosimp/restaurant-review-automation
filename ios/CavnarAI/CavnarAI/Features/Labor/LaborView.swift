import SwiftUI
import UIKit

private enum LaborSubTab: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct LaborView: View {
    @State private var showingPublishSchedule = false
    @Environment(SessionStore.self) private var sessionStore
    @Environment(\.scenePhase) private var scenePhase
    @State private var viewModel = LaborViewModel()
    @State private var analyticsViewModel = LaborAnalyticsViewModel()
    // Roster, rules, demand signals and shift requests — the set-up around
    // the generator, kept outside the Overview/Analytics branch for the
    // same reason LaborViewModel is (see scheduleResultExpanded).
    @State private var setupViewModel = ScheduleSetupViewModel()
    @State private var subTab: LaborSubTab = .overview
    @State private var showDataInfo = false
    // The schedule row whose "why this person" is open.
    @State private var explainingRow: ScheduleRow?
    // "How it scored" inside the generated schedule (density #29).
    @State private var showingHowItScored = false
    // The Scheduling setup sheet — roster, availability, demand signals,
    // team strength and shift targets (density #28) — and the person a
    // "person/<key>" link opens over it.
    @State private var showingSetup = false
    @State private var setupFocusPerson: PersonSheetTarget?
    /// The section a link pointed at — "requests", "timeoff", "team",
    /// "schedule", "overtime" (nav.py; friction audit #3). Opened and
    /// scrolled to once the page has loaded, then spent.
    var focusSection: String? = nil
    /// The item the link named inside that section — a person's key for
    /// "person/<key>" (F3-15), which opens their sheet.
    var focusItem: String? = nil
    @State private var focusSpent = false
    @State private var focusPerson: PersonSheetTarget?
    // What was sent, reachable from Labor itself — it lived only under
    // Account → More (friction audit #50).
    @State private var showingScheduleHistory = false

    init(focusSection: String? = nil, focusItem: String? = nil) {
        self.focusSection = focusSection
        self.focusItem = focusItem
    }

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: LaborSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)
                .cavnarRibbonHeaderAnchor()

            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if subTab == .overview {
                            if let stats = viewModel.stats {
                                // AI strip now lives inside heroCard itself
                                // (its own last row, sharing the card's
                                // background/border) — see heroCard's own
                                // comment.
                                heroCard(stats)
                                // Three groups, not thirteen equal rows
                                // (density #28): NEEDS YOU — what staff are
                                // waiting on, the drafted week, time off,
                                // shift requests and overtime, each decided
                                // in place; WHY — the diagnosis and what
                                // drove the hours; SCHEDULING SETUP — one row
                                // that opens a sheet with the roster,
                                // availability, demand signals, team
                                // strength and targets.
                                laborGroupHeader("Needs you")
                                // What staff are waiting on, answered in
                                // place, before any chart (Friction #18).
                                LaborWaitingOnYou(viewModel: viewModel, setupViewModel: setupViewModel) {
                                    setupViewModel.requestsExpanded = true
                                    scrollToReveal(Self.requestsID, proxy: proxy)
                                }
                                .id(Self.waitingID)
                                if let result = viewModel.scheduleResult, result.ok {
                                    scheduleResultSection(result)
                                        .id(Self.scheduleID)
                                }
                                if let error = viewModel.scheduleError {
                                    Text(error)
                                        .font(.cavnarBody(14))
                                        .foregroundStyle(Color.cavnarRed)
                                }
                                TimeOffSection(viewModel: viewModel) {
                                    scrollToReveal(Self.timeOffID, proxy: proxy)
                                }
                                .id(Self.timeOffID)
                                // Shifts handed back sit next to time off:
                                // both are the staff asking, both are
                                // decided in place.
                                ShiftRequestsSection(viewModel: setupViewModel) {
                                    scrollToReveal(Self.requestsID, proxy: proxy)
                                }
                                .id(Self.requestsID)
                                // Where the money went (the web's, from
                                // labor.money_went): the three costliest
                                // items of any kind, right under what
                                // staff are waiting on. Live shifts only.
                                if stats.isLive, let went = stats.moneyWent, !went.isEmpty {
                                    LaborMoneyWentCard(items: went, days: stats.periodDays,
                                                       moreCount: boardCount(stats)) {
                                        viewModel.staffingBoardExpanded = true
                                        scrollToReveal(Self.boardID, proxy: proxy)
                                    }
                                }

                                laborGroupHeader("Why")
                                    .padding(.top, 14)
                                // Why labor ran over, and the check that
                                // would confirm it — answerable (#25).
                                if let diagnosis = analyticsViewModel.diagnosis {
                                    LaborDiagnosisCard(diagnosis: diagnosis)
                                }
                                // By role sits directly above Overstaffed /
                                // Understaffed so they read as one group.
                                if !stats.roleSummary.isEmpty {
                                    roleSection(stats.roleSummary, dateRange: stats.dateRange)
                                }
                                // Every day and person: the staffing board
                                // (labor.staffing_board) — the web's
                                // executive strip and decision cards,
                                // replacing the phone's own overstaffed /
                                // under-target / overtime-risk lists.
                                StaffingBoardSection(
                                    board: stats.staffingBoard, isLive: stats.isLive,
                                    targetLabel: stats.savingsBreakdown.laborTargetLabel ?? "your target",
                                    blendedRate: stats.blendedRate,
                                    isExpanded: $viewModel.staffingBoardExpanded,
                                    onExpand: { scrollToReveal(Self.boardID, proxy: proxy) })
                                .id(Self.boardID)
                                // The measured layer behind the draft:
                                // outcomes, rotation, what staff keep doing.
                                ScheduleIntelSection(viewModel: setupViewModel, onExpand: {
                                    scrollToReveal(Self.intelID, proxy: proxy)
                                }, onAddPair: { pair in
                                    Task { await setupViewModel.addSuggestedPair(pair) }
                                }, demandAccuracy: viewModel.stats?.demandAccuracy,
                                   weekProjectionAccuracy: viewModel.stats?.weekProjectionAccuracy)
                                .id(Self.intelID)

                                laborGroupHeader("Scheduling setup")
                                    .padding(.top, 14)
                                setupRow
                                    .id(Self.setupID)
                            } else if viewModel.isLoading {
                                CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                            } else if let error = viewModel.errorMessage {
                                VStack(spacing: 8) {
                                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                    Button("Retry") { Task { await viewModel.load() } }
                                }
                                .padding(.top, 60)
                                .frame(maxWidth: .infinity)
                            }
                        } else {
                            LaborAnalyticsSection(viewModel: analyticsViewModel, laborStats: viewModel.stats)
                        }
                    }
                    .padding(20)
                }
                // Starting a scroll dismisses an open keyboard immediately
                // — the other standard half of keyboard dismissal, next to
                // AvailabilityManagerSection's tap-to-dismiss.
                .scrollDismissesKeyboard(.immediately)
                // Send pinned to the bottom once a week is drafted, instead
                // of under the whole table (Friction #19, U3-9).
                .safeAreaInset(edge: .bottom) {
                    if subTab == .overview, let result = viewModel.scheduleResult, result.ok,
                       result.historyId != nil {
                        LaborSendBar(issues: (result.review?.hardCount ?? 0) + (result.review?.softCount ?? 0),
                                     unsaved: viewModel.hasUnsavedFixes || viewModel.optimizerUnsaved,
                                     onReview: {
                                         withAnimation(.easeOut(duration: 0.3)) {
                                             proxy.scrollTo(Self.reviewID, anchor: .top)
                                         }
                                     },
                                     onSend: { showingPublishSchedule = true })
                    }
                }
                .cavnarEmberRefreshable {
                    await viewModel.load()
                    await viewModel.loadAvailability()
                    await viewModel.loadTimeOff()
                    await viewModel.loadTeam()
                    await setupViewModel.loadShiftRequests()
                    await setupViewModel.loadRoster()
                    await setupViewModel.loadSignals()
                }
                // A push or card about a request, the schedule or overtime
                // opens its section and scrolls to it once the page is in.
                .onChange(of: viewModel.stats != nil, initial: true) { _, loaded in
                    guard loaded else { return }
                    revealFocus(proxy: proxy)
                }
            }
        }
        .cavnarModuleBackground()
        .sheet(item: $focusPerson) { target in PersonSheet(target: target) }
        .sheet(isPresented: $showingSetup, onDismiss: { setupFocusPerson = nil }) {
            LaborSetupSheet(viewModel: viewModel, setupViewModel: setupViewModel,
                            focusPerson: $setupFocusPerson)
        }
        // The ribbon itself always shows once there's a hero card, even
        // with zero upcoming events — the panel's own empty-state copy
        // covers that case, rather than the whole feature disappearing
        // whenever nothing's coming up.
        .cavnarHeroForecastRibbon(
            isExpanded: $viewModel.forecastExpanded,
            tone: (heroTone ?? .neutral).foreground,
            icon: "calendar",
            badgeCount: viewModel.stats?.laborUpcoming.count
        ) {
            let events = viewModel.stats?.laborUpcoming ?? []
            CavnarForecastPanel(
                title: "Scheduling forecast", tone: (heroTone ?? .neutral).foreground, icon: "calendar",
                isExpanded: $viewModel.forecastExpanded
            ) {
                // Plain VStack, not a ScrollView with a fixed max height —
                // a forced height reserved space well past the actual
                // content (typically 1-3 events), leaving a slab of empty
                // background below the last row. Sizing to content
                // removes that; a restaurant with an unusually long event
                // list just gets a taller panel, which is the right
                // tradeoff over guaranteed dead space in the common case.
                if events.isEmpty {
                    Text("Nothing dining-relevant coming up in the next 3 weeks — no holiday or seasonal push to plan extra coverage around right now.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineSpacing(3)
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        ForEach(events) { event in
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(event.name)
                                        .font(.cavnarBody(14.5, weight: 700))
                                        .foregroundStyle(Color.cavnarInk)
                                    // M/D/YY from the server (date_str), then
                                    // how far off it is.
                                    HomeMixedText.make("\(event.dateStr) · \(daysAwayLabel(event.daysAway))",
                                                       size: 14, weight: 600, color: .cavnarEmber2)
                                }
                                if let label = event.label, !label.isEmpty {
                                    // This restaurant's own last-year figure,
                                    // or "check your own history" — never a
                                    // generic claim about covers (I5).
                                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                                        HomeMixedText.make(label, size: 14, color: .cavnarInk2)
                                            .lineSpacing(3)
                                            .fixedSize(horizontal: false, vertical: true)
                                        ClaimKindTag(kind: event.claimKind)
                                    }
                                    Text(event.planningLine)
                                        .font(.cavnarBody(13.5))
                                        .foregroundStyle(Color.cavnarInk3)
                                } else {
                                    Text(forecastCopy(daysAway: event.daysAway))
                                        .font(.cavnarBody(14))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .lineSpacing(3)
                                }
                            }
                        }
                    }
                }
                // How the demand forecast behind staffing has held up here,
                // and the frozen weekly projections' record (K8) — the only
                // mobile payload that carries either is /labor.
                ForEach(Self.forecastRecordLines(viewModel.stats), id: \.self) { line in
                    HomeMixedText.make(line + ".", size: 12.5, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .navigationTitle("Labor")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Labor") }
        .toolbar {
            cavnarToolbarItem(placement: .topBarTrailing) {
                Button {
                    Haptic.light()
                    showingScheduleHistory = true
                } label: {
                    Image(systemName: "clock.arrow.circlepath")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarToolbarIconGlass()
                }
                .buttonStyle(.plain)
                .tint(nil)
                .accessibilityLabel("Schedule history")
            }
        }
        .sheet(isPresented: $showingScheduleHistory) {
            ScheduleHistoryView()
        }
        .cavnarTabSwipeNavigation($subTab, primaryTab: .overview, secondaryTab: .analytics)
        .task {
            // Loaded synchronously from disk before either network call —
            // shows the last known schedule/insight immediately on a fresh
            // launch instead of an empty tab while the real fetch is still
            // in flight, and stops a relaunch from silently discarding a
            // schedule that was only ever held in memory.
            if let restaurantId = sessionStore.currentUser?.restaurantId {
                viewModel.configureCaching(restaurantId: restaurantId)
                analyticsViewModel.configureCaching(restaurantId: restaurantId)
            }
            await viewModel.load()
        }
        .task { await analyticsViewModel.load() }
        // Reopening the app after a shift re-reads labor rather than
        // showing the morning's figures as current (audit 4.2).
        .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) {
            await viewModel.load()
            await analyticsViewModel.load()
        }
        .task {
            await viewModel.loadAvailability()
            await viewModel.loadTimeOff()
        }
        // Loaded up front rather than on expand so both collapsed headers
        // read their real counts ("3 of 8 rated") instead of a placeholder
        // that changes the moment the section is opened.
        .task { await viewModel.loadTeam() }
        // Same reason as loadTeam: the collapsed headers read real counts
        // ("2 waiting for an answer", "14 on the roster") from the start.
        .task {
            await setupViewModel.loadShiftRequests()
            await setupViewModel.loadRoster()
            await setupViewModel.loadSignals()
        }
        .sheet(isPresented: $showingPublishSchedule) {
            PublishScheduleSheet(scheduleId: viewModel.scheduleResult?.historyId,
                                 unsentChanges: viewModel.unsentChanges,
                                 onSent: { viewModel.unsentChanges = [] })
        }
        .sheet(item: $explainingRow) { row in
            AssignmentExplanationSheet(row: row, explanation: viewModel.scheduleResult?.explanation(for: row))
        }
        // Belt-and-suspenders alongside the .task-time restore above: tied
        // directly to scenePhase (the same signal RootView's own Face ID
        // lock keys off — confirmed swiping away and immediately back
        // still round-trips through .background) rather than depending on
        // whether SwiftUI actually re-runs .task for this specific
        // TabView/if-else hierarchy on a quick foreground return, which a
        // reported-still-missing schedule after the .task fix suggests
        // isn't reliably happening the way the .task fix assumed. This
        // costs nothing when scheduleResult is already populated —
        // configureCaching only ever sets it from a valid cache entry, it
        // never clears an existing value.
        //
        // analyticsViewModel was missing from this same safety net until
        // now — only viewModel was restored here, which meant the Labor
        // Analytics tab's count-up/bar-grow animations (hasPlayedTilesIntro
        // / hasPlayedBarIntro, gated exactly like scheduleResult is) had no
        // fallback at all on the same "the .task fix isn't reliably firing"
        // path this comment already describes, and would replay every
        // time that path was hit.
        .onChange(of: scenePhase) { _, newPhase in
            guard newPhase == .active, let restaurantId = sessionStore.currentUser?.restaurantId else { return }
            viewModel.configureCaching(restaurantId: restaurantId)
            analyticsViewModel.configureCaching(restaurantId: restaurantId)
        }
    }

    @ViewBuilder
    private func heroCard(_ stats: LaborStats) -> some View {
        // Neither "on track" nor "over target" is a claim you can make about
        // a number you couldn't measure. A failed analysis used to default
        // every figure to zero, and 0% read as comfortably under target.
        let tone: CavnarTone = stats.figuresAreTrustworthy ? (stats.onTrack ? .good : .bad) : .neutral
        VStack(alignment: .leading, spacing: 12) {
            // Sample data is not this restaurant's data. This used to be a
            // tap-to-open popover behind an icon that rendered DIM AND PLAIN
            // for the sample case — the same "nothing to see here" icon a
            // healthy live restaurant gets — because `stale` was defined as
            // isLive && daysOld > 21, so sample data could never be stale.
            // The one state with the most to disclose got the most
            // reassuring affordance. It is a banner now.
            if !stats.isLive {
                sampleDataBanner
            }
            // Figures from this phone's cache, or a refresh that failed
            // over them, say how old they are (#37) — the cache never
            // expires, so without this a week-old read looked current.
            if let notice = viewModel.cachedNotice {
                HomeMixedText.make(notice, size: 12.5, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 6) {
                Label("Labor cost", systemImage: "person.2.fill")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                dataFreshnessInfoButton(stats)
                Spacer()
                if stats.isLive {
                    TonePill(text: stats.figuresAreTrustworthy
                             ? (stats.onTrack ? "On track" : "Over target")
                             : "Incomplete data",
                             tone: tone)
                }
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                // Colored to the same on-track/over-target read the card's
                // own tint, progress bar, and pill already carry, and lit
                // from within so it isn't flat green-on-green (or red-on-
                // red) — see LaborHeroPercent.
                LaborHeroPercent(value: stats.overallLaborPct, tone: tone)
                (Text("/ ") + Text("\(Int(stats.target))%").font(.cavnarNumber(14, weight: 600)) + Text(" target"))
                    .font(.cavnarBody(14.5))
                    .foregroundStyle(Color.cavnarInk3)
            }
            StatProgressBar(progress: stats.overallLaborPct / max(stats.target, 1), tone: tone)
            // How current the sources behind labor are, from data health.
            if stats.isLive {
                DataHealthModuleBadge(module: "labor")
            }
            if let caveat = stats.caveat {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .padding(.top, 2)
                    Text(caveat)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            // The whole window's gap above target, said as what it is — an
            // opportunity over a named number of days, never "savings"
            // (NS3 labor #11) — and never on sample data.
            if stats.isLive && stats.potentialSavings > 0 && stats.figuresAreTrustworthy {
                HomeMixedText.make("About $\(Int(stats.potentialSavings)) above your target"
                                   + (stats.periodDays.map { " over these \($0) days" } ?? " over this window")
                                   + " \u{2014} a gap to close, not money saved.",
                                   size: 14, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if stats.isLive {
                ScheduleGenerateButton(
                    tone: tone,
                    isGenerating: viewModel.isGeneratingSchedule,
                    action: { Task { await viewModel.generateSchedule() } }
                )
                .padding(.top, 2)
                // Which week: next (the default), the one after, or a date.
                GenerateWeekPicker(viewModel: viewModel)
            }

            // "Building the Week" — shifts fill a 7-day grid while an ember
            // dash travels the header, for the ~minute the generator runs
            // (see CavnarMotion). Sits right under the button that started it.
            if viewModel.isGeneratingSchedule {
                VStack(alignment: .leading, spacing: 12) {
                    CavnarWeekBuilder(caption: viewModel.joinedRunningGeneration
                                      ? "Joining the generation already running…"
                                      : (!viewModel.regeneratingDates.isEmpty
                                         ? "Redoing \(viewModel.regeneratingDates.count) \(viewModel.regeneratingDates.count == 1 ? "day" : "days") — the rest are kept"
                                         : (viewModel.generateWeek == .next ? "Building next week's schedule"
                                            : "Building the schedule for \(viewModel.generateWeek.label)")))
                    if viewModel.joinedRunningGeneration {
                        Text("Somebody else started this week's draft moments ago — from the web, or another phone. You'll get the same result when it lands.")
                            .font(.cavnarBody(13.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // What it is actually doing, rather than sixty seconds
                    // of a spinner. Every line is a real stage of the run.
                    ScheduleProgressSteps(lastYearAvailable: viewModel.stats?.lastYearAvailable == true)
                }
                .padding(.top, 10)
                .transition(.opacity)
            }

            // The AI strip lives inside this SAME card, as its own footer
            // row, instead of a separate card placed underneath it — reads
            // as this hero's own follow-up commentary.
            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                .padding(.top, 4)
            AIConsultantEmbeddedStrip(
                title: "Cavnar AI Labor Consultant",
                insight: analyticsViewModel.insight,
                isLoading: analyticsViewModel.isLoadingInsight,
                // The read's lines are keyed (insight_rec_keys) and
                // presented on `labor` — Done / Not for us / Track (#25).
                recSurface: "labor"
            )
            // A cached read served because the latest failed says how old
            // it is, on the phone as on the web (B6#12).
            if let note = analyticsViewModel.insight?.olderReadNote ?? analyticsViewModel.insightFallbackNote {
                CavnarCaveat.olderRead(note)
                    .padding(.top, 6)
            }
            // The forecast ribbon straddles this card's bottom edge (see
            // cavnarRibbonHeroAnchor below) — cavnarGlassCard's own 16pt
            // padding alone left the ribbon's ~34pt-tall pill touching the
            // AI strip text right above it. A bit of extra clearance here
            // pushes the strip up off that edge instead.
            Color.clear.frame(height: 10)
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGeneratingSchedule)
        .cavnarGlassCard(tint: tone.foreground)
        // Reports this card's bottom-center edge up to LaborView's root —
        // see CavnarRibbonAnchorKey's doc comment for why the ribbon
        // itself is no longer rendered here directly.
        .cavnarRibbonHeroAnchor()
    }

    private var sampleDataBanner: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Color.cavnarAmber)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 3) {
                Text("Sample data")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                Text("These are example figures, not your restaurant's. Upload your shifts CSV under Account to see your own numbers.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(11)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarAmber.opacity(0.12))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(Color.cavnarAmber.opacity(0.35), lineWidth: 1)
        )
    }

    private var heroTone: CavnarTone? {
        viewModel.stats.map { $0.onTrack ? .good : .bad }
    }

    private struct DataFreshness {
        let rangeText: String
        let daysOld: Int
        let stale: Bool
        let isLive: Bool
        /// The server's own sentence for it (`labor_freshness.basis`).
        var basis: String? = nil
    }

    private func freshnessInfo(_ stats: LaborStats) -> DataFreshness? {
        guard let start = stats.dateRange.start, let end = stats.dateRange.end,
              Self.isoDayFormatter.date(from: start) != nil,
              let endDate = Self.isoDayFormatter.date(from: end) else { return nil }
        let cal = Calendar.current
        let daysOld = cal.dateComponents([.day],
                                         from: cal.startOfDay(for: endDate),
                                         to: cal.startOfDay(for: Date())).day ?? 0
        let rangeText = CavnarDate.mdyRange(start, end)
        // `stale` drives a brighter, exclamation-shaped icon. It required
        // isLive, so sample data — the case with the most to disclose —
        // always drew the dim, plain "nothing to check" glyph.
        return DataFreshness(rangeText: rangeText, daysOld: daysOld,
                             stale: Self.shiftDataIsStale(isLive: stats.isLive, daysOld: daysOld,
                                                          server: stats.laborFreshness),
                             isLive: stats.isLive, basis: stats.laborFreshness?.basis)
    }

    /// Whether the shift data reads as out of date: sample data always; else
    /// the server's own judgement (`labor_freshness`, on its cadence rule)
    /// when it sent one; the phone's 21-day rule only for an older server.
    static func shiftDataIsStale(isLive: Bool, daysOld: Int, server: LaborFreshness?) -> Bool {
        if !isLive { return true }
        if let judged = server?.isStale { return judged }
        return daysOld > 21
    }

    /// Was an always-visible amber text row under the hero numbers — moved
    /// behind a tap so the card's headline stat isn't sharing the spotlight
    /// with a line about data provenance every time you glance at it. The
    /// icon itself still tells you at a glance whether there's something to
    /// check (a brighter fill + exclamation shape when the underlying shift
    /// data is stale, dim + plain "i" otherwise) without spelling it out
    /// until asked. Was amber for the stale case — amber sits close in hue
    /// to the card's own green tint and outright clashes with its red one,
    /// since this button lives directly on the tinted glass hero card. Ink
    /// tones stay neutral against either.
    @ViewBuilder
    private func dataFreshnessInfoButton(_ stats: LaborStats) -> some View {
        if let info = freshnessInfo(stats) {
            Button {
                Haptic.light()
                showDataInfo = true
            } label: {
                Image(systemName: info.stale ? "exclamationmark.circle.fill" : "info.circle")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(info.stale ? Color.cavnarInk : Color.cavnarInk3)
            }
            .buttonStyle(.plain)
            .popover(isPresented: $showDataInfo, arrowEdge: .bottom) {
                dataFreshnessPopoverContent(info)
                    .presentationCompactAdaptation(.popover)
                    // The content's own .background() doesn't get clipped to
                    // the popover's actual (rounded) presentation shape, so
                    // its square corners peeked out past — or fell short
                    // of — the system's rounded chrome, reading as a
                    // mismatched border. presentationBackground draws at the
                    // right layer, clipped to the real shape.
                    .presentationBackground(Color.cavnarPaper2)
            }
        }
    }

    private func dataFreshnessPopoverContent(_ info: DataFreshness) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(info.isLive ? "Shift data window" : "Sample data")
                .font(.cavnarBody(14, weight: 700))
                .foregroundStyle(Color.cavnarEmber)
            Group {
                if !info.isLive {
                    Text("Showing sample data for illustration — upload your shifts CSV in Account for real numbers.")
                } else if info.stale {
                    Text("Shift data is from \(info.rangeText) — \(info.daysOld) days old. Upload a fresher CSV for current numbers.")
                } else {
                    Text("Based on shift data from \(info.rangeText).")
                }
            }
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk2)
            // Without this the popover sized itself to the text's
            // unconstrained ideal (single-line) width first and only then
            // applied the frame below, clipping everything past ~7-8 words
            // instead of wrapping. This forces wrap-not-clip within
            // whatever width it's actually given.
            .fixedSize(horizontal: false, vertical: true)
            if info.isLive, let basis = info.basis, !basis.isEmpty {
                HomeMixedText.make(basis, size: 12.5, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        // A fixed width (not maxWidth) gives the popover's own auto-sizing
        // an unambiguous number to lay out against, rather than an upper
        // bound it could compute around inconsistently.
        .frame(width: 240, alignment: .leading)
    }

    @State private var rowReplacements: [String: [ScheduleReplacement]] = [:]

    private static let availabilityID = "labor-availability"

    private static let timeOffID = "labor-time-off"
    private static let requestsID = "labor-shift-requests"
    private static let rosterID = "labor-roster"
    private static let demandID = "labor-demand"
    private static let intelID = "labor-intel"
    private static let teamID = "labor-team"
    private static let targetsID = "labor-targets"
    private static let waitingID = "labor-waiting"
    private static let scheduleID = "labor-schedule"
    private static let reviewID = "labor-schedule-review"
    private static let setupID = "labor-setup"

    /// A group's small header on the Overview (density #28): three of
    /// them — Needs you, Why, Scheduling setup — so a decision never looks
    /// like a settings row.
    private func laborGroupHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .tracking(1.6)
            .foregroundStyle(Color.cavnarEmber2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, -8)
            .accessibilityAddTraits(.isHeader)
    }

    /// The one row that opens Scheduling setup — configuration the
    /// generator reads, kept off the page of decisions.
    private var setupRow: some View {
        Button {
            Haptic.light()
            showingSetup = true
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "slider.horizontal.3")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Team, availability & targets")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    Text("Roster, availability, demand signals, team strength, shift targets")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .cavnarCard()
        .accessibilityHint("Opens the scheduling setup")
    }

    /// Scrolls the just-opened section into view once its expand animation
    /// has room to settle — firing scrollTo in the same instant as the
    /// disclosure's own height-change animation reliably centers against
    /// the pre-expansion layout, not the taller one about to exist a beat
    /// later. Delay matches CavnarDropdown's real 0.3s expand animation
    /// (was 0.24s, timed against a stale "0.22s" claim that never matched
    /// the actual withAnimation(.easeOut(duration: 0.3)) in
    /// CavnarDropdown.swift — firing ~0.06s before the container had
    /// actually finished growing, scrolling to a position that was still
    /// shifting underneath it).
    /// Opens and scrolls to `focusSection`, once, when the page has loaded.
    /// The ONE handler for "where inside Labor": every way in (a push, a
    /// notification row, a Home card, the command sheet, a quick action)
    /// arrives as this screen's route. A second, inbox-driven handler used to
    /// race this one and could land a time-off link on Shift requests (F3-7).
    /// An unknown section just leaves the page at its top.
    private func revealFocus(proxy: ScrollViewProxy) {
        guard !focusSpent, let section = focusSection else { return }
        focusSpent = true
        subTab = .overview
        switch LaborFocus(section: section) {
        case .waiting:
            viewModel.timeOffExpanded = true
            setupViewModel.requestsExpanded = true
            let pending = LaborWaitingOnYou.count(timeOff: viewModel.timeOff, shifts: setupViewModel.shiftRequests)
            scrollToReveal(pending > 0 ? Self.waitingID : Self.requestsID, proxy: proxy)
        case .requests:
            setupViewModel.requestsExpanded = true
            scrollToReveal(Self.requestsID, proxy: proxy)
        case .timeOff:
            viewModel.timeOffExpanded = true
            scrollToReveal(Self.timeOffID, proxy: proxy)
        case .team:
            // The roster lives in Scheduling setup now (density #28): the
            // sheet opens on it, and person/<key> opens that person's sheet
            // over the roster (F3-15) from inside it.
            setupViewModel.rosterExpanded = true
            scrollToReveal(Self.setupID, proxy: proxy)
            if let key = focusItem, !key.isEmpty {
                setupFocusPerson = PersonSheetTarget(key: key, name: "")
            }
            showingSetup = true
        case .overtime:
            // The board's Overtime lane — people past 40, as the web shows.
            viewModel.staffingBoardExpanded = true
            scrollToReveal(Self.boardID, proxy: proxy)
        case .availability:
            viewModel.availabilityExpanded = true
            scrollToReveal(Self.setupID, proxy: proxy)
            showingSetup = true
        case .schedule:
            viewModel.scheduleResultExpanded = true
            // No draft yet: the top of Labor, where the week is built.
            if viewModel.scheduleResult?.ok == true { scrollToReveal(Self.scheduleID, proxy: proxy) }
        case nil:
            break
        }
    }

    private func scrollToReveal(_ id: String, proxy: ScrollViewProxy) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            withAnimation(.easeOut(duration: 0.25)) {
                proxy.scrollTo(id, anchor: .center)
            }
        }
    }

    private static let isoDayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        // Parsed as UTC and then compared against a device-local Date(),
        // which put the boundary in the middle of the evening for anyone
        // west of Greenwich and made a same-day sync read a day old.
        // A shift date is a calendar day where the restaurant is, so
        // parse it in the same calendar the comparison uses.
        f.timeZone = Calendar.current.timeZone
        return f
    }()


    /// The demand forecast's record and the weekly projection's, each only
    /// when something was measured (K8).
    static func forecastRecordLines(_ stats: LaborStats?) -> [String] {
        var out: [String] = []
        if let d = stats?.demandAccuracy?.sentence { out.append(d) }
        if let w = stats?.weekProjectionAccuracy?.line {
            out.append(w.replacingOccurrences(of: "Past forecasts here", with: "Past weekly sales projections here"))
        }
        return out
    }

    private func daysAwayLabel(_ days: Int) -> String {
        if days == 0 { return "today" }
        if days == 1 { return "tomorrow" }
        return "\(days) days away"
    }

    private func forecastCopy(daysAway: Int) -> String {
        if daysAway <= 3 {
            return "Expect elevated covers — confirm full kitchen and floor coverage."
        } else if daysAway <= 7 {
            return "Check this week's schedule now — add 1–2 staff if you're typically at capacity."
        } else {
            return "Flag for your next schedule build."
        }
    }

    private static let boardID = "labor-staffing-board"

    /// Everything the board holds — what "Show all" under Where the money
    /// went opens.
    private func boardCount(_ stats: LaborStats) -> Int {
        guard let b = stats.staffingBoard else { return 0 }
        return b.overstaffed.count + b.lean.count + b.overtime.count
    }

    @ViewBuilder
    private func roleSection(_ roles: [LaborRoleSummary], dateRange: LaborDateRange?) -> some View {
        // Deliberately not wrapped in .cavnarCard() — every other section on
        // this tab is a bordered box, and stacking one more made the page
        // read as an unbroken column of boxes. Let the chart float directly
        // on the page background instead.
        VStack(alignment: .leading, spacing: 14) {
            Text("By role")
                .font(.cavnarBody(14.5, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            RoleDonutChart(roles: roles, isExpanded: $viewModel.rolesExpanded, dateRange: dateRange)
        }
    }

    /// Wrapped in a dropdown that starts CLOSED (density #29) — the full
    /// schedule, its scoring model and the day-by-day table used to open
    /// under the hero and push every decision on Labor several screens
    /// down. Closed, its subtitle is the summary an owner needs: "9/28–
    /// 10/4/26 drafted · Quality 82/100 · 2 still need you". A fresh
    /// generation still opens it (the owner just asked for it). Inside,
    /// what needs a decision (the review panel) comes first, then what
    /// changed, then "How it scored" behind a tap, then the rows. Send is
    /// the pinned bar (LaborSendBar) — always on screen once a draft is
    /// saved, open or closed, so it is never under the table.
    @ViewBuilder
    private func scheduleResultSection(_ result: GeneratedSchedule) -> some View {
        CavnarDropdown(
            title: "Generated schedule",
            subtitle: Self.scheduleSubtitle(result),
            tone: (result.review?.hardCount ?? 0) > 0 ? .warning : .good,
            isExpanded: $viewModel.scheduleResultExpanded
        ) {
            VStack(alignment: .leading, spacing: 16) {
                // The rules check comes first: a hard violation is decided
                // on before anything is admired. Shown whenever the server
                // sent one, or there is pending time off to say.
                if result.review != nil || !(result.pendingTimeOff ?? [:]).isEmpty {
                    ScheduleReviewPanel(viewModel: viewModel, result: result)
                        .id(Self.reviewID)
                }

                VStack(alignment: .leading, spacing: 12) {
                    if let summary = result.summary, !summary.isEmpty {
                        // `summary` is the deterministic diff against the
                        // last published week — hours moved, who swapped —
                        // computed from the rows, never written by the
                        // model. The model's own note follows separately.
                        Text("WHAT CHANGED VS LAST PUBLISHED WEEK")
                            .font(.cavnarBody(CavnarType.kicker, weight: 700))
                            .tracking(1.2)
                            .foregroundStyle(Color.cavnarGreen)
                        ForEach(Array(summary.enumerated()), id: \.offset) { _, line in
                            HStack(alignment: .top, spacing: 6) {
                                Text("•").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                                HomeMixedText.make(line, size: 14, color: .cavnarInk2)
                                    .lineSpacing(5)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    if let narrative = result.narrative?.trimmingCharacters(in: .whitespacesAndNewlines),
                       !narrative.isEmpty {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("CAVNAR AI'S NOTE")
                                .font(.cavnarBody(12, weight: 700))
                                .tracking(1.2)
                                .foregroundStyle(Color.cavnarInk3)
                            Text(narrative)
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarInk3)
                                .lineSpacing(4)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.top, 2)
                    }
                    if let budget = result.hoursBudget, budget > 0, let scheduled = result.hoursScheduled {
                        parHoursBanner(budget: budget, scheduled: scheduled, dollars: result.laborBudgetDollars)
                    }
                    // Cost, the budget trim, staggered starts, and what the
                    // forecast could not see — each only when the payload
                    // carried it.
                    ScheduleWeekNotes(result: result, demandAccuracy: viewModel.stats?.demandAccuracy)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard()

                // The Shift Quality Engine's verdict, behind "How it
                // scored" — the score is in the closed subtitle; the
                // dimensions, optimizer, ratings and every shift are the
                // 300-second layer.
                if let quality = result.quality, quality.checked {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.2)) { showingHowItScored.toggle() }
                    } label: {
                        HStack(spacing: 6) {
                            Text("How it scored")
                                .font(.cavnarBody(CavnarType.body, weight: 700))
                            if let score = quality.score {
                                Text("\(score)/100")
                                    .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: showingHowItScored ? "chevron.up" : "chevron.down")
                                .font(.system(size: 11, weight: .bold))
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint(showingHowItScored ? "Hides the scoring detail" : "Shows the scoring detail")
                    if showingHowItScored {
                        ShiftQualityPanel(quality: quality, whatIf: result.whatIf,
                                          isRescoring: viewModel.isRescoringQuality,
                                          overrideState: viewModel.overrideState,
                                          savedTick: viewModel.savedTick,
                                          recommendationDecisions: viewModel.recommendationDecisions,
                                          onRecommendation: { text, accepted, reason in
                                              Task {
                                                  await viewModel.recordRecommendation(text, accepted: accepted,
                                                                                       reasonCode: reason?.code)
                                              }
                                          },
                                          suppressedKinds: viewModel.suppressedRecommendationKinds.isEmpty
                                              ? (quality.suppressedRecommendationKinds ?? [])
                                              : viewModel.suppressedRecommendationKinds,
                                          viewModel: viewModel)
                    }
                }
                if let rows = result.previewRows, !rows.isEmpty {
                    fullScheduleTable(rows, csv: result.scheduleCsv)
                    // Tick days on their headers; only those are redone.
                    if result.historyId != nil {
                        RedoSelectedDaysRow(viewModel: viewModel)
                    }
                }

                // The schedule used to end at a CSV download — the people
                // who actually work the shifts never saw it. Send is the
                // main act; the CSV rides along for the office.
                // Publishing sends the STORED week, so Send waits for Save,
                // as it does on the web. A saved draft's Send lives in the
                // pinned bar (LaborSendBar) — one primary on the screen, in
                // thumb reach, never under the whole table (Friction #19).
                // A result with no history id can't be sent from the bar,
                // so it keeps its Send here.
                let unsaved = viewModel.hasUnsavedFixes || viewModel.optimizerUnsaved
                HStack(spacing: 10) {
                    if result.historyId == nil {
                        Button {
                            Haptic.light()
                            showingPublishSchedule = true
                        } label: {
                            HStack(spacing: 8) {
                                Image(systemName: "paperplane.fill").font(.system(size: 13, weight: .semibold))
                                Text("Send to staff")
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: unsaved))
                        .disabled(unsaved)
                    }

                    if let csvURL = Self.csvFile(for: result) {
                        ShareLink(item: csvURL, preview: SharePreview("Schedule CSV", image: Image(systemName: "tablecells"))) {
                            Label("CSV", systemImage: "square.and.arrow.down")
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
                    }
                }
            }
        }
    }

    /// The schedule as a real .csv file on disk, so the share sheet offers
    /// Files/Mail/AirDrop with a filename instead of a blob of text.
    private static func csvFile(for result: GeneratedSchedule) -> URL? {
        guard let csv = result.scheduleCsv, !csv.isEmpty else { return nil }
        let week = result.weekDates?.first ?? "week"
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("schedule-\(week).csv")
        do {
            try csv.write(to: url, atomically: true, encoding: .utf8)
            return url
        } catch {
            return nil
        }
    }

    /// Date range first, so a client sees at a glance which week this is
    /// for, then hours scheduled — reuses the same ISO/display formatters
    /// the freshness popover already parses shift-data dates with.
    /// The closed row's summary (density #29): "9/28–10/4/26 drafted ·
    /// Quality 82/100 · 2 still need you" — each part only when the payload
    /// carried it; "ready to send" when the review found nothing.
    static func scheduleSubtitle(_ result: GeneratedSchedule) -> String? {
        var parts: [String] = []
        // M/D/YY (CLIENT-45).
        if let first = result.weekDates?.first, let last = result.weekDates?.last,
           !first.isEmpty, !last.isEmpty {
            parts.append(CavnarDate.mdyRange(first, last) + " drafted")
        } else if let hours = result.hoursScheduled {
            parts.append("\(String(format: "%.1f", hours))h scheduled")
        }
        if let q = result.quality, q.checked, let score = q.score {
            parts.append("Quality \(score)/100")
        }
        if let review = result.review {
            let open = review.hardCount + review.softCount
            parts.append(open > 0 ? "\(open) still need\(open == 1 ? "s" : "") you" : "ready to send")
        }
        return parts.isEmpty ? nil : parts.joined(separator: "  ·  ")
    }

    private func parHoursBanner(budget: Double, scheduled: Double, dollars: Double?) -> some View {
        let diff = scheduled - budget
        let withinRange = abs(diff) <= max(budget * 0.05, 1)
        return HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text("PAR HOURS CHECK")
                    .font(.cavnarBody(13.5, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarGreen)
                Text("Budgeted \(budget.commaFormatted)h\(dollars.map { " ($\($0.commaFormatted))" } ?? "") for the week")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk2)
            }
            Spacer()
            Text(withinRange ? "On budget" : (diff > 0 ? "+\(diff.commaFormatted)h over" : "\(diff.commaFormatted)h under"))
                .font(.cavnarBody(14, weight: 700))
                .foregroundStyle(withinRange ? Color.cavnarGreen : Color.cavnarAmber)
        }
        .padding(10)
        .background(Color.cavnarGreen.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // The Shift Strength banner is gone. It ran a second leadership check
    // with different semantics from the quality engine — silently dropping
    // any rule without a minimum score, which the engine enforces — and both
    // rendered, so an owner read "every target met" a few centimetres above
    // "needs 2 bartenders, found 1". The engine's leadership and operational
    // strength dimensions cover everything it reported.

    private static let scheduleDayOrder = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    /// Every generated schedule reads in the same fixed team order,
    /// regardless of what order the AI happened to write the CSV rows in:
    /// cooks, then host/carry-out, bussers, runners, bartenders, servers,
    /// shift supervisors, with anything unrecognized last. Within the
    /// cooks category specifically, every kind (Prep/Pantry/Saute/Pizza
    /// Cook, ...) further clusters by its own exact role name rather than
    /// interleaving — Pizza Cook rows sitting together, not scattered
    /// between Prep Cook rows just because that's the order the AI wrote
    /// them in. Rather than depend on prompting the model for this (the
    /// kind of formatting instruction this generator doesn't reliably
    /// follow — see the row-repair logic in client_api.py for a concrete
    /// example), this reorders deterministically on the client.
    private static let roleCategoryOrder = ["cook", "host", "busser", "runner", "bartender", "server", "supervisor"]

    private static func roleCategory(_ role: String?) -> String {
        let r = (role ?? "").lowercased()
        if r.contains("cook") || r.contains("kitchen") { return "cook" }
        if r.contains("host") || r.contains("carry") { return "host" }
        if r.contains("buss") { return "busser" }
        if r.contains("runner") { return "runner" }
        if r.contains("bartend") { return "bartender" }
        if r.contains("supervisor") || r.contains("shift lead") { return "supervisor" }
        if r.contains("server") { return "server" }
        return "other"
    }

    /// Two-level grouping: fixed category order (roleCategoryOrder) first,
    /// then every distinct exact role name within a category clusters
    /// together — a role's first appearance in `rows` decides where its
    /// cluster lands within the category; row order inside a cluster, and
    /// which role appears before another within the same category, is
    /// otherwise left exactly as the AI wrote it.
    private static func groupedByRole(_ rows: [ScheduleRow]) -> [ScheduleRow] {
        var categoriesSeen: [String] = []
        var rolesByCategory: [String: [String]] = [:]
        var rowsByExactRole: [String: [ScheduleRow]] = [:]

        for row in rows {
            let category = roleCategory(row.role)
            let exactRole = row.role ?? ""
            if !categoriesSeen.contains(category) { categoriesSeen.append(category) }
            if rowsByExactRole[exactRole] == nil {
                rowsByExactRole[exactRole] = []
                rolesByCategory[category, default: []].append(exactRole)
            }
            rowsByExactRole[exactRole]?.append(row)
        }

        // The fixed order first, then anything that didn't match any
        // named category (e.g. a role this list has never heard of),
        // appended last rather than dropped.
        let orderedCategories = roleCategoryOrder.filter(categoriesSeen.contains)
            + categoriesSeen.filter { !roleCategoryOrder.contains($0) }

        return orderedCategories.flatMap { category in
            (rolesByCategory[category] ?? []).flatMap { rowsByExactRole[$0] ?? [] }
        }
    }

    /// Grouped by day with one branded header per day instead of repeating
    /// the day name under every single row — a client scanning this can
    /// find "Monday" once and read straight down its staff, rather than
    /// re-reading the same day label six times in a row.
    ///
    /// The backend re-derives `day` from the row's `date` server-side, so
    /// every row's day value is a real weekday now even when the AI's raw
    /// CSV had that column scrambled — `day` alone can no longer signal a
    /// bad row. `needsReview` is the explicit flag the backend sets when a
    /// row's other columns (employee/role/times) were scrambled badly
    /// enough that it couldn't confidently auto-repair them; those still
    /// get routed to a separate flagged group instead of rendering as a
    /// normal (but silently wrong) day entry.
    ///
    /// A row the compliance pass flagged carries a `reviewReason` and
    /// stays in its day, highlighted, so the manager sees it in context;
    /// only a row flagged with no reason (scrambled columns) is pulled out.
    @ViewBuilder
    private func fullScheduleTable(_ rows: [ScheduleRow], csv: String?) -> some View {
        let recognized = rows.filter { $0.needsReview != true || !($0.reviewReason ?? "").isEmpty }
        let unrecognized = rows.filter { $0.needsReview == true && ($0.reviewReason ?? "").isEmpty }
        let grouped = Dictionary(grouping: recognized, by: { $0.day ?? "—" })
        let orderedDays = Self.scheduleDayOrder.filter { grouped[$0] != nil }

        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Full schedule")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                // Moved here from the summary card above — sitting next to
                // the table it actually exports reads far more directly
                // than floating next to an unrelated "hours scheduled" line.
                if let csv {
                    ShareLink(item: csv, preview: SharePreview("Schedule.csv", image: Image("LaunchSeal"))) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }
            }
            ForEach(orderedDays, id: \.self) { day in
                scheduleDayGroup(day: day, rows: grouped[day] ?? [])
            }
            if !unrecognized.isEmpty {
                needsReviewGroup(unrecognized)
            }
        }
    }

    /// One shift: tap for why this person, long-press to put somebody else
    /// on it.
    ///
    /// A `Menu` with a primary action gives both without a visible edit
    /// control: the table's job is to be read, and a pencil on every one of
    /// eighty rows would bury that. Picking a replacement re-scores the week
    /// immediately, so the manager sees what the change bought before they
    /// look away. A row the rules check flagged is tinted amber with its
    /// reason under the name.
    private func shiftRowWithOverride(_ row: ScheduleRow) -> some View {
        let wasChanged = viewModel.overriddenRows.contains(row.id)
        let candidates = rowReplacements[row.id]
        let flagged = row.needsReview == true && !(row.reviewReason ?? "").isEmpty
        return Menu {
            if let candidates {
                if candidates.isEmpty {
                    Text("Nobody else can take this shift")
                } else {
                    ForEach(candidates) { member in
                        Button {
                            Task { await viewModel.overrideEmployee(rowId: row.id, to: member.name) }
                        } label: { Text(member.label) }
                    }
                }
            } else {
                // Eligibility is the server's answer, not a guess made
                // here — the same check the what-if pass uses, so
                // availability, staff notes, double booking and the
                // forty-hour ceiling all apply.
                Button {
                    Task { rowReplacements[row.id] = await viewModel.loadReplacements(for: row) }
                } label: { Label("Find a replacement", systemImage: "person.2") }
            }
            Button {
                Haptic.light()
                explainingRow = row
            } label: { Label("Why this person?", systemImage: "questionmark.circle") }
        } label: {
            HStack(alignment: .top) {
                VStack(alignment: .leading, spacing: 1) {
                    HStack(spacing: 5) {
                        Text(row.employee ?? "")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        if wasChanged {
                            Text("CHANGED")
                                .font(.cavnarBody(9, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarBlue)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.cavnarBlue.opacity(0.15)))
                        }
                        if flagged {
                            Text("REVIEW")
                                .font(.cavnarBody(9, weight: 700))
                                .tracking(0.5)
                                .foregroundStyle(Color.cavnarAmber)
                                .padding(.horizontal, 4)
                                .padding(.vertical, 1)
                                .background(Capsule().fill(Color.cavnarAmber.opacity(0.16)))
                        }
                    }
                    if let role = row.role, !role.isEmpty {
                        Text(role).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    }
                    if flagged, let reason = row.reviewReason {
                        HomeMixedText.make(reason, size: 13, weight: 600, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer()
                Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                    .font(.cavnarNumber(14))
                    .foregroundStyle(Color.cavnarInk2)
            }
            .contentShape(Rectangle())
            .padding(.vertical, 4)
            .padding(.horizontal, flagged ? 8 : 0)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(flagged ? Color.cavnarAmber.opacity(0.09) : Color.clear))
        } primaryAction: {
            Haptic.light()
            explainingRow = row
            // Warm the replacement list on a tap, so the long-press that
            // usually follows a look at the reason has names ready.
            if rowReplacements[row.id] == nil {
                Task { rowReplacements[row.id] = await viewModel.loadReplacements(for: row) }
            }
        }
        .buttonStyle(.plain)
    }

    private func needsReviewGroup(_ rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("NEEDS REVIEW")
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarAmber)
            }
            Text("These rows didn't come back with a normal weekday — double-check them before publishing.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? row.day ?? "Unknown")
                                .font(.cavnarBody(14, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text("day: \(row.day ?? "—")  ·  role: \(row.role ?? "—")")
                                .font(.cavnarBody(14))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                            .font(.cavnarNumber(14))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    .padding(.vertical, 4)
                }
            }
            .cavnarCard()
        }
        .padding(10)
        .background(Color.cavnarAmber.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    // Shifts starting before 3pm read as morning/day coverage (lunch, prep,
    // openers); 3pm on is night coverage (dinner through close) — matches
    // Gia Mia's own lunch/dinner daypart split. A row with an unparseable
    // or missing start time falls into Night rather than being dropped,
    // so it's still visible somewhere.
    private static let shiftTimeFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "h:mma"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        return formatter
    }()
    private static let nightCutoffMinutes = 15 * 60  // 3:00pm

    private static func minutesFromMidnight(_ timeString: String?) -> Int? {
        guard let timeString, let date = shiftTimeFormatter.date(from: timeString.lowercased()) else { return nil }
        let comps = Calendar.current.dateComponents([.hour, .minute], from: date)
        guard let hour = comps.hour, let minute = comps.minute else { return nil }
        return hour * 60 + minute
    }

    private func scheduleDayGroup(day: String, rows: [ScheduleRow]) -> some View {
        let morning = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) < Self.nightCutoffMinutes }
        let night = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) >= Self.nightCutoffMinutes }
        let date = rows.first?.date
        let holiday = viewModel.scheduleResult?.holiday(on: date)
        let ticked = date.map { viewModel.selectedRedoDates.contains($0) } ?? false
        let canRedo = viewModel.scheduleResult?.historyId != nil && date != nil

        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text(day.uppercased())
                    .font(.cavnarBody(14, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarEmber)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.cavnarEmber.opacity(0.16))
                    .clipShape(Capsule())
                Text("\(rows.count) shift\(rows.count == 1 ? "" : "s")")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                // A holiday inside the week, with the lift when the record
                // has one — an owner should never be surprised by the date.
                if let holiday {
                    HStack(spacing: 4) {
                        Image(systemName: "star.fill").font(.system(size: 9, weight: .bold))
                        HomeMixedText.make(holiday.label, size: 12, weight: 700, color: .cavnarAmber)
                    }
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Capsule().fill(Color.cavnarAmber.opacity(0.14)))
                    .accessibilityLabel(holiday.basedOn.map { "\(holiday.label), based on \($0)" } ?? holiday.label)
                }
                Spacer(minLength: 4)
                if canRedo, let date {
                    Button {
                        viewModel.toggleRedoDate(date)
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: ticked ? "checkmark.square.fill" : "square")
                                .font(.system(size: 15, weight: .semibold))
                                .foregroundStyle(ticked ? Color.cavnarEmber : Color.cavnarInk3)
                            Text("Redo")
                                .font(.cavnarBody(12.5, weight: ticked ? 700 : 500))
                                .foregroundStyle(ticked ? Color.cavnarEmber : Color.cavnarInk3)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(ticked ? "Redo \(day), ticked" : "Redo \(day)")
                    .accessibilityAddTraits(ticked ? .isSelected : [])
                }
            }
            VStack(alignment: .leading, spacing: 12) {
                if !morning.isEmpty {
                    daypartRows(label: "MORNING", count: morning.count, rows: morning)
                }
                if !night.isEmpty {
                    daypartRows(label: "NIGHT", count: night.count, rows: night)
                }
            }
            .cavnarCard()
        }
    }

    private func daypartRows(label: String, count: Int, rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 4) {
                Image(systemName: label == "MORNING" ? "sun.max.fill" : "moon.stars.fill")
                    .font(.system(size: 10, weight: .bold))
                Text("\(label) · \(count)")
                    .font(.cavnarBody(14, weight: 800))
                    .tracking(1.1)
            }
            .foregroundStyle(Color.cavnarEmber)
            ForEach(Self.groupedByRole(rows)) { row in
                shiftRowWithOverride(row)
            }
        }
    }
}

/// The hero card's headline number. Three things keep it from reading as a
/// flat tone-colored figure sitting on a card of the same tone: it counts
/// up from zero the first time it appears (same treatment as Food Cost's
/// hero and the Labor Analytics tiles), its fill is a top-lit gradient of
/// the tone (a lighter, less saturated tint fading to the true color)
/// over a hard dark drop shadow so it sits ON the card rather than in it,
/// and its colored glow breathes on a slow ~2.6s cycle — the one piece of
/// ambient motion on the card, kept subtle.
private struct LaborHeroPercent: View {
    let value: Double
    let tone: CavnarTone

    @State private var animatedValue: Double = 0
    @State private var start = Date()

    private var lightenedTone: Color {
        var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        guard UIColor(tone.foreground).getHue(&h, saturation: &s, brightness: &b, alpha: &a) else {
            return tone.foreground
        }
        return Color(hue: Double(h), saturation: Double(max(s - 0.38, 0)), brightness: Double(min(b + 0.3, 1)))
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let glow = 0.4 + 0.4 * (0.5 + 0.5 * sin(t * 2 * .pi / 2.6))
            CavnarAnimatableNumber(value: animatedValue, format: { String(format: "%.1f%%", $0) })
                .font(.cavnarNumber(40, weight: 700))
                .foregroundStyle(
                    LinearGradient(colors: [lightenedTone, tone.foreground], startPoint: .top, endPoint: .bottom)
                )
                .shadow(color: .black.opacity(0.55), radius: 4, x: 0, y: 3)
                .shadow(color: tone.foreground.opacity(glow), radius: 16, x: 0, y: 0)
                .cavnarSensitive()
        }
        .onAppear {
            withAnimation(.easeOut(duration: 1.1)) { animatedValue = value }
        }
        .onChange(of: value) { _, newValue in
            animatedValue = newValue
        }
    }
}

// The forecast ribbon/panel now lives in DesignSystem/HeroForecastRibbon.swift
// as the shared .cavnarHeroForecastRibbon(...) modifier + CavnarForecastPanel
// — see heroCard's .cavnarRibbonHeroAnchor() and the .cavnarHeroForecastRibbon(...)
// call above. Extracted so Food Cost's own forecast pill can mimic this one
// exactly instead of a hand-rolled lookalike.

/// The AI schedule generator is Gia Mia's single most-used feature —
/// deliberately styled as the standout action on the page (gradient fill,
/// icon badge, glow shadow, subtitle) rather than a plain text button, and
/// placed inside the hero card near the top instead of at the bottom of a
/// long scroll.
/// Matches the hero card's own status language instead of introducing a
/// third color (orange, clashing against a card that's already tinted red
/// or green depending on whether labor is over/under target) — a dark
/// surface with the same green/red used by the "On track"/"Over target"
/// pill above it, so the button reads as an extension of that card's own
/// status rather than an unrelated CTA dropped on top of it.
private struct ScheduleGenerateButton: View {
    let tone: CavnarTone
    let isGenerating: Bool
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 12) {
                ZStack {
                    Circle().fill(tone.foreground.opacity(0.16))
                    if isGenerating {
                        PulsingSparkleIcon(color: tone.foreground)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(tone.foreground)
                    }
                }
                .frame(width: 34, height: 34)

                if isGenerating {
                    ScheduleLoadingText(color: tone.foreground)
                } else {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Generate next week's schedule")
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(tone.foreground)
                        Text("A draft from your sales & shift history")
                            .font(.cavnarBody(14, weight: 500))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }

                Spacer(minLength: 4)

                if !isGenerating {
                    Image(systemName: "arrow.right")
                        .font(.system(size: 13, weight: .bold))
                        .foregroundStyle(tone.foreground.opacity(0.85))
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            // Same background token TonePill uses for its "On track"/"Over
            // target" pill (a low-opacity tint of the tone color) — so the
            // button reads as the exact same surface, not a separately
            // chosen dark that happens to be similar.
            .background(tone.background)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(tone.foreground.opacity(0.4), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .shadow(color: tone.foreground.opacity(0.3), radius: 14, x: 0, y: 6)
        }
        .buttonStyle(.plain)
        .disabled(isGenerating)
        .scaleEffect(isGenerating ? 0.99 : 1)
        .animation(.easeOut(duration: 0.15), value: isGenerating)
        .sensoryFeedback(.impact(weight: .medium), trigger: isGenerating) { old, new in !old && new }
    }
}

/// Gentle breathing opacity/scale pulse instead of a spinning ProgressView
/// — reused only while the button is in its generating state (a fresh
/// instance is created each time isGenerating flips true, so the pulse
/// restarts cleanly rather than needing manual state resets).
private struct PulsingSparkleIcon: View {
    let color: Color
    @State private var pulse = false

    var body: some View {
        Image(systemName: "sparkles")
            .font(.system(size: 15, weight: .bold))
            .foregroundStyle(color)
            .opacity(pulse ? 1 : 0.45)
            .scaleEffect(pulse ? 1.08 : 0.9)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
    }
}

/// Cycles through a handful of status lines while the schedule generates,
/// cross-fading between them — replaces a single static "Generating…"
/// label (and the bare spinner it sat next to) with something that reads
/// as the AI actually working through real steps, matching how long a
/// real Claude call over a week of shift/sales/weather/YoY context
/// actually takes rather than an indeterminate wait.
private struct ScheduleLoadingText: View {
    let color: Color

    // "Usually takes..." was 25–35s, set before this was measured against
    // a real server log — an actual generation (full shift history + YoY +
    // weather + the PAR-reconciliation prompt) took ~71s end to end.
    // Rounded up rather than quoting a precise range that varies run to
    // run.
    private static let messages = [
        "Reviewing your sales & shift history…",
        "Usually takes about a minute…",
        "Balancing coverage across the week…",
        "Checking for overtime risk…",
        "Weighing upcoming events & weather…",
        "Reconciling against your labor budget…",
        "Almost there…",
    ]

    @State private var index = 0

    var body: some View {
        ShimmerText(text: Self.messages[index], font: .cavnarBody(14.5, weight: 700), color: color)
            .task {
                while !Task.isCancelled {
                    try? await Task.sleep(for: .seconds(3.5))
                    if Task.isCancelled { return }
                    withAnimation(.easeInOut(duration: 0.4)) {
                        index = (index + 1) % Self.messages.count
                    }
                }
            }
    }
}

/// A bright band sweeping left-to-right across the text, masked to its own
/// glyph shape — the same "reading a file" shimmer pattern common in AI
/// tools, reusing the sliding-gradient technique CavnarSkeletonBar already
/// establishes for loading states elsewhere in the app, just masked to
/// text instead of filling a bar.
///
/// Driven by TimelineView (real wall-clock time) rather than a toggled
/// @State + withAnimation(.repeatForever) — the first version used the
/// latter and the sweep only ever ran on the very first message: the
/// parent's own withAnimation(...) { index += 1 } for the cross-fade
/// between messages is an ambient transaction that gets applied to
/// whatever animatable properties re-render inside it, and it silently
/// overrode/replaced the repeat-forever animation on this view's offset
/// with that one-shot transition every time the text changed. Computing
/// the sweep position directly from timeline.date sidesteps SwiftUI's
/// animation/transaction system entirely, so no ambient animation from a
/// parent update can interrupt it.
private struct ShimmerText: View {
    let text: String
    let font: Font
    let color: Color

    private static let period: Double = 1.6

    var body: some View {
        TimelineView(.animation) { timeline in
            let elapsed = timeline.date.timeIntervalSinceReferenceDate
            let phase = (elapsed.truncatingRemainder(dividingBy: Self.period)) / Self.period

            Text(text)
                .font(font)
                .foregroundStyle(color.opacity(0.4))
                .contentTransition(.opacity)
                .overlay(
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [.clear, color, .clear],
                            startPoint: .leading, endPoint: .trailing
                        )
                        .frame(width: geo.size.width * 0.6)
                        .offset(x: -geo.size.width * 0.6 + phase * geo.size.width * 1.6)
                    }
                    .mask(Text(text).font(font))
                    .allowsHitTesting(false)
                )
        }
    }
}

/// The sections a link can name inside Labor (nav.py; friction audit #3, #18)
/// — every spelling the server, the web and older builds use, folded to one.
enum LaborFocus: Equatable {
    case waiting, requests, timeOff, team, overtime, availability, schedule

    init?(section: String) {
        switch section.lowercased() {
        case "waiting", "request": self = .waiting
        case "requests", "shift_requests", "shifts": self = .requests
        case "timeoff", "time_off", "time-off": self = .timeOff
        case "team", "roster", "people", "person": self = .team
        case "overtime": self = .overtime
        case "availability": self = .availability
        case "schedule": self = .schedule
        default: return nil
        }
    }
}

/// Scheduling setup (density #28): what the generator reads beyond the
/// shift history — who it may schedule and how, when they can work, the
/// dated demand it cannot infer, and the team ratings with the targets
/// those ratings feed. Configuration, so it lives one tap off the Labor
/// page instead of interleaved with the decisions on it. The same view
/// models as the page, so a change here is on the page the moment the
/// sheet closes.
private struct LaborSetupSheet: View {
    let viewModel: LaborViewModel
    let setupViewModel: ScheduleSetupViewModel
    /// A "person/<key>" link's person, opened over the roster.
    @Binding var focusPerson: PersonSheetTarget?
    @Environment(\.dismiss) private var dismiss

    private static let rosterID = "setup-roster"
    private static let availabilityID = "setup-availability"
    private static let demandID = "setup-demand"
    private static let teamID = "setup-team"
    private static let targetsID = "setup-targets"

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        RosterSection(viewModel: setupViewModel) { reveal(Self.rosterID, proxy) }
                            .id(Self.rosterID)
                        AvailabilityManagerSection(viewModel: viewModel) { reveal(Self.availabilityID, proxy) }
                            .id(Self.availabilityID)
                        DemandSignalsSection(viewModel: setupViewModel) { reveal(Self.demandID, proxy) }
                            .id(Self.demandID)
                        // Rating the team, then the targets those ratings
                        // feed — a target means nothing before anyone is
                        // rated, and the targets editor says so.
                        TeamStrengthSection(viewModel: viewModel) { reveal(Self.teamID, proxy) }
                            .id(Self.teamID)
                        ShiftTargetsSection(viewModel: viewModel) { reveal(Self.targetsID, proxy) }
                            .id(Self.targetsID)
                    }
                    .padding(20)
                }
                .scrollDismissesKeyboard(.immediately)
            }
            .cavnarModuleBackground()
            .toolbarBackground(.hidden, for: .navigationBar)
            .toolbar {
                cavnarTitleToolbar("Scheduling setup")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Haptic.light()
                        dismiss()
                    } label: {
                        Text("Done")
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
            .navigationBarTitleDisplayMode(.inline)
        }
        .sheet(item: $focusPerson) { target in PersonSheet(target: target) }
    }

    private func reveal(_ id: String, _ proxy: ScrollViewProxy) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(id, anchor: .center) }
        }
    }
}
