import SwiftUI

private enum MarketingSubTab: String, CaseIterable, Identifiable {
    case content = "Content"
    case analytics = "Analytics"
    var id: String { rawValue }
}

private enum MarketingContentField: Hashable, CaseIterable {
    case topic, draft, ctaLink
}

struct MarketingView: View {
    @State private var viewModel = MarketingViewModel()
    @State private var analyticsViewModel = MarketingAnalyticsViewModel()
    @State private var compose = MarketingComposeViewModel()
    @State private var subTab: MarketingSubTab = .content
    @State private var showingPreview = false
    @State private var showingSchedule = false
    @State private var schedulePlatform = "instagram"
    @FocusState private var focusedField: MarketingContentField?

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: MarketingSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 16)
                .padding(.top, 8)
                .padding(.bottom, 16)

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
                                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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
            Text(value).font(.cavnarNumber(22, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private var shelfRows: some View {
        VStack(spacing: 10) {
            shelfRow("Guest Text Club", icon: "message", badge: nil) { GuestTextClubView() }
            shelfRow("Scheduled", icon: "calendar.badge.clock",
                     badge: compose.pendingCount > 0 ? "\(compose.pendingCount)" : nil) {
                MarketingQueueView(viewModel: compose)
            }
            shelfRow("Drafts", icon: "square.and.pencil",
                     badge: compose.drafts.isEmpty ? nil : "\(compose.drafts.count)") {
                MarketingDraftsView(viewModel: compose) { draft in
                    viewModel.draft = draft.body
                    viewModel.hasDraft = true
                    if let type = draft.contentType { viewModel.selectedType = type }
                    viewModel.topic = draft.topic ?? ""
                }
            }
        }
    }

    private func shelfRow<Destination: View>(_ title: String, icon: String, badge: String?,
                                             @ViewBuilder destination: @escaping () -> Destination) -> some View {
        NavigationLink {
            destination()
        } label: {
            HStack(spacing: 10) {
                Image(systemName: icon).foregroundStyle(Color.cavnarEmber)
                Text(title).font(.cavnarBody(14.5, weight: 600))
                Spacer()
                if let badge {
                    Text(badge)
                        .font(.cavnarNumber(14, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                }
                Image(systemName: "chevron.right").foregroundStyle(Color.cavnarInk3)
            }
            .foregroundStyle(Color.cavnarInk)
            .cavnarCard()
        }
    }

    // MARK: - Generator

    @ViewBuilder
    private var generatorSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Generate content")
                .font(.cavnarBody(14.5, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            if let type = viewModel.selectedContentType {
                Text(type.description)
                    .font(.cavnarBody(14))
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
                CavnarComposingLines(widths: [1.0, 0.86, 0.94, 0.7, 0.5], lineHeight: 9, spacing: 11)
                    .padding(.vertical, 6)
                    .transition(.opacity)
            }

            if let error = viewModel.generateError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
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
        VStack(alignment: .leading, spacing: 6) {
            TextEditor(text: $viewModel.draft)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk)
                .scrollContentBackground(.hidden)
                .frame(minHeight: 150)
                .padding(10)
                .background(Color.cavnarPaper)
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .stroke(viewModel.isOverLimit ? Color.cavnarRed : Color.cavnarEmber.opacity(0.3), lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                .focused($focusedField, equals: .draft)

            HStack(spacing: 6) {
                Text("Trim it to the version you want before posting")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                if let limit = viewModel.characterLimit, let type = viewModel.selectedContentType {
                    (Text("\(viewModel.draft.count)").font(.cavnarNumber(14, weight: 600))
                        + Text(" / ")
                        + Text("\(limit)").font(.cavnarNumber(14))
                        + Text(viewModel.isOverLimit ? " over \(type.limitLabel)" : ""))
                        .font(.cavnarBody(14))
                        .foregroundStyle(viewModel.isOverLimit ? Color.cavnarRed : Color.cavnarInk3)
                }
            }
        }
    }

    /// Check it, keep it, or queue it — the three things you could not do
    /// with a generated post before.
    private var composeActions: some View {
        HStack(spacing: 10) {
            Button {
                showingPreview = true
            } label: {
                Label("Preview", systemImage: "eye").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())

            Button {
                Task {
                    await compose.saveDraft(body: viewModel.draft, topic: viewModel.topic,
                                            contentType: viewModel.selectedType)
                }
            } label: {
                if compose.isSavingDraft {
                    CavnarShimmerText(text: "Saving…")
                } else {
                    Label("Save draft", systemImage: "tray.and.arrow.down").frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(compose.isSavingDraft)

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
    }

    private var draftActions: some View {
        HStack(spacing: 10) {
            Button {
                viewModel.copyDraft()
            } label: {
                Label("Copy", systemImage: "doc.on.doc").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())

            Button {
                Task { await viewModel.generate() }
            } label: {
                Label("Regenerate", systemImage: "arrow.triangle.2.circlepath").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isGenerating)
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
            Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
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
                .font(.cavnarBody(14))
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
                    .font(.cavnarBody(14.5, weight: 700))
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
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Button {
                Task { await viewModel.generateCalendar() }
            } label: {
                if viewModel.isGeneratingCalendar {
                    CavnarShimmerText(text: "Building your week…")
                } else {
                    Text(viewModel.calendar.isEmpty ? "Generate week" : "Generate a new week")
                        .frame(maxWidth: .infinity)
                }
            }
            .buttonStyle(CavnarSecondaryButtonStyle())
            .disabled(viewModel.isGeneratingCalendar)

            if let error = viewModel.calendarError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }

            ForEach(viewModel.calendar) { idea in
                calendarCard(idea)
            }
        }
    }

    private func calendarCard(_ idea: ContentCalendarIdea) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(idea.day)
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                if let date = idea.date, !date.isEmpty {
                    Text(date).font(.cavnarNumber(14)).foregroundStyle(Color.cavnarInk3)
                }
                Spacer()
                Text(idea.platform)
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Text(idea.angle)
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)

            // Tapping a day writes it. The web tab has always done this; on
            // the phone a calendar idea was something you had to retype.
            Button {
                Task { await viewModel.generate(from: idea) }
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
