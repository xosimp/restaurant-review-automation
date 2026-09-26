import SwiftUI

/// "Ask about this →" — the affordance every Home card that carries a
/// question was missing on the phone.
///
/// The web feeds each card's question into the Ask panel (`data-ask`); iOS
/// rendered the same numbers with nowhere to take them. The server now
/// sends the question with the card (`ask` on a monthly metric, a
/// cross-module link, a good-news item), so both surfaces ask Cavnar the
/// same sentence. The tap on "Ask about this" was the decision, so it sends,
/// as the web's hbAsk does (friction audit #15, 9/25/26) — it used to land
/// on Ask with the text in the box and wait for a second tap. Only a plain
/// tap on a brief notification's body still just fills it in.
struct HomeAskLink: View {
    let question: String
    var label: String = "Ask about this"
    /// Where the question was asked from — sent with it so "this" resolves
    /// to the review, recommendation or card on screen, as the web's
    /// askScreen() does. Nil for a question that stands on its own.
    var screen: AskScreen? = nil
    @Environment(DeepLinkRouter.self) private var router

    var body: some View {
        Button {
            Haptic.light()
            // RootView observes the prompt itself, switches to Ask and sends.
            router.pendingAskScreen = screen
            router.pendingAskAutoSend = true
            router.pendingAskPrompt = question
        } label: {
            Text(label + " →")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Asks Cavnar AI this question")
    }
}

/// The `screen` block of an Ask request: the panel the owner was on and the
/// item they were looking at. The server's ask_cavnar.screen_hint validates
/// it and treats it as context, never as an instruction. Panels use the web's
/// names (reviews, labor, inventory, marketing, competitor, dsr, recs, home);
/// entity types the server resolves are "review" (a numeric id) and "rec"
/// (a recommendation key).
struct AskScreen: Encodable, Equatable {
    struct Entity: Encodable, Equatable {
        let type: String
        let id: String
    }
    let panel: String
    var entity: Entity? = nil

    init(panel: String, entityType: String? = nil, entityId: String? = nil) {
        self.panel = panel
        if let entityType, let entityId, !entityId.isEmpty {
            entity = Entity(type: entityType, id: entityId)
        }
    }
}
