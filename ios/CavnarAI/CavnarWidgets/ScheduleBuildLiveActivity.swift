import ActivityKit
import SwiftUI
import WidgetKit

/// "Building next week · 3 of 7 days drafted · about 9:41 left" (parity
/// audit #38). Read-only: the activity shows how far the draft has got and
/// opens Labor; nothing on it acts. The countdown is the system's own timer
/// text against the measured typical run, so nothing wakes up every second.
struct ScheduleBuildLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ScheduleBuildAttributes.self) { context in
            ScheduleBuildLockView(attributes: context.attributes, state: context.state)
                .padding(16)
                .cavnarForcedDark()
                .activityBackgroundTint(Color.cavnarPaperDark)
                .activitySystemActionForegroundColor(Color.cavnarInkDark)
                .widgetURL(context.attributes.link)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    ScheduleBuildGlyph(state: context.state).cavnarForcedDark()
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ScheduleBuildTimer(state: context.state)
                        .font(.cavnarNumber(15, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .cavnarForcedDark()
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(ScheduleBuildLockView.headline(context.attributes, context.state))
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                            .lineLimit(1)
                        Text(ScheduleBuildLockView.detail(context.state))
                            .font(.cavnarNumber(13))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineLimit(1)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarForcedDark()
                }
            } compactLeading: {
                ScheduleBuildGlyph(state: context.state).cavnarForcedDark()
            } compactTrailing: {
                Text(context.state.daysTotal > 0 ? "\(min(context.state.daysDrafted, context.state.daysTotal))/\(context.state.daysTotal)" : "—")
                    .font(.cavnarNumber(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarForcedDark()
            } minimal: {
                ScheduleBuildGlyph(state: context.state).cavnarForcedDark()
            }
            .widgetURL(context.attributes.link)
        }
    }
}

private struct ScheduleBuildGlyph: View {
    let state: ScheduleBuildAttributes.ContentState

    var body: some View {
        Image(systemName: state.status == "failed" ? "exclamationmark.triangle.fill"
              : state.status == "done" ? "checkmark.circle.fill" : "calendar.badge.clock")
            .foregroundStyle(state.status == "failed" ? Color.cavnarAmber
                             : state.status == "done" ? Color.cavnarGreen : Color.cavnarEmber)
    }
}

private struct ScheduleBuildTimer: View {
    let state: ScheduleBuildAttributes.ContentState

    var body: some View {
        if let range = state.timerRange() {
            Text(timerInterval: range, countsDown: true)
                .monospacedDigit()
                .multilineTextAlignment(.trailing)
        } else {
            Text(state.status == "building" ? "—" : "")
        }
    }
}

struct ScheduleBuildLockView: View {
    let attributes: ScheduleBuildAttributes
    let state: ScheduleBuildAttributes.ContentState

    static func headline(_ attributes: ScheduleBuildAttributes, _ state: ScheduleBuildAttributes.ContentState) -> String {
        switch state.status {
        case "done": return "\(attributes.weekLabel) is drafted"
        case "failed": return "\(attributes.weekLabel) didn\u{2019}t finish"
        default: return "Building \(attributes.weekLabel.lowercased())"
        }
    }

    static func detail(_ state: ScheduleBuildAttributes.ContentState) -> String {
        switch state.status {
        case "done": return "Open Labor to look it over before it goes to anyone."
        case "failed": return state.note ?? "Open Labor to see why."
        default: return state.progressLine ?? "Drafting the first day"
        }
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text("SCHEDULE")
                    .font(.cavnarBody(10.5, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarEmber)
                Text(Self.headline(attributes, state))
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                Text(Self.detail(state))
                    .font(state.status == "building" ? .cavnarNumber(13) : .cavnarBody(13))
                    .foregroundStyle(state.status == "failed" ? Color.cavnarAmber : Color.cavnarInk3)
                    .lineLimit(2)
            }
            Spacer(minLength: 8)
            if state.timerRange() != nil {
                VStack(alignment: .trailing, spacing: 1) {
                    ScheduleBuildTimer(state: state)
                        .font(.cavnarNumber(17, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    Text("typical")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                }
            } else {
                ScheduleBuildGlyph(state: state).font(.system(size: 22))
            }
        }
    }
}
