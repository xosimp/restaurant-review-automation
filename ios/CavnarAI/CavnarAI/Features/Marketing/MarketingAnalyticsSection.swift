import SwiftUI

struct MarketingAnalyticsSection: View {
    let viewModel: MarketingAnalyticsViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            AIConsultantView(
                title: "Cavnar AI Marketing Brief",
                insight: viewModel.insight,
                isLoading: viewModel.isLoadingInsight
            )

            if viewModel.isLoading && viewModel.performance == nil {
                // Analyzing performance/attribution across several requests
                // at once — the app's real "working" moment, not a plain
                // shimmering placeholder line.
                CavnarWorkingOrb(state: .solving, label: "Analyzing your marketing…")
                    .padding(.vertical, 24)
            } else {
                windowSection
                attributionSection

                if let perf = viewModel.performance, perf.hasData {
                    if let top = perf.topPost {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Top post").font(.cavnarBody(17.5, weight: 700)).foregroundStyle(Color.cavnarInk3)
                            Text(top.topic ?? "").font(.cavnarBody(18, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(top.reach) reach · \(top.likes) likes · \(top.comments) comments")
                                .font(.cavnarNumber(17.5))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        .cavnarCard()
                    }
                } else if viewModel.performance != nil {
                    Text("No published post metrics yet.")
                        .font(.cavnarBody(18))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }

            if !viewModel.recentTopics.isEmpty {
                recentlyGenerated
            }
        }
    }

    /// A period, against the period before it. "Total reach 4,231" with no
    /// denominator and no trend is a number, not a metric — and engagement
    /// RATE is the one that survives a follower count changing.
    @ViewBuilder
    private var windowSection: some View {
        if let window = viewModel.window {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Period", selection: Binding(
                    get: { viewModel.windowDays },
                    // The haptic goes in the SETTER, not on a .sensoryFeedback
                    // watching windowDays: the value is written here
                    // synchronously and the reload it kicks off is async, so a
                    // feedback modifier keyed to the published value fired late
                    // (or not at all when the request failed). This buzzes on
                    // the tap, like every other control in the app.
                    set: { days in
                        guard days != viewModel.windowDays else { return }
                        Haptic.light()
                        Task { await viewModel.setWindow(days) }
                    })) {
                    Text("7 days").tag(7)
                    Text("30 days").tag(30)
                    Text("90 days").tag(90)
                }
                .pickerStyle(.segmented)

                HStack(spacing: 0) {
                    trendTile("\(window.reach)", "Reach", window.change.reach)
                    Divider()
                    trendTile("\(window.engagement)", "Engagement", window.change.engagement)
                    Divider()
                    trendTile("\(window.engagementRate)%", "Rate", nil)
                }
                .cavnarGlassCard()

                if !window.byPlatform.isEmpty {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("By platform")
                            .font(.cavnarBody(17.5, weight: 700))
                            .foregroundStyle(Color.cavnarInk3)
                        ForEach(window.byPlatform) { platform in
                            HStack {
                                Text(platform.label)
                                    .font(.cavnarBody(18, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Spacer()
                                (Text("\(platform.posts)").font(.cavnarNumber(17.5, weight: 700))
                                    + Text(" posts · ")
                                    + Text("\(platform.reach)").font(.cavnarNumber(17.5, weight: 700))
                                    + Text(" reach · ")
                                    + Text("\(platform.engagementRate)%").font(.cavnarNumber(17.5, weight: 700)))
                                    .font(.cavnarBody(17.5))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                    }
                    .cavnarCard()
                }
            }
        }
    }

    private func trendTile(_ value: String, _ label: String, _ change: Double?) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(26, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(17.5)).foregroundStyle(Color.cavnarInk3)
            if let change {
                Text("\(change > 0 ? "+" : "")\(change, specifier: "%.0f")%")
                    .font(.cavnarNumber(17.5, weight: 700))
                    .foregroundStyle(change >= 0 ? Color.cavnarGreen : Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity)
    }

    /// What a post did to the till — the question a marketing director asks
    /// first, and the one nothing in this product could answer. Labelled as a
    /// correlation because that is what it is.
    @ViewBuilder
    private var attributionSection: some View {
        if let attribution = viewModel.attribution {
            VStack(alignment: .leading, spacing: 10) {
                Text("What posts did to sales")
                    .font(.cavnarBody(18, weight: 700))
                    .foregroundStyle(Color.cavnarInk)

                if attribution.ok, !attribution.posts.isEmpty {
                    Text("Sales in the two days after each post, against the same weekday before it. This is a correlation, not proof — a busy Friday is still a busy Friday.")
                        .font(.cavnarBody(17.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    ForEach(attribution.posts) { post in
                        HStack(alignment: .top) {
                            Text(post.topic ?? "Untitled")
                                .font(.cavnarBody(18, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 8)
                            Text("\(post.liftPct > 0 ? "+" : "")\(post.liftPct, specifier: "%.0f")%")
                                .font(.cavnarNumber(18.5, weight: 700))
                                .foregroundStyle(post.liftPct >= 0 ? Color.cavnarGreen : Color.cavnarInk3)
                        }
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.cavnarPaper2)
                        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                    }
                } else {
                    Text(attribution.emptyExplanation)
                        .font(.cavnarBody(17.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .cavnarCard()
        }
    }

    /// Per-piece history — what was written, whether it went out, and what it
    /// did. Pull to refresh pulls fresh numbers from Meta first.
    private var recentlyGenerated: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Recently generated")
                    .font(.cavnarBody(18, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if viewModel.isRefreshingMetrics {
                    CavnarShimmerText(text: "Refreshing…")
                }
            }

            ForEach(viewModel.recentTopics) { topic in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .top, spacing: 8) {
                        Text(topic.topic)
                            .font(.cavnarBody(18, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 8)
                        if topic.posted {
                            Label(topic.platformLabel ?? "Posted", systemImage: "checkmark")
                                .font(.cavnarBody(17.5, weight: 700))
                                .foregroundStyle(Color.cavnarGreen)
                        } else {
                            Text("Draft")
                                .font(.cavnarBody(17.5, weight: 700))
                                .foregroundStyle(Color.cavnarEmber2)
                        }
                    }
                    if let line = topic.metricsLine {
                        Text(line).font(.cavnarNumber(17.5)).foregroundStyle(Color.cavnarInk3)
                    } else if topic.posted {
                        Text("No numbers back from Meta yet")
                            .font(.cavnarBody(17.5))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.cavnarPaper2)
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
            }
        }
    }

    private func statTile(value: String, label: String) -> some View {
        VStack(spacing: 4) {
            Text(value).font(.cavnarNumber(26, weight: 500)).foregroundStyle(Color.cavnarInk).cavnarNumberGlow()
            Text(label).font(.cavnarBody(17.5)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }
}
