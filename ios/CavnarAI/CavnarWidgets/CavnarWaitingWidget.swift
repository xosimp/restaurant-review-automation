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
        .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular, .accessoryInline,
                            .accessoryCircular])
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
        default:
            small
        }
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
                    Text((snap.restaurantName ?? "Cavnar AI").uppercased())
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
            Text((snap?.restaurantName ?? "Cavnar AI").uppercased())
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
