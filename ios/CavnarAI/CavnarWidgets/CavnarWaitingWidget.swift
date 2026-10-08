import SwiftUI
import WidgetKit

struct WaitingEntry: TimelineEntry {
    let date: Date
    /// Nil: nobody signed in on this phone (or never opened since install),
    /// or the app has never read the location this widget is set to.
    let snapshot: WidgetSnapshot?
    /// The location the widget is set to (#96); nil = the app's own.
    var locationName: String? = nil
}

/// One provider for every widget drawn from the snapshot: the location it
/// is set to, read from the shared app group (#96).
struct WaitingProvider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> WaitingEntry {
        WaitingEntry(date: Date(), snapshot: nil)
    }

    func snapshot(for configuration: CavnarLocationWidgetIntent, in context: Context) async -> WaitingEntry {
        Self.entry(location: configuration.location, now: Date())
    }

    func timeline(for configuration: CavnarLocationWidgetIntent, in context: Context) async -> Timeline<WaitingEntry> {
        // The app reloads this timeline whenever it refreshes the snapshot —
        // in the foreground, on a background refresh and on a silent push
        // (#31). The hourly entry only exists so an old snapshot can say it
        // is old.
        let now = Date()
        return Timeline(entries: [Self.entry(location: configuration.location, now: now)],
                        policy: .after(now.addingTimeInterval(3600)))
    }

    static func entry(location: CavnarLocationEntity?, now: Date) -> WaitingEntry {
        WaitingEntry(date: now, snapshot: WidgetSnapshot.forWidget(locationId: location?.id),
                     locationName: location?.name)
    }
}

/// "3 things waiting · Last night $4,210 +8% vs last Friday". Tapping it
/// opens the command sheet when something is waiting (its first section IS
/// the waiting list), else last night's report. Each widget can be set to
/// one of the owner's locations (#96).
struct CavnarWaitingWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: WidgetSnapshot.widgetKind, intent: CavnarLocationWidgetIntent.self,
                               provider: WaitingProvider()) { entry in
            WaitingWidgetView(entry: entry)
                .cavnarForcedDark()
                .containerBackground(for: .widget) { Color.cavnarPaper.cavnarForcedDark() }
        }
        .configurationDisplayName("Cavnar AI")
        .description("What's waiting on you, and last night's sales.")
        .supportedFamilies([.systemSmall, .systemMedium, .systemLarge, .accessoryRectangular,
                            .accessoryInline, .accessoryCircular])
    }
}

struct WaitingWidgetView: View {
    let entry: WaitingEntry
    @Environment(\.widgetFamily) private var family

    private var snap: WidgetSnapshot? {
        guard let s = entry.snapshot, !s.isStale(now: entry.date) else { return nil }
        return s
    }

    private var link: URL? {
        URL(string: snap?.link(now: entry.date) ?? "cavnarai://nav/dsr")
    }

    /// The waiting line only while its count is current; an old count is
    /// never shown as now's (F3-8).
    private func waiting(_ snap: WidgetSnapshot) -> String? {
        snap.waitingIsCurrent(now: entry.date) ? snap.waitingLine : nil
    }

    /// Last night's net, with the night's date — only while current.
    private func night(_ snap: WidgetSnapshot) -> (date: String?, net: String)? {
        guard snap.nightIsCurrent(now: entry.date), let net = snap.netLabel else { return nil }
        return (snap.nightLabel, net)
    }

    var body: some View {
        content.widgetURL(link)
    }

    @ViewBuilder
    private var content: some View {
        switch family {
        case .accessoryInline:
            inline
        case .accessoryCircular:
            circular
        case .accessoryRectangular:
            rectangular
        case .systemMedium:
            medium
        case .systemLarge:
            large
        default:
            small
        }
    }

    /// What the small widget says with nothing current to draw. A widget set
    /// to a location the app hasn't read yet says how to fill it.
    static func emptyLine(_ entry: WaitingEntry) -> String {
        if entry.snapshot == nil, let name = entry.locationName {
            return "Open \(name) in Cavnar AI to load it here."
        }
        return entry.snapshot == nil ? "Sign in to Cavnar AI to see what's waiting." : "Open Cavnar AI to refresh."
    }

    /// The verdict's dot colour — green good, amber warn, red bad; nil
    /// for no verdict (no dot is drawn, never a guessed one).
    static func toneColor(_ tone: String?) -> Color? {
        switch tone {
        case "good": return .cavnarGreen
        case "warn": return .cavnarAmber
        case "bad": return .cavnarRed
        default: return nil
        }
    }

