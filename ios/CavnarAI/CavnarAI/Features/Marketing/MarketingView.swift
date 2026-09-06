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
                            statsCard(stats)
                            shelfRows
                            generatorSection
                            calendarSection
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

    // MARK: - Header

    @ViewBuilder
    private func statsCard(_ stats: MarketingStats) -> some View {
        HStack(spacing: 0) {
            statTile(value: "\(stats.thisMonth)", label: "This month")
            Divider()
            statTile(value: "\(stats.generated)", label: "Generated")
            Divider()
            statTile(value: "\(stats.published)", label: "Published")
        }
        .cavnarCard()
    }

    private func statTile(value: String, label: String) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(24, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private var shelfRows: some View {
        VStack(spacing: 10) {
            shelfRow("Guest Text Club", icon: "message", badge: nil) { shelfDestination = .guestTextClub }
            shelfRow("Scheduled", icon: "calendar.badge.clock",
                     badge: compose.pendingCount > 0 ? "\(compose.pendingCount)" : nil) {
                shelfDestination = .scheduled
            }
            shelfRow("Drafts", icon: "square.and.pencil",
                     badge: compose.drafts.isEmpty ? nil : "\(compose.drafts.count)") {
                shelfDestination = .drafts
            }
        }
        // A Button driving this, not three NavigationLinks — a
        // simultaneousGesture haptic on a NavigationLink races its own tap
        // handling (see HomeModuleGrid's identical reasoning); a Button's
        // action closure is deterministic, so the haptic and the push always
        // happen together.
    }

    private func shelfRow(_ title: String, icon: String, badge: String?, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: 10) {
                Image(systemName: icon).foregroundStyle(Color.cavnarEmber)
                Text(title).font(.cavnarBody(16.5, weight: 600))
                Spacer()
                if let badge {
                    Text(badge)
                        .font(.cavnarNumber(16.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                }
                Image(systemName: "chevron.right").foregroundStyle(Color.cavnarInk3)
            }
            .foregroundStyle(Color.cavnarInk)
            .cavnarCard()
        }
        .buttonStyle(.plain)
    }

    // MARK: - Generator

    @ViewBuilder
    private var generatorSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Generate content")
                .font(.cavnarBody(16, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            if let type = viewModel.selectedContentType {
                Text(type.description)
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            TextField("Topic (optional)", text: $viewModel.topic)
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
            TextEditor(text: $viewModel.draft)
                .font(.cavnarBody(16.5))
                .lineSpacing(5)
                .foregroundStyle(Color.cavnarInk)
                .scrollContentBackground(.hidden)
                .frame(minHeight: 210)
                .padding(14)
                .background(Color.cavnarPaper)
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .stroke(viewModel.isOverLimit ? Color.cavnarRed : Color.cavnarEmber.opacity(0.3), lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                .focused($focusedField, equals: .draft)
                .id(Self.draftEditorAnchor)

            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("Trim it to the version you want before posting")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                if let limit = viewModel.characterLimit, let type = viewModel.selectedContentType {
                    (Text("\(viewModel.draft.count)").font(.cavnarNumber(16, weight: 600))
                        + Text(" / ")
                        + Text("\(limit)").font(.cavnarNumber(16))
                        + Text(viewModel.isOverLimit ? " over \(type.limitLabel)" : ""))
                        .font(.cavnarBody(15))
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

    // MARK: - Calendar

    @ViewBuilder
    private var calendarSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("This week's content calendar")
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if !viewModel.calendar.isEmpty {
                    ShareLink(item: viewModel.calendarCSV,
                              preview: SharePreview("Content calendar")) {
                        Image(systemName: "square.and.arrow.up").foregroundStyle(Color.cavnarEmber)
                    }
                }
            }

            if viewModel.calendar.isEmpty && !viewModel.isGeneratingCalendar {
                Text("Seven ideas for the week, built from your menu, your voice and what's coming up.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if viewModel.isGeneratingCalendar {
                // Planning a week of ideas is the model deciding what to do,
                // which is what `solving` depicts. It also takes real time —
                // several seconds — so this needs actual motion, not a
                // shimmering label sitting inside a disabled button.
                CavnarWorkingOrb(state: .solving, label: "Planning your week…")
                    .padding(.vertical, 4)
                    .transition(.opacity)
            } else {
                Button {
                    Task { await viewModel.generateCalendar() }
                } label: {
                    Text(viewModel.calendar.isEmpty ? "Generate week" : "Generate a new week")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }

            if let error = viewModel.calendarError {
                Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
            }

            ForEach(viewModel.calendar) { idea in
                calendarCard(idea)
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGeneratingCalendar)
    }

    private func calendarCard(_ idea: ContentCalendarIdea) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(idea.day)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                if let date = idea.date, !date.isEmpty {
                    Text(date).font(.cavnarNumber(15)).foregroundStyle(Color.cavnarInk3)
                }
                Spacer()
                Text(idea.platform)
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Text(idea.angle)
                .font(.cavnarBody(16))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)

            // Tapping a day writes it. The web tab has always done this; on
            // the phone a calendar idea was something you had to retype.
            Button {
                Haptic.light()
                Task {
                    await viewModel.generate(from: idea)
                    // The draft lands hundreds of points ABOVE the calendar,
                    // so tapping a day left the reader staring at the bottom
                    // of the page with the one thing the tap produced off
                    // screen. Only scroll once there is actually something
                    // there to scroll to.
                    if viewModel.hasDraft { scrollToDraft = UUID() }
                }
            } label: {
                Text("Write this").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarChipButtonStyle(tone: .cavnarEmber))
            .disabled(viewModel.isGenerating)
            .padding(.top, 2)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
