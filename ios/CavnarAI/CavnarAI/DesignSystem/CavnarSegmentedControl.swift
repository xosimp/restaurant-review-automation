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
// Heights here are floors rather than fixed values so a scaled label grows
// the control instead of being clipped inside it (audit 7.2).
//
// Accessibility (re-audit S3, 10/8/26): the segments are plain Text under
// one DragGesture, so VoiceOver saw a row of static words — no buttons, no
// selected state, nothing to adjust. The control now hands VoiceOver,
// Voice Control and Switch Control a real segmented `Picker` over the same
// binding (`accessibilityRepresentation`): each segment is a button with
// the Selected trait, named by its label. The drawn segments stay 34pt; the
// row's touch area is 44pt (padding inside the gesture's content shape).
struct CavnarSegmentedControl<T: Hashable>: View {
    @Binding var selection: T
    let options: [T]
    /// What the control chooses, for VoiceOver ("View", "Send to") — nil
    /// leaves the segments to speak for themselves.
    var accessibilityTitle: String? = nil
    let label: (T) -> String

    @State private var rowWidth: CGFloat = 0

    init(selection: Binding<T>, options: [T], accessibilityTitle: String? = nil,
         label: @escaping (T) -> String) {
        self._selection = selection
        self.options = options
        self.accessibilityTitle = accessibilityTitle
        self.label = label
    }

    var body: some View {
        // A bare GeometryReader has no intrinsic size, so wrapping `segments`
        // as its child (the previous shape here) let it expand to fill all
        // available height in the ScrollView/VStack every module screen
        // hosts this in — .frame(minHeight: 34) is only a floor, and with
        // nothing capping the other side it pushed everything below it down
        // by whatever slack the screen had. Only the width is actually
        // needed, so read it off a zero-height background instead of making
        // segments a GeometryReader's child — segments then sizes itself
        // from its own (Dynamic-Type-aware) content again, same as any
        // normal view.
        segments
            .background(
                GeometryReader { geo in
                    Color.clear.onAppear { rowWidth = geo.size.width }
                        .onChange(of: geo.size.width) { _, newWidth in rowWidth = newWidth }
                }
            )
            // 34pt drawn, 44pt to touch: the padding sits inside the
            // content shape the gesture reads, and outside the segments'
            // width measurement (x is all `select` uses).
            .padding(.vertical, 5)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in select(at: value.location.x, segmentWidth: rowWidth / CGFloat(max(options.count, 1))) }
                    .onEnded { value in select(at: value.location.x, segmentWidth: rowWidth / CGFloat(max(options.count, 1))) }
            )
        // A custom control has no native automatic haptic to lean on (unlike
        // Toggle/UISwitch) — .sensoryFeedback is the one deliberate source
        // here, not stacked with anything else.
        .sensoryFeedback(.selection, trigger: selection) { _, _ in AppPreferences.hapticsEnabledSnapshot }
        .accessibilityRepresentation {
            Picker(accessibilityTitle ?? "", selection: $selection) {
                ForEach(options, id: \.self) { option in
                    Text(label(option)).tag(option)
                }
            }
            .pickerStyle(.segmented)
        }
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
            .font(.cavnar(.label))
            .foregroundStyle(isSelected ? Color.cavnarInk : Color.cavnarInk2)
            .frame(maxWidth: .infinity)
            .frame(minHeight: 34)

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
