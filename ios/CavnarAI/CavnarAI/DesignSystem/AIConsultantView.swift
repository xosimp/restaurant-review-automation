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
                        PulsingAnalyzingText()
                    } else {
                        Text("No analysis yet")
                    }
                }
                .font(.cavnarBody(15, weight: 500))
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

/// Gentle breathing opacity while the AI consultant's first insight is
/// still loading — matches PulsingSparkleIcon's exact curve/duration
/// (LaborView.swift) rather than a static "Analyzing…" label sitting
/// still for however long the request takes.
private struct PulsingAnalyzingText: View {
    @State private var pulse = false

    var body: some View {
        Text("Analyzing this week's numbers…")
            .opacity(pulse ? 1 : 0.45)
            .onAppear {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
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
    // Food Cost pulls its own forecast sentence out into a standalone
    // FoodCostForecastPill next to this strip (see
    // FoodCostAnalyticsSection) instead of leaving it as a section a user
    // only sees after tapping in — set false there so it isn't shown
    // twice. Every other caller keeps the default, unchanged behavior.
    var showForecastInSheet: Bool = true

    @State private var isPresented = false

    var body: some View {
        AIConsultantStripContent(insight: insight, isLoading: isLoading) {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        }
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight, showForecast: showForecastInSheet)
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
    var showForecastInSheet: Bool = true

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
                AIConsultantSheet(title: title, insight: insight, showForecast: showForecastInSheet)
            }
        }
    }
}

/// Full analysis, opened from the strip above. Rebuilt from a plain wall of
/// small gray/amber text into a real consultant brief: a sparkle badge and
/// kicker up top, the opening line as a Clash Display headline with the
/// owner's name in ember (same treatment as Intel's hero insight), each
/// recommendation in its own ember-tinted, accent-barred card behind a
/// glowing numbered badge, the forecast in an amber "looking ahead" panel,
/// a seal-marked footer, and the whole thing staggering in. Every figure
/// inside the prose renders in Space Grotesk (mixedText below), per the
/// app-wide numbers rule. Keeps the module's own ember-wash background. No
/// explicit close button — swipe-down-to-dismiss, same as every other sheet
/// in this app (NotificationsListView is the established precedent).
private struct AIConsultantSheet: View {
    let title: String
    let insight: AIInsight
    var showForecast: Bool = true

