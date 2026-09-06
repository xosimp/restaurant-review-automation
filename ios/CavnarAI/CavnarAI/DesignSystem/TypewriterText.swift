import SwiftUI
import UIKit

/// Word-by-word reveal mirroring the web dashboard's typewriterEffect() —
/// total duration ~1400ms split across the word count, clamped to a
/// 16-55ms per-word pace so very short or very long text both read
/// naturally. Uses .task(id:) so a new fullText value (e.g. pull-to-
/// refresh landing a fresh insight) automatically cancels any reveal
/// still in flight instead of racing it — the same guard the web version
/// does by hand with a per-element token counter.
///
/// Renders CavnarMarkdown's blocks (paragraphs, bullets, numbered lists,
/// headings) rather than a flat string — the reveal advances by TOKEN
/// across the whole block sequence, in order, so a list types out the
/// same way a plain paragraph always did, just block by block.
struct TypewriterText: View {
    let fullText: String
    var size: CGFloat = 16
    var color: Color
    var lineSpacing: CGFloat = 4
    // Set together (both non-nil) to size this Text to an exact,
    // pre-measured width via cavnarMeasuredTextWidth instead of letting it
    // report its own ideal size — AskCavnarView's ChatBubble uses this so
    // the bubble hugs short answers instead of always claiming maxWidth.
    // Only takes effect for a single-paragraph answer (see Plan below) —
    // a list or a multi-paragraph answer always uses the full width, since
    // hugging a multi-line block layout the way a single Text can doesn't
    // make sense the same way.
    var maxWidth: CGFloat? = nil
    var measuringFont: UIFont? = nil
    // Fires after every reveal tick — lets a caller whose layout grows as
    // this reveals (a chat bubble that needs to stay scrolled into view as
    // it types out) react to that growth as it happens.
    var onReveal: (() -> Void)? = nil
    /// True for a message that has already fully played its reveal once
    /// before (tracked by the caller, e.g. across a sheet dismiss/reopen
    /// where this view's own @State doesn't survive) — renders the full
    /// content immediately instead of animating it again.
    var startRevealed: Bool = false
    /// Fires once, the moment the reveal actually finishes animating (not
    /// when startRevealed skips straight to the end) — lets the caller
    /// record that this message need never retype again.
    var onComplete: (() -> Void)? = nil

    /// Precomputed parse + timing, built once per fullText rather than
    /// every frame.
    private struct Plan {
        let blocks: [CavnarMarkdown.Block]
        let totalTokens: Int
        let step: Double
        let tokensPerTick: Int
        /// Only set (and only used) when the whole answer is one plain
        /// paragraph — the "hug width" case.
        let measuredWidth: CGFloat?

        func revealedCount(elapsed: Double) -> Int {
            guard step > 0, elapsed > 0 else { return 0 }
            return min(totalTokens, Int(elapsed / step) * tokensPerTick)
        }

        /// Blocks visible at a given token budget: every fully-covered
        /// block in full, then the one block straddling the budget
        /// truncated to its share, then nothing after it.
        func visibleBlocks(revealedTokens: Int) -> [CavnarMarkdown.Block] {
            var remaining = revealedTokens
            var out: [CavnarMarkdown.Block] = []
            for block in blocks {
                guard remaining > 0 else { break }
                if remaining >= block.tokenCount {
                    out.append(block)
                    remaining -= block.tokenCount
                } else {
                    out.append(block.truncated(to: remaining))
                    break
                }
            }
            return out
        }
    }

    @State private var plan: Plan?
    @State private var start = Date()

    var body: some View {
        Group {
            // Already fully played: render once, no TimelineView at all —
            // a reopened chat's history is all in this branch, and it
            // costs nothing per frame.
            if startRevealed {
                let blocks = CavnarMarkdown.parse(fullText)
                content(blocks, width: hugWidth(blocks))
            } else if let plan {
                // TimelineView, not a Task.sleep loop — sleeping between
                // steps drifts under any main-thread contention and reads
                // as choppy, uneven pacing. Deriving the revealed token
                // count purely from elapsed wall-clock time on every
                // scheduled tick means a delayed frame just catches up to
                // where it should already be, the same wall-clock-driven
                // approach this app's own motion (orbs, HomeObsidianField)
                // already uses, applied here to text.
                TimelineView(.periodic(from: start, by: 1.0 / 30.0)) { timeline in
                    let revealed = plan.revealedCount(elapsed: timeline.date.timeIntervalSince(start))
                    let blocks = plan.visibleBlocks(revealedTokens: revealed)
                    content(blocks, width: plan.measuredWidth)
                        .onChange(of: revealed) { _, new in
                            onReveal?()
                            if new >= plan.totalTokens { onComplete?() }
                        }
                }
            } else {
                // A REAL (zero-size) node, not nothing. This branch is the
                // one frame between the message arriving and .task below
                // building the plan — and if every branch of this Group
                // resolves to nothing, the Group is an EmptyView, which has
                // no render node for `.task` to attach to, so the task
                // never runs, the plan is never built, and the bubble stays
                // blank forever. That is exactly what happened when this
                // fallback was dropped during the rich-text rewrite: every
                // answer rendered its "CAVNAR AI" label and no text at all.
                Color.clear.frame(width: 0, height: 0)
            }
        }
        .task(id: fullText) {
            guard !startRevealed else {
                plan = nil
                return
            }
            let blocks = CavnarMarkdown.parse(fullText)
            let total = blocks.reduce(0) { $0 + $1.tokenCount }
            guard total > 0 else {
                plan = nil
                onComplete?()
                return
            }

            // Total reveal duration is CAPPED, not per-word. Past the cap,
            // multiple tokens are revealed per tick instead of slowing the
            // whole thing down (audit 5.3).
            let targetDuration = 1.4
            let minStep = 0.016
            let rawStep = targetDuration / Double(total)
            let step = min(max(rawStep, minStep), 0.055)
            let tokensPerTick = max(1, Int((minStep / rawStep).rounded(.up)))

            start = Date()
            plan = Plan(blocks: blocks, totalTokens: total, step: step, tokensPerTick: tokensPerTick,
                       measuredWidth: hugWidth(blocks))
        }
    }

    /// The pre-measured width for the "hug short answers" behavior — only
    /// for a single plain paragraph; a list or multi-paragraph answer
    /// always takes the full available width.
    private func hugWidth(_ blocks: [CavnarMarkdown.Block]) -> CGFloat? {
        guard let maxWidth, let measuringFont,
              blocks.count == 1, blocks[0].kind == .paragraph else { return nil }
        let plain = blocks[0].tokens.map(\.text).joined(separator: " ")
        return cavnarMeasuredTextWidth(plain, font: measuringFont, maxWidth: maxWidth)
    }

    private func content(_ blocks: [CavnarMarkdown.Block], width: CGFloat?) -> some View {
        CavnarMarkdown.render(blocks, size: size, color: color, lineSpacing: lineSpacing)
            // nil when hugWidth doesn't apply — frame(width: nil) is a
            // no-op, so this only takes effect for a single short paragraph.
            .frame(width: width, alignment: .leading)
    }
}
