import SwiftUI

// The shared drawing kit for the eight analytics charts (Sentiment River,
// Topic Heat Grid, Response Rings, Labor Ribbon, Week Radar, Waste Ledger,
// Recoverable Gauge, Visibility Orbit). Every chart is a single Canvas
// driven by one clock: `t` runs 0→1 once over the chart's own duration for
// its entrance, and `clock` keeps running for the ambient parts (a
// breathing endpoint, an orbiting dot). Reduce Motion pins `t` to 1 and
// freezes `clock`. Same rules as CavnarMotion: the ember is the only colour
// that moves, ease-in-out only, no bounce.

/// One-shot entrance progress plus a continuous clock, drawn into a Canvas.
struct CavnarAnimatedCanvas<Overlay: View>: View {
    var duration: Double = 1.6
    var height: CGFloat = 250
    /// Runs the entrance again (e.g. when the data changes).
    var replayKey: AnyHashable = 0
    let draw: (inout GraphicsContext, CGSize, Double, Double) -> Void
    @ViewBuilder var overlay: () -> Overlay

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var start: Date?

    init(duration: Double = 1.6, height: CGFloat = 250, replayKey: AnyHashable = 0,
         draw: @escaping (inout GraphicsContext, CGSize, Double, Double) -> Void,
         @ViewBuilder overlay: @escaping () -> Overlay = { EmptyView() }) {
        self.duration = duration
        self.height = height
        self.replayKey = replayKey
        self.draw = draw
        self.overlay = overlay
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 60.0, paused: reduceMotion)) { timeline in
            Canvas { context, size in
                let origin = start ?? timeline.date
                let elapsed = timeline.date.timeIntervalSince(origin)
                let t = reduceMotion ? 1 : min(1, max(0, elapsed / duration))
                draw(&context, size, t, reduceMotion ? 0 : elapsed)
            }
        }
        .frame(height: height)
        .overlay { overlay() }
        .background(CavnarChartStage())
        .onAppear { if start == nil { start = Date() } }
        .onChange(of: replayKey) { _, _ in start = Date() }
    }
}

/// The stage every chart sits on — the render's `.stage`: a 16pt rounded
/// panel with a faint top-lit gradient and hairline. Deliberately quieter
/// than a card, so the chart is the object, not its box.
struct CavnarChartStage: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 16, style: .continuous)
            .fill(
                LinearGradient(colors: [Color.white.opacity(0.03), Color.white.opacity(0)], startPoint: .top, endPoint: .bottom)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(Color.white.opacity(0.05), lineWidth: 1)
            )
    }
}

