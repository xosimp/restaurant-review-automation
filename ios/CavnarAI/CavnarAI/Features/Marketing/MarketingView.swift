import SwiftUI

enum MarketingSubTab: String, CaseIterable, Identifiable {
    case content = "Content"
    case campaigns = "Campaigns"
    case analytics = "Analytics"
    var id: String { rawValue }
}

private enum MarketingContentField: Hashable, CaseIterable {
    case topic, draft, ctaLink
}

/// The three destinations off the Content tab's shelf (Guest Text Club,
/// Scheduled, Drafts) — one Identifiable enum driving a single
/// navigationDestination(item:) rather than three separate NavigationLinks,
/// so the row's tap can fire a deterministic haptic (see shelfRow).
enum MarketingShelfDestination: String, Identifiable {
    case guestTextClub, scheduled, drafts
    var id: String { rawValue }
}

struct MarketingView: View {
    /// Where a route asked to land (nav.py: marketing, marketing/
    /// opportunities, marketing/campaigns, marketing/text-club,
    /// marketing/drafts, …): the section, an opportunity card's key and a
    /// drafted post's id — the quiet-night push carries both of the last two.
    var focus: MarketingFocus

    init(focus: MarketingFocus = MarketingFocus()) {
        self.focus = focus
    }

    @Environment(SessionStore.self) private var sessionStore
    @State private var viewModel = MarketingViewModel()
    @State private var analyticsViewModel = MarketingAnalyticsViewModel()
    @State private var compose = MarketingComposeViewModel()
    @State private var opportunities = MarketingOpportunityViewModel()
    @State private var campaigns = CampaignsTabViewModel()
    /// The Campaign Studio, open on one seed.
    @State private var studioSeed: StudioSeed?
    @State private var focusApplied = false
    /// The Google post's confirm (it goes live on the listing at once).
    @State private var confirmingGoogle = false
    @State private var subTab: MarketingSubTab = .content
    @State private var showingPreview = false
    @State private var showingSchedule = false
    /// Every channel Schedule queues the post to (re-audit 10/8/26 H4).
    @State private var schedulePlatforms: [String] = []
    /// A generate waiting on "Replace your edited post?" (M7).
    @State private var pendingGenerate: PendingGenerate?
    /// "Plan a new week?" before seven ideas are replaced (L5).
    @State private var confirmingNewWeek = false
    /// The composer and the week sit behind their own disclosures, so the
    /// Content tab has one next move on its first screen (M15).
    @State private var showingComposer = false
    @State private var showingWeek = false
    /// The confirm before "Post to all connected" publishes (Friction #41).
    @State private var confirmingPostAll = false
    @State private var shelfDestination: MarketingShelfDestination?
    /// Bumped when a calendar day's "Write this" finishes — the ScrollViewReader
    /// watches it and moves to the caption box. A token rather than a Bool so
    /// tapping a second day still scrolls.
    @State private var scrollToDraft: UUID?
    /// Bumped to bring the Opportunity Feed into view (a linked card).
    @State private var scrollToOpportunities: UUID?
    /// Index into viewModel.calendar of the day the focus card is showing.
    /// Starts on today when today is in the week, else the first day.
    @State private var selectedDay = 0
    /// True for a beat after "Write this" succeeds so the button can say so.
    @State private var justWrote = false
    /// GET /mobile/api/marketing/header — nil on an older server.
    @State private var header: MarketingHeader?
    @FocusState private var focusedField: MarketingContentField?

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: MarketingSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

