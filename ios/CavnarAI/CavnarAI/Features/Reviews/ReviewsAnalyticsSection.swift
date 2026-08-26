import SwiftUI
import Charts

struct ReviewsAnalyticsSection: View {
    let viewModel: ReviewsAnalyticsViewModel

    @State private var selectedWeek: SentimentWeek?
    @State private var selectedPlatform: PlatformBreakdown?

    // Each section shows its own skeleton while it's individually still in
    // flight rather than gating the whole page behind one spinner — the 5
    // requests in ReviewsAnalyticsViewModel.load() run concurrently but are
    // awaited (and so become non-nil) in a fixed order, so e.g. the AI
    // insight — the slowest, since it's an LLM call, unlike the plain SQL
    // aggregates behind the other sections — used to visibly "pop in" a
    // couple seconds after everything else had already rendered.
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if let performance = viewModel.performance {
                    performanceCard(performance)
                } else if viewModel.isLoading {
                    performanceSkeleton
                }

                if let insight = viewModel.insight {
                    insightCard(insight)
                } else if viewModel.isLoading {
                    insightSkeleton
                }

                if !viewModel.platforms.isEmpty {
                    platformSection
                } else if viewModel.isLoading {
                    platformSkeleton
                }

                if !viewModel.heatmap.isEmpty {
                    topicGrid
                } else if viewModel.isLoading {
                    topicGridSkeleton
                }

