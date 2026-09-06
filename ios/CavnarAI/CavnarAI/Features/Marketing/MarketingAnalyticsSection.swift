import SwiftUI

/// The Analytics tab, top to bottom: the brief (one headline, the numbered
/// moves), the period, a glossy stats tile with the engagement-rate ring,
/// thin platform bars, what posts did to sales, and the recent pieces.
struct MarketingAnalyticsSection: View {
    let viewModel: MarketingAnalyticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            briefCard

            if viewModel.isLoading && viewModel.performance == nil {
                // Analyzing performance/attribution across several requests
                // at once — the app's real "working" moment, not a plain
                // shimmering placeholder line.
                CavnarWorkingOrb(state: .solving, label: "Analyzing your marketing…")
                    .padding(.vertical, 24)
                    .frame(maxWidth: .infinity)
            } else {
                periodSwitcher
                if let window = viewModel.window {
                    statsTile(window)
                    if !window.byPlatform.isEmpty {
                        platformBars(window)
                    }
                } else if viewModel.performance != nil {
                    Text("No published post metrics yet.")
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .cavnarCard()
                }
                attributionCard
            }

            if !viewModel.recentTopics.isEmpty {
                recentlyGenerated
            }
        }
    }

    // MARK: - Brief

    /// The consultant's read as one headline and the numbered moves — not
    /// a strip of prose. The insight endpoint already writes in this shape
    /// (Line 1, then "1." "2."); this just stops flattening it back out.
    private var briefCard: some View {
        VStack(alignment: .leading, spacing: 0) {
            if viewModel.isLoadingInsight && viewModel.insight == nil {
                CavnarWorkingOrb(state: .solving, label: "Reading your numbers…")
                    .padding(.vertical, 8)
                    .frame(maxWidth: .infinity)
            } else if let insight = viewModel.insight {
                Text(insight.intro)
                    .font(.cavnarHeadline(18))
                    .foregroundStyle(Color.cavnarInk)
                    .lineSpacing(3)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, insight.recommendations.isEmpty ? 0 : 12)

                ForEach(Array(insight.recommendations.enumerated()), id: \.offset) { index, rec in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("\(index + 1)")
                            .font(.cavnarNumber(14.5, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                        Text(rec)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk2)
                            .lineSpacing(3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(alignment: .top) {
                        Rectangle().fill(Color.cavnarEmber2.opacity(0.18)).frame(height: 1)
                    }
                }

                if let forecast = insight.forecast, !forecast.isEmpty {
                    Text(forecast)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 8)
                }
            } else {
                Text("Publish a post or two and the brief will have something to say.")
                    .font(.cavnarBody(15))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarEmber.opacity(0.12))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card).strokeBorder(Color.cavnarEmber.opacity(0.35), lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    // MARK: - Period

    private var periodSwitcher: some View {
        HStack(spacing: 0) {
            ForEach([7, 30, 90], id: \.self) { days in
                Button {
                    // The haptic fires here on the tap, not off the published
                    // value: the reload is async and a feedback modifier keyed
                    // to windowDays fired late (or not at all on failure).
                    guard days != viewModel.windowDays else { return }
                    Haptic.light()
                    Task { await viewModel.setWindow(days) }
                } label: {
                    (Text("\(days)").font(.cavnarNumber(15, weight: 700)) + Text(" days"))
                        .font(.cavnarBody(15, weight: 700))
                        .foregroundStyle(days == viewModel.windowDays ? Color.cavnarInk : Color.cavnarInk3)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .background(days == viewModel.windowDays ? Color.cavnarPaper3 : Color.clear)
                        .clipShape(Capsule())
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(Color.white.opacity(0.04))
        .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .clipShape(Capsule())
        .animation(.easeOut(duration: 0.2), value: viewModel.windowDays)
    }

    // MARK: - Stats

    /// A period against the period before it. "Total reach 4,231" with no
    /// denominator and no trend is a number, not a metric — and engagement
    /// RATE is the one that survives a follower count changing.
    private func statsTile(_ window: MarketingWindow) -> some View {
        HStack(alignment: .center, spacing: 8) {
            bigStat(window.reach.formatted(), "Reach", window.change.reach)
            bigStat(window.engagement.formatted(), "Engagement", window.change.engagement)
            rateRing(window.engagementRate)
                .frame(width: 96)
        }
        .cavnarGlossyCard()
    }

    private func bigStat(_ value: String, _ label: String, _ change: Double?) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(value)
                .font(.cavnarNumber(24, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(label).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
            if let change {
                Text("\(change > 0 ? "+" : "")\(change, specifier: "%.0f")%")
                    .font(.cavnarNumber(13, weight: 700))
                    .foregroundStyle(change >= 0 ? Color.cavnarGreen : Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Engagement rate as a ring. Drawn against a 10% full scale — social
    /// rates live between 1% and 6%, and against 100% every ring would be a
    /// sliver that says nothing.
    private func rateRing(_ rate: Double) -> some View {
        ZStack {
            Circle().stroke(Color.cavnarPaper3, lineWidth: 5)
            Circle()
                .trim(from: 0, to: min(max(rate / 10, 0), 1))
                .stroke(Color.cavnarEmber, style: StrokeStyle(lineWidth: 5, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .animation(.easeOut(duration: 0.6), value: rate)
            VStack(spacing: 1) {
                Text("\(rate, specifier: "%.1f")%")
                    .font(.cavnarNumber(17, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("Rate").font(.cavnarBody(11)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(width: 74, height: 74)
        .padding(.vertical, 5)
    }

    // MARK: - Platforms

    private func platformBars(_ window: MarketingWindow) -> some View {
        let maxReach = max(window.byPlatform.map(\.reach).max() ?? 0, 1)
        return VStack(alignment: .leading, spacing: 10) {
            Text("By platform")
                .font(.cavnarBody(16, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            ForEach(window.byPlatform) { platform in
                HStack(spacing: 10) {
                    Text(platform.label)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineLimit(1)
                        .frame(width: 82, alignment: .leading)
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.cavnarPaper3)
                            Capsule()
                                .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                                     startPoint: .leading, endPoint: .trailing))
                                .frame(width: geo.size.width * CGFloat(platform.reach) / CGFloat(maxReach))
                        }
                    }
                    .frame(height: 8)
                    Text(platform.reach > 0 ? platform.reach.formatted() : "—")
                        .font(.cavnarNumber(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 52, alignment: .trailing)
                }
            }
        }
        .cavnarCard()
    }

    // MARK: - Attribution

    /// What a post did to the till — the question a marketing director asks
    /// first. Labelled as a comparison because that is what it is.
    @ViewBuilder
    private var attributionCard: some View {
        if let attribution = viewModel.attribution {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text("What posts did to sales")
                        .font(.cavnarBody(16, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                        .fixedSize()
                        .layoutPriority(1)
                    Spacer(minLength: 6)
                    Text("same weekday, before vs after")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                        .multilineTextAlignment(.trailing)
                        .lineLimit(2)
                }
                .padding(.bottom, 4)

                if attribution.ok, !attribution.posts.isEmpty {
                    ForEach(Array(attribution.posts.enumerated()), id: \.element.id) { index, post in
                        HStack(alignment: .firstTextBaseline) {
                            Text(post.topic ?? "Untitled")
                                .font(.cavnarBody(15))
                                .foregroundStyle(Color.cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            Text("\(post.liftPct > 0 ? "+" : "")\(post.liftPct, specifier: "%.0f")%")
                                .font(.cavnarNumber(15, weight: 700))
                                .foregroundStyle(post.liftPct > 0 ? Color.cavnarGreen : Color.cavnarInk3)
                        }
                        .padding(.vertical, 9)
                        .overlay(alignment: .top) {
                            if index > 0 { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
                        }
                    }
                } else {
                    Text(attribution.emptyExplanation)
                        .font(.cavnarBody(15))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.top, 6)
                }
            }
            .cavnarCard()
        }
    }

    // MARK: - Recent

    /// Per-piece history — what was written, whether it went out, and what it
    /// did. Pull to refresh pulls fresh numbers from Meta first.
    private var recentlyGenerated: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Recently generated")
                    .font(.cavnarBody(16, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if viewModel.isRefreshingMetrics {
                    CavnarShimmerText(text: "Refreshing…")
                }
            }
            .padding(.bottom, 4)

            ForEach(Array(viewModel.recentTopics.enumerated()), id: \.element.id) { index, topic in
                VStack(alignment: .leading, spacing: 3) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(topic.topic)
                            .font(.cavnarBody(15))
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        if topic.posted {
                            pill("\(topic.platformLabel ?? "Posted") ✓", tone: Color.cavnarGreen)
                        } else {
                            pill("Draft", tone: Color.cavnarEmber2)
                        }
                    }
                    if let line = topic.metricsLine {
                        Text(line).font(.cavnarNumber(13)).foregroundStyle(Color.cavnarInk3)
                    } else if topic.posted {
                        Text("No numbers back from Meta yet")
                            .font(.cavnarBody(13))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(.vertical, 9)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(alignment: .top) {
                    if index > 0 { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
                }
            }
        }
        .cavnarCard()
    }

    private func pill(_ text: String, tone: Color) -> some View {
        Text(text)
            .font(.cavnarBody(12, weight: 700))
            .foregroundStyle(tone)
            .padding(.horizontal, 9)
            .padding(.vertical, 3)
            .background(tone.opacity(0.14))
            .clipShape(Capsule())
            .lineLimit(1)
    }
}