            ScrollViewReader { scroll in
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if subTab == .campaigns {
                        CampaignsTabSection(viewModel: campaigns,
                                            canPublish: campaigns.overview?.canPublish
                                                ?? (sessionStore.currentUser?.mayPublishMarketing ?? false),
                                            canChangeInvites: sessionStore.currentUser?.isAccountOwnerLogin ?? false,
                                            onOpenStudio: { studioSeed = $0 },
                                            onOpenTextClub: { shelfDestination = .guestTextClub })
                    } else if subTab == .content {
                        if viewModel.stats != nil {
                            CachedDataNotice(text: viewModel.stalenessNotice)
                            outcomeRow
                            // The posts a teammate wrote that wait on the
                            // owner, approvable here (readability #19) — the
                            // Drafts screen was the only place to do it.
                            waitingPostsRow
                            // The next best move is the screen's one primary:
                            // a post waiting on an approve, else the top
                            // opportunity (re-audit 10/8/26 M15).
                            MarketingOpportunitySection(viewModel: opportunities,
                                                        leadsTheScreen: compose.drafts.first(where: \.canApprove) == nil) { card in
                                draftFromCard(card)
                            }
                            .id(Self.opportunitiesAnchor)
                            // How current the post metrics are (DH4-8) —
                            // only when it is news: the nightly pull stale
                            // or failing (amber/red).
                            if let sync = viewModel.metricsSync, sync.tone == "warn" || sync.tone == "bad" {
                                ServerStatusCaption(status: sync)
                            }
                            shelfTiles
                            composerCard
                            weekSection
                        } else if viewModel.isLoading {
                            CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).cavnarText(.body)
                                Button("Retry") { Task { await viewModel.load() } }
                                    .frame(minHeight: 44)
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        MarketingAnalyticsSection(viewModel: analyticsViewModel)
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // Post / Schedule pinned in thumb reach once there is a draft to
            // send (readability round #56) — they sat at the end of a dozen
            // blocks. Only on Content, only where a channel is connected.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if subTab == .content && viewModel.hasDraft && viewModel.canPostSomewhere {
                    if canPublish {
                        CavnarPinnedBar {
                            composeActions
                            if viewModel.isGooglePost {
                                googlePublish
                            } else {
                                socialPublish
                            }
                        }
                    } else {
                        // A teammate whose login may not publish (may_publish)
                        // saves it for the owner — Post and Schedule would
                        // only be refused (re-audit 10/8/26 M4).
                        CavnarPinnedBar(note: "Only the owner can post or schedule from here.") {
                            Button {
                                Haptic.light()
                                Task { await saveDraftForOwner() }
                            } label: {
                                Group {
                                    if compose.isSavingDraft {
                                        CavnarShimmerText(text: "Saving\u{2026}", color: .white)
                                    } else {
                                        Text("Save for the owner to send")
                                    }
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: compose.isSavingDraft))
                            .disabled(compose.isSavingDraft)
                        }
                    }
                }
            }
            // Refreshes whichever tab is actually on screen. This always
            // reloaded the CONTENT view model regardless, so pulling on
            // Analytics did nothing visible.
            .cavnarEmberRefreshable {
                switch subTab {
                case .content:
                    async let a: Void = viewModel.load()
                    async let b: Void = opportunities.load(quiet: true)
                    _ = await (a, b)
                case .campaigns:
                    await campaigns.load()
                case .analytics:
                    await analyticsViewModel.refresh()
                }
            }
            .onChange(of: scrollToOpportunities) { _, token in
                guard token != nil else { return }
                withAnimation(.easeOut(duration: 0.45)) {
                    scroll.scrollTo(Self.opportunitiesAnchor, anchor: .top)
                }
            }
            .onChange(of: scrollToDraft) { _, token in
                guard token != nil else { return }
                // Land just above the caption box rather than on it, so the
                // "Generate" control it came from stays in view and the jump
                // reads as a move up the page instead of a teleport.
                withAnimation(.easeOut(duration: 0.45)) {
                    scroll.scrollTo(Self.draftEditorAnchor, anchor: .center)
                }
            }
            }
        }
        // Declared here, at the top level of the view, NOT inside the
        // ScrollView where the rows that trigger it live. A
        // navigationDestination inside a scroll view is not a supported
        // placement (Apple's own documentation says so) and the screen it
        // pushes lays out against the wrong container — which is why the
        // Drafts screen's gradient stopped short of the full width.
        .navigationDestination(item: $shelfDestination) { destination in
            switch destination {
            case .guestTextClub:
                GuestTextClubView()
            case .scheduled:
                MarketingQueueView(viewModel: compose)
            case .drafts:
                MarketingDraftsView(viewModel: compose) { draft in
                    openDraft(draft)
                }
            }
        }
        .sheet(item: $studioSeed) { seed in
            CampaignStudioView(seed: seed, connected: viewModel.channels, isOwner: isOwner,
                               canPublish: canPublish) {
                Task {
                    async let a: Void = campaigns.load()
                    async let b: Void = opportunities.load(quiet: true)
                    _ = await (a, b)
                }
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Marketing")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Marketing") }
        // Three tabs: back from Campaigns or Analytics returns to Content,
        // never out of Marketing (parity follow-up 10/7/26).
        .cavnarTabSwipeNavigation($subTab, tabs: MarketingSubTab.allCases)
        .keyboardNavToolbar($focusedField)
        .task { await viewModel.load() }
        .task { await opportunities.load() }
        .task(id: subTab) {
            if subTab == .campaigns { await campaigns.load() }
        }
        .task {
            guard !focusApplied else { return }
            focusApplied = true
            await applyFocus()
        }
        // New copy is a new draft, not an edit of the one last opened —
        // once it lands; a generate that fails keeps the draft and its id.
        .onChange(of: viewModel.generatedCount) { _, _ in
            compose.savedDraftID = nil
        }
        .confirmationDialog("Replace your edited post?",
                            isPresented: Binding(get: { pendingGenerate != nil },
                                                 set: { if !$0 { pendingGenerate = nil } }),
                            titleVisibility: .visible, presenting: pendingGenerate) { pending in
            Button("Write a new one", role: .destructive) {
                Task { await pending.run() }
            }
            Button("Keep mine", role: .cancel) {}
        } message: { _ in
            Text("Cavnar AI writes a new post in place of the one you edited. You can undo it once.")
        }
        .confirmationDialog("Plan a new week?", isPresented: $confirmingNewWeek, titleVisibility: .visible) {
            Button("Plan a new week", role: .destructive) {
                Task { await viewModel.generateCalendar() }
            }
            Button("Keep this week", role: .cancel) {}
        } message: {
            Text("Seven new ideas replace this week\u{2019}s. Posts you already wrote stay in Drafts.")
        }
        // The content tab's one outcome figure reads the 30-day window —
        // the window alone, not the whole Analytics load (which records the
        // brief's lines as shown).
        .task { await analyticsViewModel.setWindow(30) }
        // The web h1's status line (marketing_signals.header_summary), when
        // the server has the route; the window above is the fallback.
        .task { header = try? await APIClient.shared.send("/mobile/api/marketing/header", hapticOnError: false) }
        // Reopening the app after a while re-reads whichever tab is on
        // screen rather than showing earlier numbers as current (audit 4.2).
        .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) {
            switch subTab {
            case .content: await viewModel.load()
            case .campaigns: await campaigns.load()
            case .analytics: await analyticsViewModel.refresh()
            }
        }
        .onChange(of: viewModel.calendar.map(\.id)) { _, _ in
            selectedDay = viewModel.calendar.firstIndex(where: \.isToday) ?? 0
        }
        // Keyed to the tab so coming back to Analytics picks up numbers that
        // moved while you were writing, instead of holding the first load
        // forever.
        .task(id: subTab) {
            if subTab == .analytics { await analyticsViewModel.load() }
        }
        .task {
            await compose.loadScheduled()
            await compose.loadDrafts()
        }
        #if DEBUG
        // Debug-only deep link, same shape as RootView's autologin hook: only
        // fires when the launching process explicitly sets it, so it can never
        // reach a TestFlight or release build. Exists so a pushed marketing
        // screen can be opened and looked at directly, rather than driven
        // through the tile grid by hand.
        .task {
            switch ProcessInfo.processInfo.environment["CAVNAR_DEBUG_MARKETING_SHELF"] {
            case "drafts": shelfDestination = .drafts
            case "scheduled": shelfDestination = .scheduled
            case "guest": shelfDestination = .guestTextClub
            default: break
            }
        }
        #endif
        .sheet(isPresented: $showingPreview) {
            MarketingPreviewSheet(
                platform: previewPlatform,
                text: viewModel.draft,
                ctaType: viewModel.isGooglePost ? viewModel.googleCTA.rawValue : nil,
                viewModel: compose)
        }
        .sheet(isPresented: $showingSchedule) {
            MarketingScheduleSheet(
                platforms: schedulePlatforms, text: viewModel.draft, topic: viewModel.topic,
                contentType: viewModel.selectedType,
                ctaType: viewModel.isGooglePost ? viewModel.googleCTA.rawValue : nil,
                ctaURL: viewModel.isGooglePost ? viewModel.googleCTALink : nil,
                viewModel: compose)
        }
    }

    // MARK: - Pulse

    /// Whether marketing is working, not how busy Cavnar was (density #10):
    /// ONE outcome — the last 30 days' reach against the 30 before, toned —
    /// and the next post that is scheduled. The output counts (this month,
    /// generated, published) moved to Analytics. No card: a heartbeat, not
    /// a dashboard.
    @ViewBuilder
    private var outcomeRow: some View {
        let outcome = Self.outcome(analyticsViewModel.window)
        HStack(alignment: .top, spacing: CavnarSpace.m) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                CavnarKicker("Last \(header?.days ?? analyticsViewModel.window?.days ?? 30) days")
                if let status = header?.status, !status.isEmpty {
                    // The server's one sentence, the same the web h1 reads.
                    CavnarMixedText(status, role: .lead, color: header?.toneColor ?? .cavnarInk)
                    if analyticsViewModel.window?.posts ?? 0 > 0, let reach = analyticsViewModel.window?.reach {
                        HomeMixedText.make("\(reach.formatted()) reached", role: .secondary)
                    }
                } else {
                    HomeMixedText.make(outcome.headline, role: .figureM, color: .cavnarInk, numberColor: .cavnarInk)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                    if let change = outcome.change {
                        CavnarMixedText(change, role: .secondary, color: outcome.tone)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityElement(children: .combine)
            // NEXT POST opens "Scheduled & sent" — what is coming and what
            // went out (readability round #58).
            Button {
                Haptic.light()
                shelfDestination = .scheduled
            } label: {
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarKicker("Next post")
                    if let next = header?.nextScheduled, let when = next.whenLabel {
                        HomeMixedText.make(when, role: .label)
                            .fixedSize(horizontal: false, vertical: true)
                        if let platform = next.platform {
                            Text(platform.capitalized).cavnarText(.caption)
                        }
                    } else if let next = Self.nextScheduled(compose.scheduled) {
                        HomeMixedText.make(next.whenLabel, role: .label)
                            .fixedSize(horizontal: false, vertical: true)
                        Text(next.platform.capitalized).cavnarText(.caption)
                    } else {
                        Text("Nothing scheduled")
                            .cavnarText(.label, color: .cavnarAmber)
                    }
                    Text("Scheduled & sent \u{203A}")
                        .font(.cavnarBody(CavnarType.caption, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint("Opens what is scheduled and what went out")
        }
        .padding(.horizontal, CavnarSpace.xxs)
        .padding(.bottom, 2)
    }

    // MARK: - Waiting on you

    /// The saved posts still waiting on an approve — the oldest one with its
    /// words and an Approve right here, the rest a tap away in Drafts
    /// (readability round 10/8/26 #19). Approve is the server's draft
    /// approve (the same call the Drafts screen makes); nothing is posted.
    @ViewBuilder
    private var waitingPostsRow: some View {
        let waiting = compose.drafts.filter(\.canApprove)
        if let first = waiting.first {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                HStack(alignment: .firstTextBaseline) {
                    CavnarKicker(waiting.count == 1 ? "A post waiting for you" : "\(waiting.count) posts waiting for you")
                    Spacer(minLength: 0)
                    if waiting.count > 1 {
                        Button {
                            Haptic.light()
                            shelfDestination = .drafts
                        } label: {
                            Text("See all \u{203A}")
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                    }
                }
                if let topic = first.topic, !topic.isEmpty {
                    Text(topic).cavnarText(.label)
                }
                Text(first.body)
                    .cavnarText(.secondary)
                    .lineLimit(2)
                if let who = first.createdByName {
                    Text("Written by \(who)").cavnarText(.caption)
                }
                HStack(spacing: CavnarSpace.s) {
                    Button {
                        Haptic.light()
                        Task { await compose.approve(first) }
                    } label: {
                        Text("Approve").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    Button {
                        Haptic.light()
                        openDraft(first)
                        scrollToDraft = UUID()
                    } label: {
                        Text("Open").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                if let error = compose.draftError {
                    Text(error).cavnarText(.caption, color: .cavnarRedText)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    /// The outcome line from the 30-day window: reach and its change
    /// against the prior 30 days. "Nothing posted in 30 days" (amber) when
    /// nothing went out; the server's change note when the change is null
    /// (too few posts to compare), never a made-up 0%. Pure, so the rule is
    /// pinned by tests.
    struct Outcome: Equatable {
        let headline: String
        let change: String?
        let tone: Color
    }

    static func outcome(_ w: MarketingWindow?) -> Outcome {
        guard let w else { return Outcome(headline: "\u{2014}", change: nil, tone: .cavnarInk3) }
        guard w.posts > 0 else {
            return Outcome(headline: "Nothing posted", change: "No posts in the last \(w.days) days",
                           tone: .cavnarAmber)
        }
        let reach = "\(w.reach.formatted()) reached"
        guard let pct = w.change.reach else {
            return Outcome(headline: reach, change: w.changeNote, tone: .cavnarInk3)
        }
        let rounded = Int(pct.rounded())
        let arrow = rounded >= 0 ? "\u{25B2}" : "\u{25BC}"
        return Outcome(headline: reach,
                       change: "\(arrow) \(abs(rounded))% vs the \(w.days) days before",
                       tone: rounded >= 0 ? .cavnarGreen : .cavnarAmber)
    }

    /// The soonest post still waiting to go out.
    static func nextScheduled(_ posts: [ScheduledPost]) -> ScheduledPost? {
        posts.filter(\.isPending).min { $0.scheduledFor < $1.scheduledFor }
    }

    // MARK: - Shelf

    /// Scheduled & sent / Drafts as two compact tiles, each saying what it
    /// holds in words ("2 upcoming", "3 to approve"). The Text Club lives
    /// under Campaigns now (readability round #57), with the rest of guest
    /// texting.
    private var shelfTiles: some View {
        let toApprove = compose.drafts.filter(\.canApprove).count
        return HStack(spacing: CavnarSpace.xs) {
            shelfTile("Scheduled & sent", icon: "calendar.badge.clock",
                      status: compose.pendingCount > 0 ? "\(compose.pendingCount) upcoming" : "Nothing upcoming") {
                shelfDestination = .scheduled
            }
            shelfTile("Drafts", icon: "square.and.pencil",
                      status: toApprove > 0 ? "\(toApprove) to approve"
                          : (compose.drafts.isEmpty ? "None saved" : "\(compose.drafts.count) saved"),
                      highlight: toApprove > 0) {
                shelfDestination = .drafts
            }
        }
        // A Button driving each tile, not a NavigationLink — a
        // simultaneousGesture haptic on a NavigationLink races its own tap
        // handling (see HomeModuleGrid's identical reasoning); a Button's
        // action closure is deterministic, so the haptic and the push always
        // happen together.
    }

    private func shelfTile(_ title: String, icon: String, status: String, highlight: Bool = false,
                           action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs + 2) {
                Image(systemName: icon)
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarEmber2)
                Text(title)
                    .cavnarText(.label)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                HomeMixedText.make(status, role: .secondary, color: highlight ? .cavnarEmber2 : .cavnarInk2)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(CavnarSpace.s)
            .background(Color.cavnarPaper2)
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        }
        .buttonStyle(.plain)
    }

    // MARK: - Generator

    /// Open while there is a draft or one is being written; otherwise the
    /// owner opens it (M15).
    private var composerOpen: Bool { showingComposer || viewModel.hasDraft || viewModel.isGenerating }

    @ViewBuilder
    private var composerCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            disclosureHeader("Write a post", isOpen: composerOpen,
                             locked: viewModel.hasDraft || viewModel.isGenerating) {
                showingComposer.toggle()
            }
            if composerOpen {
                composerBody
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGenerating)
        .cavnarCard()
    }

    /// A section's own header that opens and closes it: the label, then a
    /// chevron; 44pt. `locked` keeps it open (a draft is on screen).
    private func disclosureHeader(_ title: String, isOpen: Bool, locked: Bool = false,
                                  toggle: @escaping () -> Void) -> some View {
        Button {
            guard !locked else { return }
            Haptic.light()
            withAnimation(.cavnarEase(0.22)) { toggle() }
        } label: {
            HStack(spacing: CavnarSpace.xs) {
                Text(title).cavnarText(.label)
                Spacer(minLength: 0)
                if !locked {
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(isOpen ? 180 : 0))
                        .accessibilityHidden(true)
                }
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityValue(locked ? "" : (isOpen ? "Expanded" : "Collapsed"))
    }

    @ViewBuilder
    private var composerBody: some View {
            TextField("Topic — optional, e.g. fall truffle menu", text: $viewModel.topic)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .topic)

            // One split button: the left side generates whatever type is
            // selected, the chevron opens the list to change it.
            CavnarSplitButton(
                icon: "sparkles",
                label: "Generate \(viewModel.selectedTypeLabel)",
                isLoading: viewModel.isGenerating,
                loadingText: "Generating…",
                // Full width — the label carries the selected type, so a
                // hugging pill resized on every change and dragged the
                // chevron with it. See CavnarSplitButton.fillsWidth.
                fillsWidth: true,
                action: { requestGenerate { await viewModel.generate() } }
            ) {
                ForEach(viewModel.socialContentTypes) { type in
                    Button {
                        viewModel.selectedType = type.id
                    } label: {
                        if viewModel.selectedType == type.id {
                            Label(type.label, systemImage: "checkmark")
                        } else {
                            Text(type.label)
                        }
                    }
                }
            }

            // Content is social-only (web d0ef5a85): a text or an email is
            // written in the Campaign Studio, under Campaigns → Create — one
            // "write" door per screen (readability round 10/8/26).

            if viewModel.isGenerating {
                // Skeleton lines say "content is streaming in", and nothing
                // streams here — the whole draft lands at once. The orb is
                // the app's own vocabulary for a model working.
                CavnarWorkingOrb(state: .composing, label: "Writing your \(viewModel.selectedTypeLabel.lowercased())…")
                    .padding(.vertical, 6)
                    .transition(.opacity)
            }

            if let error = viewModel.generateError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }

            if viewModel.hasDraft {
                draftEditor
                draftActions
                // Instagram needs a photo; every other destination is better
                // with one, so it lives with the draft rather than being
                // buried under the Instagram button the way the old URL
                // field was.
                MarketingPhotoPicker(viewModel: compose)
                // Where it goes; Post and Schedule are pinned at the bottom.
                publishSection
            }
    }

    /// Editable, because it has to be. Three of the six content types are
    /// written as TWO options ("Option 1 (Short & Punchy): …"), so a draft
    /// that goes out untouched publishes both versions and the labels.
    private var draftEditor: some View {
        VStack(alignment: .leading, spacing: 10) {
            // This is the one piece of text on the screen an owner has to
            // READ CLOSELY and then EDIT, on a phone, in a dim back office.
            // It was 16pt at default leading in a 10pt box — a solid wall of
            // small type. 17.5pt with real line spacing and 14pt of inset is
            // the difference between skimming it and working in it.
            // Sized by its text (a TextEditor with scrolling disabled lays
            // out to its content) above a floor, so a two-line caption gets
            // a compact box and a long one gets the room it needs, and the
            // page — not the box — is what scrolls.
            TextEditor(text: $viewModel.draft)
                .font(.cavnar(.lead))
                .lineSpacing(5)
                .foregroundStyle(Color.cavnarInk)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .frame(minHeight: 132)
                .padding(14)
                .background(Color.cavnarPaper)
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .stroke(viewModel.isOverLimit ? Color.cavnarRed : Color.cavnarEmber.opacity(0.3), lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                .focused($focusedField, equals: .draft)
                .id(Self.draftEditorAnchor)

            // What the post is about (the dish and occasion its result will
            // be read against) — the server's inference, shown so the owner
            // knows and can rewrite the topic if it guessed wrong.
            if let label = viewModel.draftTags?.label, !label.isEmpty {
                HStack(spacing: 8) {
                    Text("About").cavnarText(.caption)
                    AccountChip(text: label, muted: true)
                }
            }

            // The count, always; "Trim it" only when it is over the limit —
            // it used to say so under the limit too.
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                if viewModel.isOverLimit {
                    Text("Trim it before it goes out")
                        .cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if let limit = viewModel.characterLimit, let type = viewModel.selectedContentType {
                    HomeMixedText.make("\(viewModel.draft.count) / \(limit.formatted())"
                                       + (viewModel.isOverLimit ? " over \(type.limitLabel)" : ""),
                                       role: .caption,
                                       color: viewModel.isOverLimit ? .cavnarRedText : .cavnarInk3)
                        .layoutPriority(1)
                }
            }
        }
    }

    /// Scroll anchor for the caption box. "Write this" on a calendar day used
    /// to leave the reader at the bottom of the page: it filled the topic
    /// field and generated, but the draft appears hundreds of points ABOVE
    /// the calendar, so the one thing the tap produced was the one thing off
    /// screen. Now the tap carries the reader up to it.
    static let draftEditorAnchor = "marketing-draft-editor"

    /// Preview and Regenerate as two equal cells, and Copy / Save draft
    /// behind "More" (readability round #56) — four buttons of one weight
    /// sat between the words and Post. A grid's .flexible() columns are
    /// equal by definition, so a label that changes never re-splits the row.
    @ViewBuilder
    private var draftActions: some View {
        HStack(spacing: CavnarSpace.xs) {
            actionCell("Preview", systemImage: "eye") {
                showingPreview = true
            }
            actionCell("Regenerate", systemImage: "arrow.triangle.2.circlepath",
                       disabled: viewModel.isGenerating) {
                requestGenerate { await viewModel.generate() }
            }
            Menu {
                Button {
                    viewModel.copyDraft()
                } label: {
                    Label(viewModel.didCopyDraft ? "Copied" : "Copy the caption",
                          systemImage: viewModel.didCopyDraft ? "checkmark" : "doc.on.doc")
                }
                Button {
                    Task {
                        await compose.saveDraft(body: viewModel.draft, topic: viewModel.topic,
                                                contentType: viewModel.selectedType,
                                                draftRef: viewModel.draftRef, contentLogId: viewModel.contentLogId)
                    }
                } label: {
                    Label(compose.isSavingDraft ? "Saving\u{2026}" : "Save draft", systemImage: "tray.and.arrow.down")
                }
                .disabled(compose.isSavingDraft)
            } label: {
                Image(systemName: viewModel.didCopyDraft ? "checkmark" : "ellipsis")
                    .font(.cavnar(.label))
                    .foregroundStyle(viewModel.didCopyDraft ? Color.cavnarGreen : Color.cavnarEmber)
                    .frame(width: 50, height: 50)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                        .fill(Color.white.opacity(0.05)))
                    .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                    .contentShape(Rectangle())
            }
            .accessibilityLabel("More: copy or save the draft")
        }
        .animation(.easeOut(duration: 0.2), value: viewModel.didCopyDraft)
        // The draft a regenerate replaced, one tap back (M7).
        if viewModel.replacedDraft != nil && !viewModel.isGenerating {
            Button {
                viewModel.undoRegenerate()
            } label: {
                Label("Undo \u{2014} bring back the last draft", systemImage: "arrow.uturn.backward")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
    }

    /// A generate that would replace the owner's edits asks first (M7).
    private func requestGenerate(_ run: @escaping @MainActor () async -> Void) {
        if viewModel.draftHasEdits {
            pendingGenerate = PendingGenerate(run: run)
        } else {
            Task { await run() }
        }
    }

    /// Preview where it would go first: Google for a Google post, else the
    /// first channel switched on (M9) — it was always Instagram.
    private var previewPlatform: String {
        if viewModel.isGooglePost { return "google" }
        if let first = viewModel.socialTargets(hasMedia: compose.media != nil).first { return first.lowercased() }
        return viewModel.channels.instagram ? "instagram" : "facebook"
    }

    /// The channels Schedule queues to — the same targets Post would use.
    private var scheduleTargets: [String] {
        viewModel.isGooglePost ? ["google"]
            : viewModel.socialTargets(hasMedia: compose.media != nil).map { $0.lowercased() }
    }

    /// Who may post or schedule: the Campaigns overview's word, else the
    /// login's own (may_publish).
    private var canPublish: Bool {
        campaigns.overview?.canPublish ?? (sessionStore.currentUser?.mayPublishMarketing ?? false)
    }

    /// A teammate's "Save for the owner to send": the draft, saved.
    private func saveDraftForOwner() async {
        await compose.saveDraft(body: viewModel.draft, topic: viewModel.topic,
                                contentType: viewModel.selectedType,
                                draftRef: viewModel.draftRef, contentLogId: viewModel.contentLogId)
        if compose.draftError == nil { Haptic.success() }
    }

    private func actionCell(_ title: String, systemImage: String, tint: Color? = nil,
                            disabled: Bool = false, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .frame(maxWidth: .infinity)
                .frame(height: 22)
                .foregroundStyle(tint ?? Color.cavnarEmber)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: disabled))
        .disabled(disabled)
    }

    /// Queue it — beside Post in the pinned bar; only once a destination is
    /// connected.
    @ViewBuilder
    private var composeActions: some View {
        if viewModel.canPostSomewhere {
            // Every selected channel, one scheduled post each — and only
            // when Post itself could go (re-audit 10/8/26 H4): it used to
            // queue Instagram whenever Instagram was connected, switched
            // off, photo or not.
            let targets = scheduleTargets
            let blocked = targets.isEmpty || viewModel.isOverLimit
                || (viewModel.isGooglePost && viewModel.alreadyPosted(to: "Google"))
            Button {
                Haptic.light()
                schedulePlatforms = targets
                showingSchedule = true
            } label: {
                Text("Schedule")
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: blocked))
            .disabled(blocked)
        }
    }

    // MARK: - Publish

    /// Where the post goes — the channel switches, or Google's button — in
    /// the card; the Post itself is pinned (`socialPublish`,
    /// `googlePublish`).
    @ViewBuilder
    private var publishSection: some View {
        if !viewModel.canPostSomewhere {
            notConnectedNotice
        } else if viewModel.isGooglePost {
            googleOptions
        } else {
            socialChannels
        }

        if let posted = viewModel.postedPlatform {
            CavnarPostedCheck(label: "Posted to \(posted)")
                .frame(maxWidth: .infinity)
                .padding(.top, 6)
        }
        if let error = viewModel.postError {
            Text(error).cavnarText(.secondary, color: .cavnarRedText)
        }
    }

    /// A publish button for an account that isn't connected can only fail —
    /// so this says where to connect it (Account → Connections, where
    /// Instagram and Google connect from the phone) instead of offering a
    /// dead end.
    private var notConnectedNotice: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "link.badge.plus").foregroundStyle(Color.cavnarEmber)
            Text(viewModel.isGooglePost
                 ? "Connect Google Business under Account → Connections to publish this to your listing."
                 : "Connect Instagram or Facebook under Account → Connections to publish from here. You can still copy the caption and post it yourself.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(CavnarSpace.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    /// A switch per connected channel, on by default (Friction audit #41,
    /// U3-16) — in the card, above the pinned Post.
    @ViewBuilder
    private var socialChannels: some View {
        let hasMedia = compose.media != nil
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if viewModel.channels.instagram {
                Toggle(isOn: $viewModel.instagramSelected) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Instagram").cavnarText(.label)
                        if !hasMedia {
                            Text("Needs a photo — add one above.").cavnarText(.caption, color: .cavnarAmber)
                        } else if viewModel.alreadyPosted(to: "Instagram") {
                            Text("Posted").cavnarText(.caption, color: .cavnarGreen)
                        }
                    }
                }
                .tint(Color.cavnarEmber)
                .disabled(!hasMedia || viewModel.alreadyPosted(to: "Instagram"))
            }
            if viewModel.channels.facebook {
                Toggle(isOn: $viewModel.facebookSelected) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Facebook").cavnarText(.label)
                        if viewModel.alreadyPosted(to: "Facebook") {
                            Text("Posted").cavnarText(.caption, color: .cavnarGreen)
                        }
                    }
                }
                .tint(Color.cavnarEmber)
                .disabled(viewModel.alreadyPosted(to: "Facebook"))
            }
        }
    }

    /// ONE primary — "Post to Instagram and Facebook" — pinned in thumb
    /// reach. It posts outside the restaurant, so it confirms first, naming
    /// where the caption goes (DESIGN_SYSTEM §10's confirm/undo policy).
    @ViewBuilder
    private var socialPublish: some View {
        let hasMedia = compose.media != nil
        let targets = viewModel.socialTargets(hasMedia: hasMedia)
        Group {
            Button {
                Haptic.light()
                confirmingPostAll = true
            } label: {
                Group {
                    if viewModel.isPosting {
                        CavnarShimmerText(text: "Posting…", color: .white)
                    } else {
                        Text(targets.isEmpty ? "Post" : "Post to \(MarketingViewModel.channelList(targets))")
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: targets.isEmpty || viewModel.isPosting
                                                  || viewModel.isOverLimit))
            .disabled(targets.isEmpty || viewModel.isPosting || viewModel.isOverLimit)
            .confirmationDialog("Post this caption to \(MarketingViewModel.channelList(targets))?",
                                isPresented: $confirmingPostAll, titleVisibility: .visible) {
                Button(targets.count == 1 ? "Post to \(targets[0])" : "Post to \(targets.count) channels") {
                    Task { await viewModel.postToAll(media: compose.media) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("It goes live on your page right away.")
            }
        }
    }

    /// The Google post's button and its link, in the card.
    @ViewBuilder
    private var googleOptions: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            // The action button is the half of a Google post that converts,
            // and create_local_post has always accepted one.
            Picker("Button", selection: $viewModel.googleCTA) {
                ForEach(GoogleCallToAction.allCases) { cta in
                    Text(cta.label).tag(cta)
                }
            }
            .pickerStyle(.menu)
            .tint(Color.cavnarEmber)

            if viewModel.googleCTA.needsLink {
                TextField("Link for the button", text: $viewModel.googleCTALink)
                    .cavnarTextFieldStyle()
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .focused($focusedField, equals: .ctaLink)
            }
        }
    }

    /// "Post to Google", pinned. It goes live on the listing at once, with
    /// the photo above — confirmed first, like every other publish.
    @ViewBuilder
    private var googlePublish: some View {
        Group {
            Button {
                Haptic.light()
                confirmingGoogle = true
            } label: {
                Group {
                    if viewModel.isPosting {
                        CavnarShimmerText(text: "Posting\u{2026}", color: .white)
                    } else {
                        Text("Post to Google")
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isPosting || viewModel.isOverLimit || viewModel.alreadyPosted(to: "Google"))
            .confirmationDialog("Post this to your Google listing?", isPresented: $confirmingGoogle,
                                titleVisibility: .visible) {
                Button(compose.media == nil ? "Post to Google" : "Post with the photo") {
                    Task { await viewModel.postToGoogle(mediaId: compose.media?.id) }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("It goes live on your Business Profile right away.")
            }
        }
    }

    // MARK: - Week

    /// The week as a rail of seven days and ONE focused card — one orange
    /// call to action on screen instead of seven stacked cards each with
    /// its own. Tap a day (or the skip arrow) to move through the week;
    /// written days go green on the rail.
    @ViewBuilder
    private var weekSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                disclosureHeader("This week", isOpen: showingWeek || viewModel.isGeneratingCalendar,
                                 locked: viewModel.isGeneratingCalendar) {
                    showingWeek.toggle()
                }
                if let range = weekRangeLabel {
                    Text(range).font(.cavnarNumber(CavnarType.caption, weight: 500)).foregroundStyle(Color.cavnarInk2)
                        .fixedSize()
                }
                if showingWeek && !viewModel.calendar.isEmpty && !viewModel.isGeneratingCalendar {
                    // Seven ideas replaced at once: asked first (L5).
                    Button {
                        Haptic.light()
                        confirmingNewWeek = true
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarEmber)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Plan a new week")
                }
            }
            .padding(.horizontal, 4)

            if !showingWeek && !viewModel.isGeneratingCalendar {
                EmptyView()
            } else if viewModel.isGeneratingCalendar {
                // Planning a week of ideas is the model deciding what to do,
                // which is what `solving` depicts. It takes real seconds, so
                // it needs actual motion, not a label inside a dead button.
                CavnarWorkingOrb(state: .solving, label: "Planning your week…")
                    .padding(.vertical, 10)
                    .frame(maxWidth: .infinity)
                    .transition(.opacity)
            } else if viewModel.calendar.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Seven ideas for the week, built from your menu, your voice and what's coming up.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
                    Button {
                        Haptic.light()
                        Task { await viewModel.generateCalendar() }
                    } label: {
                        Text("Generate week").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                .cavnarCard()
            } else {
                weekRail
                if let idea = focusedIdea {
                    focusCard(idea)
                }
                // The week as a spreadsheet is the web's (W5).
                CavnarWebLinkRow(title: "Content calendar", subtitle: "Download the week as a spreadsheet",
                                 path: "marketing", actionLabel: "Open on the web")
            }

            if let error = viewModel.calendarError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGeneratingCalendar)
    }

    private var focusedIdea: ContentCalendarIdea? {
        guard !viewModel.calendar.isEmpty else { return nil }
        return viewModel.calendar[min(selectedDay, viewModel.calendar.count - 1)]
    }

    /// "9/6/26 – 9/12/26" from the first and last day's dates (M/D/YY,
    /// DESIGN_SYSTEM.md → Dates and times).
    private var weekRangeLabel: String? {
        guard let first = viewModel.calendar.first?.calendarDate,
              let last = viewModel.calendar.last?.calendarDate else { return nil }
        let a = CavnarDate.mdy(first), b = CavnarDate.mdy(last)
        return a == b ? a : "\(a) – \(b)"
    }

    private func select(_ index: Int) {
        guard index != selectedDay, viewModel.calendar.indices.contains(index) else { return }
        Haptic.light()
        withAnimation(.easeInOut(duration: 0.25)) { selectedDay = index }
        // That day's idea is on screen now: the server records it as shown
        // (only the opening day was recorded with the calendar). Fire and
        // forget — a failed note never blocks the rail.
        if let key = viewModel.calendar[index].recKey, !key.isEmpty {
            Task {
                _ = try? await APIClient.shared.send("/mobile/api/marketing/calendar/seen", method: .post,
                                                     body: ["rec_key": key], hapticOnError: false) as SeenReply
            }
        }
    }

    private struct SeenReply: Decodable { let ok: Bool? }

    private var weekRail: some View {
        ScrollView(.horizontal) {
            HStack(spacing: 6) {
                ForEach(Array(viewModel.calendar.enumerated()), id: \.element.id) { index, idea in
                    dayChip(idea, selected: index == selectedDay) { select(index) }
                }
            }
            .padding(.horizontal, 2)
            .padding(.vertical, 4)
        }
        .scrollIndicators(.hidden)
        // The selected chip lifts 3pt and throws a glow; without this the
        // scroll view's bounds clip both.
        .scrollClipDisabled()
    }

    private func dayChip(_ idea: ContentCalendarIdea, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 2) {
                Text(idea.dayAbbrev)
                    .cavnarText(.tag, color: .cavnarInk2)
                Text(idea.dayNumber)
                    .cavnarText(.figureS)
                // Email gets a symbol: a "✉" character renders as the color
                // emoji, which ignores foregroundStyle and can't go green.
                Group {
                    if idea.platformGlyph == "✉" {
                        Image(systemName: "envelope.fill").font(.cavnar(.tag))
                    } else {
                        Text(idea.platformGlyph).font(.cavnar(.tag))
                    }
                }
                .foregroundStyle(idea.written ? Color.cavnarGreen : Color.cavnarEmber2)
            }
            .padding(.vertical, CavnarSpace.xxs)
            // Grows with the text size instead of clipping it (L12).
            .frame(minWidth: 46, minHeight: 74)
            .background {
                if selected {
                    LinearGradient(colors: [Color.cavnarEmber.opacity(0.28), Color.cavnarEmber.opacity(0.08)],
                                   startPoint: .top, endPoint: .bottom)
                } else {
                    Color.cavnarPaper2
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 14)
                .strokeBorder(selected ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .shadow(color: Color.cavnarEmber.opacity(selected ? 0.25 : 0), radius: 10, y: 8)
            .offset(y: selected ? -3 : 0)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(idea.day), \(idea.platform)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    /// The day's idea, set as a headline, with the one button that matters.
    private func focusCard(_ idea: ContentCalendarIdea) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                CavnarKicker(idea.platform)
                    .contentTransition(.opacity)
                Spacer()
                HomeMixedText.make(focusDateLabel(idea), role: .caption)
                    .contentTransition(.opacity)
            }

            Text(idea.angle)
                .cavnarText(.headline)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentTransition(.opacity)
                .padding(.top, 10)
                .padding(.bottom, 16)

            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    requestGenerate { await write(idea) }
                } label: {
                    // Secondary: the screen's one primary is its next
                    // best move (M15); grows with the text size (L12).
                    Group {
                        if viewModel.isGenerating {
                            CavnarShimmerText(text: "Writing…")
                        } else if justWrote {
                            Label("Written", systemImage: "checkmark")
                                .foregroundStyle(Color.cavnarGreen)
                        } else {
                            Text("Write this")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.isGenerating))
                .disabled(viewModel.isGenerating)
                .animation(.easeOut(duration: 0.2), value: justWrote)

                Button {
                    select((selectedDay + 1) % max(viewModel.calendar.count, 1))
                } label: {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minWidth: 50, minHeight: 50)
                        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                            .strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1.5))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Next day")
            }

            // The idea is a recommendation: Done / Not for us answer it
            // without writing (Marketing has no metric to Track).
            if idea.showsAnswers, let key = idea.recKey {
                RecAnswerRow(key: key, surface: "marketing", module: "marketing")
                    .padding(.top, 10)
            }

            HStack(spacing: 5) {
                ForEach(viewModel.calendar.indices, id: \.self) { i in
                    Capsule()
                        .fill(i == selectedDay ? Color.cavnarEmber : Color.cavnarPaper3)
                        .frame(width: i == selectedDay ? 16 : 5, height: 5)
                }
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 14)
            .animation(.easeInOut(duration: 0.25), value: selectedDay)
        }
        .padding(18)
        .background(
            LinearGradient(stops: [.init(color: Color.cavnarEmber.opacity(0.16), location: 0),
                                   .init(color: Color.cavnarPaper2, location: 0.6)],
                           startPoint: .topLeading, endPoint: .bottomTrailing))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous)
            .strokeBorder(Color.cavnarEmber2.opacity(0.35), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
    }

    /// "Sun 6 · Today", or just "Sun 6".
    private func focusDateLabel(_ idea: ContentCalendarIdea) -> String {
        var s = idea.dayAbbrev
        if !idea.dayNumber.isEmpty { s += " \(idea.dayNumber)" }
        if idea.isToday { s += " · Today" }
        return s
    }

    /// Tapping Write writes the focused day — and carries the reader UP to
    /// the draft, which lands hundreds of points above the calendar. Only
    /// scroll once there is actually something there to scroll to.
    private func write(_ idea: ContentCalendarIdea) async {
        // A calendar SMS or Email idea is written in the Campaign Studio
        // on that channel, never as a post (AUX-4).
        if let channel = MarketingContentType.guestChannel(of: idea.type) {
            studioSeed = StudioSeed(prompt: idea.angle, channels: [channel == "email" ? .email : .text],
                                    recKey: idea.recKey, autoCreate: true)
            return
        }
        await viewModel.generate(from: idea)
        guard viewModel.hasDraft else { return }
        justWrote = true
        scrollToDraft = UUID()
        try? await Task.sleep(for: .seconds(1.4))
        justWrote = false
    }
}

