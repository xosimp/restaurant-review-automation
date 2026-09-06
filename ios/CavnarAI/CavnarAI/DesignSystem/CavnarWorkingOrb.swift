import SwiftUI

/// An orb with a line of text, for the moments a model is actually working.
///
/// The orb states are the app's vocabulary for "something is happening and
/// here is what kind" (see CavnarOrbState). Marketing's two generation
/// states were using neither: the post generator drew CavnarComposingLines
/// — skeleton lines that read as "content is arriving", when nothing arrives
/// until the whole draft lands — and the calendar drew a shimmering text
/// label with no motion of its own at all. Both are the model composing, and
/// that is what the orb is for.
///
/// Inline and horizontal, not the full-screen CavnarLoadingOrb: these appear
/// inside a card that already has content above and below them.
struct CavnarWorkingOrb: View {
    var state: CavnarOrbState = .composing
    var label: String
    var size: CGFloat = 30

    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 11) {
            CavnarOrb(state: state, size: size, paused: reduceMotion)
            Text(label)
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(label)
    }
}
