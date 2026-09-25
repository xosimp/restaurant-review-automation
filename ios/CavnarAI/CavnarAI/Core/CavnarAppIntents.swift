import AppIntents
import Foundation

/// App Shortcuts: Siri, Spotlight, the Action button and the Shortcuts app
/// reach the same places the Home Screen quick actions do (Friction audit
/// #31, U3-12 b). Every intent but Undo only OPENS the app somewhere — it
/// never sends, posts or approves on its own; anything outward still meets
/// the confirm card inside the app. The lock screen still stands between
/// Siri and the data: the destination waits in SystemEntry until the scene
/// is active, and the app's own Face ID lock runs first.

struct OpenAskCavnarIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Cavnar AI"
    static let description = IntentDescription("Opens Ask Cavnar AI, with your question in the box.")
    static let openAppWhenRun: Bool = true

    @Parameter(title: "Question")
    var question: String?

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        if let path = SystemEntry.askPath(question) { SystemEntry.open(path) }
        return .result()
    }
}

struct OpenLastNightIntent: AppIntent {
    static let title: LocalizedStringResource = "Last night's sales"
    static let description = IntentDescription("Opens last night's daily sales report.")
    static let openAppWhenRun: Bool = true

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        SystemEntry.open(QuickAction.lastNight.destination)
        return .result()
    }
}

struct OpenReplyQueueIntent: AppIntent {
    static let title: LocalizedStringResource = "Approve drafted replies"
    static let description = IntentDescription("Opens the reviews waiting on a reply, drafted replies first.")
    static let openAppWhenRun: Bool = true

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        SystemEntry.open(QuickAction.approveReplies.destination)
        return .result()
    }
}

struct ScanInvoiceIntent: AppIntent {
    static let title: LocalizedStringResource = "Scan an invoice"
    static let description = IntentDescription("Opens the camera to read a supplier invoice into Food Cost.")
    static let openAppWhenRun: Bool = true

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        SystemEntry.open(QuickAction.scanInvoice.destination)
        return .result()
    }
}

struct OpenCommandSheetIntent: AppIntent {
    static let title: LocalizedStringResource = "Find in Cavnar AI"
    static let description = IntentDescription("Opens Cavnar AI's command sheet: what's waiting on you, places, people and Ask.")
    static let openAppWhenRun: Bool = true

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        SystemEntry.open(.commandSheet)
        return .result()
    }
}

/// "Undo the pending publish" — cancels the soonest schedule or supplier
/// order Cavnar AI is about to send. Needs an unlocked device.
struct UndoSoonestPendingSendIntent: AppIntent {
    static let title: LocalizedStringResource = "Undo the pending publish"
    static let description = IntentDescription("Stops the next schedule or supplier order Cavnar AI is about to send.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun: Bool = false

    init() {}

    func perform() async throws -> some IntentResult & ProvidesDialog {
        guard let next = await PendingSendCanceller.soonestPending() else {
            return .result(dialog: "Nothing is waiting to go out.")
        }
        let outcome = await PendingSendCanceller.cancel(actionId: next.id)
        let what = next.label?.isEmpty == false ? next.label! : PendingSendAttributes.plainTitle(kind: next.kind)
        return .result(dialog: "\(outcome.stopped ? "\(what): stopped. Nothing went out." : outcome.sentence)")
    }
}

struct CavnarShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: OpenAskCavnarIntent(),
                    phrases: ["Ask \(.applicationName)", "Ask \(.applicationName) a question"],
                    shortTitle: "Ask Cavnar AI", systemImageName: "sparkles")
        AppShortcut(intent: OpenLastNightIntent(),
                    phrases: ["How did last night go in \(.applicationName)",
                              "Last night's sales in \(.applicationName)"],
                    shortTitle: "Last night", systemImageName: "chart.bar.doc.horizontal")
        AppShortcut(intent: OpenReplyQueueIntent(),
                    phrases: ["Approve replies in \(.applicationName)",
                              "Approve drafted replies in \(.applicationName)"],
                    shortTitle: "Approve replies", systemImageName: "checkmark.bubble")
        AppShortcut(intent: ScanInvoiceIntent(),
                    phrases: ["Scan an invoice in \(.applicationName)"],
                    shortTitle: "Scan invoice", systemImageName: "doc.text.viewfinder")
        AppShortcut(intent: UndoSoonestPendingSendIntent(),
                    phrases: ["Undo the pending publish in \(.applicationName)",
                              "Stop the \(.applicationName) send"],
                    shortTitle: "Undo pending send", systemImageName: "arrow.uturn.backward")
        AppShortcut(intent: OpenCommandSheetIntent(),
                    phrases: ["Find in \(.applicationName)", "Search \(.applicationName)"],
                    shortTitle: "Find in Cavnar AI", systemImageName: "magnifyingglass")
    }
}
