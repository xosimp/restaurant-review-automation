import SwiftUI
import WidgetKit

/// The staff "Next shift" widget (MISS-11), Home Screen and Lock Screen:
/// "Next shift: Fri 4pm – 10pm · Bartender", or "No shifts in the next 7
/// days". Drawn from StaffShiftSnapshot, which the app writes from the staff
/// session's own /staff/api/shifts read; this target never calls the API.
struct CavnarNextShiftWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: StaffShiftSnapshot.widgetKind, provider: NextShiftProvider()) { entry in
            NextShiftWidgetView(entry: entry)
                .cavnarForcedDark()
                .containerBackground(for: .widget) { Color.cavnarPaper.cavnarForcedDark() }
        }
        .configurationDisplayName("Next shift")
        .description("Your next shift from the published schedule.")
        .supportedFamilies([.systemSmall, .accessoryRectangular, .accessoryInline])
    }
}

struct NextShiftEntry: TimelineEntry {
    let date: Date
    /// Nil: no staff session has written one on this phone.
    let snapshot: StaffShiftSnapshot?
}

struct NextShiftProvider: TimelineProvider {
    func placeholder(in context: Context) -> NextShiftEntry {
        NextShiftEntry(date: Date(), snapshot: nil)
    }

    func getSnapshot(in context: Context, completion: @escaping (NextShiftEntry) -> Void) {
        completion(NextShiftEntry(date: Date(), snapshot: StaffShiftSnapshot.load()))
    }

    /// An entry now and one at each moment the answer can change — a
    /// shift's end, each midnight — so "Today" becomes the next shift
    /// without the app being opened. The app reloads it on every write.
    func getTimeline(in context: Context, completion: @escaping (Timeline<NextShiftEntry>) -> Void) {
        let now = Date()
        let snap = StaffShiftSnapshot.load()
        var entries = [NextShiftEntry(date: now, snapshot: snap)]
        for point in (snap?.changePoints(after: now) ?? []).prefix(20) {
            entries.append(NextShiftEntry(date: point, snapshot: snap))
        }
        let policy: TimelineReloadPolicy = entries.count > 1 ? .atEnd : .after(now.addingTimeInterval(6 * 3600))
        completion(Timeline(entries: entries, policy: policy))
    }
}

struct NextShiftWidgetView: View {
    let entry: NextShiftEntry
    @Environment(\.widgetFamily) private var family

    private var answer: StaffShiftSnapshot.Answer {
        entry.snapshot?.answer(now: entry.date) ?? .unknown
    }

    var body: some View {
        switch family {
        case .accessoryInline:
            Text(entry.snapshot?.line(now: entry.date) ?? "Open Cavnar AI to see your next shift")
        case .accessoryRectangular:
            rectangular
        default:
            small
        }
    }

    @ViewBuilder
    private var rectangular: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text("Next shift")
                .font(.cavnarBody(12, weight: 700))
            switch answer {
            case .next(let s):
                HStack(spacing: 4) {
                    Text(StaffShiftSnapshot.dayWord(s.date, now: entry.date, calendar: .current))
                        .font(.cavnarBody(14, weight: 700))
                    Text(StaffShiftSnapshot.timeRange(s))
                        .font(.cavnarNumber(14, weight: 600))
                }
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                if let role = s.role, !role.isEmpty {
                    Text(role).font(.cavnarBody(12)).lineLimit(1)
                }
            default:
                Text(sentence)
                    .font(.cavnarBody(13))
                    .lineLimit(2)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var small: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("NEXT SHIFT")
                .font(.cavnarBody(CavnarType.tag, weight: 700))
                .tracking(1.2)
                .foregroundStyle(Color.cavnarEmber2)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            switch answer {
            case .next(let s):
                Text(StaffShiftSnapshot.dayWord(s.date, now: entry.date, calendar: .current))
                    .font(.cavnarHeadline(22))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                let times = StaffShiftSnapshot.timeRange(s)
                if !times.isEmpty {
                    Text(times)
                        .font(.cavnarNumber(17, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .minimumScaleFactor(0.7)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
                if let role = s.role, !role.isEmpty {
                    Text(role)
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk2)
                        .lineLimit(2)
                }
            default:
                Spacer(minLength: 0)
                Text(sentence)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .accessibilityElement(children: .combine)
    }

    private var sentence: String {
        if entry.snapshot == nil { return "Sign in to the Cavnar AI staff app to see your next shift." }
        switch answer {
        case .none: return "No shifts in the next 7 days"
        case .noneThrough(let day): return "No shifts through \(day)"
        case .notPublished: return "No schedule posted yet"
        default: return "Open Cavnar AI to see your next shift"
        }
    }
}