/// Kicker + title above a chart — the render's card header, unboxed to match
/// how every analytics section already labels itself.
struct CavnarChartHeader: View {
    let kicker: String
    let title: String
    var detail: String? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(kicker.uppercased())
                .font(.cavnarBody(12, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            Text(title)
                .font(.cavnarHeadline(20))
                .foregroundStyle(Color.cavnarInk)
            if let detail {
                Text(detail)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - Easing, geometry and paint helpers

enum CavnarChart {
    static func easeInOut(_ t: Double) -> Double { t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t }
    static func easeOut(_ t: Double) -> Double { 1 - pow(1 - t, 3) }
    /// Progress of a sub-animation that starts at `from` and lasts `length`
    /// within the chart's overall 0…1.
    static func window(_ t: Double, from: Double, length: Double) -> Double {
        max(0, min(1, (t - from) / length))
    }

    /// A smooth curve through the points — cubic segments with horizontal
    /// control handles at the midpoint, the same construction the renders use.
    static func smoothPath(_ points: [CGPoint]) -> Path {
        var p = Path()
        guard let first = points.first else { return p }
        p.move(to: first)
        for i in 1..<points.count {
            let prev = points[i - 1], cur = points[i]
            let mid = prev.x + (cur.x - prev.x) / 2
            p.addCurve(to: cur, control1: CGPoint(x: mid, y: prev.y), control2: CGPoint(x: mid, y: cur.y))
        }
        return p
    }

    /// Same curve traced back from the last point to the first — for closing
    /// a band between two curves.
    static func appendReversedSmooth(_ p: inout Path, _ points: [CGPoint]) {
        guard points.count > 1 else { return }
        for i in stride(from: points.count - 2, through: 0, by: -1) {
            let next = points[i + 1], cur = points[i]
            let mid = next.x - (next.x - cur.x) / 2
            p.addCurve(to: cur, control1: CGPoint(x: mid, y: next.y), control2: CGPoint(x: mid, y: cur.y))
        }
    }

    static func roundedRect(_ rect: CGRect, radius: CGFloat) -> Path {
        Path(roundedRect: rect, cornerRadius: min(radius, rect.width / 2, rect.height / 2), style: .continuous)
    }

    /// Stroke with an ember glow underneath — `Canvas` has no shadow on a
    /// stroke, so the glow is the same path drawn in a blurred layer first.
    static func glowStroke(_ ctx: inout GraphicsContext, _ path: Path, color: Color, glow: Color, lineWidth: CGFloat, blur: CGFloat = 8) {
        ctx.drawLayer { layer in
            layer.addFilter(.blur(radius: blur))
            layer.stroke(path, with: .color(glow.opacity(0.85)), style: StrokeStyle(lineWidth: lineWidth + 2, lineCap: .round, lineJoin: .round))
        }
        ctx.stroke(path, with: .color(color), style: StrokeStyle(lineWidth: lineWidth, lineCap: .round, lineJoin: .round))
    }

    static func glowStroke(_ ctx: inout GraphicsContext, _ path: Path, shading: GraphicsContext.Shading, glow: Color, lineWidth: CGFloat, blur: CGFloat = 8) {
        ctx.drawLayer { layer in
            layer.addFilter(.blur(radius: blur))
            layer.stroke(path, with: .color(glow.opacity(0.85)), style: StrokeStyle(lineWidth: lineWidth + 2, lineCap: .round, lineJoin: .round))
        }
        ctx.stroke(path, with: shading, style: StrokeStyle(lineWidth: lineWidth, lineCap: .round, lineJoin: .round))
    }

    static func glowFill(_ ctx: inout GraphicsContext, _ path: Path, color: Color, glow: Color, blur: CGFloat = 8) {
        ctx.drawLayer { layer in
            layer.addFilter(.blur(radius: blur))
            layer.fill(path, with: .color(glow.opacity(0.8)))
        }
        ctx.fill(path, with: .color(color))
    }

    /// A glowing dot — the breathing endpoint on a line, the tip of a gauge.
    static func hotDot(_ ctx: inout GraphicsContext, at c: CGPoint, radius: CGFloat, halo: CGFloat, color: Color = cavnarEmberHot, glow: Color = .cavnarEmber, haloOpacity: Double = 0.28) {
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - halo, y: c.y - halo, width: halo * 2, height: halo * 2)), with: .color(glow.opacity(haloOpacity)))
        ctx.fill(Path(ellipseIn: CGRect(x: c.x - radius, y: c.y - radius, width: radius * 2, height: radius * 2)), with: .color(color))
    }

    static func text(_ ctx: inout GraphicsContext, _ text: Text, at point: CGPoint, anchor: UnitPoint = .center) {
        ctx.draw(ctx.resolve(text), at: point, anchor: anchor)
    }

    static func number(_ value: String, size: CGFloat, weight: CGFloat = 600, color: Color = .cavnarInk) -> Text {
        Text(value).font(.cavnarNumber(size, weight: weight)).foregroundStyle(color)
    }

    static func label(_ value: String, size: CGFloat, weight: CGFloat = 600, color: Color = .cavnarInk3) -> Text {
        Text(value).font(.cavnarBody(size, weight: weight)).foregroundStyle(color)
    }

    static func kicker(_ value: String, color: Color = .cavnarEmber2) -> Text {
        Text(value.uppercased()).font(.cavnarBody(9.5, weight: 700)).tracking(1.2).foregroundStyle(color)
    }

    /// Faint horizontal grid — three lines across the plot.
    static func grid(_ ctx: inout GraphicsContext, plot: CGRect, lines: Int = 3) {
        for g in 0...lines {
            let y = plot.minY + plot.height * CGFloat(g) / CGFloat(lines)
            var p = Path(); p.move(to: CGPoint(x: plot.minX, y: y)); p.addLine(to: CGPoint(x: plot.maxX, y: y))
            ctx.stroke(p, with: .color(Color.white.opacity(0.05)), lineWidth: 1)
        }
    }
}
