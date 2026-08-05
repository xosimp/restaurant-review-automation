import SwiftUI

struct ChangelogView: View {
    @State private var viewModel = ChangelogViewModel()

    var body: some View {
        Group {
            if viewModel.entries.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                ContentUnavailableView("Nothing new yet", systemImage: "sparkles")
            } else if viewModel.isLoading && viewModel.entries.isEmpty {
                ProgressView()
            } else if let error = viewModel.errorMessage {
                VStack(spacing: 8) {
                    Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    Button("Retry") { Task { await viewModel.load() } }
                }
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(viewModel.entries) { entry in
                            VStack(alignment: .leading, spacing: 4) {
                                HStack {
                                    Text(entry.title).font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk)
                                    Spacer()
                                    if let tag = entry.tag {
                                        Text(tag.uppercased())
                                            .font(.cavnarBody(9, weight: 700))
                                            .foregroundStyle(Color.cavnarEmber)
                                    }
                                }
                                if let body = entry.body, !body.isEmpty {
                                    Text(body).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            .cavnarCard()
                        }
                    }
                    .padding(20)
                }
            }
        }
        .background(Color.cavnarPaper)
        .navigationTitle("What's New")
        .task { await viewModel.load() }
    }
}
