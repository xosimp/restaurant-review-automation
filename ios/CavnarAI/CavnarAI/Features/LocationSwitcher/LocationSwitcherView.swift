import SwiftUI

/// Owner-only restaurant switcher — only ever shown when User.isOwner, since
/// /mobile/api/group-locations returns an empty list for anyone else.
struct LocationSwitcherView: View {
    @State private var viewModel = LocationSwitcherViewModel()
    @Environment(\.dismiss) private var dismiss
    var onSwitched: () -> Void
    // The row just tapped — its check pops in with one ember ripple while
    // the switch is in flight, so the tap reads immediately.
    @State private var tappedName: String?

    var body: some View {
        NavigationStack {
            List {
                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarRed)
                }
                ForEach(viewModel.locations) { location in
                    Button {
                        Haptic.selection()
                        tappedName = location.name
                        Task {
                            if await viewModel.switchTo(location) {
                                onSwitched()
                                dismiss()
                            } else {
                                tappedName = nil
                            }
                        }
                    } label: {
                        HStack {
                            Text(location.name)
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk)
                            Spacer()
                            if tappedName == location.name {
                                ZStack {
                                    CavnarRippleBurst(fromDiameter: 18, toDiameter: 48, rings: 1, duration: 0.7)
                                    Image(systemName: "checkmark")
                                        .font(.system(size: 15, weight: .semibold))
                                        .foregroundStyle(Color.cavnarEmber)
                                        .transition(.scale(scale: 0.4).combined(with: .opacity))
                                }
                                .frame(width: 24, height: 24)
                            } else if location.active {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 15, weight: .semibold))
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                    }
                    .disabled(tappedName != nil)
                    .animation(.easeOut(duration: 0.25), value: tappedName)
                }
            }
            .scrollContentBackground(.hidden)
            .overlay {
                if viewModel.isLoading { CavnarLoadingSeal() }
            }
            // Top-aligned at the same offset as the Notifications sheet's
            // empty state, not floated to the vertical center.
            .overlay(alignment: .top) {
                if !viewModel.isLoading && viewModel.locations.isEmpty && viewModel.errorMessage == nil {
                    CavnarEmptyHearth(
                        title: "No other locations",
                        message: "Additional restaurants in your group will appear here."
                    )
                    .padding(.top, 40)
                }
            }
            // The same ember chevron every other sheet closes with — this
            // was the last one still using a plain system "Close" button.
            .accountSheetChrome(viewModel.groupName ?? "Locations")
            .task { await viewModel.load() }
        }
    }
}
