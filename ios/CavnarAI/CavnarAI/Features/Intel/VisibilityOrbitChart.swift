import SwiftUI

/// Visibility Orbit — today's AI-visibility score as a ring with an
/// orbiting hot dot at its tip, and the history of every run as a thin
/// line to the right, each run landing as a dot. The drop alert, made
/// visible before it fires.
struct VisibilityOrbitChart: View {
    /// Nil when there is no measurement — drawn as an empty ring with "—"
    /// and "not measured", never as a 0% ring (J10, CA1 I1).
    let score: Int?
    let runs: [AIVisibilityRun]
    /// The score's honest bounds (`ai_score_low` / `ai_score_high`) and the
    /// server's band in words (`ai_score_label`): VoiceOver says the range
    /// and the band the card shows, never the point alone (re-audit
    /// 10/8/26 — a handful of questions is a range, not a figure).
    var low: Int? = nil
    var high: Int? = nil
    var band: String? = nil
    /// The score in the ring's centre. Off where the card already states the
    /// score beside the ring (AI visibility's hero, re-audit I8): the ring
    /// then carries only its "AI visibility" label.
    var showsCenterFigure: Bool = true
    /// An estimate, not a measurement: the ring in Ink2 at full strength,
    /// not the ember glow — the chart used to be dimmed to 60% instead.
    var muted: Bool = false

    private var delta: Int? {
        guard runs.count >= 2 else { return nil }
        return runs[runs.count - 1].aiScore - runs[runs.count - 2].aiScore
    }

    /// What the ring's centre reads: the figure, or a dash when nothing
    /// was measured.
    static func centerText(_ score: Int?, progress: Double = 1) -> String {
        guard let score else { return "\u{2014}" }
        return "\(Int((Double(score) * progress).rounded()))"
    }

