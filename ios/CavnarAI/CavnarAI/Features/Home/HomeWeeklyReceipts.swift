import SwiftUI

/// "Done for you — What Cavnar AI did this week": a short receipt — it leads
/// the day on Monday and sits in Results the rest of the week. Every line is a real number from
/// this restaurant's own week (mobile_api.py's _home_weekly_receipts);
/// when there's nothing to show yet the whole section stays hidden.
struct HomeWeeklyReceipts: View {
    let receipts: [HomeWeeklyReceipt]

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 22, style: .continuous) }

    var body: some View {
        if !receipts.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                // The web's words (parity audit #5): one title on both.
                HomeSectionHeader(kicker: "Done for you", title: "What Cavnar AI did this week")
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(receipts) { receipt in
                        HStack(alignment: .firstTextBaseline, spacing: 9) {
                            Image(systemName: "checkmark")
                                .font(.cavnar(.caption))
                                .foregroundStyle(Color.cavnarGreen)
                                .accessibilityHidden(true)
                            // The secondary role, not caption (iOS re-audit L9): a
                            // list the owner reads.
                            (HomeMixedText.make(receipt.emphasis, role: .secondary, color: .cavnarInk)
                             + Text(verbatim: " ")
                             + HomeMixedText.make(receipt.text, role: .secondary, color: .cavnarInk2))
                                .lineSpacing(CavnarText.secondary.lineSpacing)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                // The card tokens (L9), not literal colours.
                .background(shape.fill(Color.cavnarPaper2.opacity(0.85)))
                .overlay(shape.strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .shadow(color: .black.opacity(0.5), radius: 14, x: 0, y: 10)
            }
        }
    }
}