// MARK: - Routing and the Studio

extension MarketingView: MarketingFocusTarget {
    static let opportunitiesAnchor = "marketing-opportunities"

    var isOwner: Bool { sessionStore.currentUser?.isOwner ?? false }

    /// "Draft it" on a feed card: the Studio, the goal typed and only the
    /// card's channels that can reach someone on; the card's key rides on
    /// every send (rec_key).
    func draftFromCard(_ card: MarketingOpportunity) {
        let wanted = card.wantedChannels.compactMap(StudioChannel.init(rawValue:))
        studioSeed = StudioSeed(prompt: card.goal, channels: wanted, recKey: card.key, autoCreate: true)
    }

    /// A saved draft opened from the shelf: a post goes to the composer; a
    /// text or an email draft (the old quiet-night guest text, a saved
    /// Re-engagement text or Weekly email) goes to the Studio on its
    /// channel with its words (StudioSeed.savedDraft — the web's
    /// _mktDraftToStudio).
    func openDraft(_ draft: MarketingDraft) {
        // An expired draft can't be posted or sent; the row doesn't offer
        // Open, and this holds regardless.
        guard draft.canOpenInComposer else { return }
        if let seed = StudioSeed.savedDraft(draft) {
            shelfDestination = nil
            studioSeed = seed
            return
        }
        viewModel.draft = draft.body
        viewModel.hasDraft = true
        viewModel.markDraftBaseline()
        if let type = draft.contentType { viewModel.selectedType = type }
        viewModel.topic = draft.topic ?? ""
        // Its id and photo too: Save updates this draft, and
        // Post / Schedule carry the photo it was saved with.
        compose.open(draft)
        shelfDestination = nil
    }

