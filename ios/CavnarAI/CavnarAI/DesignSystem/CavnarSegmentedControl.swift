import SwiftUI

/// Branded segmented control — two standalone glass buttons (no shared
/// outer border fusing them together), using iOS 26's real Liquid Glass API
/// (`.glassEffect`/`GlassEffectContainer`) so this is rendered by the same
/// system material as the bottom tab bar, not an approximation of it. Below
/// iOS 26 it falls back to a hand-rolled Material+gradient look. A single
/// DragGesture(minimumDistance: 0) over the whole row drives selection
/// instead of per-segment Buttons — a plain tap is just a zero-distance
/// drag, so both interactions share one code path, matching the drag-across
/// feel of the home row's own tab bar.
struct CavnarSegmentedControl<T: Hashable>: View {
    @Binding var selection: T
    let options: [T]
    let label: (T) -> String

    var body: some View {
        GeometryReader { geo in
            let segmentWidth = geo.size.width / CGFloat(max(options.count, 1))
            segments
                .contentShape(Rectangle())
                .gesture(
                    DragGesture(minimumDistance: 0)
                        .onChanged { value in select(at: value.location.x, segmentWidth: segmentWidth) }
                        .onEnded { value in select(at: value.location.x, segmentWidth: segmentWidth) }
                )
        }
        .frame(height: 34)
        // A custom control has no native automatic haptic to lean on (unlike
        // Toggle/UISwitch) — .sensoryFeedback is the one deliberate source
        // here, not stacked with anything else.
        .sensoryFeedback(.selection, trigger: selection)
    }

    @ViewBuilder
    private var segments: some View {
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: 6) {
                HStack(spacing: 6) {
                    ForEach(options, id: \.self) { option in
                        segment(option, isSelected: option == selection)
                    }
                }
            }
        } else {
            HStack(spacing: 6) {
                ForEach(options, id: \.self) { option in
                    segment(option, isSelected: option == selection)
                }
            }
        }
    }

    private func select(at x: CGFloat, segmentWidth: CGFloat) {
        guard segmentWidth > 0 else { return }
        let index = min(max(Int(x / segmentWidth), 0), options.count - 1)
        let option = options[index]
        if option != selection { selection = option }
    }

    // The unselected segment gets no background at all — no material, no
    // tint — so the module gradient shows straight through it. Only the
    // selected segment keeps the dark-orange glass fill; that's the one
    // thing that visually marks it as "on."
    @ViewBuilder
    private func segment(_ option: T, isSelected: Bool) -> some View {
        let text = Text(label(option))
            .font(.cavnarBody(13, weight: 600))
            .foregroundStyle(isSelected ? Color.cavnarInk : Color.cavnarInk2)
            .frame(maxWidth: .infinity)
            .frame(height: 34)

        if isSelected {
            if #available(iOS 26.0, *) {
                text.glassEffect(.regular.tint(Color.cavnarEmber.opacity(0.85)).interactive(), in: Capsule())
            } else {
                text
                    .background {
                        Capsule().fill(.ultraThinMaterial)
                        Capsule().fill(Color.cavnarEmber.opacity(0.55))
                        Capsule().fill(
                            LinearGradient(
                                colors: [Color.white.opacity(0.10), Color.white.opacity(0)],
                                startPoint: .top, endPoint: .center
                            )
                        )
                    }
                    .shadow(color: .black.opacity(0.2), radius: 3, y: 1)
            }
        } else {
            text
        }
    }
}
