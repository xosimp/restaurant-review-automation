import SwiftUI

/// Topic Heat Grid — what guests keep bringing up, cell-by-cell across the
/// last eight weeks. Ember for praise, red for complaints, intensity is
/// volume. Cells bloom in on a diagonal stagger; each row's trend arrow
/// slides in last. Tapping a row opens that topic's reviews.
struct TopicHeatGridChart: View {
    let data: TopicWeeks
    /// Period trend per category from the existing heatmap endpoint
    /// ("up" / "down" / "flat"), keyed by category.
    let trends: [String: String]
    var onSelect: ((TopicWeekRow) -> Void)? = nil

    private var rowHeight: CGFloat { 38 }
    private var height: CGFloat { 16 + rowHeight * CGFloat(max(1, data.topics.count)) + 26 }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarChartHeader(kicker: "Topic sentiment", title: "Topic Heat Grid",
                              detail: "Ember is praise, red is complaints — intensity is how often it came up.")
            CavnarAnimatedCanvas(duration: 1.5, height: height, replayKey: data.topics.map(\.category).joined()) { ctx, size, t, _ in
                draw(&ctx, size: size, t: t)
            } overlay: {
                // Invisible row hit targets over the canvas.
                VStack(spacing: 0) {
                    Color.clear.frame(height: 16)
                    ForEach(data.topics) { row in
                        Color.clear
                            .frame(height: rowHeight)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                Haptic.light()
                                onSelect?(row)
                            }
                            .accessibilityLabel("\(row.label), \(row.total) mentions")
                            .accessibilityAddTraits(.isButton)
                    }
                    Spacer(minLength: 0)
                }
            }
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize, t: Double) {
        let left: CGFloat = 92, right: CGFloat = 44, top: CGFloat = 16
        let cols = max(1, data.weekLabels.count)
        let cw = (size.width - left - right) / CGFloat(cols)
        for (r, row) in data.topics.enumerated() {
            let cy = top + rowHeight * CGFloat(r) + rowHeight / 2
            CavnarChart.text(&ctx, CavnarChart.label(row.label, size: 11.5, weight: 700, color: .cavnarInk2),
                             at: CGPoint(x: left - 12, y: cy), anchor: .trailing)
            let rowMax = max(1, row.weeks.map(\.total).max() ?? 1)
            for (c, cell) in row.weeks.enumerated() {
                let delay = Double(r + c) * 0.055
                let s = CavnarChart.easeOut(CavnarChart.window(t, from: delay, length: 0.35))
                let rect = CGRect(x: left + cw * CGFloat(c) + 3, y: top + rowHeight * CGFloat(r) + 3, width: cw - 6, height: rowHeight - 6)
                let scaled = rect.insetBy(dx: rect.width * (1 - s) / 2, dy: rect.height * (1 - s) / 2)
                let path = CavnarChart.roundedRect(scaled, radius: 6)
                guard cell.total > 0 else {
                    ctx.fill(path, with: .color(Color.white.opacity(0.035 * s)))
                    continue
                }
                let positiveShare = Double(cell.positive) / Double(cell.total)
                let volume = Double(cell.total) / Double(rowMax)
                let praise = positiveShare >= 0.5
                let color: Color = praise ? .cavnarEmber : .cavnarRed
                let alpha = praise ? 0.18 + 0.72 * volume * positiveShare : 0.25 + 0.65 * volume * (1 - positiveShare)
                ctx.fill(path, with: .color(color.opacity(alpha * s)))
                if volume >= 0.85 {
                    CavnarChart.glowFill(&ctx, path, color: color.opacity(alpha * s), glow: color.opacity(0.7 * s), blur: 7)
                }
            }
            // Trend arrow, last.
            let at = CavnarChart.window(t, from: 0.75, length: 0.25)
            let trend = trends[row.category] ?? "flat"
            let glyph = trend == "up" ? "↑" : (trend == "down" ? "↓" : "→")
            let tone: Color = trend == "down" ? .cavnarGreen : (trend == "up" ? .cavnarEmber2 : .cavnarInk3)
            ctx.drawLayer { layer in
                layer.opacity = at
                CavnarChart.text(&layer, CavnarChart.number(glyph, size: 14, weight: 700, color: tone),
                                 at: CGPoint(x: size.width - right + 12 + (1 - at) * 8, y: cy), anchor: .leading)
            }
        }
        for c in 0..<cols {
            CavnarChart.text(&ctx, CavnarChart.label(data.weekLabels[c], size: 10),
                             at: CGPoint(x: left + cw * CGFloat(c) + cw / 2, y: size.height - 10))
        }
    }
}
