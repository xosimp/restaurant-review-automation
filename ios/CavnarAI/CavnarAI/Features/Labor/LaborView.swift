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
    // Scheduling notes, who-is-who questions and guest mentions (memory
    // round, 9/29/26): answered in Scheduling setup, nudged from Needs you.
    @State private var teamMemory = TeamMemoryViewModel()
    @State private var subTab: LaborSubTab = .overview
    @State private var showDataInfo = false
    // The schedule row whose "why this person" is open.
    @State private var explainingRow: ScheduleRow?
    // "How it scored" — a tile under Details that opens the panel as a
    // sheet (density #29; iOS readability round).
    @State private var showingHowItScored = false
    /// The generated week's one "Details" disclosure, and Cavnar AI's note
    /// past its first three lines.
    @State private var showingScheduleDetails = false
    @State private var showingFullNarrative = false
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
    /// Task sheets (task_sheets.py): the day read-only, the report, the editor.
    @State private var showingTaskSheets = false
    /// A drafted week opened from Waiting on you — its own send sheet.
    @State private var draftToSend: DraftToSend?
    /// A shift whose times are being changed, or a new one being added.
    @State private var editingShift: ShiftEditSheet.Mode?
    /// A shift the owner asked to take off the week, awaiting the confirm.
    @State private var removingRow: ScheduleRow?
    @State private var editingSectionNames = false
    /// What the schedule has learned, and measured ratings (H2-1, H2-3).
    @State private var showingMemory = false
    @State private var showingMeasuredRatings = false
    /// A person a review line named (an unmatched name's suggestion).
    @State private var reviewPerson: PersonSheetTarget?
    /// The Team inbox (parity #10), on a thread when a link named one.
    @State private var inboxTarget: TeamInboxTarget?
    @Environment(DeepLinkRouter.self) private var deepLinkRouter
    /// What the next draft is built to, in the Generate card (iOS parity
    /// #46): the labor target, the build notes and their rules, the
    /// weekly automation.
    @State private var buildSettings = ScheduleBuildSettings()
    /// The Generate sheet: the week, the owner's words, the target, the
    /// build notes and the weekly automation (iOS readability round).
    @State private var showingGenerateSheet = false
    /// Tonight's covers (iOS parity #69).
    @State private var covers = CoversModel()
    /// The day the week's pager shows (iOS parity #48).
    @State private var pagerDay: String?
    /// A week asked to open while the one on screen holds unsaved edits —
    /// the screen asks before it is replaced (iOS parity #5).
    @State private var pendingOpenWeek: Int?
    /// Generate pressed over a week with edits no save has stored — asked
    /// first, as opening another week is (re-audit 10/8/26 #7).
    @State private var confirmingGenerateOverEdits = false
    /// Where to scroll next (a week opened, the scorecard's View).
    @State private var scrollTarget: String?
    /// Scheduling setup opened on a section a link named (#49).
    @State private var setupFocusNotes = false
    @State private var setupOpenClosers = false
    /// The week as a PDF to post or print, drawn once the rows settle.
    @State private var schedulePDF: URL?

    struct DraftToSend: Identifiable {
        let id: Int
    }

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
                                // Groups, not thirteen equal rows (density
                                // #28; iOS readability round 10/8/26):
                                // NEEDS YOU — every request staff are waiting
                                // on, in ONE list, and the drafted week; WHY —
                                // the diagnosis and what drove the hours;
                                // REQUESTS & COVERS — what was decided and
                                // tonight's covers; SCHEDULING SETUP — one row
                                // that opens a sheet.
                                laborGroupHeader("Needs you")
                                // What staff are waiting on, answered in
                                // place, before any chart (Friction #18) —
                                // the only place a pending request shows.
                                LaborWaitingOnYou(viewModel: viewModel, setupViewModel: setupViewModel,
                                                  onOpenRequests: {
                                                      setupViewModel.requestsExpanded = true
                                                      scrollToReveal(Self.requestsID, proxy: proxy)
                                                  },
                                                  onOpenDraft: { draftToSend = DraftToSend(id: $0) },
                                                  onOpenWeek: { requestOpenWeek($0) })
                                .id(Self.waitingID)
                                if let id = viewModel.openingWeekId {
                                    CavnarShimmerText(text: "Opening the week\u{2026}")
                                        .font(.cavnar(.label))
                                        .accessibilityLabel("Opening week \(id)")
                                }
                                if let error = viewModel.openWeekError {
                                    Text(error)
                                        .cavnarText(.secondary, color: .cavnarRedText)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // What the team memory asks the owner — stale
                                // notes, a person listed twice, a guest naming
                                // someone — answered in Scheduling setup.
                                TeamMemoryNudge(viewModel: teamMemory) { showingSetup = true }
                                // Tonight's lineup brief while it waits on an
                                // approval or a read, and the Team inbox
                                // (parity #10, #26, #67).
                                LaborTeamSection(openInbox: { inboxTarget = TeamInboxTarget(threadId: $0) },
                                                 inboxOpen: inboxTarget != nil)
                                    .id(Self.teamOpsID)
                                if let result = viewModel.scheduleResult, result.ok {
                                    scheduleResultSection(result)
                                        .id(Self.scheduleID)
                                }
                                if let error = viewModel.scheduleError {
                                    Text(error)
                                        .cavnarText(.secondary, color: .cavnarRedText)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // "Where the money went" was removed (owner,
                                // 9/26/26), as on the web: the Staffing board
                                // below carries the same days.

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
                                // Staffing review: the staffing board
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
                                // The measured layer behind the draft is read
                                // on the web; what the owner can act on — the
                                // auto-publish offer, the suggested pairs —
                                // stays here (iOS readability round).
                                ScheduleIntelSection(viewModel: setupViewModel, onAddPair: { pair in
                                    Task { await setupViewModel.addSuggestedPair(pair) }
                                }, actionsOnly: true)
                                .id(Self.intelID)
                                // What the draft has learned, and the
                                // servers' measured ratings (account holder
                                // only) — H2-1, H2-3.
                                learningRow("What the schedule has learned",
                                            detail: "Habits and teams the draft keeps \u{2014} keep, let go or make a rule",
                                            symbol: "brain") { showingMemory = true }
                                if sessionStore.currentUser?.isOwner == true {
                                    learningRow("Measured ratings",
                                                detail: "What each server sells a guest, to confirm as a rating",
                                                symbol: "chart.bar.xaxis") { showingMeasuredRatings = true }
                                }

                                // What was decided — the pending ones are in
                                // Needs you — with Post a shift and the open
                                // board, and tonight's covers (#69).
                                laborGroupHeader("Requests & covers")
                                    .padding(.top, 14)
                                TimeOffSection(viewModel: viewModel) {
                                    scrollToReveal(Self.timeOffID, proxy: proxy)
                                }
                                .id(Self.timeOffID)
                                ShiftRequestsSection(viewModel: setupViewModel) {
                                    scrollToReveal(Self.requestsID, proxy: proxy)
                                }
                                .id(Self.requestsID)
                                CoversTile(model: covers)

                                laborGroupHeader("Scheduling setup")
                                    .padding(.top, 14)
                                setupRow
                                    .id(Self.setupID)
                            } else if viewModel.isLoading {
                                CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                            } else if let error = viewModel.errorMessage {
                                VStack(spacing: 8) {
                                    Text(error).cavnarText(.secondary)
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
                       result.historyId != nil, viewModel.weekReadOnlyReason == nil {
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
                    await viewModel.loadDraftCheck()
                    await viewModel.loadAvailability()
                    await viewModel.loadTimeOff()
                    await viewModel.loadTeam()
                    await setupViewModel.loadShiftRequests()
                    await setupViewModel.loadRoster()
                    await setupViewModel.loadSignals()
                    await teamMemory.load()
                    await covers.load()
                    await buildSettings.load(weekStart: viewModel.generateWeek.weekStart)
                }
                // A push or card about a request, the schedule or overtime
                // opens its section and scrolls to it once the page is in.
                .onChange(of: viewModel.stats != nil, initial: true) { _, loaded in
                    guard loaded else { return }
                    revealFocus(proxy: proxy)
                }
                .onChange(of: scrollTarget) { _, target in
                    guard let target else { return }
                    scrollTarget = nil
                    scrollToReveal(target, proxy: proxy)
                }
            }
        }
        .cavnarModuleBackground()
        .sheet(item: $focusPerson) { target in PersonSheet(target: target) }
        .sheet(isPresented: $showingSetup, onDismiss: { setupFocusPerson = nil }) {
            LaborSetupSheet(viewModel: viewModel, setupViewModel: setupViewModel, teamMemory: teamMemory,
                            focusPerson: $setupFocusPerson, focusNotes: $setupFocusNotes,
                            openClosers: $setupOpenClosers)
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
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineSpacing(3)
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        ForEach(events) { event in
                            VStack(alignment: .leading, spacing: 3) {
                                HStack(spacing: 6) {
                                    Text(event.name)
                                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                        .foregroundStyle(Color.cavnarInk)
                                    // M/D/YY from the server (date_str), then
                                    // how far off it is.
                                    HomeMixedText.make("\(event.dateStr) · \(daysAwayLabel(event.daysAway))",
                                                       size: CavnarType.secondary, weight: 600, color: .cavnarEmber2)
                                }
                                if let label = event.label, !label.isEmpty {
                                    // This restaurant's own last-year figure,
                                    // or "check your own history" — never a
                                    // generic claim about covers (I5).
                                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                                        HomeMixedText.make(label, size: CavnarType.secondary, color: .cavnarInk2)
                                            .lineSpacing(3)
                                            .fixedSize(horizontal: false, vertical: true)
                                        ClaimKindTag(kind: event.claimKind)
                                    }
                                    Text(event.planningLine)
                                        .font(.cavnar(.secondary))
                                        .foregroundStyle(Color.cavnarInk3)
                                } else {
                                    Text(forecastCopy(daysAway: event.daysAway))
                                        .font(.cavnar(.secondary))
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
                    HomeMixedText.make(line + ".", size: CavnarType.caption, weight: 500, color: .cavnarInk3)
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
                    showingTaskSheets = true
                } label: {
                    Image(systemName: "checklist")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarToolbarIconGlass()
                }
                .buttonStyle(.plain)
                .tint(nil)
                .accessibilityLabel("Task sheets")
            }
        }
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
            ScheduleHistoryView(onOpenWeek: { id in
                showingScheduleHistory = false
                requestOpenWeek(id)
            })
        }
        .sheet(isPresented: $showingTaskSheets) {
            TaskSheetsScreen()
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
            // The week restored from the cache is re-read against the
            // server before it is taken as current (schedule re-audit
            // 10/4/26 UI-1) — another device may have changed or replaced it.
            await viewModel.revalidateWeek()
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
            // The drafted week staff don't have yet, for Waiting on you.
            await viewModel.loadDraftCheck()
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
        .task { await teamMemory.load() }
        .task { await buildSettings.load(weekStart: viewModel.generateWeek.weekStart) }
        // The week on screen's sections and each server's usual one (H2-2) —
        // for a week restored from the cache or reopened, not only a fresh one.
        .task(id: viewModel.scheduleResult?.historyId) { await viewModel.loadSections() }
        // What is on screen, for the drafted push's banner: watching the
        // week land here is the news already (ScheduleDraftWatch).
        .onAppear {
            ScheduleDraftWatch.shared.laborOnScreen = true
            ScheduleDraftWatch.shared.onScreenScheduleId = viewModel.scheduleResult?.historyId
        }
        .onDisappear {
            ScheduleDraftWatch.shared.laborOnScreen = false
            ScheduleDraftWatch.shared.onScreenScheduleId = nil
        }
        .onChange(of: viewModel.scheduleResult?.historyId) { _, id in
            ScheduleDraftWatch.shared.onScreenScheduleId = id
        }
        .sheet(isPresented: $showingPublishSchedule, onDismiss: { Task { await viewModel.loadDraftCheck() } }) {
            PublishScheduleSheet(scheduleId: viewModel.scheduleResult?.historyId,
                                 unsentChanges: viewModel.unsentChanges,
                                 hoursBudget: viewModel.scheduleResult?.hoursBudget,
                                 onSent: { viewModel.unsentChanges = [] },
                                 onVersion: { viewModel.adoptSentVersion($0) })
        }
        .sheet(item: $draftToSend, onDismiss: { Task { await viewModel.loadDraftCheck() } }) { draft in
            PublishScheduleSheet(scheduleId: draft.id)
        }
        .sheet(item: $editingShift) { mode in
            ShiftEditSheet(mode: mode, viewModel: viewModel, roster: setupViewModel.activeRoster)
                .cavnarFormSheet()
        }
        .sheet(isPresented: $editingSectionNames, onDismiss: { Task { await viewModel.loadSections() } }) {
            FloorSectionsSheet().presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $showingGenerateSheet) {
            GenerateScheduleSheet(viewModel: viewModel, settings: buildSettings,
                                  title: generateTitle, onGenerate: startGeneration)
        }
        .sheet(isPresented: $viewModel.showingDraftSummary) {
            if let result = viewModel.scheduleResult {
                ScheduleSummarySheet(
                    result: result,
                    onView: { scrollTarget = Self.scheduleID },
                    onWarnings: { scrollTarget = Self.reviewID },
                    onPublish: { after { showingPublishSchedule = true } },
                    onOptimize: viewModel.weekReadOnlyReason == nil ? { Task { await viewModel.optimize() } } : nil)
            }
        }
        .confirmationDialog("Generate a new week?",
                            isPresented: $confirmingGenerateOverEdits, titleVisibility: .visible) {
            Button("Generate \u{2014} drop my unsaved edits", role: .destructive) {
                confirmingGenerateOverEdits = false
                Task { await viewModel.generateSchedule() }
            }
            Button("Keep editing this week", role: .cancel) { confirmingGenerateOverEdits = false }
        } message: {
            Text("The week on screen has edits that haven\u{2019}t saved yet. They stay if the new week can\u{2019}t start.")
        }
        .confirmationDialog("Open another week?",
                            isPresented: Binding(get: { pendingOpenWeek != nil }, set: { if !$0 { pendingOpenWeek = nil } }),
                            titleVisibility: .visible) {
            Button("Open it \u{2014} drop my unsaved edits", role: .destructive) {
                if let id = pendingOpenWeek { openWeekNow(id) }
                pendingOpenWeek = nil
            }
            Button("Keep editing this week", role: .cancel) { pendingOpenWeek = nil }
        } message: {
            Text("The week on screen has edits that haven\u{2019}t saved yet.")
        }
        .confirmationDialog(removingRow.map { "Take \($0.employee ?? "this person")\u{2019}s \($0.day ?? "") \($0.shiftStart ?? "") shift off the week?" } ?? "",
                            isPresented: Binding(get: { removingRow != nil }, set: { if !$0 { removingRow = nil } }),
                            titleVisibility: .visible) {
            Button("Remove shift", role: .destructive) {
                if let row = removingRow { Task { await viewModel.removeShift(rowId: row.id) } }
                removingRow = nil
            }
            Button("Cancel", role: .cancel) { removingRow = nil }
        } message: {
            Text("The week is re-scored and saved without it. Staff who already have the week hear about it when you press Send.")
        }
        .sheet(isPresented: $viewModel.showingRedoSheet) { RedoDaysSheet(viewModel: viewModel) }
        .sheet(isPresented: Binding(get: { !viewModel.whyQuestions.isEmpty },
                                    set: { if !$0 { viewModel.dismissWhy() } })) {
            EditWhySheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showingMemory) { ScheduleMemoryScreen() }
        .sheet(isPresented: $showingMeasuredRatings) { MeasuredRatingsScreen() }
        .sheet(item: $reviewPerson) { target in PersonSheet(target: target) }
        .sheet(item: $inboxTarget) { target in TeamInboxView(initialThreadId: target.threadId) }
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
            Task { await viewModel.revalidateWeek() }
        }
    }

    /// The Overview's five-second answer (iOS readability round, 10/8/26):
    /// the % against target, the gap in dollars (an opportunity, never
    /// savings), the most likely cause from the diagnosis, ONE Generate
    /// button and Cavnar AI's read as one line. Everything that shapes the
    /// draft — the week, the owner's words, the target, the build notes and
    /// the weekly automation — lives in the Generate sheet the button opens.
    @ViewBuilder
    private func heroCard(_ stats: LaborStats) -> some View {
        // Neither "on track" nor "over target" is a claim you can make about
        // a number you couldn't measure. A failed analysis used to default
        // every figure to zero, and 0% read as comfortably under target.
        let tone: CavnarTone = stats.figuresAreTrustworthy ? (stats.onTrack ? .good : .bad) : .neutral
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // Sample data is not this restaurant's data — a banner, never a
            // dim icon (the one state with the most to disclose).
            if !stats.isLive {
                sampleDataBanner
            }
            // Figures from this phone's cache, or a refresh that failed
            // over them, say how old they are (#37).
            if let notice = viewModel.cachedNotice {
                CavnarMixedText(notice, role: .caption, color: .cavnarAmber)
            }
            HStack(spacing: CavnarSpace.xxs) {
                Text("Labor cost")
                    .cavnarText(.label, color: .cavnarInk2)
                dataFreshnessInfoButton(stats)
                Spacer()
                if stats.isLive {
                    TonePill(text: stats.figuresAreTrustworthy
                             ? (stats.onTrack ? "On track" : "Over target")
                             : "Incomplete data",
                             tone: tone)
                }
            }
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                // Colored to the same on-track/over-target read the pill and
                // bar carry, lit from within (see LaborHeroPercent).
                LaborHeroPercent(value: stats.overallLaborPct, tone: tone)
                (Text("/ ") + Text("\(Int(stats.target))%").font(.cavnar(.figureS)) + Text(" target"))
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarInk2)
            }
            StatProgressBar(progress: stats.overallLaborPct / max(stats.target, 1), tone: tone)
            if let caveat = stats.caveat {
                CavnarMixedText(caveat, role: .caption, color: .cavnarInk2)
            }
            // The window's gap above target, said as what it is — an
            // opportunity over named days, never "savings" (NS3 labor #11)
            // — and never on sample data.
            if stats.isLive && stats.potentialSavings > 0 && stats.figuresAreTrustworthy {
                CavnarMixedText("About $\(Int(stats.potentialSavings)) over target"
                                + (stats.periodDays.map { " in \($0) days" } ?? "")
                                + " \u{2014} a gap to close, not money saved.",
                                role: .secondary, color: .cavnarAmber)
            }
            // Why, in one line: the diagnosis's most likely cause (the card
            // under Why carries the check and the reasoning).
            if let diagnosis = analyticsViewModel.diagnosis, diagnosis.hasCause, let cause = diagnosis.cause {
                (Text("Most likely: ").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                 + HomeMixedText.make(cause, role: .body))
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if stats.isLive {
                ScheduleGenerateButton(
                    tone: tone,
                    title: generateTitle,
                    isGenerating: viewModel.isGeneratingSchedule,
                    action: { showingGenerateSheet = true }
                )
                .padding(.top, CavnarSpace.xxs)
                // Another week is being built (a 409): that week, and a way
                // to follow it (iOS parity #15).
                if let busy = viewModel.busyRun {
                    GenerationBusyCard(busy: busy, onFollow: { Task { await viewModel.followBusyRun() } },
                                       onDismiss: { viewModel.busyRun = nil })
                }
            }

            // "Building the Week" — shifts fill a 7-day grid while an ember
            // dash travels the header, for the ~minute Cavnar AI takes
            // (see CavnarMotion). Sits right under the button that started it.
            if viewModel.isGeneratingSchedule {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    CavnarWeekBuilder(caption: viewModel.joinedRunningGeneration
                                      ? "Joining the week already being built\u{2026}"
                                      : (!viewModel.regeneratingDates.isEmpty
                                         ? "Redoing \(viewModel.regeneratingDates.count) \(viewModel.regeneratingDates.count == 1 ? "day" : "days") \u{2014} the rest are kept"
                                         : (viewModel.generateWeek == .next ? "Building next week's schedule"
                                            : "Building the schedule for \(viewModel.generateWeek.label)")))
                    if viewModel.joinedRunningGeneration {
                        Text("Someone else started this week moments ago, on the web or another phone. You'll get the same week when it lands.")
                            .cavnarText(.caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // What it is actually doing, rather than sixty seconds
                    // of a spinner. Every line is a real stage of the run.
                    ScheduleProgressSteps(lastYearAvailable: viewModel.stats?.lastYearAvailable == true,
                                          startedAt: viewModel.generationStartedAt,
                                          typical: viewModel.generationTypical)
                    // The one real measure of how far it has got: the days
                    // its answer has finished (schedule-status `progress`,
                    // AI cost audit 10/7/26 #36) — the web says the same.
                    if let p = viewModel.generationProgress, let line = p.line {
                        (Text("\(min(p.daysDrafted, p.daysTotal))").font(.cavnar(.figureS))
                         + Text(" of ").font(.cavnar(.secondary))
                         + Text("\(p.daysTotal)").font(.cavnar(.figureS))
                         + Text(p.daysTotal == 1 ? " day drafted" : " days drafted").font(.cavnar(.secondary)))
                            .foregroundStyle(Color.cavnarInk2)
                            .accessibilityLabel(line)
                            .transition(.opacity)
                    }
                    // How long it usually takes here, measured; whether this
                    // one is running long and when it stops at the latest;
                    // that the owner can leave (the web's renderScheduleEta).
                    if let started = viewModel.generationStartedAt {
                        TimelineView(.periodic(from: .now, by: 1)) { tl in
                            CavnarMixedText(GenerationCopy.etaLine(elapsed: tl.date.timeIntervalSince(started),
                                                                   typical: viewModel.generationTypical,
                                                                   until: viewModel.generationUntil),
                                            role: .caption, color: .cavnarInk2)
                        }
                    }
                }
                .padding(.top, CavnarSpace.xs)
                .transition(.opacity)
            }

            // Cavnar AI's read, as one line with a chevron, inside this SAME
            // card as its own footer row.
            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                .padding(.top, CavnarSpace.xxs)
            AIConsultantEmbeddedStrip(
                title: "Cavnar AI Labor Consultant",
                insight: analyticsViewModel.insight,
                isLoading: analyticsViewModel.isLoadingInsight,
                // The read's lines are keyed (insight_rec_keys) and
                // presented on `labor` — Done / Not for us / Track (#25).
                recSurface: "labor",
                readable: false
            )
            // A figure or a cause the read could not trace to the data it
            // was given is said on the hero, as it is under the read itself.
            if let insight = analyticsViewModel.insight {
                if insight.figuresVerified == false {
                    CavnarCaveat.unverifiedFigures(insight.unsupportedFigures ?? [])
                }
                if insight.causesVerified == false {
                    CavnarCaveat.unverifiedCauses(insight.unsupportedCauses ?? [])
                }
            }
            // A cached read served because the latest failed says how old
            // it is, on the phone as on the web (B6#12).
            if let note = analyticsViewModel.insight?.olderReadNote ?? analyticsViewModel.insightFallbackNote {
                CavnarCaveat.olderRead(note)
            }
            // A read the server could not write, in its own words
            // (InsightRefresh.follow, re-audit 10/8/26 #3).
            if let message = analyticsViewModel.insightError {
                CavnarCaveat.readUnavailable(message)
            }
            // Clearance for the forecast ribbon that straddles this card's
            // bottom edge (cavnarRibbonHeroAnchor below).
            Color.clear.frame(height: 10)
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGeneratingSchedule)
        .cavnarGlassCard(tint: tone.foreground)
        // Reports this card's bottom-center edge up to LaborView's root —
        // see CavnarRibbonAnchorKey's doc comment for why the ribbon
        // itself is no longer rendered here directly.
        .cavnarRibbonHeroAnchor()
        // The picked week's freshness and budget caveat, read before the
        // owner presses Generate (the button's label and refusal use it).
        .task(id: viewModel.generateWeek) { await viewModel.loadGenerateForecast() }
    }

    /// "Generate the week of 10/19/26" — the week the sheet will build,
    /// from the server's own week start once the picked week is read.
    private var generateTitle: String {
        let picked = viewModel.generateWeek.weekStart ?? ""
        if viewModel.generateForecastFor == picked, let start = viewModel.generateForecast?.weekStart, !start.isEmpty {
            return "Generate the week of \(CavnarDate.mdy(start))"
        }
        switch viewModel.generateWeek {
        case .next: return "Generate next week"
        case .weekAfter: return "Generate the week after"
        case .date: return "Generate the week of \(viewModel.generateWeek.label)"
        }
    }

    /// The Generate sheet's own button: the sheet goes first, then the
    /// week on screen is asked about when it holds unsaved edits.
    private func startGeneration() {
        showingGenerateSheet = false
        after {
            if viewModel.hasLocalEdits {
                confirmingGenerateOverEdits = true
            } else {
                Task { await viewModel.generateSchedule() }
            }
        }
    }

    private var sampleDataBanner: some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                Text("Sample data")
                    .cavnarText(.label, color: .cavnarAmber)
                Text("Example figures, not yours. Connect your POS or upload shifts on the web to see your own.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(CavnarSpace.s)
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

    /// How current the shift data is, behind a tap so the hero's figure
    /// isn't sharing the spotlight with provenance. The icon still says at a
    /// glance whether there's something to check (a filled exclamation when
    /// stale, a plain "i" otherwise), in ink so it reads on either tint. The
    /// data-health badge lives in the popover with it (it repeated this
    /// icon on the hero).
    @ViewBuilder
    private func dataFreshnessInfoButton(_ stats: LaborStats) -> some View {
        if let info = freshnessInfo(stats) {
            Button {
                Haptic.light()
                showDataInfo = true
            } label: {
                Image(systemName: info.stale ? "exclamationmark.circle.fill" : "info.circle")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(info.stale ? Color.cavnarInk : Color.cavnarInk3)
                    .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityLabel("How current the shift data is")
            .popover(isPresented: $showDataInfo, arrowEdge: .bottom) {
                dataFreshnessPopoverContent(info)
                    .presentationCompactAdaptation(.popover)
                    // presentationBackground draws at the popover's own
                    // (rounded) layer, so no square corners peek out.
                    .presentationBackground(Color.cavnarPaper2)
            }
        }
    }

    private func dataFreshnessPopoverContent(_ info: DataFreshness) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text(info.isLive ? "Shift data window" : "Sample data")
                .cavnarText(.label, color: .cavnarEmber2)
            Group {
                if !info.isLive {
                    Text("Sample figures for illustration \u{2014} connect your POS, or upload shifts on the web, for real numbers.")
                } else if info.stale {
                    Text("Shift data is from \(info.rangeText) \u{2014} \(info.daysOld) days old. Upload fresher shifts on the web for current numbers.")
                } else {
                    Text("Based on shift data from \(info.rangeText).")
                }
            }
            .cavnarText(.secondary)
            // Wrap, never clip, within the popover's fixed width.
            .fixedSize(horizontal: false, vertical: true)
            if info.isLive, let basis = info.basis, !basis.isEmpty {
                CavnarMixedText(basis, role: .caption)
            }
            // How current every source behind labor is, from data health.
            if info.isLive {
                DataHealthModuleBadge(module: "labor")
            }
        }
        .padding(CavnarSpace.m)
        // A fixed width gives the popover's auto-sizing an unambiguous
        // number to lay out against.
        .frame(width: 260, alignment: .leading)
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
    private static let teamOpsID = "labor-team-ops"

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
        switch LaborFocus(section: section, item: focusItem) {
        case .inbox(let threadId):
            inboxTarget = TeamInboxTarget(threadId: threadId)
        case .lineup:
            scrollToReveal(Self.teamOpsID, proxy: proxy)
        case .waiting:
            viewModel.timeOffExpanded = true
            setupViewModel.requestsExpanded = true
            let pending = LaborWaitingOnYou.count(timeOff: viewModel.timeOff, shifts: setupViewModel.shiftRequests,
                                                  draft: viewModel.draftCheck != nil, redo: viewModel.redoOffer != nil)
            scrollToReveal(pending > 0 ? Self.waitingID : Self.requestsID, proxy: proxy)
        // A pending request is decided in Waiting on you (the one list);
        // with nothing pending, the decided ones are in their sections.
        case .requests:
            if setupViewModel.pendingRequests.isEmpty {
                setupViewModel.requestsExpanded = true
                scrollToReveal(Self.requestsID, proxy: proxy)
            } else {
                scrollToReveal(Self.waitingID, proxy: proxy)
            }
        case .timeOff:
            if viewModel.timeOffPending == 0 {
                viewModel.timeOffExpanded = true
                scrollToReveal(Self.timeOffID, proxy: proxy)
            } else {
                scrollToReveal(Self.waitingID, proxy: proxy)
            }
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
            // schedule/<id> (the drafted push, History, a held send) opens
            // that week in the editor (iOS parity #5).
            if let id = focusItem.flatMap(Int.init), id > 0 {
                requestOpenWeek(id)
            } else if viewModel.scheduleResult?.ok == true {
                // No draft yet: the top of Labor, where the week is built.
                scrollToReveal(Self.scheduleID, proxy: proxy)
            }
        case .notes:
            // Scheduling notes live in Scheduling setup (#49).
            teamMemory.isExpanded = true
            setupFocusNotes = true
            showingSetup = true
        case .closers:
            setupViewModel.rosterExpanded = true
            setupOpenClosers = true
            showingSetup = true
        case .tasks:
            showingTaskSheets = true
        case .ratings:
            if sessionStore.currentUser?.isOwner == true { showingMeasuredRatings = true }
        case .intel:
            setupViewModel.intelExpanded = true
            scrollToReveal(Self.intelID, proxy: proxy)
        case .learned:
            showingMemory = true
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

    @ViewBuilder
    private func roleSection(_ roles: [LaborRoleSummary], dateRange: LaborDateRange?) -> some View {
        // Deliberately not wrapped in .cavnarCard() — every other section on
        // this tab is a bordered box, and stacking one more made the page
        // read as an unbroken column of boxes. Let the chart float directly
        // on the page background instead.
        VStack(alignment: .leading, spacing: 14) {
            Text("By role")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            RoleDonutChart(roles: roles, isExpanded: $viewModel.rolesExpanded, dateRange: dateRange)
        }
    }

    /// Wrapped in a dropdown that starts CLOSED (density #29); closed, its
    /// subtitle is the summary an owner needs: "9/28–10/4/26 drafted ·
    /// Quality 82/100 · 2 still need you". A fresh generation still opens
    /// it. Inside, in the order a manager decides it (iOS readability round,
    /// 10/8/26): what the generation could not do (red), the managers' days
    /// to set, the rules check's top three with Apply fixes, then THE WEEK —
    /// and one "Details" disclosure for everything that explains it (what
    /// changed, Cavnar AI's note, the budget, the week's notes, how it
    /// scored, the starting point). Send is the pinned bar (LaborSendBar).
    @ViewBuilder
    private func scheduleResultSection(_ result: GeneratedSchedule) -> some View {
        CavnarDropdown(
            title: "Generated schedule",
            subtitle: Self.scheduleSubtitle(result),
            tone: (result.review?.hardCount ?? 0) > 0 ? .warning : .good,
            isExpanded: $viewModel.scheduleResultExpanded
        ) {
            VStack(alignment: .leading, spacing: CavnarSpace.m) {
                // A copy of the week a newer one replaced is read-only, and
                // says so before anything else (schedule re-audit 10/4/26
                // UI-3); a week re-read from the server says when it changed.
                if let why = viewModel.weekReadOnlyReason {
                    ScheduleNotice(text: why, symbol: "lock.fill")
                }
                if let note = viewModel.weekNotice {
                    ScheduleNotice(text: note, tone: .cavnarInk2, symbol: "arrow.triangle.2.circlepath") {
                        Button {
                            viewModel.weekNotice = nil
                        } label: {
                            Text("Got it")
                                .cavnarText(.label, color: .cavnarEmber2)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                    }
                }
                // What the generation could not do leads the draft: days it
                // could not write, days nobody can work, the managers' plan
                // (schedule audit 10/3/26 B2, M, E). The informational
                // notes (a starting point, how current the sales were) are
                // under Details.
                DraftNotices(viewModel: viewModel, result: result, part: .urgent,
                             onOpenAvailability: openAvailability, onOpenClosures: openHours)
                if let plan = result.plan, let question = plan.question, !plan.unknownPattern.isEmpty {
                    ManagerQuestionRow(viewModel: viewModel, question: question, names: plan.unknownPattern)
                }
                // The rules check: what must be fixed, top three, with the
                // one Apply fixes — decided before anything is admired.
                if result.review != nil {
                    ScheduleReviewPanel(viewModel: viewModel, result: result,
                                        onOpenPerson: { reviewPerson = PersonSheetTarget(key: nil, name: $0) },
                                        onOpenHours: openHours)
                        .id(Self.reviewID)
                }
                // THE WEEK.
                if let rows = result.previewRows, !rows.isEmpty {
                    fullScheduleTable(rows, csv: result.scheduleCsv)
                    // Tick days on their headers; only those are redone.
                    if result.historyId != nil {
                        RedoSelectedDaysRow(viewModel: viewModel)
                    }
                }

                // Publishing sends the STORED week, so Send waits for Save,
                // as it does on the web. A saved draft's Send lives in the
                // pinned bar (LaborSendBar) — one primary on the screen, in
                // thumb reach (Friction #19). A result with no history id
                // can't be sent from the bar, so it keeps its Send here.
                let unsaved = viewModel.hasUnsavedFixes || viewModel.optimizerUnsaved
                HStack(spacing: CavnarSpace.s) {
                    if result.historyId == nil {
                        Button {
                            Haptic.light()
                            showingPublishSchedule = true
                        } label: {
                            HStack(spacing: CavnarSpace.xs) {
                                Image(systemName: "paperplane.fill").font(.cavnar(.secondary))
                                Text("Send to staff")
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: unsaved))
                        .disabled(unsaved)
                    }
                    // A PDF to post on the wall or print (the web's Print);
                    // the CSV is the share icon over the week.
                    if let pdf = schedulePDF {
                        ShareLink(item: pdf, preview: SharePreview("Schedule PDF", image: Image(systemName: "doc.richtext"))) {
                            Label("PDF", systemImage: "printer")
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .simultaneousGesture(TapGesture().onEnded { Haptic.light() })
                    }
                    if result.historyId != nil {
                        Button {
                            Haptic.light()
                            viewModel.showingDraftSummary = true
                        } label: {
                            Label("At a glance", systemImage: "square.grid.2x2")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }

                scheduleDetails(result)
            }
        }
        .task(id: Self.pdfKey(result)) {
            try? await Task.sleep(for: .milliseconds(500))
            guard !Task.isCancelled else { return }
            schedulePDF = SchedulePDF.file(for: result)
        }
        .sheet(isPresented: $showingHowItScored) {
            if let quality = result.quality, quality.checked {
                NavigationStack {
                    ScrollView {
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
                            .padding(CavnarSpace.gutter)
                    }
                    .accountSheetChrome("How it scored")
                }
                .presentationDetents([.large])
                .presentationDragIndicator(.visible)
            }
        }
    }

    /// Everything that explains the week, behind one "Details" — what
    /// changed against the last published week (three, then "+N more"),
    /// Cavnar AI's note (three lines, then More), the hourly budget, the
    /// week's notes, How it scored as one tile, what each shift was asked
    /// for (on the web) and the informational notices.
    @ViewBuilder
    private func scheduleDetails(_ result: GeneratedSchedule) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) { showingScheduleDetails.toggle() }
            } label: {
                HStack(spacing: CavnarSpace.xxs + 2) {
                    Text(showingScheduleDetails ? "Hide details" : "Details")
                        .cavnarText(.label, color: .cavnarEmber2)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showingScheduleDetails ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .cavnarHitTarget()
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingScheduleDetails ? "Expanded" : "Collapsed")

            if showingScheduleDetails {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    DraftNotices(viewModel: viewModel, result: result, part: .info,
                                 onOpenAvailability: openAvailability, onOpenClosures: openHours)
                    if let summary = result.summary, !summary.isEmpty {
                        // `summary` is the deterministic diff against the
                        // last published week, computed from the rows.
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            CavnarKicker("What changed vs last published week")
                            ForEach(Array(summary.prefix(3).enumerated()), id: \.offset) { _, line in
                                changeLine(line)
                            }
                            if summary.count > 3 {
                                CavnarMoreDisclosure(hiddenCount: summary.count - 3) {
                                    ForEach(Array(summary.dropFirst(3).enumerated()), id: \.offset) { _, line in
                                        changeLine(line)
                                    }
                                }
                            }
                        }
                    }
                    if let narrative = result.narrative?.trimmingCharacters(in: .whitespacesAndNewlines),
                       !narrative.isEmpty {
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            CavnarKicker("Cavnar AI's note")
                            Text(narrative)
                                .cavnarText(.body)
                                .lineLimit(showingFullNarrative ? nil : 3)
                                .fixedSize(horizontal: false, vertical: true)
                            if narrative.count > 160 {
                                Button {
                                    Haptic.light()
                                    showingFullNarrative.toggle()
                                } label: {
                                    Text(showingFullNarrative ? "Less" : "More")
                                        .cavnarText(.label, color: .cavnarEmber2)
                                        .cavnarHitTarget()
                                }
                                .buttonStyle(.plain)
                            }
                        }
                    }
                    if let budget = result.hoursBudget, budget > 0, let scheduled = result.hoursScheduled {
                        // The hourly crew's budget against the hourly hours
                        // (schedule audit 10/3/26 D-1, E-7/P-6, E-24).
                        ParHoursCheck(budget: budget, scheduled: scheduled, hourly: result.hoursHourly,
                                      salaried: result.hoursSalaried, dollars: result.laborBudgetDollars,
                                      basis: result.budgetBasis?.value)
                    }
                    // Cost, the budget trim, staggered starts, and what the
                    // forecast could not see — each only when the payload
                    // carried it.
                    ScheduleWeekNotes(result: result, demandAccuracy: viewModel.stats?.demandAccuracy)
                    if let quality = result.quality, quality.checked {
                        howItScoredTile(quality)
                    }
                    // The requirements the week was written and scored to
                    // (E) — a wide table, read on the web.
                    if let reqs = result.requirements?.items, !reqs.isEmpty {
                        CavnarWebLinkRow(title: "What each shift was asked for", path: "labor/schedule",
                                         actionLabel: "See it on the web")
                    }
                }
                .transition(.opacity)
            }
        }
    }

    private func changeLine(_ line: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Text("\u{2022}").cavnarText(.body)
            CavnarMixedText(line, role: .body)
        }
    }

    /// The Shift Quality verdict as one tile — the score, its band and the
    /// top recommendation; every dimension, the optimizer and each shift
    /// are in the sheet it opens.
    private func howItScoredTile(_ quality: ScheduleQuality) -> some View {
        Button {
            Haptic.light()
            showingHowItScored = true
        } label: {
            HStack(alignment: .center, spacing: CavnarSpace.m) {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("How it scored")
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        if let score = quality.score {
                            (Text("\(score)").font(.cavnar(.figureM)) + Text("/100").font(.cavnar(.figureS)))
                                .foregroundStyle(Color.cavnarInk)
                        }
                        if let band = quality.band, !band.isEmpty {
                            Text(band.prefix(1).uppercased() + band.dropFirst().replacingOccurrences(of: "_", with: " "))
                                .cavnarText(.secondary)
                        }
                    }
                    if let top = quality.recommendations?.first, !top.isEmpty {
                        CavnarMixedText(top, role: .secondary)
                            .lineLimit(2)
                    }
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .cavnarCard()
        .accessibilityHint("Opens how the week scored")
    }

    /// Changes whenever the rows do, so the PDF is redrawn after an edit.
    static func pdfKey(_ result: GeneratedSchedule) -> String {
        "\(result.historyId ?? 0)|\(result.version ?? 0)|" + (result.previewRows ?? []).map(\.signature).joined(separator: ";")
    }

    /// Open a week in the editor, asking first when the one on screen holds
    /// an edit no save has stored.
    private func requestOpenWeek(_ id: Int) {
        if viewModel.hasLocalEdits, viewModel.scheduleResult?.historyId != id {
            pendingOpenWeek = id
        } else {
            openWeekNow(id)
        }
    }

    private func openWeekNow(_ id: Int) {
        subTab = .overview
        Task {
            if await viewModel.openWeek(id) { scrollTarget = Self.scheduleID }
        }
    }

    /// A second sheet from this screen waits for the first to go.
    private func after(_ action: @escaping @MainActor () -> Void) {
        Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(450))
            action()
        }
    }

    /// The schedule as a real .csv file on disk, so the share sheet offers
    /// Files/Mail/AirDrop with a filename instead of a blob of text.
    /// The share icon over the week uses it (the separate CSV button went:
    /// one way to export, iOS readability round).
    private static func csvFile(_ csv: String?, week: String?) -> URL? {
        guard let csv, !csv.isEmpty else { return nil }
        let week = week ?? "week"
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
        // Days the generation could not write are shown empty, tagged "Not
        // written", so the week never reads as complete (P-34).
        let unwritten = Dictionary((viewModel.scheduleResult?.unwritten ?? []).map {
            ($0.day ?? LaborViewModel.weekdayName($0.date) ?? $0.date, $0.date)
        }, uniquingKeysWith: { a, _ in a })
        let orderedDays = Self.scheduleDayOrder.filter { grouped[$0] != nil || unwritten[$0] != nil }

        VStack(alignment: .leading, spacing: CavnarSpace.m) {
            HStack(spacing: CavnarSpace.xs) {
                Text("The week")
                    .cavnarText(.headline)
                Spacer()
                // The web editor's "+ Add a shift" (web parity, 9/25/26);
                // none on a replaced copy (UI-3).
                if viewModel.weekReadOnlyReason == nil {
                    Button {
                        Haptic.light()
                        editingShift = .add
                    } label: {
                        HStack(spacing: CavnarSpace.xxs) {
                            Image(systemName: "plus").font(.cavnar(.caption))
                            Text("Add a shift").cavnarText(.label, color: .cavnarEmber2)
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                }
                // The week as a .csv file to share — the one export button.
                if let url = Self.csvFile(csv, week: viewModel.scheduleResult?.weekDates?.first) {
                    ShareLink(item: url, preview: SharePreview("Schedule CSV", image: Image(systemName: "tablecells"))) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarEmber2)
                            .cavnarHitTarget()
                    }
                    .accessibilityLabel("Share the week as a spreadsheet")
                }
            }
            // A page per day under a strip of day chips, or everyone's week
            // (iOS parity #48) — the web's grid and list, phone-shaped.
            ScheduleWeekPager(
                days: orderedDays.map { day in
                    ScheduleDayPage(day: day, date: grouped[day]?.first?.date ?? unwritten[day],
                                    count: grouped[day]?.count ?? 0,
                                    notWritten: (grouped[day] ?? []).isEmpty && unwritten[day] != nil)
                },
                rows: recognized,
                selectedDay: $pagerDay,
                // The iPad week grid (#99): a tapped shift opens the same
                // editor as "Change the times"; a replaced copy only reads.
                onEditShift: viewModel.weekReadOnlyReason == nil ? { editingShift = .edit($0) } : nil
            ) { page in
                scheduleDayGroup(day: page.day, rows: grouped[page.day] ?? [], emptyDate: unwritten[page.day])
            }
            if !unrecognized.isEmpty {
                needsReviewGroup(unrecognized)
            }
        }
    }

    /// One shift: tap for why this person; the visible "⋯" beside it puts
    /// somebody else on it, changes its times or section, or takes it off
    /// the week (iOS readability round — no hidden long-press to learn).
    /// Picking a replacement re-scores the week immediately, so the manager
    /// sees what the change bought before they look away. A row the rules
    /// check flagged is tinted amber with its reason under the name.
    private func shiftRowWithOverride(_ row: ScheduleRow) -> some View {
        let wasChanged = viewModel.overriddenRows.contains(row.id)
        let flagged = row.needsReview == true && !(row.reviewReason ?? "").isEmpty
        return HStack(alignment: .top, spacing: CavnarSpace.xxs) {
            Button {
                Haptic.light()
                explainingRow = row
                // Warm the replacement list on a tap, so the ⋯ menu that
                // usually follows a look at the reason has names ready.
                if rowReplacements[row.id] == nil {
                    Task { rowReplacements[row.id] = await viewModel.loadReplacements(for: row) }
                }
            } label: {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: CavnarSpace.xxs + 1) {
                            Text(row.employee ?? "")
                                .cavnarText(.label)
                            if fixedRowIds.contains(row.rowId ?? "\u{0}") && !wasChanged {
                                ScheduleRowTag(text: "Fixed", tone: .cavnarBlue)
                            }
                            if wasChanged {
                                ScheduleRowTag(text: "Changed", tone: .cavnarBlue)
                            }
                            if flagged {
                                ScheduleRowTag(text: "Review", tone: .cavnarAmber)
                            }
                        }
                        if let role = row.role, !role.isEmpty {
                            Text(role).cavnarText(.secondary)
                        }
                        // The manager plan's chip, the clock change, the section
                        // or the usual one to assign (M-2, A2-6, H2-2).
                        ScheduleRowBadges(viewModel: viewModel, row: row)
                        if flagged, let reason = row.reviewReason {
                            CavnarMixedText(reason, role: .secondary, color: .cavnarAmber)
                        }
                    }
                    Spacer()
                    Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                        .font(.cavnar(.figureS))
                        .foregroundStyle(Color.cavnarInk2)
                        .minimumScaleFactor(0.85)
                        .lineLimit(1)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Shows why this person is on the shift")
            Menu {
                shiftRowMenu(row)
            } label: {
                Image(systemName: "ellipsis")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarEmber2)
                    .cavnarHitTarget()
            }
            .accessibilityLabel("Change \(row.employee ?? "this")\u{2019}s shift")
        }
        .padding(.vertical, CavnarSpace.xxs)
        .padding(.horizontal, flagged ? CavnarSpace.xs : 0)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(flagged ? Color.cavnarAmber.opacity(0.09) : Color.clear))
    }

    /// The ⋯ menu on a shift: a replacement (the server's answer to who is
    /// eligible), why this person, the times, the section, remove.
    @ViewBuilder
    private func shiftRowMenu(_ row: ScheduleRow) -> some View {
        if viewModel.weekReadOnlyReason == nil {
            if let candidates = rowReplacements[row.id] {
                if candidates.isEmpty {
                    Text("Nobody else can take this shift")
                } else {
                    Section("Swap in") {
                        ForEach(candidates) { member in
                            Button {
                                Task { await viewModel.overrideEmployee(rowId: row.id, to: member.name) }
                            } label: { Text(member.label) }
                        }
                    }
                }
            } else {
                // Eligibility is the server's answer, not a guess made
                // here — the same check the what-if pass uses, so
                // availability, staff notes, double booking and the
                // forty-hour ceiling all apply.
                Button {
                    Task { rowReplacements[row.id] = await viewModel.loadReplacements(for: row) }
                } label: { Label("Find a swap", systemImage: "person.2") }
            }
            Button {
                Haptic.light()
                explainingRow = row
            } label: { Label("Why this person?", systemImage: "questionmark.circle") }
            // The web editor's pencil and ✕ (web parity, 9/25/26): saved
            // like a swap — re-scored and stored at once.
            Button {
                Haptic.light()
                editingShift = .edit(row)
            } label: { Label("Change the times", systemImage: "pencil") }
            if !viewModel.sections.sections.isEmpty, viewModel.sections.isFrontOfHouse(row.role) {
                Menu {
                    ForEach(viewModel.sections.sections, id: \.self) { name in
                        Button(name) { Task { await viewModel.assignSection(row, section: name) } }
                    }
                    if viewModel.sections.section(for: row) != nil {
                        Button("No section") { Task { await viewModel.assignSection(row, section: "") } }
                    }
                    // The names themselves (the web's "Edit sections").
                    if viewModel.sections.canEdit != false {
                        Divider()
                        Button { editingSectionNames = true } label: { Label("Edit sections", systemImage: "pencil") }
                    }
                } label: { Label("Section", systemImage: "square.grid.2x2") }
            } else if viewModel.sections.sections.isEmpty, viewModel.sections.isFrontOfHouse(row.role),
                      viewModel.sections.canEdit == true {
                // No sections named yet: a front-of-house shift offers
                // to name them, as the web's shift pane does.
                Button { editingSectionNames = true } label: {
                    Label("Name floor sections", systemImage: "square.grid.2x2")
                }
            }
            Button(role: .destructive) {
                removingRow = row
            } label: { Label("Remove this shift", systemImage: "trash") }
        } else {
            Button {
                Haptic.light()
                explainingRow = row
            } label: { Label("Why this person?", systemImage: "questionmark.circle") }
        }
    }

    /// Rows a generation's fix lines name by their stable id (B1-1).
    private var fixedRowIds: Set<String> {
        Set((viewModel.scheduleResult?.review?.fixes ?? []).compactMap(\.rowId))
    }

    /// Scheduling setup, opened on availability.
    private func openAvailability() {
        viewModel.availabilityExpanded = true
        showingSetup = true
    }

    /// Account → Profile, where the hours and closed dates are set.
    private func openHours() {
        if let nav = NavPath("account/profile") { deepLinkRouter.open(nav) }
    }

    /// One row that opens a learning screen, in the setup row's shape.
    private func learningRow(_ title: String, detail: String, symbol: String,
                             action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 12) {
                Image(systemName: symbol)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    Text(detail)
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
    }

    private func needsReviewGroup(_ rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color.cavnarAmber)
                Text("NEEDS REVIEW")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .tracking(1)
                    .foregroundStyle(Color.cavnarAmber)
            }
            Text("These rows didn't come back with a normal weekday — double-check them before publishing.")
                .font(.cavnar(.secondary))
                .foregroundStyle(Color.cavnarInk3)
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? row.day ?? "Unknown")
                                .font(.cavnarBody(CavnarType.secondary, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text("day: \(row.day ?? "—")  ·  role: \(row.role ?? "—")")
                                .font(.cavnar(.secondary))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                            .font(.cavnarNumber(CavnarType.secondary))
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

    private func scheduleDayGroup(day: String, rows: [ScheduleRow], emptyDate: String? = nil) -> some View {
        let morning = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) < Self.nightCutoffMinutes }
        let night = rows.filter { (Self.minutesFromMidnight($0.shiftStart) ?? Self.nightCutoffMinutes) >= Self.nightCutoffMinutes }
        let date = rows.first?.date ?? emptyDate
        let notWritten = emptyDate != nil && (viewModel.scheduleResult?.unwritten ?? []).contains { $0.date == date }
        let holiday = viewModel.scheduleResult?.holiday(on: date)
        let ticked = date.map { viewModel.selectedRedoDates.contains($0) } ?? false
        let canRedo = viewModel.scheduleResult?.historyId != nil && date != nil

        return VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: CavnarSpace.xs) {
                Text(day)
                    .font(.cavnar(.label))
                    .textCase(.uppercase)
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 4)
                    .background(Color.cavnarEmber.opacity(0.16))
                    .clipShape(Capsule())
                if notWritten {
                    ScheduleRowTag(text: "Not written", tone: .cavnarAmber, symbol: "exclamationmark.triangle.fill")
                } else {
                    Text("\(rows.count) shift\(rows.count == 1 ? "" : "s")")
                        .cavnarText(.secondary)
                }
                // A holiday inside the week, with the lift when the record
                // has one — an owner should never be surprised by the date.
                if let holiday {
                    HStack(spacing: CavnarSpace.xxs) {
                        Image(systemName: "star.fill").font(.cavnar(.tag))
                        HomeMixedText.make(holiday.label, role: .caption, color: .cavnarAmber)
                    }
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Capsule().fill(Color.cavnarAmber.opacity(0.14)))
                    .accessibilityLabel(holiday.basedOn.map { "\(holiday.label), based on \($0)" } ?? holiday.label)
                }
                Spacer(minLength: CavnarSpace.xxs)
                if canRedo, let date {
                    Button {
                        viewModel.toggleRedoDate(date)
                    } label: {
                        HStack(spacing: CavnarSpace.xxs + 1) {
                            Image(systemName: ticked ? "checkmark.square.fill" : "square")
                                .font(.cavnar(.body))
                                .foregroundStyle(ticked ? Color.cavnarEmber2 : Color.cavnarInk2)
                            Text("Redo")
                                .font(ticked ? .cavnar(.label) : .cavnar(.secondary))
                                .foregroundStyle(ticked ? Color.cavnarEmber2 : Color.cavnarInk2)
                        }
                        .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(ticked ? "Redo \(day), ticked" : "Redo \(day)")
                    .accessibilityAddTraits(ticked ? .isSelected : [])
                }
            }
            // The day's manager window, its day-level breaches, stretches
            // nobody could manage and standing shifts not used (M-3, M-4,
            // M-5, A2-1, A2-2) — on the day, never on a person's row.
            if let date, let result = viewModel.scheduleResult {
                DayManagerNotes(viewModel: viewModel, date: date, result: result,
                                onChangeAvailability: openAvailability)
            }
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                if notWritten {
                    Text("This day wasn\u{2019}t written. Tick Redo and redo it, or add its shifts by hand.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if !morning.isEmpty {
                    daypartRows(label: "Morning", count: morning.count, rows: morning)
                }
                if !night.isEmpty {
                    daypartRows(label: "Night", count: night.count, rows: night)
                }
            }
            .cavnarCard()
        }
    }

    private func daypartRows(label: String, count: Int, rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker("\(label) \u{00B7} \(count)", icon: label == "Morning" ? "sun.max.fill" : "moon.stars.fill")
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
    // Reduce Motion: the glow holds at its mid strength and the figure
    // lands without counting up.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var lightenedTone: Color {
        var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        guard UIColor(tone.foreground).getHue(&h, saturation: &s, brightness: &b, alpha: &a) else {
            return tone.foreground
        }
        return Color(hue: Double(h), saturation: Double(max(s - 0.38, 0)), brightness: Double(min(b + 0.3, 1)))
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: reduceMotion)) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            let glow = reduceMotion ? 0.6 : 0.4 + 0.4 * (0.5 + 0.5 * sin(t * 2 * .pi / 2.6))
            CavnarAnimatableNumber(value: animatedValue, format: { String(format: "%.1f%%", $0) })
                .font(.cavnar(.figureXL))
                .foregroundStyle(
                    LinearGradient(colors: [lightenedTone, tone.foreground], startPoint: .top, endPoint: .bottom)
                )
                .shadow(color: .black.opacity(0.55), radius: 4, x: 0, y: 3)
                .shadow(color: tone.foreground.opacity(glow), radius: 16, x: 0, y: 0)
                .cavnarSensitive()
        }
        .onAppear {
            if reduceMotion { animatedValue = value; return }
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
    /// "Generate the week of 10/19/26" — the picked week, named.
    let title: String
    let isGenerating: Bool
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: CavnarSpace.s) {
                ZStack {
                    Circle().fill(tone.foreground.opacity(0.16))
                    if isGenerating {
                        PulsingSparkleIcon(color: tone.foreground)
                    } else {
                        Image(systemName: "sparkles")
                            .font(.cavnar(.body))
                            .foregroundStyle(tone.foreground)
                    }
                }
                .frame(width: 34, height: 34)

                if isGenerating {
                    ScheduleLoadingText(color: tone.foreground)
                } else {
                    VStack(alignment: .leading, spacing: 2) {
                        CavnarMixedText(title, role: .label, color: tone.foreground)
                        Text("Pick the week and how it's built")
                            .cavnarText(.caption, color: .cavnarInk2)
                    }
                }

                Spacer(minLength: CavnarSpace.xxs)

                if !isGenerating {
                    Image(systemName: "chevron.right")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(tone.foreground.opacity(0.85))
                }
            }
            .padding(.horizontal, CavnarSpace.m)
            .padding(.vertical, CavnarSpace.s + 2)
            .frame(minHeight: 44)
            // Same background token TonePill uses for its "On track"/"Over
            // target" pill, so the button reads as the card's own surface.
            .background(tone.background)
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(tone.foreground.opacity(0.4), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .shadow(color: tone.foreground.opacity(0.3), radius: 14, x: 0, y: 6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(isGenerating)
        .scaleEffect(isGenerating ? 0.99 : 1)
        .animation(.easeOut(duration: 0.15), value: isGenerating)
        .sensoryFeedback(.impact(weight: .medium), trigger: isGenerating) { old, new in !old && new && AppPreferences.hapticsEnabledSnapshot }
    }
}

