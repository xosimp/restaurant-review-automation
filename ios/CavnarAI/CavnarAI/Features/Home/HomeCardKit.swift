import SwiftUI

/// Home on a phone (10/8/26, Will): owners are often older and reading in a
/// dim dining room, so a Home card shows only what fits a phone — its
/// headline, one short line, the figure, and buttons a thumb can hit — and
/// the rest (evidence, how sure, the basis behind a figure, what else could
/// explain it) waits behind Details on the same card. Nothing the web shows
/// is dropped; it is one tap further in.
enum HomeType {
    /// A card's headline (Apfel Grotezk semibold).
    static let title: CGFloat = 19
    /// A list line that is its own item (a brief line, an issue, a row).
    static let line: CGFloat = 17
    /// The one supporting sentence under a headline.
    static let body: CGFloat = 16
    /// Meta: who has it, when, what a figure covers. Never smaller.
    static let meta: CGFloat = 14
    /// A text action (Ask, Details, a row's own action).
    static let action: CGFloat = 16
}

/// "Details ⌄" / "Less ⌃" — opens a card's second layer in place. A 44pt
/// target; the content animates in under the card's own headline.
struct HomeDetailsToggle: View {
    @Binding var open: Bool
    var label = "Details"

    var body: some View {
        Button {
            Haptic.light()
            withAnimation(.easeOut(duration: 0.22)) { open.toggle() }
        } label: {
            HStack(spacing: 5) {
                Text(open ? "Less" : label)
                    .font(.cavnarBody(HomeType.action, weight: 700))
                Image(systemName: "chevron.down")
                    .font(.system(size: 12, weight: .bold))
                    .rotationEffect(.degrees(open ? 180 : 0))
            }
            .foregroundStyle(Color.cavnarInk2)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(open ? "Show less" : label)
    }
}

/// A server sentence that can run long (an event line, a brief line): the
/// first few lines at reading size, then "More" to read the rest in place.
/// Short text shows whole, with no button.
struct HomeClampedText: View {
    let text: String
    var size: CGFloat = HomeType.line
    var weight: CGFloat = 500
    var color: Color = .cavnarInk
    var lines = 3
    /// Below this many characters the whole sentence fits in `lines` on a
    /// phone at this size, so no "More" is offered.
    var clampAfter = 130

    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HomeMixedText.make(text, size: size, weight: weight, color: color)
                .lineLimit(expanded || text.count <= clampAfter ? nil : lines)
                .fixedSize(horizontal: false, vertical: true)
            if text.count > clampAfter {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) { expanded.toggle() }
                } label: {
                    Text(expanded ? "Less" : "More")
                        .font(.cavnarBody(HomeType.meta + 1, weight: 700))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(minHeight: 36, alignment: .leading)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }
}

/// A text action on a Home card (Measure it, Assign, Hide, Open Labor): the
/// label as styled, inside a 44pt-tall target, dimming while pressed.
struct HomeTextButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(minHeight: 44)
            .contentShape(Rectangle())
            .opacity(configuration.isPressed ? 0.55 : 1)
    }
}
