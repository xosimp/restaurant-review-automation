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
    /// True for a message that has already fully played its reveal once
    /// before (tracked by the caller, e.g. across a sheet dismiss/reopen
    /// where this view's own @State doesn't survive) — renders the full
    /// text immediately instead of animating it again.
    var startRevealed: Bool = false
    /// Fires once, the moment the reveal actually finishes animating (not
    /// when startRevealed skips straight to the end) — lets the caller
    /// record that this message need never retype again.
    var onComplete: (() -> Void)? = nil

    /// Precomputed cumulative prefixes plus the timing to walk through
    /// them, built once per fullText rather than every frame.
    private struct Plan {
        let prefixes: [String]
        let total: Int
        let step: Double
        let wordsPerTick: Int

        func revealedCount(elapsed: Double) -> Int {
            guard step > 0, elapsed > 0 else { return 0 }
            return min(total, Int(elapsed / step) * wordsPerTick)
        }
    }

    @State private var plan: Plan?
    @State private var start = Date()
    /// Measured once per message rather than per frame. As a computed
    /// property this was a full TextKit layout pass (NSString.boundingRect
    /// over the whole answer) on every body evaluation (audit 3.2).
    @State private var measuredWidth: CGFloat?

    var body: some View {
        Group {
            // Already fully played: render once, no TimelineView at all —
            // a reopened chat's history is all in this branch, and it
            // costs nothing per frame.
            if startRevealed {
                textView(fullText)
            } else if let plan {
                // TimelineView, not a Task.sleep loop — the previous
                // version advanced the reveal by sleeping between steps,
                // which drifts under any main-thread contention (a scroll,
                // another tool-call frame painting) and reads as choppy,
                // uneven pacing. Deriving the revealed word count purely
                // from elapsed wall-clock time on every scheduled tick
                // means a delayed frame just catches up to where it should
                // already be instead of compounding a lag — the same
                // wall-clock-driven approach this app's own motion (orbs,
                // HomeObsidianField) already uses, applied here to text.
                TimelineView(.periodic(from: start, by: 1.0 / 30.0)) { timeline in
                    let revealed = plan.revealedCount(elapsed: timeline.date.timeIntervalSince(start))
                    textView(plan.prefixes[revealed])
                        .onChange(of: revealed) { _, new in
                            onReveal?()
                            if new >= plan.total { onComplete?() }
                        }
                }
            } else {
                // One frame only, before .task below builds the plan —
                // empty, not the full text, so a new answer never flashes
                // its whole text before dropping back to typing it out.
                textView("")
            }
        }
        .task(id: fullText) {
            if let maxWidth, let measuringFont {
                measuredWidth = cavnarMeasuredTextWidth(fullText, font: measuringFont, maxWidth: maxWidth)
            }
            guard !startRevealed else {
                plan = nil
                return
            }
            let split = fullText.split(separator: " ").map(String.init)
            let total = split.count
            guard total > 0 else {
                plan = nil
                onComplete?()
                return
            }

            // Build every prefix once, up front.
            var running: [String] = [""]
            running.reserveCapacity(total + 1)
            var accumulated = ""
            for word in split {
                accumulated += accumulated.isEmpty ? word : " " + word
                running.append(accumulated)
            }

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

            start = Date()
            plan = Plan(prefixes: running, total: total, step: step, wordsPerTick: wordsPerTick)
        }
    }

    private func textView(_ text: String) -> some View {
        Text(text)
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
    }
}
