import SwiftUI

/// Just the tappable strip content (sparkle + truncated intro + chevron) —
/// no background/border of its own. AIConsultantEmbeddedStrip and
/// AIConsultantView below both build on this; the difference between them
/// is only whether they draw their own card chrome around it.
private struct AIConsultantStripContent: View {
    let insight: AIInsight?
    let isLoading: Bool
    /// The hero's readable form (density #30): the intro at up to three
    /// lines in ink2 with "Read the analysis ›" under it, instead of one
    /// clipped grey line — the 30-second "why" behind the hero's number.
    var readable: Bool = false
    let onTap: () -> Void

    var body: some View {
        if readable {
            readableBody
        } else {
            stripBody
        }
    }

    private var readableBody: some View {
        Button(action: onTap) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(.top, 3)
                VStack(alignment: .leading, spacing: 6) {
                    Group {
                        if let insight, !insight.intro.isEmpty {
                            HomeMixedText.make(insight.intro, role: .body)
                        } else if isLoading {
                            PulsingAnalyzingText()
                                .font(.cavnar(.body))
                                .foregroundStyle(Color.cavnarInk2)
                        } else {
                            Text("No analysis yet")
                                .font(.cavnar(.body))
                                .foregroundStyle(Color.cavnarInk2)
                        }
                    }
                    .lineLimit(3)
                    .truncationMode(.tail)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    if insight != nil {
                        HStack(spacing: 4) {
                            Text("Read the analysis")
                                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                            Image(systemName: "chevron.right")
                                .font(.cavnar(.caption))
                        }
                        .foregroundStyle(Color.cavnarEmber2)
                    }
                }
                Spacer(minLength: 0)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(insight == nil)
    }

