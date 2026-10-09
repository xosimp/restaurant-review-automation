import ActivityKit
import SwiftUI
import WidgetKit

/// "Tonight's service" (parity audit #94): the night against a typical same
/// weekday and who hasn't clocked in, from service open to close. It shows
/// and opens Labor's Today — nothing on it acts, and no dollar figure is on
/// the Lock Screen (the pulse is a percent).
struct ServiceLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: ServiceAttributes.self) { context in
            ServiceLockView(attributes: context.attributes, state: context.state)
                .padding(16)
                .cavnarForcedDark()
                .activityBackgroundTint(Color.cavnarPaperDark)
                .activitySystemActionForegroundColor(Color.cavnarInkDark)
                .widgetURL(context.attributes.link)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Text(context.attributes.restaurantName.isEmpty ? "Tonight" : context.attributes.restaurantName)
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                        .lineLimit(1)
                        .cavnarForcedDark()
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ServicePulse(state: context.state, size: 15).cavnarForcedDark()
                }
                DynamicIslandExpandedRegion(.bottom) {
                    Text(context.state.coverageLine)
                        .font(.cavnarBody(13))
                        .foregroundStyle((context.state.missingCount ?? context.state.missing.count) > 0
                                         ? Color.cavnarAmber : Color.cavnarInk3)
                        .lineLimit(1)
                        .privacySensitive()
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .cavnarForcedDark()
                }
            } compactLeading: {
                Image(systemName: "fork.knife")
                    .foregroundStyle(Color.cavnarEmber)
                    .cavnarForcedDark()
            } compactTrailing: {
                ServicePulse(state: context.state, size: 13, compact: true).cavnarForcedDark()
            } minimal: {
                Image(systemName: "fork.knife")
                    .foregroundStyle(Color.cavnarEmber)
                    .cavnarForcedDark()
            }
            .widgetURL(context.attributes.link)
        }
    }
}

/// "▲ 8%" in green or "▼ 12%" in red — the percent in Space Grotesk; "—"
/// while the pulse can't say.
private struct ServicePulse: View {
    let state: ServiceAttributes.ContentState
    var size: CGFloat
    var compact = false

    var body: some View {
        if let line = state.pulseLine {
            Text(compact ? (line.components(separatedBy: " vs ").first ?? line) : line)
                .font(.cavnarNumber(size, weight: 600))
                .foregroundStyle(state.pulseUp == true ? Color.cavnarGreen
                                 : state.pulseUp == false ? Color.cavnarRed : Color.cavnarInk)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
        } else {
            Text("—")
                .font(.cavnarNumber(size, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
        }
    }
}

struct ServiceLockView: View {
    let attributes: ServiceAttributes
    let state: ServiceAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("TONIGHT\u{2019}S SERVICE")
                    .font(.cavnarBody(CavnarType.tag, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarEmber2)
                Text(attributes.dayLabel)
                    .font(.cavnarNumber(12, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer(minLength: 4)
                if state.status == "closed" {
                    Text("Closed")
                        .font(.cavnarBody(12, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                } else if let closes = state.closesAt, closes > Date() {
                    HStack(spacing: 3) {
                        Text("closes in").font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
                        Text(timerInterval: Date()...closes, countsDown: true)
                            .font(.cavnarNumber(12, weight: 600))
                            .foregroundStyle(Color.cavnarInk2)
                            .monospacedDigit()
                            .frame(maxWidth: 64, alignment: .trailing)
                    }
                }
            }
            if !attributes.restaurantName.isEmpty {
                Text(attributes.restaurantName)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
            }
            if state.pulseLine != nil {
                ServicePulse(state: state, size: 20)
            } else {
                Text(state.pulseNote ?? "No pulse yet")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
                    .lineLimit(1)
            }
            Text(state.coverageLine)
                .font(.cavnarBody(13, weight: (state.missingCount ?? state.missing.count) > 0 ? 600 : 400))
                .foregroundStyle((state.missingCount ?? state.missing.count) > 0 ? Color.cavnarAmber : Color.cavnarInk3)
                .lineLimit(2)
                .privacySensitive()
        }
    }
}
