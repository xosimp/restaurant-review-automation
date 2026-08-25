import SwiftUI

/// The AI consultant surface every module shows — redesigned from a full
/// inline expand/collapse block (title row + a 2-line intro preview + an
/// amber "N recommendations" line, ~90-110pt even collapsed) into a single
/// compact strip that opens the full analysis as a sheet on tap instead of
/// growing the page in place. The old version read as a disconnected slab
/// of text splitting the page in half; this one is sized and tinted to sit
/// tight against whatever lead/hero card a module already has above it —
/// each call site places it directly after its own hero with a small gap
/// (not the page's normal section spacing) so it reads as that card's own
/// follow-up commentary, not a second unrelated section.
struct AIConsultantView: View {
    let title: String
    let insight: AIInsight?
    let isLoading: Bool

    @State private var isPresented = false

    var body: some View {
        Button {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        } label: {
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
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .frame(maxWidth: .infinity)
            .background(Color.cavnarEmber.opacity(0.12))
            .overlay(
                RoundedRectangle(cornerRadius: CavnarRadius.control)
                    .strokeBorder(Color.cavnarEmber.opacity(0.32), lineWidth: 1)
            )
            .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(insight == nil)
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight)
            }
        }
    }
}

/// Full analysis, opened from the strip above — same intro/recommendations/
/// forecast content the old inline-expanded state showed, just presented as
/// its own screen instead of pushing the module's page content down.
private struct AIConsultantSheet: View {
    let title: String
    let insight: AIInsight
    @Environment(\.dismiss) private var dismiss

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
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
        }
    }
}
