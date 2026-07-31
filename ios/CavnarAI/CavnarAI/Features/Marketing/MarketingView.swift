import SwiftUI

struct MarketingView: View {
    @State private var viewModel = MarketingViewModel()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if let stats = viewModel.stats {
                    statsCard(stats)
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
            }
            .padding(20)
        }
        .background(Color.cavnarPaper)
        .refreshable { await viewModel.load() }
        .navigationTitle("Marketing")
        .task { await viewModel.load() }
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
            Text(value).font(.cavnarNumber(22, weight: 500)).foregroundStyle(Color.cavnarInk)
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
                    .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.cavnarEmber.opacity(0.3), lineWidth: 1))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
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
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }
}
