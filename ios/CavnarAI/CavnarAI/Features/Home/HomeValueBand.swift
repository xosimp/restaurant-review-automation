import SwiftUI

/// "Value delivered since you joined" as a full-bleed band: the count-up
/// figure on top, this month's gain under it, and the value history drawn
/// as the band's own backdrop — a green line that reveals left to right
/// while the number climbs. The 1M/3M/6M chart moved to a tap-through
/// (the chevron); Home shouldn't ask you to operate a chart.
struct HomeValueBand: View {
    let total: Int
    let history: [ValueSnapshot]
    let activeModuleKeys: [String]
    let onOpen: () -> Void

    @State private var animatedTotal: Double = 0
    @State private var hasCountedUp = false

    private var hasRealTrend: Bool {
        guard history.count >= 2 else { return false }
        let values = history.map(\.value)
        return (values.max() ?? 0) != (values.min() ?? 0)
    }

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            ValueBandSparkline(values: hasRealTrend ? history.map { Double($0.value) } : Self.sampleTrend)
                .opacity(hasRealTrend ? 1 : 0.35)

            VStack(alignment: .leading, spacing: 8) {
                Text("VALUE DELIVERED SINCE YOU JOINED")
                    .font(.cavnarBody(11.5, weight: 700))
                    .tracking(1.6)
                    .foregroundStyle(Color.cavnarEmber2)

                HStack(alignment: .firstTextBaseline, spacing: 0) {
                    Text("$")
                    CavnarAnimatableNumber(value: animatedTotal, format: { Self.digits(Int($0.rounded())) })
                }
                .font(.cavnarNumber(46, weight: 600))
                .foregroundStyle(Color.cavnarGreen)
                .cavnarNumberGlow(.cavnarGreen)
                .cavnarSensitive()
                .onAppear {
                    guard !hasCountedUp else { return }
                    hasCountedUp = true
                    withAnimation(.easeOut(duration: 1.6)) { animatedTotal = Double(total) }
                }
                .onChange(of: total) { _, newValue in
                    guard hasCountedUp else { return }
                    animatedTotal = Double(newValue)
                }

                deltaLine
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.trailing, 44)
            }
            .padding(.horizontal, 20)
            .padding(.top, 22)
            .padding(.bottom, 18)
            .frame(maxWidth: .infinity, alignment: .leading)

            Button(action: onOpen) {
                Image(systemName: "chevron.right")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber)
                    .frame(width: 30, height: 30)
                    .background(Circle().fill(Color.cavnarEmber.opacity(0.16)))
            }
            .buttonStyle(.plain)
            .padding(.trailing, 20)
            .padding(.bottom, 18)
            .accessibilityLabel("Value delivered over time")
        }
        .background(
            LinearGradient(colors: [Color.cavnarEmber.opacity(0.08), Color.cavnarPaper.opacity(0)],
                           startPoint: .top, endPoint: .bottom)
        )
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarEmber2.opacity(0.35)).frame(height: 1) }
        .overlay(alignment: .bottom) { Rectangle().fill(Color.white.opacity(0.05)).frame(height: 1) }
        .accessibilityElement(children: .contain)
    }

    // "+$1,240 this month · reviews answered, labor trimmed, waste caught"
    // — or, before there's a month of history to compare against, an honest
    // "tracking starts today" rather than a fabricated gain.
    @ViewBuilder
    private var deltaLine: some View {
        if let delta = monthDelta {
            (Text("+$")
                .font(.cavnarNumber(13, weight: 700))
             + Text(Self.digits(delta))
                .font(.cavnarNumber(13, weight: 700))
             + Text(" this month")
                .font(.cavnarBody(13, weight: 700)))
                .foregroundStyle(Color.cavnarGreen)
            + Text(" · " + contributions)
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
        } else {
            Text("Tracking starts today · grows as Cavnar AI answers, trims, and catches things")
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
        }
    }

    private var contributions: String {
        var parts: [String] = []
        if activeModuleKeys.contains("reviews") { parts.append("reviews answered") }
        if activeModuleKeys.contains("labor") { parts.append("labor trimmed") }
        if activeModuleKeys.contains("inventory") { parts.append("waste caught") }
        if activeModuleKeys.contains("marketing") { parts.append("posts drafted") }
        return parts.isEmpty ? "everything Cavnar AI runs" : parts.joined(separator: ", ")
    }

    /// Gain since the first snapshot of this calendar month, or nil when
    /// there isn't one yet (or it hasn't moved).
    private var monthDelta: Int? {
        guard hasRealTrend else { return nil }
        let calendar = Calendar.current
        guard let monthStart = calendar.date(from: calendar.dateComponents([.year, .month], from: Date())) else { return nil }
        let base = history.first { snapshot in
            guard let date = Self.snapshotDateFormatter.date(from: snapshot.date) else { return false }
            return date >= monthStart
        }
        guard let base else { return nil }
        let delta = total - base.value
        return delta > 0 ? delta : nil
    }

    private static let snapshotDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd"
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter
    }()

    private static func digits(_ value: Int) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        formatter.groupingSeparator = ","
        return formatter.string(from: NSNumber(value: value)) ?? "\(value)"
    }

    // The same illustrative curve ValueChartCard falls back to before a
    // restaurant has a real trend — drawn faint here, never next to a
    // fabricated gain (see monthDelta).
    private static let sampleTrend: [Double] = [
        0.12, 0.20, 0.17, 0.26, 0.31, 0.27, 0.35, 0.44, 0.39, 0.47,
        0.43, 0.53, 0.61, 0.56, 0.65, 0.60, 0.70, 0.78, 0.73, 0.85, 1.0,
    ]
}

