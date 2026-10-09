import SwiftUI

/// Owner-only restaurant switcher — only ever shown when User.isOwner, since
/// /mobile/api/group-locations returns an empty list for anyone else.
struct LocationSwitcherView: View {
    @State private var viewModel = LocationSwitcherViewModel()
    @Environment(\.dismiss) private var dismiss
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
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
                        .cavnarText(.secondary, color: .cavnarRedText)
                }
                // The locations first (iOS readability round, 10/8/26, #98):
                // switching is what this sheet is for.
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
                        HStack(spacing: CavnarSpace.s) {
                            // Status dot, the name, and — under it — how
                            // many things need the owner there and last
                            // night's net (the web switcher's row).
                            let signal = viewModel.signal(for: location)
                            if let signal {
                                Circle()
                                    .fill(Self.healthColor(signal.health))
                                    .frame(width: 10, height: 10)
                                    .accessibilityHidden(true)
                            }
                            VStack(alignment: .leading, spacing: 2) {
                                Text(location.name)
                                    .cavnarText(.label)
                                if let detail = signal?.detail {
                                    CavnarMixedText(detail, role: .secondary)
                                        .cavnarSensitive()
                                }
                            }
                            Spacer()
                            if tappedName == location.name {
                                ZStack {
                                    CavnarRippleBurst(fromDiameter: 18, toDiameter: 48, rings: 1, duration: 0.7)
                                    Image(systemName: "checkmark")
                                        .font(.cavnar(.label))
                                        .foregroundStyle(Color.cavnarEmber)
                                        .transition(reduceMotion ? .opacity : .scale(scale: 0.4).combined(with: .opacity))
                                }
                                .frame(width: 24, height: 24)
                            } else if location.active {
                                Image(systemName: "checkmark")
                                    .font(.cavnar(.label))
                                    .foregroundStyle(Color.cavnarEmber)
                            }
                        }
                        .frame(minHeight: 44)
                    }
                    .disabled(tappedName != nil)
                    .animation(.easeOut(duration: 0.25), value: tappedName)
                }
                // The whole group as ONE row: "All locations · 3 need you ›".
                // The side-by-side cards, what needs the owner at each
                // location and how the locations compare (with how the
                // comparison is made) are on that screen, not here.
                if let g = viewModel.group {
                    Section {
                        NavigationLink {
                            LocationGroupHomeView(viewModel: viewModel, onSwitched: onSwitched,
                                                  close: { dismiss() })
                        } label: {
                            HStack(spacing: CavnarSpace.s) {
                                Image(systemName: "square.grid.2x2")
                                    .font(.cavnar(.body))
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .accessibilityHidden(true)
                                CavnarMixedText(Self.groupRowTitle(g), role: .label, color: .cavnarInk)
                            }
                            .frame(minHeight: 44)
                        }
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

    /// "All locations · 3 need you" — or "All locations · nothing needs you".
    static func groupRowTitle(_ g: LocationGroupBrief) -> String {
        let n = g.attention.count
        return "All locations \u{00B7} " + (n == 0 ? "nothing needs you" : "\(n) need\(n == 1 ? "s" : "") you")
    }
}
