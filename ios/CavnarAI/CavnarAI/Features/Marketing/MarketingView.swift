import SwiftUI

private enum MarketingSubTab: String, CaseIterable, Identifiable {
    case content = "Content"
    case analytics = "Analytics"
    var id: String { rawValue }
}

private enum MarketingContentField: Hashable, CaseIterable {
    case topic, imageURL
}

struct MarketingView: View {
    @State private var viewModel = MarketingViewModel()
    @State private var analyticsViewModel = MarketingAnalyticsViewModel()
    @State private var subTab: MarketingSubTab = .content
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
                            NavigationLink {
                                GuestTextClubView()
                            } label: {
                                HStack {
                                    Text("Guest Text Club").font(.cavnarBody(13, weight: 600))
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                }
                                .foregroundStyle(Color.cavnarInk)
                                .cavnarCard()
                            }
                            generatorSection
                            if !viewModel.calendar.isEmpty {
                                calendarSection
                            }
                        } else if viewModel.isLoading {
                            CavnarLoadingSeal().padding(.top, 60).frame(maxWidth: .infinity)
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
            .cavnarEmberRefreshable { await viewModel.load() }
        }
        .cavnarModuleBackground()
        .navigationTitle("Marketing")
        .navigationBarTitleDisplayMode(.inline)
        .cavnarTabSwipeNavigation($subTab, primaryTab: .content, secondaryTab: .analytics)
        .keyboardNavToolbar($focusedField)
        .task { await viewModel.load() }
        .task { await analyticsViewModel.load() }
    }

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
            Text(label).font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    private var selectedTypeLabel: String {
        viewModel.contentTypes.first { $0.0 == viewModel.selectedType }?.1 ?? "Content"
    }

    @ViewBuilder
    private var generatorSection: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Generate content")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            TextField("Topic (optional)", text: $viewModel.topic)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .topic)

            // Was a separate Picker(.menu) above a plain "Generate"
            // button — two controls for one decision ("what kind, then
            // go"). One split button now: the left side generates
            // whatever type is currently selected, the chevron opens the
            // same type list to change it — the exact "New ▾" pattern
            // from the design reference, applied to a real pick-a-type-
            // then-act flow instead of the earlier bolted-on preview.
            CavnarSplitButton(
                icon: "sparkles",
                label: "Generate \(selectedTypeLabel)",
                isLoading: viewModel.isGenerating,
                loadingText: "Generating…",
                action: { Task { await viewModel.generate() } }
            ) {
                ForEach(viewModel.contentTypes, id: \.0) { key, label in
                    Button {
                        viewModel.selectedType = key
                    } label: {
                        if viewModel.selectedType == key {
                            Label(label, systemImage: "checkmark")
                        } else {
                            Text(label)
                        }
                    }
                }
            }

            // "Composing" — an ember caret writing lines into place while
            // Claude drafts the post (see CavnarMotion).
            if viewModel.isGenerating {
                CavnarComposingLines(widths: [1.0, 0.86, 0.94, 0.7, 0.5], lineHeight: 9, spacing: 11)
                    .padding(.vertical, 6)
                    .transition(.opacity)
            }

            if let error = viewModel.generateError {
                Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
            }

            if let content = viewModel.generatedContent {
                Text(content)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk)
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.cavnarPaper)
                    .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).stroke(Color.cavnarEmber.opacity(0.3), lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))

                if viewModel.selectedType == "instagram_post" {
                    TextField("Image URL (required for Instagram)", text: $viewModel.imageURL)
                        .cavnarTextFieldStyle()
                        .focused($focusedField, equals: .imageURL)
                }

                HStack(spacing: 10) {
                    Button {
                        Task { await viewModel.postToInstagram() }
                    } label: {
                        Text("Post to Instagram")
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(viewModel.isPosting)

                    Button {
                        Task { await viewModel.postToFacebook() }
                    } label: {
                        Text("Post to Facebook")
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(viewModel.isPosting)
                }

                if let posted = viewModel.postedPlatform {
                    // "Posted" — plays once when the real POST comes back ok.
                    CavnarPostedCheck(label: "Posted to \(posted)")
                        .frame(maxWidth: .infinity)
                        .padding(.top, 6)
                }
                if let error = viewModel.postError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
            }
        }
        .animation(.easeOut(duration: 0.3), value: viewModel.isGenerating)
        .cavnarCard()
    }

    @ViewBuilder
    private var calendarSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("This week's content calendar")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            VStack(spacing: 8) {
                ForEach(viewModel.calendar) { idea in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(idea.day)
                                .font(.cavnarBody(12, weight: 700))
                                .foregroundStyle(Color.cavnarEmber)
                            Spacer()
                            Text(idea.platform)
                                .font(.cavnarBody(11.5, weight: 600))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Text(idea.angle)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk)
                    }
                    .padding(12)
                    .background(Color.cavnarPaper2)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
        }
    }
}
