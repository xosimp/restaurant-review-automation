import SwiftUI

/// Word-by-word reveal mirroring the web dashboard's typewriterEffect() —
/// total duration ~1400ms split across the word count, clamped to a
/// 16-55ms per-word pace so very short or very long text both read
/// naturally. Uses .task(id:) so a new fullText value (e.g. pull-to-
/// refresh landing a fresh insight) automatically cancels any reveal
/// still in flight instead of racing it — the same guard the web version
/// does by hand with a per-element token counter.
struct TypewriterText: View {
    let fullText: String
    var font: Font
    var color: Color
    var lineSpacing: CGFloat = 4
    // Fires after every word becomes visible — lets a caller whose layout
    // grows as this reveals (a chat bubble that needs to stay scrolled into
    // view as it types out, for instance) react to that growth as it
    // happens, not just once when the full text first arrives. nil by
    // default so every existing call site (AIConsultantView's insight
    // boxes) is unaffected.
    var onReveal: (() -> Void)? = nil

    @State private var visibleWordCount = 0

    private var words: [String] { fullText.split(separator: " ").map(String.init) }

    var body: some View {
        Text(words.prefix(visibleWordCount).joined(separator: " "))
            .font(font)
            .foregroundStyle(color)
            .lineSpacing(lineSpacing)
            // Without this, a Text sitting inside a width-constrained
            // container (like a chat bubble's `.frame(maxWidth:)`) can
            // report an inflated ideal width to its parent instead of its
            // true (wrapped) content size — the parent then sizes itself
            // to that inflated proposal rather than shrinking to fit a
            // short string. This forces Text to always report its real
            // wrapped size: it can still grow taller (multi-line), just
            // never wider than its content actually needs.
            .fixedSize(horizontal: false, vertical: true)
            .task(id: fullText) {
                visibleWordCount = 0
                let total = words.count
                guard total > 0 else { return }
                let delayNanos = UInt64(min(max(1400.0 / Double(total), 16), 55) * 1_000_000)
                for i in 1...total {
                    try? await Task.sleep(nanoseconds: delayNanos)
                    if Task.isCancelled { return }
                    visibleWordCount = i
                    onReveal?()
                }
            }
    }
}
