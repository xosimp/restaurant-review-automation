import SwiftUI

/// The ember thread — Cavnar AI's signature motif, on the phone.
///
/// A lit line from evidence to conclusion, with one point of light
/// travelling along it. It appears wherever an insight rests on data:
/// Home's module pills to the finding, a chart to the read beneath it, an
/// answer's sources to the answer. It is drawn only where a real link
/// exists in the payload — never as decoration — which is what lets it
/// mean something when it appears. Same shape as the web's `.ember-thread`.
struct EmberThread: View {
    enum Axis { case vertical, horizontal }
    var axis: Axis = .vertical
    var length: CGFloat = 24
    /// A ring at the far end when the conclusion is newly updated (a real
    /// `generated_at` inside the last few minutes).
    var fresh: Bool = false
    var paused: Bool = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        let animate = !(paused || reduceMotion)
        TimelineView(.animation(minimumInterval: 1 / 30, paused: !animate)) { context in
            let t = animate ? (context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 2.4) / 2.4) : 0.5
            Canvas { ctx, size in
                let horizontal = axis == .horizontal
                let start = CGPoint(x: horizontal ? 0 : size.width / 2, y: horizontal ? size.height / 2 : 0)
                let end = CGPoint(x: horizontal ? size.width : size.width / 2, y: horizontal ? size.height / 2 : size.height)
                var path = Path()
                path.move(to: start); path.addLine(to: end)
                let gradient = Gradient(colors: [Color.cavnarEmber2, Color.cavnarEmber, Color.cavnarEmber2])
                ctx.stroke(path, with: .linearGradient(gradient, startPoint: start, endPoint: end),
                           style: StrokeStyle(lineWidth: 2, lineCap: .round))
                // the travelling light
                let p = CGPoint(x: start.x + (end.x - start.x) * t, y: start.y + (end.y - start.y) * t)
                let fade = t < 0.12 ? t / 0.12 : (t > 0.88 ? (1 - t) / 0.12 : 1)
                let glow = Path(ellipseIn: CGRect(x: p.x - 7, y: p.y - 7, width: 14, height: 14))
                ctx.fill(glow, with: .color(Color.cavnarEmber.opacity(0.28 * fade)))
                let dot = Path(ellipseIn: CGRect(x: p.x - 4, y: p.y - 4, width: 8, height: 8))
                ctx.fill(dot, with: .color(Color.cavnarEmber.opacity(fade)))
                if fresh {
                    let ring = 0.6 + 1.2 * t
                    let r = 7 * ring
                    let ringPath = Path(ellipseIn: CGRect(x: end.x - r, y: end.y - r, width: 2 * r, height: 2 * r))
                    ctx.stroke(ringPath, with: .color(Color.cavnarEmber.opacity(0.8 * (1 - t))), lineWidth: 2)
                }
            }
        }
        .frame(width: axis == .horizontal ? length : 16, height: axis == .horizontal ? 16 : length)
        .accessibilityHidden(true)
    }
}
