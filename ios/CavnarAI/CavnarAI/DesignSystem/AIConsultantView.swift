import SwiftUI

/// The AI consultant surface every module shows — deliberately unboxed
/// (no background fill, no border, no left-edge stripe) rather than another
/// bordered card among the many already on a module's Overview tab. Same
/// reasoning as ValueChartCard on Home: sits flush on the page, and
/// typography/color/spacing (not container chrome) signal "this is the AI
/// surface" — an uppercase ember eyebrow, generous line-height, and a
/// numeral-not-badge marker on each recommendation.
///
/// Collapsed by default once an insight lands — the full intro + numbered
/// recommendations + forecast could run long enough to push everything
/// below it off-screen on a module's Overview tab. A two-line preview plus
/// a tap-to-expand affordance reads as more considered than a wall of text
/// always rendered in full.
struct AIConsultantView: View {
    let title: String
    let insight: AIInsight?
    let isLoading: Bool
    // Whether the intro has already typewritten out once — nil (the
    // default) falls back to view-local state, which still stops it
    // replaying on every re-expand/collapse within one continuous view,
    // but resets whenever this view itself gets torn down and recreated.
    // A caller that also wants it to survive leaving and re-entering the
    // screen entirely (Labor's Overview call site persists this via
    // UserDefaults) passes its own longer-lived binding instead.
    var hasPlayedIntro: Binding<Bool>? = nil

    @State private var isExpanded = false
    @State private var localHasPlayedIntro = false

    private var playedIntroBinding: Binding<Bool> {
        hasPlayedIntro ?? $localHasPlayedIntro
    }

    var body: some View {
        Button {
            guard insight != nil else { return }
            Haptic.selection()
            withAnimation(.easeOut(duration: 0.22)) { isExpanded.toggle() }
        } label: {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    Image(systemName: "sparkles")
                        .font(.system(size: 10, weight: .semibold))
                    Text(title)
                        .font(.cavnarBody(10, weight: 700))
                        .tracking(2)
                        .textCase(.uppercase)
                    Spacer()
                    if insight != nil {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .rotationEffect(.degrees(isExpanded ? 180 : 0))
                    }
                }
                .foregroundStyle(Color.cavnarEmber)

                if let insight {
                    if isExpanded {
                        InsightContent(insight: insight, hasPlayedIntro: playedIntroBinding)
                            .transition(.opacity)
                    } else {
                        CollapsedInsightPreview(insight: insight)
                    }
                } else if isLoading {
                    InsightSkeleton()
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(insight == nil)
    }
}

private struct CollapsedInsightPreview: View {
    let insight: AIInsight

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !insight.intro.isEmpty {
                Text(insight.intro)
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk2)
                    .lineSpacing(4)
                    .lineLimit(2)
            }
            if !insight.recommendations.isEmpty {
                Text("\(insight.recommendations.count) recommendation\(insight.recommendations.count == 1 ? "" : "s") — tap to view")
                    .font(.cavnarBody(11, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
            }
        }
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
    @Binding var hasPlayedIntro: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !insight.intro.isEmpty {
                if hasPlayedIntro {
                    Text(insight.intro)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineSpacing(5)
                } else {
                    TypewriterText(fullText: insight.intro, font: .cavnarBody(13), color: Color.cavnarInk2, lineSpacing: 5)
                        .onAppear { hasPlayedIntro = true }
                }
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
                                .font(.cavnarNumber(13, weight: 700))
                                .foregroundStyle(Color.cavnarEmber)
                                .frame(width: 16, alignment: .leading)
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
                    Label("Forecast", systemImage: "sparkles")
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
            }
        }
    }
}
