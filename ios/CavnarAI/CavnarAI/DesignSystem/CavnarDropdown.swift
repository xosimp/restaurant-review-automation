import SwiftUI

/// Branded collapsible section — a tappable header row (title + optional
/// count badge + chevron) that expands/collapses its own content, instead
/// of a list dumped flat onto the page that forces endless scrolling past
/// it to reach anything below. Chevron rotates 180° on toggle; content
/// fades in rather than snapping open, matching the app's existing
/// .easeOut micro-animation convention (segmented control, Done button).
///
/// Takes its expanded state as a `Binding` rather than owning it as local
/// `@State` — a caller sitting inside an `if/else` branch (like the
/// Overview/Analytics switch in LaborView) has its whole subtree torn down
/// and rebuilt on every switch, which would silently reset any local
/// `@State` back to its initial value on return. Backing it with a binding
/// into a view model that lives outside that branch lets the user's actual
/// open/closed choice survive the switch.
struct CavnarDropdown<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    var badge: Int? = nil
    var tone: CavnarTone = .neutral
    @Binding var isExpanded: Bool
    // Fires right as this transitions closed→open — lets a parent whose
    // ScrollView doesn't otherwise know the page just grew (a section
    // expanding below the fold) bring the newly-opened content into view,
    // instead of leaving the user to scroll and find it themselves.
    var onExpand: (() -> Void)? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.selection()
                let opening = !isExpanded
                withAnimation(.easeOut(duration: 0.3)) { isExpanded.toggle() }
                if opening { onExpand?() }
            } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(title)
                                .font(.cavnarBody(13, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                            if let badge {
                                Text("\(badge)")
                                    .font(.cavnarNumber(11, weight: 700))
                                    .foregroundStyle(tone.foreground)
                                    .padding(.horizontal, 7)
                                    .padding(.vertical, 2)
                                    .background(tone.background)
                                    .clipShape(Capsule())
                            }
                        }
                        if let subtitle {
                            Text(subtitle)
                                .font(.cavnarBody(11))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    Spacer()
                    Image(systemName: "chevron.down")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                        .rotationEffect(.degrees(isExpanded ? 180 : 0))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 4)
            }
            .buttonStyle(.plain)

            if isExpanded {
                content()
                    .padding(.top, 10)
                    // Opacity + a slight in-place scale (anchored to the
                    // top, not a directional move) — a move(edge:) was
                    // tried here previously and animated content in from
                    // the top of the ScrollView's coordinate space while
                    // the VStack around it reflowed simultaneously, so it
                    // visibly slid down the page behind sibling content.
                    // Scale-from-98%-in-place has no such offset to fight,
                    // just reads as the content settling into place as it
                    // fades — smoother than a flat opacity-only cut too.
                    .transition(.opacity.combined(with: .scale(scale: 0.98, anchor: .top)))
            }
        }
    }
}