    /// "LAST NIGHT · 9/24/26" with the verdict's dot beside it (density #50).
    private func lastNightKicker(_ snap: WidgetSnapshot, date: String?) -> some View {
        HStack(spacing: 5) {
            if let dot = Self.toneColor(snap.nightTone) {
                Circle().fill(dot).frame(width: 7, height: 7)
                    .accessibilityHidden(true)
            }
            Text(date.map { "LAST NIGHT · \($0)" } ?? "LAST NIGHT")
                .font(.cavnarBody(9.5, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
        }
    }

    // MARK: Home Screen, medium

    /// "3 things waiting · Last night: Good day 82/100 · $8,420" — the
    /// waiting count on the left, the night's verdict with its score and
    /// the net on the right (density #50).
    @ViewBuilder
    private var medium: some View {
        if let snap {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text((entry.locationName ?? snap.restaurantName ?? "Cavnar AI").uppercased())
                        .font(.cavnarBody(10.5, weight: 700))
                        .tracking(1.2)
                        .foregroundStyle(Color.cavnarEmber2)
                        .lineLimit(1)
                    Text(waiting(snap) ?? "Open Cavnar AI to refresh")
                        .font(.cavnarHeadline(18))
                        .foregroundStyle(waiting(snap) != nil && snap.waitingCount > 0 ? Color.cavnarInk : Color.cavnarInk2)
                        .lineLimit(3)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                VStack(alignment: .leading, spacing: 5) {
                    if snap.nightIsCurrent(now: entry.date) {
                        lastNightKicker(snap, date: snap.nightLabel)
                        if let verdict = snap.verdictLine {
                            Text(verdict)
                                .font(.cavnarBody(14, weight: 700))
                                .foregroundStyle(Color.cavnarInk)
                                .lineLimit(2)
                        }
                        if let n = night(snap) {
                            Text(n.net)
                                .font(.cavnarNumber(22, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                                .privacySensitive()
                            if let change = snap.changeLabel {
                                Text(change)
                                    .font(.cavnarNumber(12, weight: 700))
                                    .foregroundStyle(snap.changeIsUp == false ? Color.cavnarRed : Color.cavnarGreen)
                                    .privacySensitive()
                            }
                        }
                    } else {
                        Text("No report for last night yet")
                            .font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            small
        }
    }

    // MARK: Home Screen, large (an iPad's Home Screen, parity audit #99)

    /// The medium widget's two halves stacked, with last night's labor and
    /// food cost against their targets under them — "—" for a figure the
    /// night didn't measure, never 0%.
    @ViewBuilder
    private var large: some View {
        if let snap {
            VStack(alignment: .leading, spacing: 10) {
                Text((entry.locationName ?? snap.restaurantName ?? "Cavnar AI").uppercased())
                    .font(.cavnarBody(11, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                    .lineLimit(1)
                Text(waiting(snap) ?? "Open Cavnar AI to refresh")
                    .font(.cavnarHeadline(24))
                    .foregroundStyle(waiting(snap) != nil && snap.waitingCount > 0 ? Color.cavnarInk : Color.cavnarInk2)
                    .lineLimit(2)
                Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                if snap.nightIsCurrent(now: entry.date) {
                    lastNightKicker(snap, date: snap.nightLabel)
                    if let verdict = snap.verdictLine {
                        Text(verdict)
                            .font(.cavnarBody(15, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(2)
                    }
                    if let n = night(snap) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(n.net)
                                .font(.cavnarNumber(30, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            if let change = snap.changeLabel {
                                Text(change)
                                    .font(.cavnarNumber(13, weight: 700))
                                    .foregroundStyle(snap.changeIsUp == false ? Color.cavnarRed : Color.cavnarGreen)
                            }
                            if let basis = snap.changeBasis {
                                Text(basis)
                                    .font(.cavnarBody(12))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .lineLimit(1)
                            }
                        }
                        .privacySensitive()
                        if let budget = snap.budgetLabel {
                            Text(budget)
                                .font(.cavnarNumber(12.5, weight: 700))
                                .foregroundStyle(snap.budgetIsUp == false ? Color.cavnarRed : Color.cavnarGreen)
                                .privacySensitive()
                        }
                    }
                    HStack(spacing: 0) {
                        costFigure("LABOR", snap.laborLabel, value: snap.laborPct, target: snap.laborTarget)
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(width: 1, height: 34)
                        // An estimate, as the costs widget labels it.
                        costFigure("FOOD COST \u{00B7} EST.", snap.foodLabel, value: snap.foodPct,
                                   target: snap.foodTarget)
                    }
                    .padding(.top, 4)
                } else {
                    Text("No report for last night yet")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
                Spacer(minLength: 0)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        } else {
            small
        }
    }

    /// One of last night's cost figures, toned and worded as the costs
    /// widget does (CostsWidgetView.tone): "—" when unmeasured.
    private func costFigure(_ title: String, _ label: String?, value: Double?, target: Double?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title)
                .font(.cavnarBody(9.5, weight: 700))
                .tracking(0.8)
                .foregroundStyle(Color.cavnarInk3)
            Text(label ?? "—")
                .font(.cavnarNumber(20, weight: 600))
                .foregroundStyle(CostsWidgetView.tone(value: value, target: target))
                .privacySensitive()
            if let target, value != nil {
                Text("target \(String(format: "%g", target))%")
                    .font(.cavnarNumber(11))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 6)
    }

    // MARK: Lock Screen

    @ViewBuilder
    private var inline: some View {
        if let snap {
            if let line = waiting(snap), snap.waitingCount > 0 {
                Text(line)
            } else if let n = night(snap) {
                Text([n.date, n.net].compactMap { $0 }.joined(separator: " "))
            } else {
                Text(waiting(snap) ?? "Open Cavnar AI")
            }
        } else {
            Text("Open Cavnar AI")
        }
    }

    @ViewBuilder
    private var circular: some View {
        if let snap, waiting(snap) != nil {
            VStack(spacing: 0) {
                Text("\(snap.waitingCount)")
                    .font(.cavnarNumber(22, weight: 700))
                Text("waiting")
                    .font(.cavnarBody(9, weight: 700))
            }
        } else {
            Image(systemName: "sparkles")
        }
    }

    @ViewBuilder
    private var rectangular: some View {
        if let snap {
            VStack(alignment: .leading, spacing: 1) {
                Text(waiting(snap) ?? "Open Cavnar AI to refresh")
                    .font(.cavnarBody(14, weight: 700))
                if let n = night(snap) {
                    HStack(spacing: 4) {
                        Text(n.date ?? "Last night")
                            .font(.cavnarNumber(12))
                        Text(n.net)
                            .font(.cavnarNumber(13, weight: 600))
                            .privacySensitive()
                        if let change = snap.changeLabel {
                            Text(change)
                                .font(.cavnarNumber(12, weight: 700))
                                .privacySensitive()
                        }
                    }
                } else if snap.nightIsCurrent(now: entry.date), let label = snap.nightLabel {
                    Text("Report \(label)").font(.cavnarBody(12))
                }
            }
        } else {
            Text("Open Cavnar AI to refresh")
                .font(.cavnarBody(13))
        }
    }

    // MARK: Home Screen

    @ViewBuilder
    private var small: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Which store, for an owner with more than one.
            Text((entry.locationName ?? snap?.restaurantName ?? "Cavnar AI").uppercased())
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
                .lineLimit(1)
            if let snap {
                Text(waiting(snap) ?? "Open Cavnar AI to refresh")
                    .font(.cavnarHeadline(17))
                    .foregroundStyle(waiting(snap) != nil && snap.waitingCount > 0 ? Color.cavnarInk : Color.cavnarInk2)
                    .lineLimit(2)
                Spacer(minLength: 0)
                if let n = night(snap) {
                    let net = n.net
                    lastNightKicker(snap, date: n.date)
                    Text(net)
                        .font(.cavnarNumber(22, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .privacySensitive()
                    if let change = snap.changeLabel {
                        HStack(spacing: 4) {
                            Text(change)
                                .font(.cavnarNumber(12, weight: 700))
                                .foregroundStyle(snap.changeIsUp == false ? Color.cavnarRed : Color.cavnarGreen)
                            Text(snap.changeBasis ?? "")
                                .font(.cavnarBody(11))
                                .foregroundStyle(Color.cavnarInk3)
                                .lineLimit(1)
                        }
                        .privacySensitive()
                    }
                } else {
                    Text("No report for last night yet")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                }
            } else {
                Spacer(minLength: 0)
                Text(Self.emptyLine(entry))
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
