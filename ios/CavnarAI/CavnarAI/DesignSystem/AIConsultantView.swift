import SwiftUI

/// The AI consultant box every analytics module shows — ports the web
/// dashboard's dark-insight-card: ember left-edge accent stripe, uppercase
/// ember module header ("Cavnar AI Labor Consultant" etc.), pulsing
/// skeleton bars while loading, a word-by-word typewriter reveal once the
/// insight arrives, numbered ember-circle recommendations in amber, and an
/// optional italic Forecast callout.
struct AIConsultantView: View {
    let title: String
    let insight: AIInsight?
    let isLoading: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.cavnarBody(10, weight: 700))
                .tracking(2)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)

            if let insight {
                InsightContent(insight: insight)
            } else if isLoading {
                InsightSkeleton()
            }
        }
        .padding(16)
        .background(Color.cavnarSurface)
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.cavnarEmber).frame(width: 3)
        }
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarPaper3, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }
}

private struct InsightSkeleton: View {
    @State private var pulse = false
    private let widths: [CGFloat] = [0.88, 0.72, 0.60]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(widths.indices, id: \.self) { i in
                GeometryReader { geo in
                    RoundedRectangle(cornerRadius: 5)
                        .fill(Color.cavnarInk.opacity(pulse ? 0.30 : 0.14))
                        .frame(width: geo.size.width * widths[i], height: 13)
                }
                .frame(height: 13)
            }
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true)) {
                pulse = true
            }
        }
    }
}

private struct InsightContent: View {
    let insight: AIInsight

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !insight.intro.isEmpty {
                TypewriterText(fullText: insight.intro, font: .cavnarBody(13), color: Color.cavnarInk2, lineSpacing: 5)
            }

            if !insight.recommendations.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("Recommendations")
                        .font(.cavnarBody(11, weight: 700))
                        .tracking(0.9)
                        .textCase(.uppercase)
                        .foregroundStyle(Color.cavnarEmber)

                    ForEach(Array(insight.recommendations.enumerated()), id: \.offset) { index, rec in
                        HStack(alignment: .top, spacing: 10) {
                            Text("\(index + 1)")
                                .font(.cavnarNumber(10, weight: 700))
                                .foregroundStyle(.white)
                                .frame(width: 20, height: 20)
                                .background(Color.cavnarEmber)
                                .clipShape(Circle())
                            Text(rec)
                                .font(.cavnarBody(13, weight: 500))
                                .foregroundStyle(Color.cavnarAmber)
                                .lineSpacing(4)
                        }
                    }
                }
            }

            if let forecast = insight.forecast, !forecast.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("🔮 Forecast")
                        .font(.cavnarBody(10, weight: 700))
                        .tracking(0.9)
                        .textCase(.uppercase)
                        .foregroundStyle(Color.cavnarEmber)
                    Text(forecast)
                        .font(.cavnarBody(13))
                        .italic()
                        .foregroundStyle(Color.cavnarInk2)
                        .lineSpacing(4)
                }
                .padding(10)
                .background(Color.cavnarEmber.opacity(0.08))
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.cavnarEmber).frame(width: 2)
                }
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
        }
    }
}
