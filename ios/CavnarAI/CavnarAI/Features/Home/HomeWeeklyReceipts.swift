import SwiftUI

/// "This week — what Cavnar AI did for you": a short receipt that closes the
/// page, so the last thing read is proof. Every line is a real number from
/// this restaurant's own week (mobile_api.py's _home_weekly_receipts);
/// when there's nothing to show yet the whole section stays hidden.
struct HomeWeeklyReceipts: View {
    let receipts: [HomeWeeklyReceipt]

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 22, style: .continuous) }

    var body: some View {
        if !receipts.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                HomeSectionHeader(kicker: "This week", title: "What Cavnar AI did for you")
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
