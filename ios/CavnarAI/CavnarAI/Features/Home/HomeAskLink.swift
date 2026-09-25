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
    @Environment(DeepLinkRouter.self) private var router

    var body: some View {
        Button {
            Haptic.light()
            // RootView observes the prompt itself, switches to Ask and sends.
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