    /// Lands where the route asked (MarketingFocus.plan): the sub-tab, the
    /// feed card, the Text Club, the queue, the drafts — and a drafted post
    /// by id. The quiet-night push names a card AND its drafted post: both
    /// happen, the card lit in the feed and the post in the composer.
    func applyFocus() async {
        await focus.plan.apply(to: self)
    }

    func land(on tab: MarketingSubTab) { subTab = tab }

    func open(shelf: MarketingShelfDestination) { shelfDestination = shelf }

    func focusCard(_ key: String) async {
        if !opportunities.loaded { await opportunities.load() }
        await opportunities.focus(key)
    }

    func scrollToFeed() { scrollToOpportunities = UUID() }

    func openDraft(id: Int) async {
        if !compose.drafts.contains(where: { $0.id == id }) { await compose.loadDrafts() }
        if let draft = compose.drafts.first(where: { $0.id == id }), draft.canOpenInComposer {
            subTab = .content
            openDraft(draft)
            scrollToDraft = UUID()
        } else {
            shelfDestination = .drafts
        }
    }
}

/// GET /mobile/api/marketing/header — the web Marketing h1's status
/// (marketing_signals.header_summary): "Reach up 18% vs the 30 days
/// before" / "Nothing posted in 12 days", its tone, and the next scheduled
/// post. Every field lenient; an older server sends none, and the content
/// tab reads the 30-day window itself.
struct MarketingHeader: Decodable, Equatable {
    struct Next: Decodable, Equatable {
        let date: String?
        let time: String?
        let platform: String?
        init(from decoder: Decoder) throws {
            let c = try? decoder.container(keyedBy: CodingKeys.self)
            date = (try? c?.decodeIfPresent(String.self, forKey: .date)) ?? nil
            time = (try? c?.decodeIfPresent(String.self, forKey: .time)) ?? nil
            platform = (try? c?.decodeIfPresent(String.self, forKey: .platform)) ?? nil
        }
        enum CodingKeys: String, CodingKey { case date, time, platform }
        /// "9/26/26 · 5:00pm"
        var whenLabel: String? {
            guard let date, !date.isEmpty else { return nil }
            guard let time, !time.isEmpty else { return date }
            return "\(date) \u{00B7} \(time)"
        }
    }

