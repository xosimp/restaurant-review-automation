import SwiftUI
import WidgetKit

/// "Last night" — the night half of the snapshot on its own: the date
/// (M/D/YY), the net in Space Grotesk, the change against the same weekday
/// last week (else yesterday), and the net against budget for a login
/// allowed the budget. Same WidgetSnapshot, same provider and the same
/// staleness rules as the waiting widget; the app writes both, neither
/// calls the API. Tapping it opens that night's report.
struct CavnarLastNightWidget: Widget {
    var body: some WidgetConfiguration {
        StaticConfiguration(kind: WidgetSnapshot.lastNightWidgetKind, provider: WaitingProvider()) { entry in
            LastNightWidgetView(entry: entry)
                .containerBackground(for: .widget) { Color.cavnarPaper }
        }
        .configurationDisplayName("Last night")
        .description("Last night's net sales against last week and budget.")
        .supportedFamilies([.systemSmall, .accessoryRectangular, .accessoryInline])
    }
}

struct LastNightWidgetView: View {
    let entry: WaitingEntry
    @Environment(\.widgetFamily) private var family

    /// The snapshot, only while its night is current — an old night is
    /// never shown as last night's.
    private var snap: WidgetSnapshot? {
        guard let s = entry.snapshot, s.nightIsCurrent(now: entry.date), s.netLabel != nil else { return nil }
        return s
    }

    var body: some View {
        content.widgetURL(URL(string: snap?.nightLink ?? "cavnarai://nav/dsr"))
    }

    @ViewBuilder
    private var content: some View {
        switch family {
        case .accessoryInline: inline
        case .accessoryRectangular: rectangular
        default: small
        }
    }

    // MARK: Home Screen

    @ViewBuilder
    private var small: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let name = entry.snapshot?.restaurantName {
                Text(name.uppercased())
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .foregroundStyle(Color.cavnarEmber2)
                    .lineLimit(1)
            }
            HStack(spacing: 5) {
                if let dot = WaitingWidgetView.toneColor(snap?.nightTone) {
                    Circle().fill(dot).frame(width: 7, height: 7).accessibilityHidden(true)
                }
                Text("LAST NIGHT")
                    .font(.cavnarBody(9.5, weight: 700))
                    .tracking(0.8)
                    .foregroundStyle(Color.cavnarInk3)
                if let date = snap?.nightLabel {
                    Text(date)
                        .font(.cavnarNumber(9.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            if let snap, let net = snap.netLabel {
                Text(net)
                    .font(.cavnarNumber(28, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .minimumScaleFactor(0.6)
                    .lineLimit(1)
                    .privacySensitive()
                Text("net sales")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 2)
                if let change = snap.changeLabel {
                    comparison(change, up: snap.changeIsUp, basis: snap.changeBasis ?? "")
                }
                if let budget = snap.budgetLabel {
                    comparison(Self.figure(of: budget), up: snap.budgetIsUp, basis: "vs budget", down: .cavnarAmber)
                }
            } else {
                Spacer(minLength: 0)
                Text(entry.snapshot == nil ? "Sign in to Cavnar AI to see last night."
                                           : "No report for last night yet.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk2)
                Spacer(minLength: 0)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    /// "+8%  vs last Friday" — the figure in Space Grotesk, green up and
    /// `down` (red; amber under budget, as Home's card) down, the basis in
    /// the muted body face.
    private func comparison(_ figure: String, up: Bool?, basis: String, down: Color = .cavnarRed) -> some View {
        HStack(spacing: 4) {
            Text(figure)
                .font(.cavnarNumber(12, weight: 700))
                .foregroundStyle(up == false ? down : Color.cavnarGreen)
            Text(basis)
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
                .lineLimit(1)
        }
        .privacySensitive()
    }

    /// "+$525" out of "+$525 vs budget".
    static func figure(of budgetLabel: String) -> String {
        budgetLabel.components(separatedBy: " vs ").first ?? budgetLabel
    }

    // MARK: Lock Screen

    @ViewBuilder
    private var rectangular: some View {
        if let snap, let net = snap.netLabel {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 4) {
                    Text("Last night")
                        .font(.cavnarBody(12, weight: 700))
                    if let date = snap.nightLabel {
                        Text(date).font(.cavnarNumber(12))
                    }
                }
                HStack(spacing: 4) {
                    Text(net)
                        .font(.cavnarNumber(15, weight: 600))
                    if let change = snap.changeLabel {
                        Text(change).font(.cavnarNumber(12, weight: 700))
                    }
                }
                .privacySensitive()
                if let budget = snap.budgetLabel {
                    HStack(spacing: 3) {
                        Text(Self.figure(of: budget)).font(.cavnarNumber(12, weight: 600))
                        Text("vs budget").font(.cavnarBody(12))
                    }
                    .privacySensitive()
                }
            }
        } else {
            Text(entry.snapshot == nil ? "Open Cavnar AI" : "No report for last night yet")
                .font(.cavnarBody(13))
        }
    }

    @ViewBuilder
    private var inline: some View {
        if let snap, let net = snap.netLabel {
            Text([snap.nightLabel, net, snap.changeLabel].compactMap { $0 }.joined(separator: " "))
        } else {
            Text("Open Cavnar AI")
        }
    }
}
