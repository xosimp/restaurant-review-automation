import SwiftUI

/// Robinhood/finance-app-style hero stat: eyebrow label, big Space Grotesk
/// number, a colored delta line, a Canvas-drawn sparkline with a glowing
/// endpoint, and time-range pills underneath — matches the approved mockup,
/// in the app's ember palette instead of gold. Deliberately unboxed (no
/// .cavnarCard() wrapper), sitting flush on the page like the reference
/// screenshot rather than looking like another KPI tile.
struct ValueChartCard: View {
    let totalValue: Int
    let history: [ValueSnapshot]

    @State private var selectedRange: ChartRange = .oneMonth

    // The big number counts up from 0 on the very first appearance only —
    // a flat instant number read as "just a stat," counting up reads as
    // "watch how much this is." hasCountedUp guards against replaying that
    // on every pull-to-refresh; a later change to totalValue just snaps the
    // displayed figure straight to the new value instead.
    @State private var animatedTotal: Double = 0
    @State private var hasCountedUp = false

    private var rangeHistory: [ValueSnapshot] {
        Self.filter(history, to: selectedRange)
    }

    // Real trend needs both enough points AND actual variation between
    // them — two identical snapshots (nothing changed day over day, common
    // for a brand-new restaurant) technically satisfy "count >= 2" but plot
    // as a flat line with none of the fill/glow/curve that makes the chart
    // read as a chart. Either case falls back to the same illustrative
    // curve below.
    private var hasRealTrend: Bool {
        guard rangeHistory.count >= 2 else { return false }
        let values = rangeHistory.map(\.value)
        return (values.max() ?? 0) != (values.min() ?? 0)
    }

    private var accessibilitySummary: String {
        let current = Self.currencyText(totalValue)
        guard let first = history.first, history.count > 1 else { return current }
        return "\(current), up from \(Self.currencyText(first.value)) "
            + "across \(history.count) snapshots"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("TOTAL VALUE DELIVERED")
                .font(.cavnarBody(14, weight: 700))
                .tracking(1.5)
                .foregroundStyle(Color.cavnarEmber2)

            AnimatableNumberText(value: animatedTotal, format: Self.currencyText)
                .font(.cavnarNumber(38, weight: 600))
                .foregroundStyle(Color.cavnarGreen)
                .cavnarNumberGlow(.cavnarGreen)
                .cavnarSensitive()
                .onAppear {
                    guard !hasCountedUp else { return }
                    hasCountedUp = true
                    // Fast enough not to feel like a stall, slow enough that
                    // the client actually watches the number climb rather
                    // than just glancing at a static figure.
                    withAnimation(.easeOut(duration: 1.6)) {
                        animatedTotal = Double(totalValue)
                    }
                }
                .onChange(of: totalValue) { _, newValue in
                    guard hasCountedUp else { return }
                    animatedTotal = Double(newValue)
                }

            Group {
                if let delta = deltaInfo {
                    HStack(spacing: 4) {
                        Text(delta.isPositive ? "▲" : "▼")
                        Text(delta.text)
                    }
                    .foregroundStyle(delta.isPositive ? Color.cavnarGreen : Color.cavnarRed)
                } else {
                    // No baseline yet to compare against (first day using the
                    // app) — an empty delta line would look broken, and a
                    // fabricated one would be lying about this restaurant's
                    // own numbers.
                    Text("Tracking starts today")
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .font(.cavnarBody(14, weight: 600))
            .padding(.bottom, 10)

            // With no real trend yet, the chart still draws — just from a
            // representative sample curve — instead of sitting blank or
            // (worse) a flat line with no context, so a brand-new
            // restaurant sees what the chart becomes rather than an empty
            // box.
            SparklineCanvas(values: hasRealTrend ? rangeHistory.map { Double($0.value) } : Self.sampleTrend)
                .id(selectedRange)
                .frame(height: 120)

            if !hasRealTrend {
                Text("Example — shows the trend a typical restaurant sees over time")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3.opacity(0.8))
                    .padding(.top, 4)
            }

            rangePills
        }
        // audit 7.4 — the headline number the whole Home screen is built around
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Total value delivered")
        .accessibilityValue(accessibilitySummary)
    }