    let status: String?
    let tone: String?
    let days: Int?
    let nextScheduled: Next?

    enum CodingKeys: String, CodingKey {
        case status, tone, days
        case nextScheduled = "next_scheduled"
    }

    init(from decoder: Decoder) throws {
        let c = try? decoder.container(keyedBy: CodingKeys.self)
        status = (try? c?.decodeIfPresent(String.self, forKey: .status)) ?? nil
        tone = (try? c?.decodeIfPresent(String.self, forKey: .tone)) ?? nil
        days = (try? c?.decodeIfPresent(Int.self, forKey: .days)) ?? nil
        nextScheduled = (try? c?.decodeIfPresent(Next.self, forKey: .nextScheduled)) ?? nil
    }

    var toneColor: Color {
        switch tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        case "bad": return .cavnarRed
        default: return .cavnarInk
        }
    }
}

/// Where a route asked Marketing to land, read once from the route: the
/// section, the Opportunity Feed card and the drafted post. The
/// quiet-night push (push.nav_for) is
/// "marketing/opportunities?card=slow_day:Tue&post_draft_id=41" — a card
/// and a post, kept apart so neither is lost (re-audit 10/8/26: the post's
/// id used to stand in for the card, and the post never opened).
struct MarketingFocus: Equatable {
    var section = ""
    /// An Opportunity Feed card's key.
    var card: String?
    /// A drafted post's id.
    var draftId: Int?
    /// Anything else the route named after the section.
    var item: String?

