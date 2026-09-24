import SwiftUI

/// Recoverable Gauge — the waste above tolerance, projected to a month from
/// one week's count, as a half-arc; the year beneath once the arc lands. An
/// OPPORTUNITY, labelled one: projected, with its basis, never "this month"
/// as if measured, never "claw back" (a certainty), never "overstock"
/// (which is not in the figure) — NS1 #6.
struct RecoverableGaugeChart: View {
    let monthly: Double
    let annual: Double
    /// What "full" means — the month's projected waste, so the arc reads
    /// as "this much of what's being wasted is above the normal trim".
    let ceiling: Double
    /// The server's basis for the figure (`annual_recoverable_basis`),
    /// shown under the gauge; the model's default when absent.
    var basis: String = "Projected from one week\u{2019}s count \u{2014} what is still being lost, not money saved"
    /// `recoverable_kind` — "opportunity" on today's server.
    var kind: String? = nil

    private var fraction: Double { ceiling > 0 ? min(1, monthly / ceiling) : 0 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Recoverable \u{00B7} \(kind ?? "opportunity")", title: "Recoverable Gauge",
                              detail: "Waste above the normal trim for each category, projected to a month \u{2014} available with better ordering, not captured yet.")
            CavnarAnimatedCanvas(duration: 2.8, height: 230, replayKey: "\(Int(monthly))-\(Int(ceiling))") { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
            HomeMixedText.make(basis, size: 12.5, color: .cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let center = CGPoint(x: size.width / 2, y: size.height * 0.72)
        let R = min(size.width * 0.34, size.height * 0.6)
        let s = CavnarChart.easeInOut(min(1, t / 1.0))
        var track = Path()
        track.addArc(center: center, radius: R, startAngle: .degrees(180), endAngle: .degrees(360), clockwise: false)
        ctx.stroke(track, with: .color(Color.white.opacity(0.06)), style: StrokeStyle(lineWidth: 18, lineCap: .round))
        let end = Angle.degrees(180 + 180 * fraction * s)
        var arc = Path()
        arc.addArc(center: center, radius: R, startAngle: .degrees(180), endAngle: end, clockwise: false)
        if fraction * s > 0.002 {
            CavnarChart.glowStroke(&ctx, arc,
                                   shading: .linearGradient(Gradient(colors: [.cavnarEmber, cavnarEmberHot]),
                                                            startPoint: CGPoint(x: center.x - R, y: 0), endPoint: CGPoint(x: center.x + R, y: 0)),
                                   glow: .cavnarEmber, lineWidth: 18, blur: 12)
            let tip = CGPoint(x: center.x + CGFloat(cos(end.radians)) * R, y: center.y + CGFloat(sin(end.radians)) * R)
            ctx.drawLayer { layer in
                layer.addFilter(.shadow(color: cavnarEmberHot.opacity(0.9), radius: 10))
                layer.fill(Path(ellipseIn: CGRect(x: tip.x - 7, y: tip.y - 7, width: 14, height: 14)), with: .color(cavnarEmberHot))
            }
        }
        // The readout sits in the middle of the bowl (the open area between
        // the top of the arc and its diameter), not crowded down by the
        // baseline: at `center.y - 16` the big number was practically
        // resting on the caption line under it while the
        // whole upper half of the gauge sat empty. Anchoring off R keeps it
        // centered at any canvas width, since R is what actually sets how
        // tall the open area is.
        let bowlMid = center.y - R * 0.55
        // Drawn flat. This used to sit inside a drawLayer with a 14pt ember
        // shadow filter behind it, which at this size read as the number
        // itself being blurry rather than as a glow. The arc already
        // carries the gauge's glow; the figure just needs to be crisp.
        CavnarChart.text(&ctx, CavnarChart.number("$\(Int((monthly * s).rounded()).formatted())", size: 34, weight: 700),
                         at: CGPoint(x: center.x, y: bowlMid))
        // +40 / +64, not +28 / +50. The big figure keeps its position; the
        // caption drops away from it. At 28pt below a 34pt number the two
        // were nearly touching — the number's own descenders ran into the
        // caption's cap height — and the gauge has plenty of room here.
        CavnarChart.text(&ctx, CavnarChart.kicker("Projected / mo \u{00B7} opportunity"), at: CGPoint(x: center.x, y: bowlMid + 40))
        let at = CavnarChart.window(t, from: 0.85, length: 0.15)
        ctx.drawLayer { layer in
            layer.opacity = at
            CavnarChart.text(&layer, CavnarChart.number("$\(Int(annual.rounded()).formatted()) / year projected", size: 13),
                             at: CGPoint(x: center.x, y: bowlMid + 64))
        }
        CavnarChart.text(&ctx, CavnarChart.label("$0", size: 10), at: CGPoint(x: center.x - R - 6, y: center.y + 22), anchor: .leading)
        CavnarChart.text(&ctx, CavnarChart.label("$\(Int(ceiling.rounded()).formatted())", size: 10), at: CGPoint(x: center.x + R + 6, y: center.y + 22), anchor: .trailing)
    }
}
