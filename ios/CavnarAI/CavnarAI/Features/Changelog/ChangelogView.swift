import SwiftUI

struct ChangelogView: View {
    @State private var viewModel = ChangelogViewModel()

    // Own NavigationStack, not pushed onto a parent's — this is now
    // presented as a sheet from AccountView (see its own comment for why:
    // matches how every other modal in this app opens, sliding up from
    // the bottom, instead of a horizontal push like the rest of Account's
    // rows). NotificationsListView is the established precedent for this
    // exact shape (List-style sheet, own NavigationStack + navigationTitle,
    // no explicit close button — swipe-down-to-dismiss is this app's
    // convention for every sheet).
    var body: some View {
        NavigationStack {
        Group {
            if viewModel.entries.isEmpty && !viewModel.isLoading && viewModel.errorMessage == nil {
                CavnarEmptyHearth(
                    title: "Nothing new yet",
                    message: "Product updates will appear here as they ship."
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
                .padding(.top, 40)
            } else if viewModel.isLoading && viewModel.entries.isEmpty {
                // maxWidth/maxHeight matter here, not just centering — see
                // AccountView/ModulesGridView's identical fix:
                // .cavnarModuleBackground()'s wash sizes to whatever it's
                // attached to, and a bare loading view hugging its own
                // tiny size made it flash as a narrow rectangle instead of
                // full-screen for the split second this state is visible.
                CavnarLoadingSeal()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
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
                                            .font(.cavnarBody(11, weight: 700))
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
        .cavnarModuleBackground()
        .navigationTitle("What's New")
        .navigationBarTitleDisplayMode(.inline)
        .task { await viewModel.load() }
        }
    }
}