    var body: some View {
        CavnarAnimatedCanvas(duration: 1.6, height: 150, replayKey: "\(score.map(String.init) ?? "none")-\(runs.count)-\(muted)", ambient: true) { ctx, size, t, clock in
            draw(&ctx, size: size, t: t, clock: clock)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("AI visibility score")
        .accessibilityValue(Self.spokenSummary(score: score, runs: runs, low: low, high: high, band: band))
    }

    /// The ring and the trend in a sentence. No measurement is said as
    /// "not measured", never as zero; a score with bounds is said as its
    /// range and band, as the card shows it.
    static func spokenSummary(score: Int?, runs: [AIVisibilityRun], low: Int? = nil, high: Int? = nil,
                              band: String? = nil) -> String {
        guard let score else { return "Not measured." }
        var parts: [String] = []
        if let lo = low, let hi = high, hi > lo {
            parts.append("Somewhere between \(lo) and \(hi) out of 100")
        } else {
            parts.append("\(score) out of 100")
        }
        if let b = band?.trimmingCharacters(in: .whitespacesAndNewlines), !b.isEmpty {
            parts.append(b.prefix(1).uppercased() + b.dropFirst())
        }
        if runs.count >= 2 {
            let vals = runs.map(\.aiScore)
            let delta = vals[vals.count - 1] - vals[vals.count - 2]
            parts.append(delta == 0 ? "No change since last run" : (delta > 0 ? "Up \(delta) since last run" : "Down \(-delta) since last run"))
            parts.append("Last \(runs.count) runs ranged \(vals.min() ?? 0) to \(vals.max() ?? 0)")
        } else {
            parts.append("First check, no trend yet")
        }
        return parts.joined(separator: ". ") + "."
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double, clock: Double) {
        let center = CGPoint(x: size.width * 0.26, y: size.height / 2)
        let R: CGFloat = 46
        let s = CavnarChart.easeInOut(min(1, t / 0.9))
        ctx.stroke(Path(ellipseIn: CGRect(x: center.x - R, y: center.y - R, width: R * 2, height: R * 2)), with: .color(Color.white.opacity(0.06)), lineWidth: 10)
        if let score {
            let sweep = 360 * Double(score) / 100 * s
            var arc = Path()
            arc.addArc(center: center, radius: R, startAngle: .degrees(-90), endAngle: .degrees(-90 + sweep), clockwise: false)
            if sweep > 0.5 {
                if muted {
                    ctx.stroke(arc, with: .color(.cavnarInk2), style: StrokeStyle(lineWidth: 10, lineCap: .round))
                } else {
                    CavnarChart.glowStroke(&ctx, arc, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 10, blur: 8)
                }
            }
            let orbit = (-90 + sweep) * Double.pi / 180
            let dot = CGPoint(x: center.x + CGFloat(cos(orbit)) * R, y: center.y + CGFloat(sin(orbit)) * R)
            if !muted {
                let r = 5 + CGFloat(sin(clock * 3))
                ctx.drawLayer { layer in
                    layer.addFilter(.shadow(color: cavnarEmberHot.opacity(0.9), radius: 8))
                    layer.fill(Path(ellipseIn: CGRect(x: dot.x - r, y: dot.y - r, width: r * 2, height: r * 2)), with: .color(cavnarEmberHot))
                }
            }
            if showsCenterFigure {
                CavnarChart.text(&ctx, CavnarChart.number(Self.centerText(score, progress: s), size: 26, weight: 700), at: CGPoint(x: center.x, y: center.y - 4))
                CavnarChart.text(&ctx, CavnarChart.kicker("AI visibility"), at: CGPoint(x: center.x, y: center.y + 15))
            } else {
                CavnarChart.text(&ctx, CavnarChart.kicker("AI visibility", color: muted ? .cavnarInk2 : .cavnarEmber2),
                                 at: CGPoint(x: center.x, y: center.y))
            }
        } else if showsCenterFigure {
            CavnarChart.text(&ctx, CavnarChart.number(Self.centerText(nil), size: 26, weight: 700, color: .cavnarInk3), at: CGPoint(x: center.x, y: center.y - 4))
            CavnarChart.text(&ctx, CavnarChart.kicker("Not measured", color: .cavnarInk3), at: CGPoint(x: center.x, y: center.y + 15))
        } else {
            CavnarChart.text(&ctx, CavnarChart.kicker("Not measured", color: .cavnarInk2), at: CGPoint(x: center.x, y: center.y))
        }

        // History — a trend needs two runs; until then the right half is
        // a single line of copy, not an empty plot.
        let L = size.width * 0.52, Rr = size.width - 18, T: CGFloat = 40, B = size.height - 40
        guard runs.count >= 2 else {
            CavnarChart.text(&ctx, CavnarChart.label("FIRST CHECK", size: CavnarType.kicker, weight: 700), at: CGPoint(x: L, y: center.y - 12), anchor: .leading)
            CavnarChart.text(&ctx, CavnarChart.label("Run another check to start the trend line.", size: CavnarType.caption, color: .cavnarInk2), at: CGPoint(x: L, y: center.y + 8), anchor: .leading)
            return
        }
        CavnarChart.text(&ctx, CavnarChart.label("LAST \(runs.count) RUNS", size: CavnarType.kicker, weight: 700), at: CGPoint(x: L, y: T - 14), anchor: .leading)
        let n = runs.count
        let vals = runs.map { Double($0.aiScore) }
        let lo = max(0, (vals.min() ?? 0) - 10), hi = min(100, (vals.max() ?? 100) + 10)
        func x(_ i: Int) -> CGFloat { L + (Rr - L) * CGFloat(i) / CGFloat(n - 1) }
        func y(_ v: Double) -> CGFloat { B - (B - T) * CGFloat((v - lo) / max(1, hi - lo)) }
        let ln = CavnarChart.easeOut(CavnarChart.window(t, from: 0.35, length: 0.65))
        var line = Path()
        for i in 0..<n { let p = CGPoint(x: x(i), y: y(vals[i])); i == 0 ? line.move(to: p) : line.addLine(to: p) }
        ctx.drawLayer { layer in
            layer.clip(to: Path(CGRect(x: L - 4, y: 0, width: (Rr - L) * ln + 8, height: size.height)))
            CavnarChart.glowStroke(&layer, line, color: .cavnarEmber2, glow: .cavnarEmber, lineWidth: 2, blur: 5)
            var fill = line
            fill.addLine(to: CGPoint(x: Rr, y: B)); fill.addLine(to: CGPoint(x: L, y: B)); fill.closeSubpath()
            layer.fill(fill, with: .linearGradient(Gradient(colors: [Color.cavnarEmber.opacity(0.25), Color.cavnarEmber.opacity(0)]),
                                                   startPoint: CGPoint(x: 0, y: T), endPoint: CGPoint(x: 0, y: B)))
        }
        for i in 0..<n where x(i) <= L + (Rr - L) * ln {
            let p = CGPoint(x: x(i), y: y(vals[i]))
            ctx.fill(Path(ellipseIn: CGRect(x: p.x - 2.5, y: p.y - 2.5, width: 5, height: 5)), with: .color(i == n - 1 ? cavnarEmberHot : .cavnarEmber2))
        }
        if let delta {
            let text = delta == 0 ? "No change since last run" : (delta > 0 ? "+\(delta) since last run" : "\(delta) since last run")
            ctx.drawLayer { layer in
                layer.opacity = ln
                CavnarChart.text(&layer, CavnarChart.number(text, size: CavnarType.caption, weight: 700, color: delta >= 0 ? .cavnarGreen : .cavnarRedText),
                                 at: CGPoint(x: Rr, y: size.height - 14), anchor: .trailing)
            }
        }
    }
}
