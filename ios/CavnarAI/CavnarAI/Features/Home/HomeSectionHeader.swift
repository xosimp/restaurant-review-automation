import SwiftUI

/// Home's section header — a Clash title with the web's 14×3 ember bar
/// before it, and an optional right-aligned note ("1 of 3", "4 active").
/// One component so every Home section labels itself the same way.
///
/// Title only (iOS re-audit L13, 10/8/26): the ember kicker that sat over
/// every title said the same thing twice ("Follow-through / Still open",
/// "Measured / What your changes did"). `kicker` is still accepted so the
/// call sites read as before; it is never drawn.
struct HomeSectionHeader: View {
    let kicker: String
    let title: String
    var trailing: String? = nil

    var body: some View {
        HStack(alignment: .lastTextBaseline) {
            HStack(alignment: .center, spacing: 10) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.cavnarEmber)
                    .frame(width: 14, height: 3)
                    .accessibilityHidden(true)
                Text(title)
                    .cavnarText(.headline)
                    .accessibilityAddTraits(.isHeader)
            }
            Spacer(minLength: 12)
            if let trailing {
                CavnarMixedText(trailing, role: .secondary, color: .cavnarInk2)
            }
        }
    }
}
