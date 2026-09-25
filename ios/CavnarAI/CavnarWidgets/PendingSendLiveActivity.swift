import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

/// "Next week's schedule goes out in 12:04 — Undo" on the Lock Screen and in
/// the Dynamic Island (Friction audit #47, U3-12 e). Undo runs
/// UndoPendingSendIntent in the app's process; the countdown is the system's
/// own timer text, so nothing here has to wake up every second.
struct PendingSendLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PendingSendAttributes.self) { context in
            PendingSendLockView(attributes: context.attributes, state: context.state)
                .padding(16)
                .activityBackgroundTint(Color.cavnarPaper)
                .activitySystemActionForegroundColor(Color.cavnarInk)
                .widgetURL(URL(string: "cavnarai://nav/action/\(context.attributes.actionId)"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: context.attributes.kind == "order_send" ? "shippingbox.fill" : "calendar")
                        .foregroundStyle(Color.cavnarEmber)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    PendingSendCountdown(state: context.state)
                        .font(.cavnarNumber(15, weight: 600))
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text(context.attributes.title)
                            .font(.cavnarBody(14, weight: 700))
                            .lineLimit(1)
                        Spacer()
                        PendingSendUndoButton(actionId: context.attributes.actionId, state: context.state)
                    }
                }
            } compactLeading: {
                Image(systemName: context.attributes.kind == "order_send" ? "shippingbox.fill" : "calendar")
                    .foregroundStyle(Color.cavnarEmber)
            } compactTrailing: {
                PendingSendCountdown(state: context.state)
                    .font(.cavnarNumber(13, weight: 600))
                    .frame(maxWidth: 52)
            } minimal: {
                Image(systemName: "arrow.uturn.backward")
                    .foregroundStyle(Color.cavnarEmber)
            }
            .widgetURL(URL(string: "cavnarai://nav/action/\(context.attributes.actionId)"))
        }
    }
}

private struct PendingSendLockView: View {
    let attributes: PendingSendAttributes
    let state: PendingSendAttributes.ContentState

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(attributes.kicker.uppercased())
                    .font(.cavnarBody(10.5, weight: 700))
                    .tracking(1.1)
                    .foregroundStyle(Color.cavnarEmber2)
                Text(attributes.title)
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .lineLimit(1)
                statusLine
            }
            Spacer(minLength: 8)
            PendingSendUndoButton(actionId: attributes.actionId, state: state)
        }
    }

    @ViewBuilder
    private var statusLine: some View {
        switch state.status {
        case "pending":
            HStack(spacing: 4) {
                Text("in").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                PendingSendCountdown(state: state)
                    .font(.cavnarNumber(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
            }
        case "stopped":
            Text("Stopped. Nothing went out.")
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(Color.cavnarGreen)
        default:
            Text(state.note ?? "Open Cavnar AI to see where it stands.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarAmber)
                .lineLimit(2)
        }
    }
}

private struct PendingSendCountdown: View {
    let state: PendingSendAttributes.ContentState

    var body: some View {
        if state.status == "pending", state.fireAt > Date() {
            Text(timerInterval: Date()...state.fireAt, countsDown: true)
                .monospacedDigit()
                .multilineTextAlignment(.trailing)
        } else {
            Text("—")
        }
    }
}

private struct PendingSendUndoButton: View {
    let actionId: Int
    let state: PendingSendAttributes.ContentState

    var body: some View {
        if state.status == "pending" {
            Button(intent: UndoPendingSendIntent(actionId: actionId)) {
                Text("Undo")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                    .background(Capsule().strokeBorder(Color.cavnarEmber.opacity(0.7), lineWidth: 1))
            }
            .buttonStyle(.plain)
        }
    }
}