    private var rangePills: some View {
        HStack(spacing: 6) {
            ForEach(ChartRange.allCases) { range in
                Button {
                    Haptic.light()
                    selectedRange = range
                } label: {
                    Text(range.rawValue)
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(range == selectedRange ? Color.cavnarEmber2 : Color.cavnarInk3)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(
                            Capsule().fill(range == selectedRange ? Color.cavnarEmber.opacity(0.16) : .clear)
                        )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.top, 6)
    }

    private var deltaInfo: (isPositive: Bool, text: String)? {
        guard hasRealTrend, let first = rangeHistory.first?.value else { return nil }
        let delta = totalValue - first
        let isPositive = delta >= 0
        let dollarText = Self.currencyText(abs(delta))
        if first != 0 {
            let pct = abs(Double(delta) / Double(first) * 100)
            return (isPositive, "\(dollarText) (\(String(format: "%.1f", pct))%) \(selectedRange.periodLabel)")
        }
        return (isPositive, "\(dollarText) \(selectedRange.periodLabel)")
    }

    private static func currencyText(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        let digits = formatter.string(from: NSNumber(value: value)) ?? "\(value)"
        return "$\(digits)"
    }

    private static let snapshotDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    private static func filter(_ history: [ValueSnapshot], to range: ChartRange) -> [ValueSnapshot] {
        guard let days = range.days else { return history }
        guard let cutoff = Calendar.current.date(byAdding: .day, value: -days, to: Date()) else { return history }
        return history.filter { snapshot in
            guard let date = snapshotDateFormatter.date(from: snapshot.date) else { return true }
            return date >= cutoff
        }
    }

    // A mostly-up, occasional-dip cumulative curve — the shape real usage
    // tends to produce — normalized 0-1 so SparklineCanvas can plot it the
    // same way it plots real dollar history. Purely illustrative: never
    // shown next to a real dollar figure implying it IS this restaurant's
    // data (see the "Example" caption above).
    private static let sampleTrend: [Double] = [
        0.12, 0.20, 0.17, 0.26, 0.31, 0.27, 0.35, 0.44, 0.39, 0.47,
        0.43, 0.53, 0.61, 0.56, 0.65, 0.60, 0.70, 0.78, 0.73, 0.85, 1.0,
    ]
}

/// 1M/3M/6M/1Y/ALL — filters the already-fetched value_history client-side
/// rather than a network call per tap, since the backend already returns up
/// to a year of daily snapshots in one response (see mobile_api.py's
/// _do_mobile_home).
enum ChartRange: String, CaseIterable, Identifiable {
    case oneMonth = "1M", threeMonths = "3M", sixMonths = "6M", oneYear = "1Y", all = "ALL"

    var id: String { rawValue }

    var days: Int? {
        switch self {
        case .oneMonth: return 30
        case .threeMonths: return 90
        case .sixMonths: return 180
        case .oneYear: return 365
        case .all: return nil
        }
    }

    var periodLabel: String {
        switch self {
        case .oneMonth: return "this month"
        case .threeMonths: return "over 3 months"
        case .sixMonths: return "over 6 months"
        case .oneYear: return "this year"
        case .all: return "all time"
        }
    }
}

/// Interpolates its own numeric value across an implicit animation and
/// re-formats it every intermediate frame — this is what makes the big
/// dollar figure visibly count up rather than just cross-fading between two
/// static strings.
private struct AnimatableNumberText: View, Animatable {
    var value: Double
    var format: (Int) -> String

    // nonisolated: SwiftUI drives animatableData from its own animation
    // machinery, which is not guaranteed to run on the main actor. The
    // property only reads/writes stored value types, so leaving it unisolated
    // is both correct and required for the conformance to be valid in the
    // Swift 6 language mode (audit 2.3).
    nonisolated var animatableData: Double {
        get { value }
        set { value = newValue }
    }

    var body: some View {
        Text(format(Int(value.rounded())))
    }
}

/// The sparkline itself — a progressive left-to-right line reveal on first
/// appearance (mirrors the mockup's requestAnimationFrame draw-in), then
/// settles into a single static Canvas draw so it isn't re-rendering every
/// frame forever afterward. Takes plain plot values rather than
/// ValueSnapshot directly, so the same drawing code serves both real
/// history and the synthetic sample curve.
private struct SparklineCanvas: View {
    let values: [Double]

    @State private var startDate = Date()
    @State private var isRevealed = false
    // Drives the endpoint dot/glow's breathing pulse — separate from the
    // Canvas entirely (see PulsingEndpointDot below) so the pulse can use
    // ordinary SwiftUI animation instead of forcing the line/fill drawing
    // back into a continuous per-frame TimelineView redraw forever, which
    // is exactly what isRevealed's static single Canvas draw above was
    // added to avoid.
    @State private var pulse = false

    // Matches the big total's own count-up duration (see ValueChartCard's
    // AnimatableNumberText.onAppear) — was 0.9s against the number's 1.6s,
    // so the line consistently finished drawing 0.7s before the number was
    // done counting up instead of both settling together.
    private static let revealDuration: Double = 1.6

    var body: some View {
        GeometryReader { geo in
            ZStack {
                if isRevealed {
                    // drawEndpointMarker: false — the pulsing overlay above
                    // owns the endpoint dot/glow once the reveal has settled.
                    Canvas { context, size in draw(context: context, size: size, progress: 1, drawEndpointMarker: false) }
                } else {
                    // Capped at 60fps, not the display's uncapped native
                    // rate (up to 120 on ProMotion) — this chart replays its
                    // reveal fresh every time the value-delivered sheet
                    // opens, right as the sheet's own presentation
                    // transition is animating; an uncapped TimelineView here
                    // was extra, invisible-at-60fps recompute competing with
                    // that transition for the same frame budget, which is
                    // part of what made opening the sheet feel laggy.
                    TimelineView(.animation(minimumInterval: 1.0 / 60.0)) { timeline in
                        Canvas { context, size in
                            let progress = min(1, timeline.date.timeIntervalSince(startDate) / Self.revealDuration)
                            draw(context: context, size: size, progress: progress)
                        }
                    }
                    .task {
                        try? await Task.sleep(for: .seconds(Self.revealDuration + 0.05))
                        isRevealed = true
                    }
                }

                if isRevealed, let endpoint = endpointPosition(in: geo.size) {
                    PulsingEndpointDot(pulse: pulse)
                        .position(endpoint)
                }
            }
        }
        .onAppear {
            // Waits for the line reveal to finish so the dot doesn't jump
            // straight into a mid-pulse frame the instant it appears.
            DispatchQueue.main.asyncAfter(deadline: .now() + Self.revealDuration) {
                withAnimation(.easeInOut(duration: 0.9).repeatForever(autoreverses: true)) {
                    pulse = true
                }
            }
        }
    }

    /// Same math draw() uses to place the endpoint glow/dot, pulled out so
    /// the SwiftUI overlay above and the Canvas's own coordinate space
    /// always agree on exactly where the last point lands.
    private func endpointPosition(in size: CGSize) -> CGPoint? {
        guard let minV = values.min(), let maxV = values.max(), let lastValue = values.last,
              values.count > 1 else { return nil }
        let range = max(maxV - minV, 1)
        let padX: CGFloat = 16
        let padY: CGFloat = 20
        let stepX = (size.width - padX * 2) / CGFloat(values.count - 1)
        let normalized = (lastValue - minV) / range
        let x = padX + CGFloat(values.count - 1) * stepX
        let y = padY + (1 - CGFloat(normalized)) * (size.height - padY * 2 - 14)
        return CGPoint(x: x, y: y)
    }

    private func draw(context: GraphicsContext, size: CGSize, progress: Double, drawEndpointMarker: Bool = true) {
        guard let minV = values.min(), let maxV = values.max() else { return }
        let range = max(maxV - minV, 1)
        // Horizontal pad tightened (20->16, the floor set by the endpoint
        // glow's own 16pt radius below — any less and the glow clips
        // against the canvas edge) so the plotted line/fill stretches
        // wider, closer to the chart's actual container edges, instead of
        // reading narrower/more condensed than the rest of the home page.
        let padX: CGFloat = 16
        let padY: CGFloat = 20
        let stepX = values.count > 1 ? (size.width - padX * 2) / CGFloat(values.count - 1) : 0

        let coords: [CGPoint] = values.enumerated().map { index, v in
            let normalized = (v - minV) / range
            let x = padX + CGFloat(index) * stepX
            let y = padY + (1 - CGFloat(normalized)) * (size.height - padY * 2 - 14)
            return CGPoint(x: x, y: y)
        }
        guard coords.count >= 2 else { return }

        // Reveal fractionally between points rather than jumping a whole
        // point at a time. With the 21-point sample curve that jump was
        // fine-grained enough to read as smooth; with a handful of real
        // history points (a restaurant a few days into tracking) the same
        // logic only has 3-4 discrete steps to work with, which reads as a
        // few sharp snaps instead of a continuous left-to-right sweep.
        let lastIndex = Double(coords.count - 1)
        let revealPosition = min(lastIndex, lastIndex * progress)
        let wholeIndex = Int(revealPosition)
        let fraction = revealPosition - Double(wholeIndex)

        var visible = Array(coords.prefix(wholeIndex + 1))
        if wholeIndex + 1 < coords.count, fraction > 0 {
            let a = coords[wholeIndex]
            let b = coords[wholeIndex + 1]
            visible.append(CGPoint(x: a.x + (b.x - a.x) * CGFloat(fraction),
                                    y: a.y + (b.y - a.y) * CGFloat(fraction)))
        }
        guard visible.count >= 2, let last = visible.last, let first = visible.first else { return }

        var fillPath = Path()
        fillPath.move(to: CGPoint(x: first.x, y: size.height))
        visible.forEach { fillPath.addLine(to: $0) }
        fillPath.addLine(to: CGPoint(x: last.x, y: size.height))
        fillPath.closeSubpath()
        context.fill(
            fillPath,
            with: .linearGradient(
                // Bumped up from 0.28 — the wash under the line was barely
                // visible against the page background at that opacity.
                Gradient(colors: [Color.cavnarEmber.opacity(0.5), Color.cavnarEmber.opacity(0.02)]),
                startPoint: CGPoint(x: 0, y: 0),
                endPoint: CGPoint(x: 0, y: size.height)
            )
        )

        var linePath = Path()
        linePath.move(to: first)
        visible.dropFirst().forEach { linePath.addLine(to: $0) }
        context.stroke(linePath, with: .color(Color.cavnarEmber2), lineWidth: 2)

        // During the initial line reveal this still travels with the
        // leading edge frame-by-frame (progress < 1, isRevealed still
        // false) — only the settled static draw skips it in favor of
        // PulsingEndpointDot below, which takes over from there.
        if drawEndpointMarker {
            let glowRect = CGRect(x: last.x - 16, y: last.y - 16, width: 32, height: 32)
            context.fill(
                Path(ellipseIn: glowRect),
                with: .radialGradient(
                    Gradient(colors: [Color.cavnarEmber2.opacity(0.9), Color.cavnarEmber2.opacity(0)]),
                    center: last, startRadius: 0, endRadius: 16
                )
            )
            let dotRect = CGRect(x: last.x - 3, y: last.y - 3, width: 6, height: 6)
            context.fill(Path(ellipseIn: dotRect), with: .color(Color.cavnarInk))
        }
    }
}

/// Takes over the endpoint marker once the sparkline's reveal has settled
/// into its static Canvas draw — a real SwiftUI view (not more Canvas
/// drawing) specifically so it can breathe on an ordinary repeatForever
/// animation without forcing the whole line/fill back into a continuous
/// per-frame redraw. Mirrors the Canvas version's exact sizing (32pt glow,
/// 6pt dot) at rest so the handoff between the two is seamless.
private struct PulsingEndpointDot: View {
    let pulse: Bool

    var body: some View {
        ZStack {
            Circle()
                .fill(
                    RadialGradient(
                        colors: [Color.cavnarEmber2.opacity(pulse ? 1.0 : 0.9), Color.cavnarEmber2.opacity(0)],
                        center: .center, startRadius: 0, endRadius: pulse ? 20 : 16
                    )
                )
                .frame(width: pulse ? 40 : 32, height: pulse ? 40 : 32)
            Circle()
                .fill(Color.cavnarInk)
                .frame(width: 6, height: 6)
                .scaleEffect(pulse ? 1.3 : 1.0)
        }
    }
}
