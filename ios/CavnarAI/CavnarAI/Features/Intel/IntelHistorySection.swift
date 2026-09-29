import SwiftUI

/// The market's history and the restaurant's own rating over time, kept
/// forever (memory round, 9/29/26: event_memory — GET /mobile/api/intel/
/// movement's `market_history` and `own_rating_history`). The weekly
/// snapshots behind "What changed" are pruned at a year; these are the
/// record: "Bella's opened nearby on 3/14/26", "your rating 4.3★ → 4.6★
/// since the week of 3/2/26".
struct IntelHistorySection: View {
    let movement: IntelMovement
    @State private var showingAll = false

    var body: some View {
        let own = movement.ownRatingHistory
        let events = movement.marketHistory
        if own?.available == true || !events.isEmpty || own?.reason != nil {
            VStack(alignment: .leading, spacing: 10) {
                Text("OVER TIME")
                    .font(.cavnarBody(13, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber)
                    .padding(.top, 10)
                if let own { ownRating(own) }
                if !events.isEmpty { marketList(events) }
            }
        }
    }

    // MARK: Your rating

    @ViewBuilder
    private func ownRating(_ own: OwnRatingHistory) -> some View {
        if own.available, let line = own.line {
            VStack(alignment: .leading, spacing: 6) {
                HomeMixedText.make("Your Google rating: " + line, size: 14.5, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                if own.series.count >= 3 {
                    OwnRatingTrace(points: own.series)
                }
                HomeMixedText.make("One reading a week, \(own.weeks ?? own.series.count) weeks on file \u{2014} measured, kept forever.",
                                   size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else if let reason = own.reason {
            Text("Your rating over time: " + reason + ".")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    // MARK: The market's history

    private func marketList(_ events: [MarketEvent]) -> some View {
        let shown = showingAll ? events : Array(events.prefix(5))
        return VStack(alignment: .leading, spacing: 0) {
            Text("What the market did")
                .font(.cavnarBody(14.5, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .padding(.top, 4)
                .padding(.bottom, 2)
            ForEach(shown) { e in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Circle()
                        // A competitor arriving or climbing is one to watch;
                        // ember is never a status (DESIGN_SYSTEM §9).
                        .fill(e.kind == "arrived" || e.isRise ? Color.cavnarAmber : Color.cavnarInk3)
                        .frame(width: 6, height: 6)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(e.name)
                            .font(.cavnarBody(15, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(e.what, size: 13, color: .cavnarInk2)
                    }
                    Spacer(minLength: 4)
                    if let d = e.dateLabel {
                        Text(d).font(.cavnarNumber(12.5, weight: 600)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                .padding(.vertical, 6)
            }
            if events.count > 5 {
                Button {
                    Haptic.selection()
                    withAnimation(.easeOut(duration: 0.2)) { showingAll.toggle() }
                } label: {
                    Text(showingAll ? "Show fewer" : "Show all \(events.count)")
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
                .padding(.top, 4)
            }
        }
    }
}

/// The owner's weekly rating as one ember line, traced in once (motion 08,
/// the sparkline trace), the latest week lit. Labels name values the line
/// reaches: its first and latest readings.
private struct OwnRatingTrace: View {
    let points: [OwnRatingHistory.Point]

    var body: some View {
        CavnarAnimatedCanvas(duration: 1.6, height: 92, replayKey: points.map(\.week).joined()) { ctx, size, t, _ in
            draw(&ctx, size: size, t: t)
        }
        .accessibilityElement()
        .accessibilityLabel("Rating by week, from \(String(format: "%.1f", points.first?.rating ?? 0)) to \(String(format: "%.1f", points.last?.rating ?? 0)) stars")
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let values = points.map(\.rating)
        guard values.count > 1 else { return }
        let plot = CGRect(x: 34, y: 10, width: size.width - 46, height: size.height - 24)
        let lo = (values.min() ?? 4) - 0.1, hi = (values.max() ?? 5) + 0.1
        let span = max(hi - lo, 0.2)
        let n = values.count
        func x(_ i: Int) -> CGFloat { plot.minX + plot.width * CGFloat(i) / CGFloat(n - 1) }
        func y(_ v: Double) -> CGFloat { plot.maxY - CGFloat((v - lo) / span) * plot.height }
        let pts = (0..<n).map { CGPoint(x: x($0), y: y(values[$0])) }
        let reveal = CavnarChart.easeOut(min(1, t))
        let line = CavnarChart.smoothPath(pts)
        ctx.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: plot.minX - 4, y: 0, width: plot.width * reveal + 8, height: size.height)))
            CavnarChart.glowStroke(&layer, line,
                                   shading: .linearGradient(Gradient(colors: [.cavnarEmber2, cavnarEmberHot]),
                                                            startPoint: CGPoint(x: plot.minX, y: 0),
                                                            endPoint: CGPoint(x: plot.maxX, y: 0)),
                                   glow: .cavnarEmber, lineWidth: 2.2, blur: 6)
            var fill = line
            fill.addLine(to: CGPoint(x: plot.maxX, y: plot.maxY))
            fill.addLine(to: CGPoint(x: plot.minX, y: plot.maxY))
            fill.closeSubpath()
            layer.fill(fill, with: .linearGradient(Gradient(colors: [Color.cavnarEmber.opacity(0.22), Color.cavnarEmber.opacity(0)]),
                                                   startPoint: CGPoint(x: 0, y: plot.minY),
                                                   endPoint: CGPoint(x: 0, y: plot.maxY)))
        }
        if reveal >= 0.98, let last = pts.last {
            CavnarChart.hotDot(&ctx, at: last, radius: 3.5, halo: 9)
        }
        CavnarChart.text(&ctx, CavnarChart.number(String(format: "%.1f", values.first ?? 0), size: 10.5,
                                                  color: .cavnarInk3),
                         at: CGPoint(x: plot.minX - 6, y: pts.first?.y ?? plot.midY), anchor: .trailing)
        CavnarChart.text(&ctx, CavnarChart.label(points.first?.weekLabel ?? "", size: 10),
                         at: CGPoint(x: plot.minX, y: size.height - 6), anchor: .leading)
        CavnarChart.text(&ctx, CavnarChart.label("now", size: 10),
                         at: CGPoint(x: plot.maxX, y: size.height - 6), anchor: .trailing)
    }
}
