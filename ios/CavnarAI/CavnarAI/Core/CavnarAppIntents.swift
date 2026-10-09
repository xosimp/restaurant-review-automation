import AppIntents
import Foundation
import SwiftUI

/// App Shortcuts: Siri, Spotlight, the Action button and the Shortcuts app
/// reach the same places the Home Screen quick actions do (Friction audit
/// #31, U3-12 b). Every intent but Undo, "How was last night?" (which only
/// reads the widget snapshot aloud) and "Ask Cavnar AI" (which answers a
/// question in Siri, parity audit #58) only OPENS the app somewhere — none
/// of them sends, posts or approves on its own; anything outward, an Ask
/// proposal included, still meets the confirm card inside the app. The lock screen still stands between
/// Siri and the data: the destination waits in SystemEntry until the scene
/// is active, and the app's own Face ID lock runs first.

struct OpenAskCavnarIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Ask Cavnar AI"
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

/// "How was last night?" — answered aloud from the widget snapshot, without
/// opening the app: the net, the change against the same weekday last week,
/// the net against budget (a login allowed it) and the night's verdict.
/// Reads only what the app last wrote (WidgetSnapshot), never the API: an
/// intent that could sign in would be a second session to secure. Needs an
/// unlocked device — these are the restaurant's sales, spoken out loud.
struct LastNightSummaryIntent: AppIntent {
    static let title: LocalizedStringResource = "How was last night?"
    static let description = IntentDescription("Tells you last night's net sales, against last week and budget.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun: Bool = false

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult & ProvidesDialog {
        .result(dialog: "\(Self.answer(WidgetSnapshot.load(), restaurantId: SessionScope.activeRestaurantId))")
    }

    /// The sentence, or why there isn't one. A snapshot from another
    /// location than the one this phone is on answers nothing.
    static func answer(_ snapshot: WidgetSnapshot?, restaurantId: Int, now: Date = Date()) -> String {
        guard let snapshot, restaurantId > 0 else {
            return "Sign in to Cavnar AI to hear last night's numbers."
        }
        if let stamped = snapshot.restaurantId, stamped != restaurantId {
            return "Open Cavnar AI to refresh last night's numbers."
        }
        return snapshot.lastNightSentence(now: now)
            ?? "There's no report for last night yet. Open Cavnar AI to check on it."
    }
}

// MARK: - Ask Cavnar AI, answered in Siri (parity audit #58)

/// "Ask Cavnar AI" — Siri asks for the question, Cavnar AI answers it, and
/// the answer is spoken and shown in a snippet without opening the app. The
/// same route and the same tools as Ask's own screen (POST
/// /mobile/api/ask-cavnar, a new conversation, so it is in Ask's history
/// afterwards). An answer that proposes an action never acts: Siri says
/// what was proposed and that it is waiting in Ask, where the confirm card
/// is. Needs an unlocked device — the restaurant's figures, out loud.
///
/// With the app's own lock on (Face ID to reopen, or an app passcode) the
/// answer is never given outside the app: nothing is asked, and Siri says
/// to open Cavnar AI, whose lock runs first (re-audit 10/8/26 #8). An
/// unlocked iPhone handed to someone else used to answer from the
/// restaurant's figures with no app lock at all.
struct AskCavnarAnswerIntent: AppIntent {
    static let title: LocalizedStringResource = "Ask Cavnar AI"
    static let description = IntentDescription("Asks Cavnar AI about your restaurant and tells you the answer.")
    static let authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication
    static let openAppWhenRun: Bool = false

    @Parameter(title: "Question", requestValueDialog: "What would you like to ask Cavnar AI?")
    var question: String

    init() {}
    init(question: String) { self.question = question }

    func perform() async throws -> some IntentResult & ProvidesDialog & ShowsSnippetView {
        let outcome = SessionStore.appLockConfigured ? SiriAsk.locked : await SiriAsk.ask(question)
        return .result(dialog: IntentDialog(stringLiteral: outcome.spoken),
                       view: SiriAskSnippet(question: question, outcome: outcome))
    }
}

/// The body POST /mobile/api/ask-cavnar reads — every key the route takes
/// (mobile_api.mobile_ask_cavnar): a fresh conversation, no history, no
/// screen it was asked from.
struct SiriAskBody: Encodable, Equatable {
    let question: String
    let history: [String]
    let conversationId: Int?
    let newConversation: Bool
    let screen: String?

    enum CodingKeys: String, CodingKey {
        case question, history, screen
        case conversationId = "conversation_id"
        case newConversation = "new_conversation"
    }

    init(question: String) {
        self.question = question
        self.history = []
        self.conversationId = nil
        self.newConversation = true
        self.screen = nil
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(question, forKey: .question)
        try c.encode(history, forKey: .history)
        try c.encode(conversationId, forKey: .conversationId)
        try c.encode(newConversation, forKey: .newConversation)
        try c.encode(screen, forKey: .screen)
    }
}

enum SiriAsk {
    struct Outcome: Equatable {
        /// What Siri says: the answer as plain sentences, clipped.
        let spoken: String
        /// What the snippet shows: the answer, markdown removed.
        let text: String
        /// How many actions the answer proposed — none were taken.
        let proposals: Int
        let ok: Bool
    }