/// The band's backdrop: a smooth green line with a soft fill, revealed
/// left to right over the same 1.6s the figure counts up, then a hot dot
/// on the endpoint. Redraws only during the reveal — the TimelineView
/// pauses itself once the line has landed.
private struct ValueBandSparkline: View {
    let values: [Double]

    @State private var start = Date()
    @State private var done = false

    private static let revealDuration: Double = 1.6

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: done)) { timeline in
            Canvas { context, size in
                let raw = min(1, timeline.date.timeIntervalSince(start) / Self.revealDuration)
                let progress = 1 - pow(1 - raw, 3)
                draw(&context, size: size, progress: progress)
            }
        }
        .onAppear { start = Date() }
        .task {
            try? await Task.sleep(for: .seconds(Self.revealDuration + 0.1))
            done = true
        }
        .allowsHitTesting(false)
    }

    private func draw(_ context: inout GraphicsContext, size: CGSize, progress: Double) {
        guard values.count >= 2, let lo = values.min(), let hi = values.max() else { return }
        let range = max(hi - lo, 0.0001)
        let top: CGFloat = 26
        let bottom = size.height - 4
        let points: [CGPoint] = values.enumerated().map { i, v in
            CGPoint(x: size.width * CGFloat(i) / CGFloat(values.count - 1),
                    y: bottom - (bottom - top) * CGFloat((v - lo) / range))
        }
        let line = CavnarChart.smoothPath(points)
        context.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: 0, y: 0, width: size.width * progress + 6, height: size.height)))
            var fill = line
            fill.addLine(to: CGPoint(x: size.width, y: bottom))
            fill.addLine(to: CGPoint(x: 0, y: bottom))
            fill.closeSubpath()
            layer.fill(
                fill,
                with: .linearGradient(
                    Gradient(colors: [Color.cavnarGreen.opacity(0.22), Color.cavnarGreen.opacity(0)]),
                    startPoint: CGPoint(x: 0, y: top), endPoint: CGPoint(x: 0, y: bottom)
                )
            )
            CavnarChart.glowStroke(&layer, line, color: Color.cavnarGreen.opacity(0.9), glow: .cavnarGreen, lineWidth: 2, blur: 8)
        }
        if progress >= 0.999, let last = points.last {
            CavnarChart.hotDot(&context, at: last, radius: 4, halo: 11, color: .cavnarEmber2, glow: .cavnarGreen, haloOpacity: 0.35)
        }
    }
}
