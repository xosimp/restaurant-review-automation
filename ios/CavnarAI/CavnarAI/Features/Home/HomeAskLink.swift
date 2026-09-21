import SwiftUI

/// "Ask about this →" — the affordance every Home card that carries a
/// question was missing on the phone.
///
/// The web feeds each card's question into the Ask panel (`data-ask`); iOS
/// rendered the same numbers with nowhere to take them. The server now
/// sends the question with the card (`ask` on a monthly metric, a
/// cross-module link, a good-news item), so both surfaces ask Cavnar the
/// same sentence. Prefilled, never auto-sent: the owner decides whether to
/// ask it — the same rule the brief push follows (DeepLinkRouter).
struct HomeAskLink: View {
    let question: String
    var label: String = "Ask about this"
    @Environment(DeepLinkRouter.self) private var router

    var body: some View {
        Button {
            Haptic.light()
            // RootView observes the prompt itself and switches to Ask.
            router.pendingAskPrompt = question
        } label: {
            Text(label + " →")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
        }
        .buttonStyle(.plain)
        .accessibilityHint("Opens Ask Cavnar AI with this question")
    }
}
