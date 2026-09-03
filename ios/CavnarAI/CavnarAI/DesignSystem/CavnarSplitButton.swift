import SwiftUI

/// The "New ▾" split-button pattern from the approved design reference:
/// an icon chip + label on the left (the primary, currently-selected
/// action), a hairline divider, and a chevron on the right that opens a
/// menu of alternatives — for exactly the "do the usual thing, or pick a
/// different one" shape of action a plain button + separate Picker used
/// to split across two controls. Shares CavnarPremiumButtonSurface with
/// CavnarPrimaryButtonStyle so both read as the same button language.
struct CavnarSplitButton<MenuContent: View>: View {
    /// SF Symbol shown inside the small glass icon chip on the left.
    var icon: String? = nil
    var label: String
    var isLoading: Bool = false
    var loadingText: String = "Working…"
    var isDisabled: Bool = false
    var action: () -> Void
    @ViewBuilder var menuContent: () -> MenuContent

    // Plain Button + Menu here have no ButtonStyle of their own to hang
    // .sensoryFeedback off configuration.isPressed the way
    // CavnarPrimaryButtonStyle does — these had no haptic at all before.
    // A toggled trigger tied to the tap itself gets the same effect.
    @State private var actionTapTrigger = false
    @State private var menuTapTrigger = false

    var body: some View {
        HStack(spacing: 0) {
            Button {
                actionTapTrigger.toggle()
                action()
            } label: {
                HStack(spacing: 10) {
                    if let icon {
                        iconChip(icon)
                    }
                    if isLoading {
                        PulsingText(loadingText)
                    } else {
                        Text(label)
                    }
                }
                .font(.cavnarBody(15, weight: 600))
                .foregroundStyle(.white)
                .padding(.leading, icon != nil ? 10 : 18)
                .padding(.trailing, 14)
                .padding(.vertical, 12)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .sensoryFeedback(.impact(weight: .light), trigger: actionTapTrigger)

            Rectangle()
                .fill(Color.white.opacity(0.22))
                .frame(width: 1)
                .padding(.vertical, 9)

            Menu {
                menuContent()
            } label: {
                Image(systemName: "chevron.down")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.white.opacity(0.85))
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                    .contentShape(Rectangle())
            }
            // .simultaneousGesture, not a wrapping tap gesture, so this
            // doesn't compete with Menu's own gesture for opening it.
            .simultaneousGesture(TapGesture().onEnded { menuTapTrigger.toggle() })
            .sensoryFeedback(.impact(weight: .light), trigger: menuTapTrigger)
        }
        // Explicit Capsule — this component's own reference match was a
        // full pill, unlike CavnarPrimaryButtonStyle's newer moderate
        // rounded-rect, and nothing since has said to change it.
        .cavnarPremiumButtonSurface(isDisabled: isDisabled, shape: AnyShape(Capsule()))
        .opacity(isDisabled || isLoading ? 0.75 : 1)
        .disabled(isDisabled || isLoading)
    }

    private func iconChip(_ name: String) -> some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8)
                .fill(Color.white.opacity(0.2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color.white.opacity(0.32), lineWidth: 1)
                )
                .overlay(alignment: .top) {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.white.opacity(0.4), lineWidth: 1)
                        .mask(LinearGradient(colors: [.black, .clear], startPoint: .top, endPoint: .bottom))
                }
            Image(systemName: name)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(.white)
        }
        .frame(width: 27, height: 27)
        // 27pt visual, 44pt hit region — HIG minimum (audit 7.3).
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
    }
}
