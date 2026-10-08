import SwiftUI
import WidgetKit

struct CostsEntry: TimelineEntry {
    let date: Date
    let snapshot: WidgetSnapshot?
    var locationName: String? = nil
    var metric: CavnarCostMetric = .labor
}

struct CostsProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> CostsEntry {
        CostsEntry(date: Date(), snapshot: nil)
    }

    func snapshot(for configuration: CavnarCostsWidgetIntent, in context: Context) async -> CostsEntry {
        Self.entry(configuration, now: Date())
    }

    func timeline(for configuration: CavnarCostsWidgetIntent, in context: Context) async -> Timeline<CostsEntry> {
        let now = Date()
        return Timeline(entries: [Self.entry(configuration, now: now)], policy: .after(now.addingTimeInterval(3600)))
    }

    static func entry(_ configuration: CavnarCostsWidgetIntent, now: Date) -> CostsEntry {
        CostsEntry(date: now, snapshot: WidgetSnapshot.forWidget(locationId: configuration.location?.id),
                   locationName: configuration.location?.name, metric: configuration.metric)
    }
}

/// Last night's labor % and food cost % against their targets (parity audit
/// #59) — the night report's own KPI figures, written into the snapshot by
/// the app. Small on the Home Screen; a gauge on the Lock Screen. The
/// figures are privacy-sensitive: a locked phone shows them redacted.
struct CavnarCostsWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: WidgetSnapshot.costsWidgetKind, intent: CavnarCostsWidgetIntent.self,
                               provider: CostsProvider()) { entry in
            CostsWidgetView(entry: entry)
                .cavnarForcedDark()
                .containerBackground(for: .widget) { Color.cavnarPaper.cavnarForcedDark() }
        }
        .configurationDisplayName("Labor and food cost")
        .description("Last night's labor % and food cost % against your targets.")
        .supportedFamilies([.systemSmall, .accessoryCircular, .accessoryInline])
    }
}

struct CostsWidgetView: View {
    let entry: CostsEntry
    @Environment(\.widgetFamily) private var family

    /// Only while last night is current — an old night's percentages are
    /// never shown as last night's.
    private var snap: WidgetSnapshot? {
        guard let s = entry.snapshot, s.nightIsCurrent(now: entry.date) else { return nil }
        return s
    }

    var body: some View {
        content.widgetURL(URL(string: snap?.nightLink ?? "cavnarai://nav/dsr"))
    }

    @ViewBuilder
    private var content: some View {
        switch family {
        case .accessoryCircular: circular
        case .accessoryInline: Text(snap?.costsLine ?? "Open Cavnar AI")
        default: small
        }
    }

    /// A figure at or under its target reads green, over it amber; with no
    /// target the figure is plain ink — never a guessed verdict.
    static func tone(value: Double?, target: Double?) -> Color {
        guard let value, let target else { return .cavnarInk }
        return value <= target ? .cavnarGreen : .cavnarAmber
    }

    /// The gauge's top: twice the target, or enough to hold the figure.
    static func gaugeMax(value: Double, target: Double?) -> Double {
        max((target ?? 30) * 2, value * 1.1, 1)
    }

    // MARK: Lock Screen

    @ViewBuilder
    private var circular: some View {
        let isLabor = entry.metric == .labor
        let value = isLabor ? snap?.laborPct : snap?.foodPct
        let target = isLabor ? snap?.laborTarget : snap?.foodTarget
        if let value {
            Gauge(value: min(value, Self.gaugeMax(value: value, target: target)),
                  in: 0...Self.gaugeMax(value: value, target: target)) {
                Text(isLabor ? "LAB" : "FOOD")
                    .font(.cavnarBody(9, weight: 700))
            } currentValueLabel: {
                Text(String(format: "%.0f", value))
                    .font(.cavnarNumber(17, weight: 700))
                    .privacySensitive()
            }
            .gaugeStyle(.accessoryCircular)
            .widgetAccentable()
        } else {
            VStack(spacing: 0) {
                Text("—").font(.cavnarNumber(20, weight: 700))
                Text(isLabor ? "labor" : "food").font(.cavnarBody(9, weight: 700))
            }
        }
    }

    // MARK: Home Screen

    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text((entry.locationName ?? snap?.restaurantName ?? "Last night").uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
                .lineLimit(1)
            if let snap {
                if let date = snap.nightLabel {
                    Text(date)
                        .font(.cavnarNumber(10, weight: 700))
                        .foregroundStyle(Color.cavnarInk3)
                }
                Spacer(minLength: 0)
                row("Labor", label: snap.laborLabel, value: snap.laborPct, target: snap.laborTarget)
                row("Food cost", label: snap.foodLabel, value: snap.foodPct, target: snap.foodTarget, estimate: true)
            } else {
                Spacer(minLength: 0)
                Text(entry.snapshot == nil
                     ? (entry.locationName.map { "Open \($0) in Cavnar AI to load it here." }
                        ?? "Sign in to Cavnar AI to see last night.")
                     : "No report for last night yet.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func row(_ name: String, label: String?, value: Double?, target: Double?, estimate: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(estimate ? "\(name.uppercased()) \u{00B7} EST." : name.uppercased())
                .font(.cavnarBody(9.5, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            HStack(alignment: .firstTextBaseline, spacing: 5) {
                Text(label ?? "—")
                    .font(.cavnarNumber(22, weight: 600))
                    .foregroundStyle(Self.tone(value: value, target: target))
                    .privacySensitive()
                if let target, value != nil {
                    Text("target \(String(format: "%g", target))%")
                        .font(.cavnarNumber(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .lineLimit(1)
                }
            }
        }
    }
}
