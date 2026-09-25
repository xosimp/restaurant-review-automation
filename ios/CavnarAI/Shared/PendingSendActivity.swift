import ActivityKit
import AppIntents
import Foundation

/// The auto-publish / supplier-order countdown on the Lock Screen and in the
/// Dynamic Island, with Undo (Friction audit #47, U3-12 e). The countdown is
/// a delayed action (delayed.py): the product is about to send something on
/// its own, and until `fireAt` the owner can stop it.
///
/// Compiled into both targets: the app starts and ends the activity
/// (PendingSendActivities), the widget extension draws it.
struct PendingSendAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        /// When it goes out (the delayed row's execute_at).
        var fireAt: Date
        /// pending | stopped | sent | failed
        var status: String
        /// The server's own sentence after an Undo that didn't take
        /// ("That already went out, or was already undone.").
        var note: String?
    }

    /// delayed_actions.id — what Undo cancels.
    let actionId: Int
    /// schedule_publish | order_send
    let kind: String
    /// The row's own label, or a plain one for its kind.
    let title: String

    /// "Schedule goes out" / "Order goes out" — the kicker.
    var kicker: String {
        kind == "order_send" ? "Supplier order goes out" : "Schedule goes out"
    }

    /// The delayed kinds that send something outside the restaurant and so
    /// earn a countdown. Anything else is left to the in-app activity feed.
    static let countdownKinds: Set<String> = ["schedule_publish", "order_send"]

    static func plainTitle(kind: String) -> String {
        kind == "order_send" ? "Supplier order" : "Next week's schedule"
    }
}

/// Undo from the Lock Screen, the Dynamic Island or Siri ("Undo the pending
/// publish"). Cancelling a pending send is the safe direction — nothing has
/// gone out — so it runs at once; the device must still be unlocked, the
/// same bar U3-5 set for acting from a notification.
///
/// A LiveActivityIntent runs in the APP's process, so the network call below
/// only exists in the app build; the widget extension compiles the type (its
/// button needs it) but never performs it and never sees a token.
struct UndoPendingSendIntent: LiveActivityIntent {
    static let title: LocalizedStringResource = "Undo the pending send"
    static let description = IntentDescription("Stops a schedule or supplier order Cavnar AI is about to send.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun: Bool = false

    @Parameter(title: "Action")
    var actionId: Int

    init() {}
    init(actionId: Int) { self.actionId = actionId }

    func perform() async throws -> some IntentResult & ProvidesDialog {
        #if CAVNAR_WIDGET_EXTENSION
        return .result(dialog: "Open Cavnar AI to undo this.")
        #else
        let outcome = await PendingSendCanceller.cancel(actionId: actionId)
        return .result(dialog: "\(outcome.sentence)")
        #endif
    }
}