/// Gentle breathing opacity/scale pulse instead of a spinning ProgressView
/// — reused only while the button is in its generating state (a fresh
/// instance is created each time isGenerating flips true, so the pulse
/// restarts cleanly rather than needing manual state resets).
private struct PulsingSparkleIcon: View {
    let color: Color
    @State private var pulse = false
    // Reduce Motion: the sparkle holds at full strength, no breath.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Image(systemName: "sparkles")
            .font(.system(size: 15, weight: .bold))
            .foregroundStyle(color)
            .opacity(pulse ? 1 : 0.45)
            .scaleEffect(pulse ? 1.08 : 0.9)
            .onAppear {
                if reduceMotion { pulse = true; return }
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

    // No time claim here: a week takes minutes now, and how long it usually
    // takes is the measured line under the steps (iOS parity #15).
    private static let messages = [
        "Reviewing your sales & shift history…",
        "Balancing coverage across the week…",
        "Checking for overtime risk…",
        "Weighing upcoming events & weather…",
        "Reconciling against your labor budget…",
        "Almost there…",
    ]

    @State private var index = 0

    var body: some View {
        ShimmerText(text: Self.messages[index], font: .cavnarBody(CavnarType.secondary, weight: 700), color: color)
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
    // Reduce Motion: the status line as plain text, no sweep.
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private static let period: Double = 1.6

    var body: some View {
        if reduceMotion {
            Text(text).font(font).foregroundStyle(color)
        } else {
            sweeping
        }
    }

    private var sweeping: some View {
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
    case waiting, requests, timeOff, team, overtime, availability, schedule, ratings, intel, learned
    // Scheduling notes, the closer cleanup and the task sheets (iOS parity
    // #49): action_queue's "labor/notes" and the web's own sections.
    case notes, closers, tasks
    /// The Team inbox — on a thread when the link names one
    /// ("labor/inbox?thread=7", an employee_message push; parity #10).
    case inbox(threadId: Int?)
    /// Tonight's lineup brief ("labor/lineup", a lineup_brief_waiting push; #26).
    case lineup

    init?(section: String) { self.init(section: section, item: nil) }

    init?(section: String, item: String?) {
        switch section.lowercased() {
        case "inbox", "messages": self = .inbox(threadId: item.flatMap { Int($0) }.flatMap { $0 > 0 ? $0 : nil })
        case "lineup", "brief", "staff-brief": self = .lineup
        case "waiting", "request": self = .waiting
        case "requests", "shift_requests", "shifts": self = .requests
        case "timeoff", "time_off", "time-off": self = .timeOff
        case "team", "roster", "people", "person": self = .team
        case "overtime": self = .overtime
        case "availability": self = .availability
        case "schedule": self = .schedule
        // The action queue's measured-ratings and calibration items (H2-4).
        case "ratings": self = .ratings
        case "intel": self = .intel
        case "learned", "schedule-memory", "memory": self = .learned
        case "notes", "staff-notes", "staff_notes", "scheduling-notes": self = .notes
        case "closers", "closer": self = .closers
        case "tasks", "task-sheets", "task_sheets", "tasksheets": self = .tasks
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
    let teamMemory: TeamMemoryViewModel
    /// A "person/<key>" link's person, opened over the roster.
    @Binding var focusPerson: PersonSheetTarget?
    /// labor/notes and labor/closers (#49): scrolled to, or opened, once.
    @Binding var focusNotes: Bool
    @Binding var openClosers: Bool
    @Environment(\.dismiss) private var dismiss

    private static let rosterID = "setup-roster"
    private static let notesID = "setup-notes"
    private static let availabilityID = "setup-availability"
    private static let demandID = "setup-demand"
    private static let teamID = "setup-team"
    private static let targetsID = "setup-targets"

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        RosterSection(viewModel: setupViewModel, onExpand: { reveal(Self.rosterID, proxy) },
                                      openClosers: $openClosers)
                            .id(Self.rosterID)
                        TeamMemorySection(viewModel: teamMemory,
                                          names: setupViewModel.roster.map(\.name)) { reveal(Self.notesID, proxy) }
                            .id(Self.notesID)
                        AvailabilityManagerSection(viewModel: viewModel,
                                                   names: setupViewModel.activeRoster.map(\.name)) {
                            reveal(Self.availabilityID, proxy)
                        }
                            .id(Self.availabilityID)
                        DemandSignalsSection(viewModel: setupViewModel) { reveal(Self.demandID, proxy) }
                            .id(Self.demandID)
                        // Rating the team (1–5) stays on the phone.
                        TeamStrengthSection(viewModel: viewModel, setup: setupViewModel.teamSetup,
                                            rolesFor: rolesByName) { reveal(Self.teamID, proxy) }
                            .id(Self.teamID)
                        // Configuration that is a wide table or a
                        // multi-field form is set on the web (iOS
                        // readability round, 10/8/26): next week's forecast
                        // as the draft will be handed it, the work per
                        // person-hour (D-1, D-24, D-25), and the shift
                        // targets the ratings feed.
                        VStack(spacing: 0) {
                            CavnarWebLinkRow(title: "Shift targets",
                                             subtitle: "How strong each shift should be", path: "labor/team")
                            CavnarWebLinkRow(title: "Labor standards",
                                             subtitle: "The work one person carries an hour", path: "labor/team")
                            CavnarWebLinkRow(title: "Next week's forecast",
                                             subtitle: "Sales and hours, day by day", path: "labor/schedule",
                                             actionLabel: "See it on the web")
                        }
                        .cavnarCard()
                        .id(Self.targetsID)
                    }
                    .padding(20)
                }
                .scrollDismissesKeyboard(.immediately)
                .onAppear {
                    guard focusNotes else { return }
                    focusNotes = false
                    reveal(Self.notesID, proxy)
                }
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
                            .font(.cavnar(.label))
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

    /// The roles each person worked lately, from the roster — what the
    /// team's per-role ratings offer (schedule audit 10/3/26 D-12).
    private var rolesByName: [String: [String]] {
        var out: [String: [String]] = [:]
        for m in setupViewModel.roster { out[m.name] = ([m.role ?? ""] + (m.recentRoles ?? [])).filter { !$0.isEmpty } }
        return out
    }
}
