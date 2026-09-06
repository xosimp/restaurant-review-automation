import SwiftUI

private enum MarketingSubTab: String, CaseIterable, Identifiable {
    case content = "Content"
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
private enum MarketingShelfDestination: String, Identifiable {
    case guestTextClub, scheduled, drafts
    var id: String { rawValue }
}

struct MarketingView: View {
    @State private var viewModel = MarketingViewModel()
    @State private var analyticsViewModel = MarketingAnalyticsViewModel()
    @State private var compose = MarketingComposeViewModel()
    @State private var subTab: MarketingSubTab = .content
    @State private var showingPreview = false
    @State private var showingSchedule = false
    @State private var schedulePlatform = "instagram"
    @State private var shelfDestination: MarketingShelfDestination?
    /// Bumped when a calendar day's "Write this" finishes — the ScrollViewReader
    /// watches it and moves to the caption box. A token rather than a Bool so
    /// tapping a second day still scrolls.
    @State private var scrollToDraft: UUID?
    /// Index into viewModel.calendar of the day the focus card is showing.
    /// Starts on today when today is in the week, else the first day.
    @State private var selectedDay = 0
    /// True for a beat after "Write this" succeeds so the button can say so.
    @State private var justWrote = false
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
                    if subTab == .content {
                        if let stats = viewModel.stats {
                            pulseRow(stats)
                            shelfTiles
                            composerCard
                            weekSection
                        } else if viewModel.isLoading {
                            CavnarLoadingOrb().padding(.top, 60).frame(maxWidth: .infinity)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        MarketingAnalyticsSection(viewModel: analyticsViewModel)
                    }
                }
                .padding(20)
            }
            // Refreshes whichever tab is actually on screen. This always
            // reloaded the CONTENT view model regardless, so pulling on
            // Analytics did nothing visible.
            .cavnarEmberRefreshable {
                if subTab == .content {
                    await viewModel.load()
                } else {
                    await analyticsViewModel.refresh()
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
                    viewModel.draft = draft.body
                    viewModel.hasDraft = true
                    if let type = draft.contentType { viewModel.selectedType = type }
                    viewModel.topic = draft.topic ?? ""
                    shelfDestination = nil
                }
            }
        }
        .cavnarModuleBackground()
        .navigationTitle("Marketing")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Marketing") }
        .cavnarTabSwipeNavigation($subTab, primaryTab: .content, secondaryTab: .analytics)
        .keyboardNavToolbar($focusedField)
        .task { await viewModel.load() }
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
                platform: viewModel.isGooglePost ? "google" : "instagram",
                text: viewModel.draft,
                ctaType: viewModel.isGooglePost ? viewModel.googleCTA.rawValue : nil,
                viewModel: compose)
        }
        .sheet(isPresented: $showingSchedule) {
            MarketingScheduleSheet(
                platform: schedulePlatform, text: viewModel.draft, topic: viewModel.topic,
                contentType: viewModel.selectedType,
                ctaType: viewModel.isGooglePost ? viewModel.googleCTA.rawValue : nil,
                ctaURL: viewModel.isGooglePost ? viewModel.googleCTALink : nil,
                viewModel: compose)
        }
    }

    // MARK: - Pulse

    /// Three numbers in a row, no card — a heartbeat, not a dashboard. The
    /// heavy stats card up top made the page open on a wall of chrome
    /// before the thing you came to do.
    @ViewBuilder
    private func pulseRow(_ stats: MarketingStats) -> some View {
        HStack(alignment: .top, spacing: 0) {
            pulseStat("\(stats.thisMonth)", "This month")
            pulseStat("\(stats.generated)", "Generated")
            pulseStat("\(stats.published)", "Published")
        }
        .padding(.horizontal, 4)
        .padding(.bottom, 2)
    }

    private func pulseStat(_ value: String, _ label: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value)
                .font(.cavnarNumber(24, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Shelf

    /// Text Club / Scheduled / Drafts as three compact tiles with their
    /// counts, instead of three full-width rows stacked under the stats.
    private var shelfTiles: some View {
        HStack(spacing: 8) {
            shelfTile("Text Club", icon: "message.fill", count: viewModel.guestTextable) {
                shelfDestination = .guestTextClub
            }
            shelfTile("Scheduled", icon: "calendar.badge.clock", count: compose.pendingCount) {
                shelfDestination = .scheduled
            }
            shelfTile("Drafts", icon: "square.and.pencil", count: compose.drafts.count) {
                shelfDestination = .drafts
            }
        }
        // A Button driving each tile, not a NavigationLink — a
        // simultaneousGesture haptic on a NavigationLink races its own tap
        // handling (see HomeModuleGrid's identical reasoning); a Button's
        // action closure is deterministic, so the haptic and the push always
        // happen together.
    }

    private func shelfTile(_ title: String, icon: String, count: Int, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                Text(title)
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                Text("\(count)")
                    .font(.cavnarNumber(18, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 12)
            .padding(.horizontal, 10)
            .padding(.bottom, 10)
            .background(Color.cavnarPaper2)
            .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
        }
        .buttonStyle(.plain)
    }

    // MARK: - Generator

    @ViewBuilder
    private var composerCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Write something")
                .font(.cavnarBody(16, weight: 700))
                .foregroundStyle(Color.cavnarInk)

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
                action: { Task { await viewModel.generate() } }
            ) {
                ForEach(viewModel.contentTypes) { type in
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

            if viewModel.isGenerating {
                // Skeleton lines say "content is streaming in", and nothing
                // streams here — the whole draft lands at once. The orb is
                // the app's own vocabulary for a model working.
                CavnarWorkingOrb(state: .composing, label: "Writing your \(viewModel.selectedTypeLabel.lowercased())…")
                    .padding(.vertical, 6)
                    .transition(.opacity)
            }

            if let error = viewModel.generateError {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }

            if viewModel.hasDraft {
                draftEditor
                // Instagram needs a photo; every other destination is better
                // with one, so it lives with the draft rather than being
                // buried under the Instagram button the way the old URL
                // field was.
                MarketingPhotoPicker(viewModel: compose)
                draftActions
                composeActions
                publishSection
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGenerating)
        .cavnarCard()
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
                .font(.cavnarBody(16.5))
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

            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("Trim it before it goes out")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if let limit = viewModel.characterLimit, let type = viewModel.selectedContentType {
                    (Text("\(viewModel.draft.count)").font(.cavnarNumber(14, weight: 700))
                        + Text(" / ")
                        + Text(limit.formatted()).font(.cavnarNumber(14))
                        + Text(viewModel.isOverLimit ? " over \(type.limitLabel)" : ""))
                        .font(.cavnarBody(14))
                        .foregroundStyle(viewModel.isOverLimit ? Color.cavnarRed : Color.cavnarInk3)
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

    /// Copy, Regenerate, Preview, Save draft — four cells of one grid, so
    /// they are the same size whatever their labels say. They were two
    /// HStacks, and an HStack hands each child its ideal width first: when
    /// Copy became "Copied ✓" it measured wider, the row re-split, and the
    /// button visibly grew and shrank back. A grid's .flexible() columns are
    /// equal by definition, and a fixed cell height keeps a two-line label
    /// from ever changing the row.
    private var draftActions: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                  spacing: 10) {
            actionCell(viewModel.didCopyDraft ? "Copied" : "Copy",
                       systemImage: viewModel.didCopyDraft ? "checkmark" : "doc.on.doc",
                       tint: viewModel.didCopyDraft ? Color.cavnarGreen : nil) {
                viewModel.copyDraft()
            }
            actionCell("Regenerate", systemImage: "arrow.triangle.2.circlepath",
                       disabled: viewModel.isGenerating) {
                Task { await viewModel.generate() }
            }
            actionCell("Preview", systemImage: "eye") {
                showingPreview = true
            }
            actionCell(compose.isSavingDraft ? "Saving…" : "Save draft",
                       systemImage: "tray.and.arrow.down", disabled: compose.isSavingDraft) {
                Task {
                    await compose.saveDraft(body: viewModel.draft, topic: viewModel.topic,
                                            contentType: viewModel.selectedType)
                }
            }
        }
        .animation(.easeOut(duration: 0.2), value: viewModel.didCopyDraft)
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

    /// Queue it — kept apart from the four editing actions above because it
    /// publishes, and only appears once a destination is connected.
    @ViewBuilder
    private var composeActions: some View {
        if viewModel.canPostSomewhere {
            Button {
                schedulePlatform = viewModel.isGooglePost
                    ? "google" : (viewModel.channels.instagram ? "instagram" : "facebook")
                showingSchedule = true
            } label: {
                Label("Schedule", systemImage: "calendar.badge.clock").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
        }
    }

    // MARK: - Publish

    @ViewBuilder
    private var publishSection: some View {
        if !viewModel.canPostSomewhere {
            notConnectedNotice
        } else if viewModel.isGooglePost {
            googlePublish
        } else {
            socialPublish
        }

        if let posted = viewModel.postedPlatform {
            CavnarPostedCheck(label: "Posted to \(posted)")
                .frame(maxWidth: .infinity)
                .padding(.top, 6)
        }
        if let error = viewModel.postError {
            Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
        }
    }

    /// A publish button for an account that isn't connected can only fail, and
    /// Instagram/Facebook has no connect flow in the app — so this says where
    /// to go instead of offering a dead end.
    private var notConnectedNotice: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "link.badge.plus").foregroundStyle(Color.cavnarEmber)
            Text(viewModel.isGooglePost
                 ? "Connect Google Business under Account → Connections to publish this to your listing."
                 : "Connect Instagram or Facebook under Account → Connections to publish from here. You can still copy the caption and post it yourself.")
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    @ViewBuilder
    private var socialPublish: some View {
        HStack(spacing: 10) {
            if viewModel.channels.instagram {
                Button {
                    Task { await viewModel.postToInstagram(imageURL: compose.media?.url) }
                } label: {
                    Text("Post to Instagram").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.isPosting || viewModel.isOverLimit || compose.media == nil)
            }
            if viewModel.channels.facebook {
                Button {
                    Task { await viewModel.postToFacebook() }
                } label: {
                    Text("Post to Facebook").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(viewModel.isPosting || viewModel.isOverLimit)
            }
        }
    }

    @ViewBuilder
    private var googlePublish: some View {
        VStack(alignment: .leading, spacing: 10) {
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

            Button {
                Task { await viewModel.postToGoogle() }
            } label: {
                Text("Post to Google").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isPosting || viewModel.isOverLimit)
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
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("This week")
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if let range = weekRangeLabel {
                    Text(range).font(.cavnarNumber(13)).foregroundStyle(Color.cavnarInk3)
                }
                if !viewModel.calendar.isEmpty && !viewModel.isGeneratingCalendar {
                    Button {
                        Haptic.light()
                        Task { await viewModel.generateCalendar() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .accessibilityLabel("Plan a new week")
                    ShareLink(item: viewModel.calendarCSV,
                              preview: SharePreview("Content calendar")) {
                        Image(systemName: "square.and.arrow.up")
                            .font(.system(size: 14, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                }
            }
            .padding(.horizontal, 4)

            if viewModel.isGeneratingCalendar {
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
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
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
            }

            if let error = viewModel.calendarError {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGeneratingCalendar)
    }

    private var focusedIdea: ContentCalendarIdea? {
        guard !viewModel.calendar.isEmpty else { return nil }
        return viewModel.calendar[min(selectedDay, viewModel.calendar.count - 1)]
    }

    /// "Sep 6 – 12" from the first and last day's dates.
    private var weekRangeLabel: String? {
        guard let first = viewModel.calendar.first?.calendarDate,
              let last = viewModel.calendar.last?.calendarDate else { return nil }
        let month = first.formatted(.dateTime.month(.abbreviated))
        let lastMonth = last.formatted(.dateTime.month(.abbreviated))
        let a = first.formatted(.dateTime.day())
        let b = last.formatted(.dateTime.day())
        return month == lastMonth ? "\(month) \(a) – \(b)" : "\(month) \(a) – \(lastMonth) \(b)"
    }

    private func select(_ index: Int) {
        guard index != selectedDay, viewModel.calendar.indices.contains(index) else { return }
        Haptic.light()
        withAnimation(.easeInOut(duration: 0.25)) { selectedDay = index }
    }

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
                Text(idea.dayAbbrev.uppercased())
                    .font(.cavnarBody(11))
                    .tracking(0.6)
                    .foregroundStyle(Color.cavnarInk3)
                Text(idea.dayNumber)
                    .font(.cavnarNumber(18, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                // Email gets a symbol: a "✉" character renders as the color
                // emoji, which ignores foregroundStyle and can't go green.
                Group {
                    if idea.platformGlyph == "✉" {
                        Image(systemName: "envelope.fill").font(.system(size: 10, weight: .bold))
                    } else {
                        Text(idea.platformGlyph).font(.cavnarBody(10, weight: 700))
                    }
                }
                .foregroundStyle(idea.written ? Color.cavnarGreen : Color.cavnarEmber2)
            }
            .frame(width: 46, height: 74)
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
                Text(idea.platform.uppercased())
                    .font(.cavnarBody(12, weight: 700))
                    .tracking(1.4)
                    .foregroundStyle(Color.cavnarEmber2)
                    .contentTransition(.opacity)
                Spacer()
                Text(focusDateLabel(idea))
                    .font(.cavnarNumber(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .contentTransition(.opacity)
            }

            Text(idea.angle)
                .font(.cavnarHeadline(21))
                .foregroundStyle(Color.cavnarInk)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentTransition(.opacity)
                .padding(.top, 10)
                .padding(.bottom, 16)

            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    Task { await write(idea) }
                } label: {
                    Group {
                        if viewModel.isGenerating {
                            CavnarShimmerText(text: "Writing…")
                        } else if justWrote {
                            Label("Written", systemImage: "checkmark")
                        } else {
                            Text("Write this")
                        }
                    }
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(.white)
                    .frame(maxWidth: .infinity)
                    .frame(height: 50)
                    .background(justWrote ? Color.cavnarGreen : Color.cavnarEmber)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
                    .shadow(color: (justWrote ? Color.cavnarGreen : Color.cavnarEmber).opacity(0.32), radius: 13, y: 10)
                }
                .buttonStyle(.plain)
                .disabled(viewModel.isGenerating)
                .animation(.easeOut(duration: 0.2), value: justWrote)

                Button {
                    select((selectedDay + 1) % max(viewModel.calendar.count, 1))
                } label: {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(width: 50, height: 50)
                        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                            .strokeBorder(Color.cavnarEmber.opacity(0.5), lineWidth: 1.5))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Next day")
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
        await viewModel.generate(from: idea)
        guard viewModel.hasDraft else { return }
        justWrote = true
        scrollToDraft = UUID()
        try? await Task.sleep(for: .seconds(1.4))
        justWrote = false
    }
}