    init(section: String? = nil, card: String? = nil, draftId: Int? = nil, item: String? = nil) {
        self.section = (section ?? "").lowercased()
        self.card = card.flatMap { $0.isEmpty ? nil : $0 }
        self.draftId = draftId
        self.item = item.flatMap { $0.trimmingCharacters(in: .whitespaces).isEmpty ? nil : $0.trimmingCharacters(in: .whitespaces) }
    }

    init(route: ModuleRoute?) {
        let query = route?.navPath?.query ?? [:]
        let draftParam = query["post_draft_id"].flatMap { $0.isEmpty ? nil : $0 }
        // ModuleRoute puts the post_draft_id in itemId; the item proper is
        // what the path itself names ("marketing/opportunities/<key>").
        let pathItem = route?.navPath?.rest.dropFirst().first
        let rawItem = draftParam != nil ? pathItem : (route?.itemId ?? pathItem)
        self.init(section: route?.section, card: query["card"], draftId: draftParam.flatMap { Int($0) },
                  item: rawItem)
    }

    /// What landing there does, in order: the tab or shelf, the card lit
    /// in the feed, then the drafted post opened in the composer.
    struct Plan: Equatable {
        var subTab: MarketingSubTab?
        var shelf: MarketingShelfDestination?
        var card: String?
        var draftId: Int?
        /// Bring the feed into view (a card with no post to open).
        var scrollToFeed = false
    }

