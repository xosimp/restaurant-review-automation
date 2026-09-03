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
    /// Precomputed cumulative prefixes: `prefixes[i]` is the first i words
    /// already joined. Rendering frame i is then an O(1) array read.
    ///
    /// `words` used to be a computed property, so every one of n body
    /// evaluations re-split the entire answer AND rebuilt a growing joined
    /// string — O(n²) character work across a reveal, on the main actor,
    /// between animation frames (audit 3.1).
    @State private var prefixes: [String] = [""]
    /// Measured once per message rather than per frame. As a computed
    /// property this was a full TextKit layout pass (NSString.boundingRect
    /// over the whole answer) on every body evaluation (audit 3.2).
    @State private var measuredWidth: CGFloat?


    var body: some View {
        // Measured against the FULL final text (not the partially-revealed
        // string), so the bubble's width is fixed for the whole reveal
        // instead of jittering wider/narrower word by word — only height
        // grows as it types.
        Text(prefixes.indices.contains(visibleWordCount) ? prefixes[visibleWordCount] : fullText)
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
            .frame(width: measuredWidth, alignment: .leading)
            .task(id: fullText) {
                let split = fullText.split(separator: " ").map(String.init)

                // Build every prefix once, up front.
                var running: [String] = [""]
                running.reserveCapacity(split.count + 1)
                var accumulated = ""
                for word in split {
                    accumulated += accumulated.isEmpty ? word : " " + word
                    running.append(accumulated)
                }
                prefixes = running

                if let maxWidth, let measuringFont {
                    measuredWidth = cavnarMeasuredTextWidth(fullText, font: measuringFont, maxWidth: maxWidth)
                }

                visibleWordCount = 0
                let total = split.count
                guard total > 0 else { return }

                // Total reveal duration is CAPPED, not per-word. The old
                // formula clamped the per-word delay at a 16ms floor, so
                // total time grew linearly past ~87 words: a typical 240-word
                // answer spent 3.8s, and a long one 6.4s, typing out text the
                // device had already received in full — pure added latency on
                // top of generation (audit 5.3). Past the cap, whole words are
                // revealed per tick instead of slowing the whole thing down.
                let targetDuration = 1.4
                let minStep = 0.016
                let rawStep = targetDuration / Double(total)
                let step = min(max(rawStep, minStep), 0.055)
                // How many words to advance per tick to still finish on time.
                let wordsPerTick = max(1, Int((minStep / rawStep).rounded(.up)))
                let delayNanos = UInt64(step * 1_000_000_000)

                var revealed = 0
                while revealed < total {
                    try? await Task.sleep(nanoseconds: delayNanos)
                    if Task.isCancelled { return }
                    revealed = min(revealed + wordsPerTick, total)
                    visibleWordCount = revealed
                    onReveal?()
                }
            }
    }
}
