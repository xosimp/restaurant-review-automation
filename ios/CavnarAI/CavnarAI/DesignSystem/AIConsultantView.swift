import SwiftUI

/// Just the tappable strip content (sparkle + truncated intro + chevron) —
/// no background/border of its own. AIConsultantEmbeddedStrip and
/// AIConsultantView below both build on this; the difference between them
/// is only whether they draw their own card chrome around it.
private struct AIConsultantStripContent: View {
    let insight: AIInsight?
    let isLoading: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.system(size: 11, weight: .semibold))
                Group {
                    if let insight, !insight.intro.isEmpty {
                        Text(insight.intro)
                    } else if isLoading {
                        Text("Analyzing this week's numbers…")
                    } else {
                        Text("No analysis yet")
                    }
                }
                .font(.cavnarBody(11.5, weight: 500))
                .lineLimit(1)
                .truncationMode(.tail)
                Spacer(minLength: 8)
                if insight != nil {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 10, weight: .semibold))
                }
            }
            .foregroundStyle(Color.cavnarEmber2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(insight == nil)
    }
}

/// For embedding directly inside a module's own hero card, as its own
/// footer row after a divider — sharing the hero's background/border
/// instead of drawing a second, separate card right underneath it. See
/// each module's own heroCard for exactly how it's placed (a divider line,
/// then this, inside the SAME VStack the hero's own stats sit in, so the
/// whole thing shares one .background()/.overlay(border)/.clipShape()).
struct AIConsultantEmbeddedStrip: View {
    let title: String
    let insight: AIInsight?
    let isLoading: Bool

    @State private var isPresented = false

    var body: some View {
        AIConsultantStripContent(insight: insight, isLoading: isLoading) {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        }
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight)
            }
        }
    }
}

/// Self-contained version — its own bordered pill — for a module tab with
/// no hero-style card to embed into (Marketing's Analytics tab today).
struct AIConsultantView: View {
    let title: String
    let insight: AIInsight?
    let isLoading: Bool

    @State private var isPresented = false

    var body: some View {
        AIConsultantStripContent(insight: insight, isLoading: isLoading) {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 11)
        .frame(maxWidth: .infinity)
        .background(Color.cavnarEmber.opacity(0.12))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarEmber.opacity(0.32), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight)
            }
        }
    }
}

/// Full analysis, opened from the strip above — same intro/recommendations/
/// forecast content the old inline-expanded state showed, just presented as
/// its own screen instead of pushing the module's page content down. No
/// explicit close button — swipe-down-to-dismiss, same as every other sheet
/// in this app (NotificationsListView is the established precedent).
private struct AIConsultantSheet: View {
    let title: String
    let insight: AIInsight

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if !insight.intro.isEmpty {
                        Text(insight.intro)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(6)
                    }

                    if !insight.recommendations.isEmpty {
                        VStack(alignment: .leading, spacing: 12) {
                            Text("Recommendations")
                                .font(.cavnarBody(11, weight: 700))
                                .tracking(0.9)
                                .textCase(.uppercase)
                                .foregroundStyle(Color.cavnarEmber)

                            ForEach(Array(insight.recommendations.enumerated()), id: \.offset) { index, rec in
                                HStack(alignment: .top, spacing: 10) {
                                    Text("\(index + 1)")
                                        .font(.cavnarNumber(14, weight: 700))
                                        .foregroundStyle(Color.cavnarEmber)
                                        .frame(width: 18, alignment: .leading)
                                    Text(rec)
                                        .font(.cavnarBody(14, weight: 500))
                                        .foregroundStyle(Color.cavnarAmber)
                                        .lineSpacing(4)
                                }
                            }
                        }
                    }

                    if let forecast = insight.forecast, !forecast.isEmpty {
                        VStack(alignment: .leading, spacing: 4) {
                            Label("Forecast", systemImage: "sparkles")
                                .font(.cavnarBody(11, weight: 700))
                                .tracking(0.9)
                                .textCase(.uppercase)
                                .foregroundStyle(Color.cavnarEmber)
                            Text(forecast)
                                .font(.cavnarBody(14))
                                .italic()
                                .foregroundStyle(Color.cavnarInk2)
                                .lineSpacing(4)
                        }
                    }
                }
                .padding(20)
            }
            .cavnarModuleBackground()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
        }
    }
}
