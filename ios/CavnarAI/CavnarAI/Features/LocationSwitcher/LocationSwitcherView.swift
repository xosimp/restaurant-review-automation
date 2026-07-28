import SwiftUI

/// Owner-only restaurant switcher — only ever shown when User.isOwner, since
/// /mobile/api/group-locations returns an empty list for anyone else.
struct LocationSwitcherView: View {
    @State private var viewModel = LocationSwitcherViewModel()
    @Environment(\.dismiss) private var dismiss
    var onSwitched: () -> Void

    var body: some View {
        NavigationStack {
            List {
                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarRed)
                }
                ForEach(viewModel.locations) { location in
                    Button {
                        Task {
                            if await viewModel.switchTo(location) {
                                onSwitched()
                                dismiss()
                            }
                        }
                    } label: {
                        HStack {
                            Text(location.name)
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer()
                            if location.active {
                                Image(systemName: "checkmark")
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                    }
                }
            }
            .overlay {
                if viewModel.isLoading { ProgressView() }
                else if viewModel.locations.isEmpty && viewModel.errorMessage == nil {
                    ContentUnavailableView("No other locations", systemImage: "building.2")
                }
            }
            .navigationTitle(viewModel.groupName ?? "Locations")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
            .task { await viewModel.load() }
        }
    }
}