    // Staggered reveal: 1 opening line, 2 recommendations, 3 forecast,
    // 4 footer. (No header row — the sheet's title already names the
    // consultant, a badge + kicker restating it read as redundant.)
    @State private var stage = 0

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 30) {
                    if !insight.intro.isEmpty {
                        openingLine
                            .consultantReveal(stage >= 1)
                    }
                    if !insight.recommendations.isEmpty {
                        recommendations
                    }
                    if showForecast, let forecast = insight.forecast, !forecast.isEmpty {
                        forecastPanel(forecast)
                            .consultantReveal(stage >= 3)
                    }
                    footer
                        .consultantReveal(stage >= 4)
                }
                .padding(.horizontal, 22)
                .padding(.top, 18)
                .padding(.bottom, 44)
            }
            .cavnarModuleBackground()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(title) }
            .task {
                for step in 1...4 {
                    withAnimation(.easeOut(duration: 0.45)) { stage = step }
                    try? await Task.sleep(for: .seconds(0.12))
                }
            }
        }
    }

    /// The AI's own opening sentence, sized and set like Intel's hero
    /// insight: the owner's name (the leading "Brian," when there is one)
    /// in ember, every number in Space Grotesk, the rest in ink.
    private var openingLine: some View {
        let (name, rest) = Self.splitLeadingName(insight.intro)
        let numberFont = Font.cavnarNumber(22, weight: 600)
        var text = Text("")
        if let name {
            text = text + Text(name).foregroundStyle(Color.cavnarEmber)
        }
        text = text + Self.mixedText(rest, numberFont: numberFont, color: Color.cavnarInk)
        return text
            .font(.cavnarHeadline(22))
            .lineSpacing(6)
            .fixedSize(horizontal: false, vertical: true)
    }

    private var recommendations: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: "bolt.fill")
                    .font(.system(size: 11, weight: .bold))
                Text("WHAT TO DO THIS WEEK")
                    .font(.cavnarBody(13, weight: 700))
                    .tracking(1.3)
            }
            .foregroundStyle(Color.cavnarEmber)
            .consultantReveal(stage >= 2)

            ForEach(Array(insight.recommendations.enumerated()), id: \.offset) { index, rec in
                HStack(alignment: .top, spacing: 14) {
                    ZStack {
                        Circle()
                            .fill(Color.cavnarEmber)
                            .frame(width: 30, height: 30)
                            .shadow(color: Color.cavnarEmber.opacity(0.6), radius: 7, x: 0, y: 0)
                        Text("\(index + 1)")
                            .font(.cavnarNumber(14, weight: 700))
                            .foregroundStyle(.white)
                    }
                    Self.mixedText(rec, numberFont: .cavnarNumber(16, weight: 600), color: Color.cavnarInk)
                        .font(.cavnarBody(16))
                        .lineSpacing(5)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 4)
                }
                .padding(.vertical, 14)
                .padding(.horizontal, 16)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.cavnarEmber.opacity(0.09))
                .overlay(
                    RoundedRectangle(cornerRadius: CavnarRadius.control)
                        .strokeBorder(Color.cavnarEmber.opacity(0.22), lineWidth: 1)
                )
                .overlay(alignment: .leading) {
                    Rectangle().fill(Color.cavnarEmber.opacity(0.75)).frame(width: 2.5)
                }
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                .consultantReveal(stage >= 2)
                .animation(.easeOut(duration: 0.45).delay(Double(index) * 0.08), value: stage)
            }
        }
    }

    private func forecastPanel(_ forecast: String) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: "calendar.badge.clock")
                    .font(.system(size: 11, weight: .bold))
                Text("LOOKING AHEAD")
                    .font(.cavnarBody(13, weight: 700))
                    .tracking(1.3)
            }
            .foregroundStyle(Color.cavnarAmber)
            Self.mixedText(forecast, numberFont: .cavnarNumber(16, weight: 600), color: Color.cavnarInk2)
                .font(.cavnarBody(16))
                .lineSpacing(5)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarAmber.opacity(0.08))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.cavnarAmber.opacity(0.3), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    private var footer: some View {
        HStack(spacing: 8) {
            CavnarSealMark(size: 20)
            Text("Cavnar AI · analysis of your latest synced data")
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 6)
    }

    /// "Brian, your 22.3% ..." -> ("Brian,", " your 22.3% ..."). Only a
    /// short, digit-free, capitalized run ending in the first comma counts
    /// as a name — anything else stays unsplit.
    private static func splitLeadingName(_ s: String) -> (String?, String) {
        guard let comma = s.firstIndex(of: ","), s.distance(from: s.startIndex, to: comma) <= 24 else { return (nil, s) }
        let candidate = String(s[s.startIndex...comma])
        guard let first = candidate.first, first.isUppercase,
              !candidate.contains(where: { $0.isNumber }) else { return (nil, s) }
        return (candidate, String(s[s.index(after: comma)...]))
    }

    /// Splits prose into runs, giving every run that carries a digit
    /// (with any attached $ , . %) the number font — "$3,448", "43.1%",
    /// "6/1" — and leaving the words to whatever font the caller applies
    /// to the composed Text.
    private static func mixedText(_ s: String, numberFont: Font, color: Color) -> Text {
        let numberChars = Set("0123456789$,.%")
        var pieces: [(String, Bool)] = []
        var current = ""
        var inNumber = false
        func flush() {
            guard !current.isEmpty else { return }
            let isNumber = inNumber && current.contains(where: { $0.isNumber })
            pieces.append((current, isNumber))
            current = ""
        }
        for ch in s {
            let isNumChar = numberChars.contains(ch)
            if isNumChar != inNumber { flush(); inNumber = isNumChar }
            current.append(ch)
        }
        flush()
        return pieces.reduce(Text("")) { acc, piece in
            let t = Text(piece.0).foregroundStyle(color)
            return acc + (piece.1 ? t.font(numberFont) : t)
        }
    }
}

private extension View {
    /// One step of AIConsultantSheet's staggered reveal — fades and rises.
    func consultantReveal(_ shown: Bool) -> some View {
        opacity(shown ? 1 : 0).offset(y: shown ? 0 : 16)
    }
}
