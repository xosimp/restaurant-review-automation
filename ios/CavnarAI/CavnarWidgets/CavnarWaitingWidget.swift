import SwiftUI
import WidgetKit

struct WaitingEntry: TimelineEntry {
    let date: Date
    /// Nil: nobody signed in on this phone (or never opened since install).
    let snapshot: WidgetSnapshot?
}

struct WaitingProvider: TimelineProvider {
    func placeholder(in context: Context) -> WaitingEntry {
        WaitingEntry(date: Date(), snapshot: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (WaitingEntry) -> Void) {
        completion(WaitingEntry(date: Date(), snapshot: WidgetSnapshot.load()))
    }

    func getTimeline(in context: Context, completion: @escaping (Timeline<WaitingEntry>) -> Void) {
        // The app reloads this timeline whenever it refreshes the snapshot.
        // The hourly entry only exists so an old snapshot can say it is old.
        let now = Date()
        let entry = WaitingEntry(date: now, snapshot: WidgetSnapshot.load())
        completion(Timeline(entries: [entry], policy: .after(now.addingTimeInterval(3600))))
    }
}

/// "3 things waiting · Last night $4,210 +8% vs last Friday". Tapping it
/// opens the command sheet when something is waiting (its first section IS
/// the waiting list), else last night's report.
struct CavnarWaitingWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: WidgetSnapshot.widgetKind, provider: WaitingProvider()) { entry in
            WaitingWidgetView(entry: entry)
                .containerBackground(for: .widget) { Color.cavnarPaper }
        }
        .configurationDisplayName("Cavnar AI")
        .description("What's waiting on you, and last night's sales.")
        .supportedFamilies([.systemSmall, .accessoryRectangular, .accessoryInline, .accessoryCircular])
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
        if let snap, snap.waitingCount > 0 { return URL(string: "cavnarai://command") }
        return URL(string: "cavnarai://nav/dsr")
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
        default:
            small
        }
    }

    // MARK: Lock Screen

    @ViewBuilder
    private var inline: some View {
        if let snap {
            Text(snap.waitingCount > 0 ? snap.waitingLine : (snap.netLabel.map { "Last night \($0)" } ?? snap.waitingLine))
        } else {
            Text("Open Cavnar AI")
        }
    }

    @ViewBuilder
    private var circular: some View {
        if let snap {
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
                Text(snap.waitingLine)
                    .font(.cavnarBody(14, weight: 700))
                if let net = snap.netLabel {
                    HStack(spacing: 4) {
                        Text("Last night")
                            .font(.cavnarBody(12))
                        Text(net)
                            .font(.cavnarNumber(13, weight: 600))
                            .privacySensitive()
                        if let change = snap.changeLabel {
                            Text(change)
                                .font(.cavnarNumber(12, weight: 700))
                                .privacySensitive()
                        }
                    }
                } else if let night = snap.nightLabel {
                    Text("Report \(night)").font(.cavnarBody(12))
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
            Text("CAVNAR AI")
                .font(.cavnarBody(10.5, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
            if let snap {
                Text(snap.waitingLine)
                    .font(.cavnarHeadline(17))
                    .foregroundStyle(snap.waitingCount > 0 ? Color.cavnarInk : Color.cavnarInk2)
                    .lineLimit(2)
                Spacer(minLength: 0)
                if let net = snap.netLabel {
                    Text(snap.nightLabel.map { "LAST NIGHT · \($0)" } ?? "LAST NIGHT")
                        .font(.cavnarBody(9.5, weight: 700))
                        .tracking(0.8)
                        .foregroundStyle(Color.cavnarInk3)
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
                Text(entry.snapshot == nil ? "Sign in to Cavnar AI to see what's waiting."
                                           : "Open Cavnar AI to refresh.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
