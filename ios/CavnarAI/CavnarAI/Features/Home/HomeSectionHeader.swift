import SwiftUI

/// Home's section header — an ember kicker over a Clash Display title, with
/// an optional right-aligned note ("1 of 3", "4 active"). One component so
/// Needs Attention, Your Modules and This Week all label themselves the
/// same way. The web's `.hb-sh` to the token (parity audit #85): the
/// kicker in `--ember` (never ember2), the title in Clash Medium, and the
/// 14×3 ember bar the web's h2 draws before its title.
struct HomeSectionHeader: View {
    let kicker: String
    let title: String
    var trailing: String? = nil

    var body: some View {
        HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 5) {
                Text(kicker.uppercased())
                    .font(.cavnarBody(CavnarType.kicker, weight: 700))
                    .tracking(1.6)
                    .foregroundStyle(Color.cavnarEmber)
                HStack(alignment: .center, spacing: 10) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.cavnarEmber)
                        .frame(width: 14, height: 3)
                        .accessibilityHidden(true)
                    Text(title)
                        .font(.cavnarHeadline(CavnarType.section, weight: .medium))
                        .foregroundStyle(Color.cavnarInk)
                }
            }
            Spacer(minLength: 12)
            if let trailing {
                HomeMixedText.make(trailing, size: 12, weight: 700, color: .cavnarInk3)
            }
        }
    }
}
