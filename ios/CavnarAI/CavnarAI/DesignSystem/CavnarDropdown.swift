import SwiftUI

/// Branded collapsible section — a tappable header row (title + optional
/// count badge + chevron) that expands/collapses its own content, instead
/// of a list dumped flat onto the page that forces endless scrolling past
/// it to reach anything below. Chevron rotates 180° on toggle; content
/// fades/slides in rather than snapping open, matching the app's existing
/// .easeOut micro-animation convention (segmented control, Done button).
struct CavnarDropdown<Content: View>: View {
    let title: String
    var subtitle: String? = nil
    var badge: Int? = nil
    var tone: CavnarTone = .neutral
    // Fires right as this transitions closed→open — lets a parent whose
    // ScrollView doesn't otherwise know the page just grew (a section
    // expanding below the fold) bring the newly-opened content into view,
    // instead of leaving the user to scroll and find it themselves.
    var onExpand: (() -> Void)? = nil
    @State private var isExpanded: Bool
    @ViewBuilder let content: () -> Content

    init(
        title: String, subtitle: String? = nil, badge: Int? = nil, tone: CavnarTone = .neutral,
        startExpanded: Bool = false, onExpand: (() -> Void)? = nil,
        @ViewBuilder content: @escaping () -> Content
    ) {
        self.title = title
        self.subtitle = subtitle
        self.badge = badge
        self.tone = tone
        self.onExpand = onExpand
        self._isExpanded = State(initialValue: startExpanded)
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.selection()
                let opening = !isExpanded
                withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
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
                    // Plain opacity only — no .move(edge:), which animated
                    // the content in from the top of its transition's
                    // coordinate space (effectively the top of the
                    // ScrollView) while the VStack around it was
                    // simultaneously reflowing to make room, so it visibly
                    // slid down the page and rendered behind sibling
                    // content mid-flight. The VStack's own height-change
                    // animation (already wrapped in withAnimation above)
                    // provides the "opens downward" motion on its own —
                    // this only needed a fade, not a second directional
                    // transition fighting it.
                    .transition(.opacity)
            }
        }
    }
}
