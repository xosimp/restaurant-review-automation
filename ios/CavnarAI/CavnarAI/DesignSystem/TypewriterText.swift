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

    @State private var visibleWordCount = 0

    private var words: [String] { fullText.split(separator: " ").map(String.init) }

    var body: some View {
        Text(words.prefix(visibleWordCount).joined(separator: " "))
            .font(font)
            .foregroundStyle(color)
            .lineSpacing(lineSpacing)
            .task(id: fullText) {
                visibleWordCount = 0
                let total = words.count
                guard total > 0 else { return }
                let delayNanos = UInt64(min(max(1400.0 / Double(total), 16), 55) * 1_000_000)
                for i in 1...total {
                    try? await Task.sleep(nanoseconds: delayNanos)
                    if Task.isCancelled { return }
                    visibleWordCount = i
                }
            }
    }
}
