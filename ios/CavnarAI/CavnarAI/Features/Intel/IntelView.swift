import SwiftUI

private enum IntelSubTab: String, CaseIterable, Identifiable {
    case competitors = "Competitors"
    case aiVisibility = "AI Visibility"
    var id: String { rawValue }
}

struct IntelView: View {
    @State private var viewModel = IntelViewModel()
    @State private var aiVisibilityViewModel = AIVisibilityViewModel()
    @State private var subTab: IntelSubTab = .competitors

    var body: some View {
        VStack(spacing: 0) {
            CavnarSegmentedControl(selection: $subTab, options: IntelSubTab.allCases) { $0.rawValue }
                .padding(.horizontal, 20)
                .padding(.vertical, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if subTab == .competitors {
                        if let summary = viewModel.summary {
                            if !summary.hasData {
                                emptyState
                            } else {
                                content(summary)
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
                        AIVisibilitySection(viewModel: aiVisibilityViewModel)
                    }
                }
                .padding(20)
            }
        }
        .cavnarModuleBackground()
        .refreshable { await viewModel.load() }
        .navigationTitle("Intel")
        .navigationBarTitleDisplayMode(.inline)
        .cavnarEmberTitle("Intel")
        .cavnarEmberBackButton()
        .task { await viewModel.load() }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "binoculars")
                .font(.system(size: 32))
                .foregroundStyle(Color.cavnarInk3)
            Text("No competitor data yet")
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("Competitor analysis refreshes automatically — check back soon.")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    @ViewBuilder
    private func content(_ summary: IntelSummary) -> some View {
        if let intro = summary.intro, !intro.isEmpty {
            Text(intro)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk2)
        }

        ForEach(summary.sections) { section in
            VStack(alignment: .leading, spacing: 8) {
                Text(section.name)
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                ForEach(section.bullets, id: \.self) { bullet in
                    Text("• \(bullet)")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk2)
                }
            }
            .cavnarCard()
        }

        if !summary.recommendations.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text("Recommendations")
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarEmber)
                ForEach(summary.recommendations, id: \.self) { rec in
                    HStack(alignment: .top, spacing: 8) {
                        Image(systemName: "lightbulb.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(Color.cavnarEmber)
                        Text(rec)
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk)
                    }
                }
            }
            .cavnarCard()
        }
    }
}
