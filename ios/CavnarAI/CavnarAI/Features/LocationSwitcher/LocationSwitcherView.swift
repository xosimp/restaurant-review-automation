import SwiftUI

/// Owner-only restaurant switcher — only ever shown when User.isOwner, since
/// /mobile/api/group-locations returns an empty list for anyone else.
struct LocationSwitcherView: View {
    @State private var viewModel = LocationSwitcherViewModel()
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var session
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
                            viewModel.session = session
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
                // The group view on the phone: the owner's locations side by
                // side on the Benchmark Engine's `location` kind (#19) —
                // each against its own normal first, a gap only beyond noise.
                if viewModel.locations.count > 1 {
                    Section {
                        LocationComparisonSection()
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 8, leading: 0, bottom: 8, trailing: 0))
                    } header: {
                        Text("HOW YOUR LOCATIONS COMPARE")
                            .font(.cavnarBody(13, weight: 700))
                            .tracking(1.4)
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .overlay {
                if viewModel.isLoading { CavnarLoadingOrb() }
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