                if !viewModel.sentimentWeeks.isEmpty {
                    trendChartCard
                } else if viewModel.isLoading {
                    trendChartSkeleton
                }
            }
            .padding(20)
        }
        .navigationDestination(item: $selectedPlatform) { platform in
            FilteredReviewsView(title: platform.platform.capitalized, platform: platform.platform)
        }
    }

    // MARK: - Loading skeletons

    private var performanceSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.5)
            HStack {
                ForEach(0..<3, id: \.self) { _ in
                    VStack(spacing: 6) {
                        CavnarSkeletonBar(height: 20, widthFraction: 0.5)
                        CavnarSkeletonBar(height: 10, widthFraction: 0.8)
                    }
                    .frame(maxWidth: .infinity)
                }
            }
        }
    }

    private var insightSkeleton: some View {
        let widths: [CGFloat] = [0.92, 0.78, 0.85, 0.62]
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(widths.indices, id: \.self) { index in
                HStack(alignment: .top, spacing: 12) {
                    Circle().fill(Color.cavnarEmber.opacity(0.15)).frame(width: 20, height: 20)
                    CavnarSkeletonBar(height: 12, widthFraction: widths[index])
                }
                .padding(.vertical, 12)
                if index < widths.count - 1 {
                    Rectangle().fill(Color.cavnarPaper3.opacity(0.25)).frame(height: 1)
                }
            }
        }
    }

    private var platformSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.3)
            HStack(spacing: 12) {
                ForEach(0..<2, id: \.self) { _ in
                    VStack(alignment: .leading, spacing: 10) {
                        CavnarSkeletonBar(height: 11, widthFraction: 0.5)
                        CavnarSkeletonBar(height: 10, widthFraction: 0.6)
                        CavnarSkeletonBar(height: 26, widthFraction: 0.4)
                        CavnarSkeletonBar(height: 11, widthFraction: 0.7)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarGlassCard()
                }
            }
        }
    }

    private var topicGridSkeleton: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.35)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(0..<4, id: \.self) { _ in
                    VStack(alignment: .leading, spacing: 8) {
                        CavnarSkeletonBar(height: 12, widthFraction: 0.6)
                        CavnarSkeletonBar(height: 20, widthFraction: 0.3)
                        CavnarSkeletonBar(height: 6, widthFraction: 1.0)
                    }
                    .padding(12)
                    .background(Color.cavnarPaper2.opacity(0.6))
                    .overlay(
                        RoundedRectangle(cornerRadius: CavnarRadius.control)
                            .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
                    )
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
        }
    }

    private var trendChartSkeleton: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarSkeletonBar(height: 11, widthFraction: 0.45)
            CavnarSkeletonBar(height: 190, widthFraction: 1.0)
            HStack(spacing: 16) {
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
                CavnarSkeletonBar(height: 9, widthFraction: 0.15)
            }
        }
        .cavnarCard()
    }

    // MARK: - AI insight (emoji lines → branded icon rows)

    // Reviews' AI insight is a distinct web convention from Labor/Food Cost/
    // Marketing's structured AIInsight — a handful of emoji-prefixed lines
    // (see client_api.py's review-insight prompt) rather than intro/
    // recommendations/forecast fields. Parsed here into icon rows instead of
    // rendering the raw emoji characters, matching the app's branded-icon
    // convention everywhere else.
    private static let insightIcons: [(prefix: String, systemImage: String)] = [
        ("📊", "chart.bar.fill"),
        ("⚠️", "exclamationmark.triangle.fill"),
        ("✅", "checkmark.circle.fill"),
        ("🔮", "sparkles"),
    ]

    // No card/gradient container here on purpose — the page already has
    // plenty of boxed sections below. These four lines float directly on
    // the module background; a dark lift-shadow plus a faint ember glow
    // (the same two-shadow technique as .cavnarNumberGlow) keeps the icon
    // and text reading as raised/lit rather than flat against the wash,
    // with hairline dividers standing in for a container edge.
    private func insightCard(_ insight: String) -> some View {
        let lines = insightLines(insight)
        return VStack(alignment: .leading, spacing: 0) {
            ForEach(Array(lines.enumerated()), id: \.offset) { index, line in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: line.icon)
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 20, alignment: .center)
                        .shadow(color: .cavnarEmber.opacity(0.55), radius: 5, x: 0, y: 0)
                    Text(line.text)
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineSpacing(4)
                        .shadow(color: .black.opacity(0.45), radius: 3, x: 0, y: 1)
                }
                .padding(.vertical, 12)
                if index < lines.count - 1 {
                    Rectangle()
                        .fill(Color.cavnarPaper3.opacity(0.25))
                        .frame(height: 1)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func insightLines(_ raw: String) -> [(icon: String, text: String)] {
        raw.split(separator: "\n").compactMap { rawLine in
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { return nil }
            for entry in Self.insightIcons where line.hasPrefix(entry.prefix) {
                let text = line.dropFirst(entry.prefix.count).trimmingCharacters(in: .whitespaces)
                return (entry.systemImage, text)
            }
            return ("circle.fill", line)
        }
    }

    // MARK: - Platform breakdown

    private var platformSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("By platform")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 12) {
                ForEach(viewModel.platforms) { platform in
                    platformCard(platform)
                        .contentShape(Rectangle())
                        .onTapGesture { selectedPlatform = platform }
                }
            }
        }
    }

    private func platformCard(_ platform: PlatformBreakdown) -> some View {
        let ratings = viewModel.platforms.map(\.avgRating)
        let isStrongest = viewModel.platforms.count > 1 && platform.avgRating == ratings.max()
        let isWeakest = viewModel.platforms.count > 1 && platform.avgRating == ratings.min() && !isStrongest

        return VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(platform.platform.uppercased())
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk2)
                Spacer()
                if isStrongest {
                    Text("Strongest")
                        .font(.cavnarBody(9, weight: 700))
                        .foregroundStyle(Color.cavnarGreen)
                } else if isWeakest {
                    Text("Needs attention")
                        .font(.cavnarBody(9, weight: 700))
                        .foregroundStyle(Color.cavnarRed)
                }
            }

            Text("\(platform.total) reviews")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)

            HStack(alignment: .firstTextBaseline, spacing: 3) {
                Text(String(format: "%.1f", platform.avgRating))
                    .font(.cavnarNumber(26, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarNumberGlow()
                Image(systemName: "star.fill")
                    .font(.system(size: 13))
                    .foregroundStyle(Color.cavnarAmber)
            }

            HStack(spacing: 12) {
                Label("\(platform.positive)", systemImage: "arrow.up")
                    .font(.cavnarNumber(11, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Label("\(platform.negative)", systemImage: "arrow.down")
                    .font(.cavnarNumber(11, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // .cavnarGlassCard(), not .cavnarGlossyCard() — the latter is real
        // interactive Liquid Glass on iOS 26 (.glassEffect(...interactive()))
        // and, wrapped inside this card's NavigationLink, was swallowing the
        // tap before it ever reached the link: tapping did nothing at all,
        // not even after a second tap. .cavnarGlassCard() is the same
        // ember-tinted-gradient family with no glass gesture recognizer of
        // its own, so the NavigationLink's tap goes through normally.
        .cavnarGlassCard()
    }

    // MARK: - Response performance

    // Free-floating like the insight lines below it — no card background,
    // it's the first thing on the page and doesn't need its own container
    // to read as a distinct section.
    private func performanceCard(_ performance: ResponsePerformance) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            (Text("Last ")
                + Text("\(performance.days)").font(.cavnarNumber(11, weight: 700))
                + Text("d"))
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack {
                statTile("Approved as-is", performance.approvedAsIs)
                statTile("Edited", performance.edited)
                statTile("Regenerated", performance.regenerated)
            }
        }
    }

    private func statTile(_ label: String, _ value: Int) -> some View {
        VStack(spacing: 2) {
            Text("\(value)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            Text(label)
                .font(.cavnarBody(10))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
    }

    // MARK: - Topic sentiment grid

    private var topicGrid: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Topic sentiment")
                .font(.cavnarBody(11, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible())], spacing: 10) {
                ForEach(viewModel.heatmap.filter { $0.count > 0 }) { entry in
                    NavigationLink {
                        FilteredReviewsView(title: entry.label, category: entry.category)
                    } label: {
                        topicCard(entry)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func topicTone(_ entry: TopicHeatmapEntry) -> CavnarTone {
        if entry.pctNegative > 20 { return .bad }
        if entry.pctPositive >= 70 { return .good }
        return .warning
    }

    private func topicCard(_ entry: TopicHeatmapEntry) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 4) {
                Text(entry.label)
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                trendIcon(entry.trend)
                Spacer()
            }
            Text("\(entry.count)")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            StatProgressBar(progress: Double(entry.pctPositive) / 100, tone: topicTone(entry))
            HStack {
                Text("\(entry.pctPositive)% pos")
                    .font(.cavnarNumber(9, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                Spacer()
                Text("\(entry.pctNegative)% neg")
                    .font(.cavnarNumber(9, weight: 600))
                    .foregroundStyle(Color.cavnarRed)
            }
        }
        .padding(12)
        .background(Color.cavnarPaper2.opacity(0.6))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.5), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    @ViewBuilder
    private func trendIcon(_ trend: String) -> some View {
        switch trend {
        case "up": Image(systemName: "arrow.up.right").font(.system(size: 9)).foregroundStyle(Color.cavnarRed)
        case "down": Image(systemName: "arrow.down.right").font(.system(size: 9)).foregroundStyle(Color.cavnarGreen)
        default: EmptyView()
        }
    }

    // MARK: - Sentiment trend chart

    private var overallAvgRating: Double {
        let totalReviews = viewModel.sentimentWeeks.reduce(0) { $0 + $1.total }
        guard totalReviews > 0 else { return 0 }
        let weightedSum = viewModel.sentimentWeeks.reduce(0.0) { $0 + $1.avgRating * Double($1.total) }
        return weightedSum / Double(totalReviews)
    }

    /// One continuous bar per week (bottom→top: positive, neutral,
    /// negative) rather than three stacked BarMarks — a single gradient can
    /// blend smoothly across each segment boundary, where three flat-color
    /// marks would always meet at a hard edge. Zero-count segments are
    /// dropped BEFORE building stops (not just clamped after) — a week
    /// that's 100% positive gets a single solid-green gradient with no
    /// amber/red stop at all, and a positive+negative week with no neutral
    /// gets one direct green→red blend instead of two collapsed,
    /// cancel-each-other-out transitions that rendered as a hard edge.
    /// Bright at the base, fading to a dark shadow at the top — same
    /// treatment as Food Cost/Labor's trend bars (the confirmed mockup
    /// style) — applied per-stop here via `faded`, so the sentiment-
    /// composition blend below (which color transitions where, and when)
    /// still reads correctly underneath the fade rather than being
    /// replaced by it.
    private func faded(_ color: Color, at location: Double) -> Color {
        color.opacity(0.9 - location * 0.55)
    }

    private func barGradient(for week: SentimentWeek) -> LinearGradient {
        let total = Double(week.total)
        let segments = [
            (color: Color.cavnarGreen, count: week.positive),
            (color: Color.cavnarAmber, count: week.neutral),
            (color: Color.cavnarRed, count: week.negative),
        ].filter { $0.count > 0 }

        guard total > 0, !segments.isEmpty else {
            return LinearGradient(colors: [Color.cavnarPaper3], startPoint: .bottom, endPoint: .top)
        }
        guard segments.count > 1 else {
            return LinearGradient(
                stops: [.init(color: faded(segments[0].color, at: 0), location: 0),
                        .init(color: faded(segments[0].color, at: 1), location: 1)],
                startPoint: .bottom, endPoint: .top
            )
        }

        let blend = 0.06
        var stops: [Gradient.Stop] = [.init(color: faded(segments[0].color, at: 0), location: 0)]
        var cumulative = 0.0
        for i in 0..<segments.count - 1 {
            cumulative += Double(segments[i].count) / total
            let loc1 = max(0, cumulative - blend)
            let loc2 = min(1, cumulative + blend)
            stops.append(.init(color: faded(segments[i].color, at: loc1), location: loc1))
            stops.append(.init(color: faded(segments[i + 1].color, at: loc2), location: loc2))
        }
        stops.append(.init(color: faded(segments.last!.color, at: 1), location: 1))
        for i in 1..<stops.count where stops[i].location < stops[i - 1].location {
            stops[i].location = stops[i - 1].location
        }
        return LinearGradient(gradient: Gradient(stops: stops), startPoint: .bottom, endPoint: .top)
    }

    /// Average weekly review volume across the visible window — a neutral
    /// reference line (no red/green implication the way Food Cost's waste
    /// average or Labor's target line has): more reviews than average
    /// isn't "bad" the way more waste or higher labor % is, so unlike
    /// those two charts this line never recolors a bar, it's purely
    /// there for the same "is this week typical" context at a glance.
    private var averageWeeklyVolume: Double {
        guard !viewModel.sentimentWeeks.isEmpty else { return 0 }
        return viewModel.sentimentWeeks.reduce(0.0) { $0 + Double($1.total) } / Double(viewModel.sentimentWeeks.count)
    }

    @ChartContentBuilder
    private func trendBars() -> some ChartContent {
        ForEach(viewModel.sentimentWeeks) { week in
            BarMark(x: .value("Week", week.label), y: .value("Reviews", week.total))
                .foregroundStyle(barGradient(for: week))
                .cornerRadius(3)
        }
    }

    private var trendChartCard: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("SENTIMENT TREND — LAST 8 WEEKS")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                Spacer()
                (Text("Avg ") + Text(String(format: "%.1f★", overallAvgRating)).font(.cavnarNumber(11, weight: 700)))
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(Color.cavnarAmber)
            }

            // Chart marks aren't Views, so they can't take .shadow() — a
            // blurred, non-interactive duplicate of the same bars sitting
            // directly behind the crisp chart gives each bar its own soft
            // border glow instead of one flat halo behind the whole plot
            // rectangle. The glow copy mirrors the crisp chart's AXIS
            // CONTENT exactly (same "60"/"40"/... value text, same font),
            // just with a clear foreground, rather than a fixed placeholder
            // — a leading-position y-axis reserves gutter width equal to
            // its widest rendered label, so a differently-sized placeholder
            // (or .hidden(), which reserves none) shifts that whole chart's
            // plot area left/right relative to the crisp one, throwing the
            // bars out of alignment. A small blur radius keeps the glow
            // hugging the bar edges instead of spreading into a wide blob.
            ZStack {
                Chart { trendBars() }
                    .chartLegend(.hidden)
                    .chartYAxis {
                        AxisMarks(position: .leading) { value in
                            AxisGridLine().foregroundStyle(.clear)
                            AxisValueLabel {
                                if let v = value.as(Int.self) {
                                    Text("\(v)").font(.cavnarBody(9)).foregroundStyle(.clear)
                                }
                            }
                        }
                    }
                    .chartXAxis {
                        AxisMarks { _ in
                            AxisValueLabel().font(.cavnarBody(9)).foregroundStyle(.clear)
                        }
                    }
                    .opacity(0.6)
                    .blur(radius: 1.6)
                    .allowsHitTesting(false)

                // The average RuleMark + its capsule annotation live only
                // on this crisp chart, not the blurred glow copy above —
                // duplicating it there would blur the label text itself,
                // which reads as a mistake rather than a glow.
                Chart {
                    trendBars()
                    RuleMark(y: .value("Average", averageWeeklyVolume))
                        .foregroundStyle(Color.cavnarInk.opacity(0.8))
                        .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                        .annotation(position: .top, alignment: .trailing) {
                            Text("avg \(Int(averageWeeklyVolume))")
                                .font(.cavnarBody(9, weight: 700))
                                .foregroundStyle(Color.black)
                                .padding(.horizontal, 6)
                                .padding(.vertical, 3)
                                .background(Color.cavnarInk)
                                .clipShape(Capsule())
                        }
                }
                    .chartLegend(.hidden)
                    .chartYAxis {
                        AxisMarks(position: .leading) { value in
                            AxisGridLine().foregroundStyle(Color.cavnarPaper3.opacity(0.4))
                            AxisValueLabel {
                                if let v = value.as(Int.self) {
                                    Text("\(v)").font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                        }
                    }
                    .chartXAxis {
                        AxisMarks { _ in
                            AxisValueLabel()
                                .font(.cavnarBody(9))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    .shadow(color: .black.opacity(0.3), radius: 2, x: 0, y: 1)
                    .chartOverlay { proxy in
                        GeometryReader { geo in
                            weekSelectionOverlay(proxy: proxy, geo: geo)
                        }
                    }
            }
            .frame(height: 190)

            HStack(spacing: 16) {
                legendItem(color: .cavnarGreen, label: "Positive")
                legendItem(color: .cavnarAmber, label: "Neutral")
                legendItem(color: .cavnarRed, label: "Negative")
            }
        }
        .cavnarCard()
    }

    /// Press-and-hold-to-inspect, the touch equivalent of a desktop hover
    /// tooltip: drag (with zero minimum distance, so a plain touch-down
    /// already counts) picks the nearest week under the finger and shows
    /// its exact pos/neutral/negative split; lifting the finger dismisses it.
    @ViewBuilder
    private func weekSelectionOverlay(proxy: ChartProxy, geo: GeometryProxy) -> some View {
        if let selectedWeek, let plotFrame = proxy.plotFrame {
            let frame = geo[plotFrame]
            if let xPosition = proxy.position(forX: selectedWeek.label) {
                let clampedX = min(max(xPosition + frame.origin.x, frame.minX + 60), frame.maxX - 60)
                weekTooltip(selectedWeek)
                    .position(x: clampedX, y: frame.minY + 30)
            }
        }
        Rectangle()
            .fill(Color.clear)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { value in
                        guard let plotFrame = proxy.plotFrame else { return }
                        let originX = geo[plotFrame].origin.x
                        guard let label: String = proxy.value(atX: value.location.x - originX) else { return }
                        selectedWeek = viewModel.sentimentWeeks.first { $0.label == label }
                    }
                    .onEnded { _ in selectedWeek = nil }
            )
    }

    private func weekTooltip(_ week: SentimentWeek) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(week.label)
                .font(.cavnarBody(10, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            HStack(spacing: 10) {
                tooltipStat(week.positive, color: .cavnarGreen)
                tooltipStat(week.neutral, color: .cavnarAmber)
                tooltipStat(week.negative, color: .cavnarRed)
            }
        }
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.95))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.control)
                .strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
        .shadow(color: .black.opacity(0.4), radius: 8, x: 0, y: 4)
        .fixedSize()
    }

    private func tooltipStat(_ value: Int, color: Color) -> some View {
        HStack(spacing: 3) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text("\(value)").font(.cavnarNumber(11, weight: 700)).foregroundStyle(Color.cavnarInk)
        }
    }

    private func legendItem(color: Color, label: String) -> some View {
        HStack(spacing: 4) {
            Circle().fill(color).frame(width: 6, height: 6)
            Text(label).font(.cavnarBody(9)).foregroundStyle(Color.cavnarInk3)
        }
    }
}