    /// Only what Siri needs from the answer. A proposal is counted, never
    /// decoded into something that could be run.
    struct Response: Decodable {
        struct Ignored: Decodable { init(from decoder: Decoder) throws {} }
        let ok: Bool
        let answer: String?
        let error: String?
        let proposals: [Ignored]?
        /// The iPhone card (iOS readability round #92) — the mobile route
        /// writes to the phone's contract — and the answer's measured
        /// confidence, so Siri says the headline, the first thing to do and
        /// how sure, not the first 600 characters of an essay.
        var card: AskLenientCard? = nil
        var confidenceDetail: TrustConfidence? = nil

        enum CodingKeys: String, CodingKey {
            case ok, answer, error, proposals, card
            case confidenceDetail = "confidence_detail"
        }
    }

    /// The longest answer Siri reads out; the snippet shows all of it.
    static let spokenLimit = 600

    /// What Siri says when the app's lock is on (SessionStore.appLockConfigured):
    /// no question is sent, no figure is read out.
    static let locked = Outcome(
        spoken: "Cavnar AI is locked with Face ID or a passcode on this iPhone. Open Cavnar AI to ask there.",
        text: "Cavnar AI is locked with Face ID or a passcode on this iPhone. Open Cavnar AI to ask there.",
        proposals: 0, ok: false)

    static func ask(_ question: String, client: APIClient = .shared) async -> Outcome {
        let q = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty else {
            return Outcome(spoken: "Ask me a question about your restaurant.", text: "", proposals: 0, ok: false)
        }
        guard let bearer = Keychain.get(Keychain.Key.sessionToken), !bearer.isEmpty else {
            return Outcome(spoken: "Sign in to Cavnar AI on this iPhone first.", text: "", proposals: 0, ok: false)
        }
        do {
            let r: Response = try await client.sendWithBearer(
                "/mobile/api/ask-cavnar", method: .post, body: SiriAskBody(question: String(q.prefix(2000))),
                bearer: bearer)
            guard r.ok, let answer = r.answer, !answer.isEmpty else {
                let why = r.error ?? "Cavnar AI couldn\u{2019}t answer that just now."
                return Outcome(spoken: why, text: why, proposals: 0, ok: false)
            }
            return outcome(answer: answer, proposals: r.proposals?.count ?? 0,
                           card: r.card?.card, confidence: r.confidenceDetail)
        } catch let error as APIClient.APIError {
            return Outcome(spoken: error.message, text: error.message, proposals: 0, ok: false)
        } catch {
            let why = "Cavnar AI is still working on that. Open Ask to see the answer."
            return Outcome(spoken: why, text: why, proposals: 0, ok: false)
        }
    }

    static func outcome(answer: String, proposals: Int, card: AskCard? = nil,
                        confidence: TrustConfidence? = nil) -> Outcome {
        var text = plain(answer)
        var spoken = spokenLead(answer: answer, card: card, confidence: confidence)
        if let card {
            // The snippet shows the card, not the whole analysis.
            text = [card.headline, card.summary, card.action.map { "Do first: " + $0 },
                    card.outcome.map { "Expect: " + $0 }]
                .compactMap { $0 }.map(sentence).joined(separator: " ")
        }
        if spoken.count > spokenLimit { spoken = clip(spoken, to: spokenLimit) }
        if proposals > 0 {
            spoken += proposals == 1
                ? " Cavnar AI suggested one thing to do. It\u{2019}s waiting in Ask for you to look over; nothing was done."
                : " Cavnar AI suggested \(proposals) things to do. They\u{2019}re waiting in Ask for you to look over; nothing was done."
        }
        return Outcome(spoken: spoken, text: text, proposals: proposals, ok: true)
    }

    /// What Siri says first (#92): with a card, the headline, the first
    /// thing to do and how sure; without one, the answer's first sentence
    /// and its "Do first" line when it has one.
    static func spokenLead(answer: String, card: AskCard?, confidence: TrustConfidence?) -> String {
        var parts: [String] = []
        if let card {
            parts.append(card.headline)
            if let action = card.action { parts.append("Do first: " + action) }
        } else {
            let lines = answer.components(separatedBy: .newlines)
            let doFirst = lines.first { $0.range(of: #"^\s*\**\s*do\s+first\s*\**\s*:"#,
                                                  options: [.regularExpression, .caseInsensitive]) != nil }
            let body = lines.filter { $0 != doFirst }.joined(separator: "\n")
            if let first = firstSentence(plain(body)) { parts.append(first) }
            if let doFirst { parts.append(plain(doFirst)) }
        }
        if let confidence {
            let d = ConfidenceDisplay(confidence)
            if d.isRenderable, d.pct != nil { parts.append(d.lineLabel) }
        }
        return parts.map(sentence).joined(separator: " ")
    }

