import SwiftUI

/// The four figures the web's Reviews header has always shown as pills —
/// rating, answered, to approve, urgent — on the phone.
///
/// ReviewStats was modelled in full and then called from nowhere in the
/// app, so the Reviews tab opened straight into a list with no summary of
/// the restaurant's reputation anywhere on it. Two of the honesty lines
/// that model carries were invisible for the same reason and are shown
/// here too: how much of Google's history our average actually covers
/// (isFullHistory / officialReviewCount), and how many reviews the
/// sentiment and topic charts don't cover yet (unanalysed).
struct ReviewsStatStrip: View {
    let stats: ReviewStats

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ScrollView(.horizontal) {
                HStack(spacing: 8) {
                    pill(value: String(format: "%.1f", stats.avgRating), unit: "★",
                         label: "rating", tone: ratingTone,
                         spoken: "\(String(format: "%.1f", stats.avgRating)) star average rating")
                    pill(value: "\(Int(stats.responseRate))", unit: "%",
                         label: "answered", tone: stats.responseRate >= 70 ? .good : (stats.responseRate >= 40 ? .warning : .bad),
                         spoken: "\(Int(stats.responseRate)) percent of reviews answered")
                    pill(value: "\(stats.awaitingApproval + stats.needsResponse)", unit: nil,
                         label: "to approve",
                         tone: (stats.awaitingApproval + stats.needsResponse) == 0 ? .good
                               : ((stats.awaitingApproval + stats.needsResponse) > 2 ? .bad : .warning),
                         spoken: "\(stats.awaitingApproval + stats.needsResponse) replies waiting for you")
                    pill(value: "\(stats.urgent)", unit: nil, label: "urgent",
                         tone: stats.urgent > 0 ? .bad : .good,
                         spoken: "\(stats.urgent) urgent reviews")
                }
                .padding(.vertical, 2)
            }
            .scrollIndicators(.hidden)

            if let caveat = coverageCaveat {
                Text(caveat)
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var ratingTone: CavnarTone {
        stats.avgRating >= 4 ? .good : (stats.avgRating >= 3 ? .warning : .bad)
    }

    /// Two separate honesty statements, joined only when both apply.
    private var coverageCaveat: String? {
        var parts: [String] = []
        if !stats.holdsEveryReview, let official = stats.officialReviewCount, official > stats.total {
            parts.append("Average is over the \(stats.total) reviews Cavnar holds, not all \(official) on Google.")
        }
        if let unanalysed = stats.unanalysed, unanalysed > 0 {
            parts.append("\(unanalysed) not yet analysed — the topic and sentiment charts don't cover them.")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }

    private func pill(value: String, unit: String?, label: String,
                      tone: CavnarTone, spoken: String) -> some View {
        HStack(spacing: 7) {
            Circle()
                .fill(tone.foreground)
                .frame(width: 7, height: 7)
            HStack(alignment: .firstTextBaseline, spacing: 1) {
                Text(value)
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if let unit {
                    Text(unit)
                        .font(.cavnarNumber(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Text(label)
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.cavnarPaper2.opacity(0.75))
        .overlay(Capsule().strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1))
        .clipShape(Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken)
    }
}
