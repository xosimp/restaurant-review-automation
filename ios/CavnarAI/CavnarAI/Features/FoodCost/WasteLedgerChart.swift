import SwiftUI

/// Waste Ledger — every flagged item as an ember bar with its dollar amount
/// counting up beside it, sorted by cost so the first bar is always the
/// thing to fix. Bars extend with a sheen travelling their length. Also
/// used for overstock ("tied-up capital") with the same shape.
struct WasteLedgerChart: View {
    struct Row: Identifiable {
        let id: String
        let name: String
        let value: Double
        let detail: String?
    }

    let kicker: String
    let title: String
    let headline: String
    let rows: [Row]
    var tint: Color = .cavnarEmber
    var maxRows: Int = 6

    private var shown: [Row] { Array(rows.sorted { $0.value > $1.value }.prefix(maxRows)) }
    private var height: CGFloat { 44 + CGFloat(max(1, shown.count)) * 40 + 8 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: kicker, title: title)
            CavnarAnimatedCanvas(duration: 1.6, height: height, replayKey: shown.map(\.id).joined()) { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let left: CGFloat = 18, right: CGFloat = 92, top: CGFloat = 44, rh: CGFloat = 40
        CavnarChart.text(&ctx, CavnarChart.label(headline.uppercased(), size: 10.5, weight: 700), at: CGPoint(x: left, y: 22), anchor: .leading)
        let maxValue = max(1, shown.map(\.value).max() ?? 1) * 1.08
        for (i, row) in shown.enumerated() {
            let s = CavnarChart.easeOut(CavnarChart.window(t, from: Double(i) * 0.1, length: 0.7))
            let y = top + rh * CGFloat(i) + 6
            let bh = rh - 14
            let full = size.width - left - right
            let bw = full * CGFloat(row.value / maxValue) * CGFloat(s)
            ctx.fill(CavnarChart.roundedRect(CGRect(x: left, y: y, width: full, height: bh), radius: bh / 2), with: .color(Color.white.opacity(0.05)))
            let bar = CavnarChart.roundedRect(CGRect(x: left, y: y, width: max(bw, bh), height: bh), radius: bh / 2)
            ctx.drawLayer { layer in
                layer.addFilter(.blur(radius: 6))
                layer.fill(bar, with: .color(tint.opacity(0.6)))
            }
            ctx.fill(bar, with: .linearGradient(Gradient(colors: [tint, Color.cavnarEmber2]),
                                                startPoint: CGPoint(x: left, y: 0), endPoint: CGPoint(x: left + bw, y: 0)))
            if s > 0 && s < 1 {
                // The sheen: a soft band riding the bar's leading edge.
                let sx = left + bw
                ctx.drawLayer { layer in
                    layer.clip(to: bar)
                    layer.fill(Path(CGRect(x: sx - 18, y: y, width: 24, height: bh)),
                               with: .linearGradient(Gradient(colors: [Color.white.opacity(0), Color.white.opacity(0.35), Color.white.opacity(0)]),
                                                     startPoint: CGPoint(x: sx - 18, y: 0), endPoint: CGPoint(x: sx + 6, y: 0)))
                }
            }
            CavnarChart.text(&ctx, CavnarChart.label(row.name, size: 12, weight: 700, color: .cavnarInk), at: CGPoint(x: left + 12, y: y + bh / 2), anchor: .leading)
            CavnarChart.text(&ctx, CavnarChart.number("$\(Int((row.value * s).rounded()).formatted())", size: 14, weight: 700),
                             at: CGPoint(x: size.width - 16, y: y + bh / 2 - (row.detail == nil ? 0 : 6)), anchor: .trailing)
            if let detail = row.detail {
                CavnarChart.text(&ctx, CavnarChart.label(detail, size: 10), at: CGPoint(x: size.width - 16, y: y + bh / 2 + 9), anchor: .trailing)
            }
        }
    }
}