    /// The first sentence of plain text, or nil when there is none.
    static func firstSentence(_ text: String) -> String? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return nil }
        if let r = t.range(of: #"[.!?](\s|$)"#, options: .regularExpression) {
            return String(t[..<r.upperBound]).trimmingCharacters(in: .whitespaces)
        }
        return t
    }

    /// A line as a spoken sentence: ends in a full stop unless it already
    /// ends a sentence.
    static func sentence(_ s: String) -> String {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let last = t.last else { return t }
        return (last == "." || last == "?" || last == "!") ? t : t + "."
    }

    /// Markdown to plain sentences: no **bold**, # headings, bullets,
    /// backticks or link syntax read aloud.
    static func plain(_ markdown: String) -> String {
        var lines: [String] = []
        for raw in markdown.components(separatedBy: .newlines) {
            var line = raw.trimmingCharacters(in: .whitespaces)
            while line.hasPrefix("#") { line.removeFirst() }
            for bullet in ["- ", "* ", "\u{2022} "] where line.hasPrefix(bullet) {
                line = String(line.dropFirst(bullet.count))
            }
            line = line.replacingOccurrences(of: "**", with: "")
                .replacingOccurrences(of: "__", with: "")
                .replacingOccurrences(of: "`", with: "")
            line = line.replacingOccurrences(of: #"\[([^\]]+)\]\([^)]*\)"#, with: "$1", options: .regularExpression)
            line = line.trimmingCharacters(in: .whitespaces)
            if !line.isEmpty { lines.append(line) }
        }
        return lines.map { l in
            let last = l.last
            return (last == "." || last == "?" || last == "!" || last == ":") ? l : l + "."
        }.joined(separator: " ")
    }

    /// Cut at the last sentence end inside `limit`, else the last word.
    static func clip(_ text: String, to limit: Int) -> String {
        let head = String(text.prefix(limit))
        if let end = head.lastIndex(where: { $0 == "." || $0 == "?" || $0 == "!" }) {
            return String(head[...end])
        }
        if let space = head.lastIndex(of: " ") { return String(head[..<space]) + "\u{2026}" }
        return head + "\u{2026}"
    }
}

/// The answer as Siri shows it — dark, Cavnar AI's type.
struct SiriAskSnippet: View {
    let question: String
    let outcome: SiriAsk.Outcome

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarKicker("Ask Cavnar AI")
            Text(question)
                .cavnarText(.secondary)
                .lineLimit(2)
            CavnarMixedText(outcome.text.isEmpty ? outcome.spoken : outcome.text, role: .body,
                            color: outcome.ok ? Color.cavnarInk : Color.cavnarAmber)
                .lineLimit(14)
            if outcome.proposals > 0 {
                HomeMixedText.make(outcome.proposals == 1
                                   ? "1 suggestion waiting in Ask \u{2014} nothing was done."
                                   : "\(outcome.proposals) suggestions waiting in Ask \u{2014} nothing was done.",
                                   role: .secondary, color: .cavnarEmber2)
            }
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper)
        .environment(\.colorScheme, .dark)
    }
}

// MARK: - Switch location (parity audit #96)

/// "Switch to Wicker Park in Cavnar AI" — opens the app on that location,
/// the same switch the header's location picker makes (location/<id>).
struct SwitchLocationIntent: AppIntent {
    static let title: LocalizedStringResource = "Switch location"
    static let description = IntentDescription("Opens Cavnar AI on another of your locations.")
    static let openAppWhenRun: Bool = true

    @Parameter(title: "Location")
    var location: CavnarLocationEntity

    init() {}

    @MainActor
    func perform() async throws -> some IntentResult {
        if let path = NavPath("location/\(location.id)") { SystemEntry.open(path) }
        return .result()
    }
}

struct CavnarShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: AskCavnarAnswerIntent(),
                    phrases: ["Ask \(.applicationName)", "Ask \(.applicationName) a question"],
                    shortTitle: "Ask Cavnar AI", systemImageName: "sparkles")
        AppShortcut(intent: OpenAskCavnarIntent(),
                    phrases: ["Open Ask in \(.applicationName)"],
                    shortTitle: "Open Ask", systemImageName: "text.bubble")
        AppShortcut(intent: OpenLastNightIntent(),
                    phrases: ["How did last night go in \(.applicationName)",
                              "Last night's sales in \(.applicationName)"],
                    shortTitle: "Last night", systemImageName: "chart.bar.doc.horizontal")
        AppShortcut(intent: LastNightSummaryIntent(),
                    phrases: ["How was last night in \(.applicationName)",
                              "Last night's numbers from \(.applicationName)"],
                    shortTitle: "How was last night?", systemImageName: "dollarsign.circle")
        AppShortcut(intent: OpenReplyQueueIntent(),
                    phrases: ["Approve replies in \(.applicationName)",
                              "Approve drafted replies in \(.applicationName)",
                              "Open replies waiting in \(.applicationName)"],
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
        AppShortcut(intent: SwitchLocationIntent(),
                    phrases: ["Switch to \(\.$location) in \(.applicationName)",
                              "Switch locations in \(.applicationName)"],
                    shortTitle: "Switch location", systemImageName: "building.2")
    }
}
