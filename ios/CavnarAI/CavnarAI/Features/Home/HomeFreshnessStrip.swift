import SwiftUI

/// "DATA AS OF 9/22/26" and one small capsule per source behind Home — how
/// current each one is (K4 `freshness[]`, `data_as_of`, `monitoring`; the
/// web's `.hb-fresh`). Sits under the pulse tiles so every figure below it
/// is read with its age. Unknown age is never drawn as current; a sample
/// source is hatched, never scored. Draws nothing for an older server.
struct HomeFreshnessStrip: View {
    let entries: [HomeFreshnessEntry]
    var dataAsOf: String? = nil
    var monitoring: HomeMonitoring? = nil

    var body: some View {
        if !entries.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                if let kicker = Self.kicker(dataAsOf: dataAsOf, monitoring: monitoring) {
                    HomeMixedText.make(kicker, size: 11, weight: 700, color: .cavnarInk3, numberColor: .cavnarInk2)
                        .tracking(1.1)
                        .accessibilityLabel(kicker.lowercased())
                }
                AccountFlowLayout(spacing: 6, lineSpacing: 6) {
                    ForEach(entries) { entry in
                        chip(entry)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// "DATA AS OF 9/22/26 · 4 LIVE" — nil when there is neither a date nor
    /// a live count to state.
    static func kicker(dataAsOf: String?, monitoring: HomeMonitoring?) -> String? {
        var parts: [String] = []
        if let d = dataAsOf ?? monitoring?.stalestAsOf { parts.append("DATA AS OF \(d)") }
        if let n = monitoring?.countLive, n > 0 { parts.append("\(n) LIVE") }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    static func dotColor(_ state: HomeFreshnessEntry.State) -> Color {
        switch state {
        case .current: return .cavnarGreen
        case .aging, .stale: return .cavnarAmber
        case .notConnected, .unknown, .sample: return .cavnarInk3
        }
    }

    private func chip(_ e: HomeFreshnessEntry) -> some View {
        let tone = Self.dotColor(e.state)
        let glows = e.state == .current || e.state == .aging || e.state == .stale
        return HStack(spacing: 6) {
            Group {
                if e.state == .sample {
                    // Hatched: sample data is not a reading at all.
                    Circle().strokeBorder(Color.cavnarInk3, style: StrokeStyle(lineWidth: 1.5, dash: [1.5, 1.5]))
                } else {
                    Circle().fill(tone)
                        .shadow(color: glows ? tone.opacity(0.8) : .clear, radius: e.state == .stale ? 4.5 : 3.5)
                }
            }
            .frame(width: 7, height: 7)
            .accessibilityHidden(true)
            (Text(e.name).font(.cavnarBody(12.5, weight: 700)).foregroundColor(.cavnarInk2)
             + (e.caption.map { HomeMixedText.make(" " + $0, size: 12.5, weight: 500, color: .cavnarInk3) }
                ?? Text(verbatim: ""))
             + (e.pct.map { Text(" \($0)%").font(.cavnarNumber(12.5, weight: 600)).foregroundColor(tone == .cavnarInk3 ? .cavnarInk3 : .cavnarInk2) }
                ?? Text(verbatim: "")))
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .background(Color.cavnarPaper2.opacity(0.7), in: Capsule())
        .overlay(Capsule().strokeBorder(e.state == .stale ? Color.cavnarAmber.opacity(0.7)
                                                           : Color.cavnarPaper3.opacity(0.8), lineWidth: 1))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(e.name), \(Self.spokenState(e.state))" + (e.caption.map { ", \($0)" } ?? "")
                            + (e.pct.map { ", \($0) percent" } ?? ""))
    }

    /// The state in words, so colour never carries it alone.
    static func spokenState(_ s: HomeFreshnessEntry.State) -> String {
        switch s {
        case .current: return "current"
        case .aging: return "aging"
        case .stale: return "stale"
        case .notConnected: return "not connected"
        case .unknown: return "age unknown"
        case .sample: return "sample data"
        }
    }
}
