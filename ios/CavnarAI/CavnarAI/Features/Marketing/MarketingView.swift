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
                            ProgressView().padding(.top, 60)
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
        }
        .cavnarModuleBackground()
        .refreshable { await viewModel.load() }
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
            Text(label).font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var generatorSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Generate content")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            Picker("Type", selection: $viewModel.selectedType) {
                ForEach(viewModel.contentTypes, id: \.0) { key, label in
                    Text(label).tag(key)
                }
            }
            .pickerStyle(.menu)
            .tint(Color.cavnarEmber)

            TextField("Topic (optional)", text: $viewModel.topic)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .topic)

            Button {
                Task { await viewModel.generate() }
            } label: {
                if viewModel.isGenerating {
                    ProgressView().tint(.white)
                } else {
                    Text("Generate")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isGenerating)

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
                    Label("Posted to \(posted)", systemImage: "checkmark.circle.fill")
                        .font(.cavnarBody(12, weight: 600))
                        .foregroundStyle(Color.cavnarGreen)
                }
                if let error = viewModel.postError {
                    Text(error).font(.cavnarBody(12)).foregroundStyle(Color.cavnarRed)
                }
            }
        }
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
                                .font(.cavnarBody(10, weight: 600))
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
