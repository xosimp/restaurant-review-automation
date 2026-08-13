import SwiftUI
import UIKit

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
    // Set together (both non-nil) to size this Text to an exact,
    // pre-measured width via cavnarMeasuredTextWidth instead of letting it
    // report its own ideal size — AskCavnarView's ChatBubble uses this so
    // the bubble hugs short answers instead of always claiming maxWidth;
    // see cavnarMeasuredTextWidth's comment for why. nil by default, so
    // existing call sites (AIConsultantView's insight intro, a plain
    // full-width wrapping block) are unaffected.
    var maxWidth: CGFloat? = nil
    var measuringFont: UIFont? = nil
    // Fires after every word becomes visible — lets a caller whose layout
    // grows as this reveals (a chat bubble that needs to stay scrolled into
    // view as it types out, for instance) react to that growth as it
    // happens, not just once when the full text first arrives. nil by
    // default so every existing call site (AIConsultantView's insight
    // boxes) is unaffected.
    var onReveal: (() -> Void)? = nil

    @State private var visibleWordCount = 0

    private var words: [String] { fullText.split(separator: " ").map(String.init) }

    // Measured once against the FULL final text (not the partially-revealed
    // string), so the bubble's width is fixed for the whole reveal instead
    // of jittering wider/narrower word by word — only height should
    // visibly grow as it types.
    private var resolvedWidth: CGFloat? {
        guard let maxWidth, let measuringFont else { return nil }
        return cavnarMeasuredTextWidth(fullText, font: measuringFont, maxWidth: maxWidth)
    }

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
            // nil when maxWidth/measuringFont aren't both set — frame(width:
            // nil) is a no-op, so this only takes effect for callers that
            // opted in.
            .frame(width: resolvedWidth, alignment: .leading)
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
