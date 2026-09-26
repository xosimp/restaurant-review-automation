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
                                .font(.system(size: 11, weight: .bold))
                                .foregroundStyle(Color.cavnarGreen)
                            (HomeMixedText.make(receipt.emphasis, size: 13.5, weight: 700, color: .cavnarInk)
                             + Text(verbatim: " ")
                             + HomeMixedText.make(receipt.text, size: 13.5, weight: 600, color: .cavnarInk2))
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    shape.fill(
                        LinearGradient(
                            colors: [Color(red: 0.082, green: 0.082, blue: 0.090), Color(red: 0.059, green: 0.059, blue: 0.063)],
                            startPoint: .top, endPoint: .bottom
                        )
                    )
                )
                .overlay(shape.strokeBorder(Color.white.opacity(0.07), lineWidth: 1))
                .shadow(color: .black.opacity(0.5), radius: 14, x: 0, y: 10)
            }
        }
    }
}
