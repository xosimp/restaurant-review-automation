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
    @State private var isExpanded: Bool
    @ViewBuilder let content: () -> Content

    init(
        title: String, subtitle: String? = nil, badge: Int? = nil, tone: CavnarTone = .neutral,
        startExpanded: Bool = false, @ViewBuilder content: @escaping () -> Content
    ) {
        self.title = title
        self.subtitle = subtitle
        self.badge = badge
        self.tone = tone
        self._isExpanded = State(initialValue: startExpanded)
        self.content = content
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                Haptic.selection()
                withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
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
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
    }
}
