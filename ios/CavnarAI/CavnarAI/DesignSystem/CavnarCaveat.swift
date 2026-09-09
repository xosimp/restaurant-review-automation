import SwiftUI

/// The banner shown above an AI passage the app cannot fully stand behind:
/// example data standing in for a restaurant that hasn't connected inventory,
/// or figures the backend could not trace back to the data it handed the
/// model. Both used to render exactly like a verified analysis, which is the
/// whole problem — an owner had no way to tell a measured number from one the
/// model produced.
struct CavnarCaveat: View {
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.cavnarAmber)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.cavnarBody(12.5, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
                Text(detail)
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 9)
        .background(Color.cavnarAmber.opacity(0.10), in: RoundedRectangle(cornerRadius: 9))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .stroke(Color.cavnarAmber.opacity(0.28), lineWidth: 1)
        )
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(title). \(detail)")
    }

    /// The two the app actually raises today.
    static var exampleData: CavnarCaveat {
        CavnarCaveat(
            title: "Example data",
            detail: "This is a sample pantry, not your numbers. Connect Toast or upload a count to see your own."
        )
    }

    static func unverifiedFigures(_ figures: [String]) -> CavnarCaveat {
        let detail: String
        if figures.isEmpty {
            detail = "Cavnar couldn't trace every figure here back to your data. Check before acting on them."
        } else if figures.count == 1 {
            detail = "Cavnar couldn't trace \(figures[0]) back to your data. Check before acting on it."
        } else {
            detail = "Cavnar couldn't trace \(figures.count) figures here (\(figures.prefix(3).joined(separator: ", "))) back to your data. Check before acting on them."
        }
        return CavnarCaveat(title: "Unverified numbers", detail: detail)
    }
}