    /// MarketingView.applyFocus: the plan, carried out on the screen.
    @MainActor
    static func apply(_ p: Plan, to target: some MarketingFocusTarget) async {
        if let tab = p.subTab { target.land(on: tab) }
        if let shelf = p.shelf { target.open(shelf: shelf) }
        if let card = p.card { await target.focusCard(card) }
        if let id = p.draftId {
            await target.openDraft(id: id)
        } else if p.scrollToFeed {
            target.scrollToFeed()
        }
    }

    var plan: Plan {
        var p = Plan()
        switch section {
        case "campaigns", "campaign", "guests", "newsletter":
            p.subTab = .campaigns
        case "analytics":
            p.subTab = .analytics
        case "text-club", "textclub", "guest-text-club", "contacts":
            p.shelf = .guestTextClub
        case "scheduled", "queue", "schedule":
            p.shelf = .scheduled
        case "opportunities", "opportunity":
            p.subTab = .content
            p.card = card ?? item
            p.draftId = draftId
            p.scrollToFeed = draftId == nil
        case "drafts", "draft":
            p.draftId = draftId ?? item.flatMap { Int($0) }
            if p.draftId == nil { p.shelf = .drafts }
        case "", "marketing", "content":
            p.draftId = draftId ?? item.flatMap { Int($0) }
            if let card { p.subTab = .content; p.card = card; p.scrollToFeed = p.draftId == nil }
        default:
            p.draftId = draftId
        }
        return p
    }
}

extension MarketingFocus.Plan {
    @MainActor
    func apply(to target: some MarketingFocusTarget) async { await MarketingFocus.apply(self, to: target) }
}

/// A generate held behind "Replace your edited post?" (re-audit 10/8/26 M7):
/// what runs once the owner says to write a new one.
struct PendingGenerate {
    let run: @MainActor () async -> Void
}

/// What landing on a Marketing focus needs of the screen — MarketingView,
/// or a recorder in a test.
@MainActor
protocol MarketingFocusTarget {
    func land(on tab: MarketingSubTab)
    func open(shelf: MarketingShelfDestination)
    func focusCard(_ key: String) async
    func openDraft(id: Int) async
    func scrollToFeed()
}
