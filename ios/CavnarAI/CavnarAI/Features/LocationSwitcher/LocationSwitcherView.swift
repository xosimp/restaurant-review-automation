import SwiftUI

/// Owner-only restaurant switcher — only ever shown when User.isOwner, since
/// /mobile/api/group-locations returns an empty list for anyone else.
struct LocationSwitcherView: View {
    @State private var viewModel = LocationSwitcherViewModel()
    @Environment(\.dismiss) private var dismiss
    @Environment(SessionStore.self) private var session
    @Environment(DeepLinkRouter.self) private var router
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
                // All locations, compact (parity audit #8): the portfolio
                // strip and what needs the owner across them. The full
                // side-by-side table stays on the web's group Home.
                if let g = viewModel.group {
                    Section {
                        groupSummary(g)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                    } header: {
                        Text("ALL LOCATIONS")
                            .font(.cavnarBody(13, weight: 700))
                            .tracking(1.4)
                            .foregroundStyle(Color.cavnarInk3)
                    }
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
                            // Status dot, the name, and — under it — how
                            // many things need the owner there and last
                            // night's net (the web switcher's row).
                            let signal = viewModel.signal(for: location)
                            if let signal {
                                Circle()
                                    .fill(Self.healthColor(signal.health))
                                    .frame(width: 8, height: 8)
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(location.name)
                                    .font(.cavnarBody(15))
                                    .foregroundStyle(Color.cavnarInk)
                                if let detail = signal?.detail {
                                    HomeMixedText.make(detail, size: 12.5, weight: 600, color: .cavnarInk3)
                                        .cavnarSensitive()
                                }
                            }
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

    /// The web's location dot: red critical, amber important / watch,
    /// green healthy — a status, never ember.
    static func healthColor(_ health: String?) -> Color {
        switch health {
        case "critical": return .cavnarRed
        case "important", "watch": return .cavnarAmber
        case "healthy": return .cavnarGreen
        default: return .cavnarInk3
        }
    }

    /// The group in a few lines: its headline, the portfolio strip, the one
    /// sentence naming who needs a look, and the attention across locations
    /// — each row opens its item at that location (switching there first).
    private func groupSummary(_ g: LocationGroupBrief) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let headline = g.headline {
                Text(headline)
                    .font(.cavnarHeadline(18))
                    .foregroundStyle(HomeView.briefToneColor(g.tone))
            }
            if let line = g.portfolio?.line {
                HomeMixedText.make(line, size: 13, weight: 600, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                    .cavnarSensitive()
            }
            if let sentence = g.summaryLine {
                HomeMixedText.make(sentence, size: 13, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(g.attention.prefix(5)) { a in
                Button {
                    Haptic.light()
                    guard let rid = a.restaurantId, let nav = NavPath(a.nav ?? "home") else { return }
                    dismiss()
                    router.open(nav, restaurantId: rid)
                } label: {
                    HStack(alignment: .top, spacing: 10) {
                        Circle().fill(Self.healthColor(a.severity)).frame(width: 7, height: 7).padding(.top, 6)
                        VStack(alignment: .leading, spacing: 2) {
                            HomeMixedText.make(a.text, size: 14, weight: 600, color: .cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                            if let loc = a.location {
                                Text(loc).font(.cavnarBody(12.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        Spacer(minLength: 8)
                        Text(a.actionLabel ?? "Open")
                            .font(.cavnarBody(13, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .frame(minHeight: 44)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            if g.attention.isEmpty {
                Text("Nothing needs you at any location.")
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }
}
