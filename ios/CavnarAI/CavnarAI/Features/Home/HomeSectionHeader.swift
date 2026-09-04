import SwiftUI

/// Home's section header — an ember kicker over a Clash Display title, with
/// an optional right-aligned note ("1 of 3", "4 active"). One component so
/// Needs Attention, Your Modules and This Week all label themselves the
/// same way.
struct HomeSectionHeader: View {
    let kicker: String
    let title: String
    var trailing: String? = nil

    var body: some View {
        HStack(alignment: .lastTextBaseline) {
            VStack(alignment: .leading, spacing: 5) {
                Text(kicker.uppercased())
                    .font(.cavnarBody(11.5, weight: 700))
                    .tracking(1.6)
                    .foregroundStyle(Color.cavnarEmber2)
                Text(title)
                    .font(.cavnarHeadline(19))
                    .foregroundStyle(Color.cavnarInk)
            }
            Spacer(minLength: 12)
            if let trailing {
                HomeMixedText.make(trailing, size: 12, weight: 700, color: .cavnarInk3)
            }
        }
    }
}