    private var stripBody: some View {
        Button(action: onTap) {
            HStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.cavnar(.secondary))
                Group {
                    if let insight, !insight.intro.isEmpty {
                        Text(insight.intro)
                    } else if isLoading {
                        PulsingAnalyzingText()
                    } else {
                        Text("No analysis yet")
                    }
                }
                .font(.cavnar(.body))
                .lineLimit(1)
                .truncationMode(.tail)
                // The sentence itself is body copy and reads as the app's
                // other small text does — ember stays on the sparkle and
                // the chevron, which is what actually says "this is the AI,
                // and it opens." A whole line of orange competed with the
                // hero's own figures right above it for no added meaning.
                .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 8)
                if insight != nil {
                    Image(systemName: "chevron.right")
                        .font(.cavnar(.caption))
                }
            }
            .foregroundStyle(Color.cavnarEmber2)
            .frame(minHeight: 44)
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
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var pulse = false

    var body: some View {
        Text("Analyzing this week's numbers…")
            .opacity(pulse ? 1 : 0.45)
            .onAppear {
                // Reduce Motion settles this rather than looping forever
                // (audit 7.6).
                guard !reduceMotion else { return }
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
    /// The rec_ledger surface ("food", "marketing", …) the sheet's
    /// recommendations answer for. Nil — every caller that hasn't opted in —
    /// shows no answer controls, even when the insight carries keys.
    var recSurface: String? = nil
    /// The hero's readable why — the intro at 2–3 lines in ink2 with "Read
    /// the analysis ›" (density #30). On by default: the Labor and Food
    /// Cost heroes are this strip's only callers.
    var readable: Bool = true

    @State private var isPresented = false

    var body: some View {
        AIConsultantStripContent(insight: insight, isLoading: isLoading, readable: readable) {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        }
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight, showForecast: showForecastInSheet,
                                  recSurface: recSurface)
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
    /// See AIConsultantEmbeddedStrip.recSurface.
    var recSurface: String? = nil

    @State private var isPresented = false

    var body: some View {
        AIConsultantStripContent(insight: insight, isLoading: isLoading) {
            guard insight != nil else { return }
            Haptic.selection()
            isPresented = true
        }
        .padding(.horizontal, 14)
        // The strip itself is 44pt tall now (a tap target); the pill adds
        // only a hair around it.
        .padding(.vertical, 2)
        .frame(maxWidth: .infinity)
        .background(Color.cavnarEmber.opacity(0.12))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarEmber.opacity(0.32), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .sheet(isPresented: $isPresented) {
            if let insight {
                AIConsultantSheet(title: title, insight: insight, showForecast: showForecastInSheet,
                                  recSurface: recSurface)
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
/// app-wide numbers rule. Readability round (10/8/26): only the intro's
/// first sentence is the headline, the rest is Body; the unverified-figures
/// and unverified-causes caveats sit at the top; recommendation #1 is the
/// hero and the others wait behind "+N more". Keeps the module's own
/// ember-wash background. No
/// explicit close button — swipe-down-to-dismiss, same as every other sheet
/// in this app (NotificationsListView is the established precedent).
private struct AIConsultantSheet: View {
    let title: String
    let insight: AIInsight
    var showForecast: Bool = true
    var recSurface: String? = nil

    // Staggered reveal: 1 opening line, 2 recommendations, 3 forecast,
    // 4 footer. (No header row — the sheet's title already names the
    // consultant, a badge + kicker restating it read as redundant.)
    @State private var stage = 0
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.xxl) {
                    if insight.figuresVerified == false || insight.causesVerified == false {
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            if insight.figuresVerified == false {
                                CavnarCaveat.unverifiedFigures(insight.unsupportedFigures ?? [])
                            }
                            if insight.causesVerified == false {
                                CavnarCaveat.unverifiedCauses(insight.unsupportedCauses ?? [])
                            }
                        }
                    }
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
                .padding(.horizontal, CavnarSpace.gutter)
                .padding(.top, CavnarSpace.l)
                .padding(.bottom, 44)
            }
            .cavnarModuleBackground()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { cavnarTitleToolbar(title) }
            .task {
                // Reduce Motion: everything at once, no rise.
                if reduceMotion { stage = 4; return }
                for step in 1...4 {
                    withAnimation(.easeOut(duration: 0.45)) { stage = step }
                    try? await Task.sleep(for: .seconds(0.12))
                }
            }
        }
    }

    /// The AI's own opening sentence as the headline — the owner's name (the
    /// leading "Brian," when there is one) in ember, every number in Space
    /// Grotesk, the rest in ink — and the rest of the intro under it as Body.
    /// The whole intro used to be one 22pt Clash block.
    private var openingLine: some View {
        let (first, rest) = Self.splitFirstSentence(insight.intro)
        let (name, headline) = Self.splitLeadingName(first)
        let numberFont = Font.cavnarNumber(CavnarText.headline.size, weight: 600,
                                           relativeTo: CavnarText.headline.textStyle)
        var text = Text("")
        if let name {
            text = text + Text(name).foregroundStyle(Color.cavnarEmber)
        }
        text = text + Self.mixedText(headline, numberFont: numberFont, color: Color.cavnarInk)
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            text
                .cavnarText(.headline)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityAddTraits(.isHeader)
            if !rest.isEmpty {
                CavnarMixedText(rest, role: .body)
            }
            claimKindLabel("Reading of your numbers", icon: "chart.bar.doc.horizontal", color: Color.cavnarInk2)
        }
    }

    /// "Labor ran 31.2% last week. Two closers…" → ("Labor ran 31.2% last
    /// week.", "Two closers…"). A sentence ends at . ! or ? followed by a
    /// space and a capital, digit or "$" — so "31.2%" and "Sep. 3" stay put.
    static func splitFirstSentence(_ s: String) -> (String, String) {
        let chars = Array(s)
        var i = 0
        while i + 2 < chars.count {
            if ".!?".contains(chars[i]), chars[i + 1] == " ",
               chars[i + 2].isUppercase || chars[i + 2].isNumber || chars[i + 2] == "$" {
                let head = String(chars[0...i]).trimmingCharacters(in: .whitespaces)
                let tail = String(chars[(i + 2)...]).trimmingCharacters(in: .whitespaces)
                return (head, tail)
            }
            i += 1
        }
        return (s.trimmingCharacters(in: .whitespaces), "")
    }

    /// A measured fact, the model's read of it, a guess about next week and
    /// a suggestion were all rendered as the same prose in the same weight,
    /// so a reader had no way to tell one from another. The API has labelled
    /// them as inferred / suggestion / forecast for a while; nothing showed
    /// the distinction. These say which is which.
    private func claimKindLabel(_ text: String, icon: String, color: Color) -> some View {
        // A tag in a Paper3 capsule, like ClaimKindTag: it says what kind of
        // statement this is, not how good the news is.
        HStack(spacing: CavnarSpace.xxs) {
            Image(systemName: icon).accessibilityHidden(true)
            Text(text)
        }
        .cavnarText(.tag, color: color)
        .padding(.horizontal, CavnarSpace.xs)
        .padding(.vertical, 3)
        .background(Color.cavnarPaper3, in: Capsule())
    }

    private var recommendations: some View {
        let recs = Array(insight.recommendations.enumerated())
        return VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Suggested \u{2014} your call", icon: "bolt.fill")
                .consultantReveal(stage >= 2)

            // #1 is the hero; the rest wait behind "+N more".
            if let first = recs.first {
                recommendationCard(index: first.offset, text: first.element, isHero: true)
                    .consultantReveal(stage >= 2)
            }
            if recs.count > 1 {
                CavnarMoreDisclosure(hiddenCount: recs.count - 1) {
                    ForEach(recs.dropFirst(), id: \.offset) { index, rec in
                        recommendationCard(index: index, text: rec, isHero: false)
                    }
                }
                .consultantReveal(stage >= 2)
            }
        }
    }

    private func recommendationCard(index: Int, text rec: String, isHero: Bool) -> some View {
        HStack(alignment: .top, spacing: 14) {
            ZStack {
                Circle()
                    .fill(Color.cavnarEmber)
                    .frame(width: 30, height: 30)
                    .frame(width: 44, height: 44)   // HIG tap target (audit 7.3)
                    .contentShape(Rectangle())
                    .shadow(color: Color.cavnarEmber.opacity(0.6), radius: 7, x: 0, y: 0)
                Text("\(index + 1)")
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(.white)
            }
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarMixedText(rec, role: isHero ? .lead : .body, color: .cavnarInk)
                // Done / Not for us / Track — only for a line the
                // server keyed, on a surface that opted in.
                if let recSurface, let key = insight.recKey(at: index) {
                    RecAnswerRow(key: key, surface: recSurface)
                }
            }
            .padding(.top, 8)
        }
        .padding(.vertical, 14)
        .padding(.horizontal, CavnarSpace.m)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarEmber.opacity(isHero ? 0.11 : 0.07))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarEmber.opacity(0.22), lineWidth: 1)
        )
        .overlay(alignment: .leading) {
            Rectangle().fill(Color.cavnarEmber.opacity(0.75)).frame(width: 2.5)
        }
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func forecastPanel(_ forecast: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            // Amber: the kicker's colour is its meaning here (a projection).
            CavnarKicker("Looking ahead", icon: "calendar.badge.clock", tint: .cavnarAmber)
            CavnarMixedText(forecast, role: .body)
            Text("A projection, not a measurement. It assumes the current trend holds.")
                .cavnarText(.caption)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(CavnarSpace.cardPadding)
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
                .cavnarText(.caption)
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
