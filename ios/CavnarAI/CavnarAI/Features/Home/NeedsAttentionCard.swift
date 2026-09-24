import SwiftUI

/// "All clear" — only when the server says it may be (OwnerCopy.allClear:
/// `monitoring.all_clear`, a live source, none stale). Otherwise the
/// reason, with no green tick: nothing flagged on stale data, or with no
/// live source at all, is not a clean bill (NS1 #8).
struct AllClearRow: View {
    /// Nil draws "All clear"; a sentence draws that instead.
    var notClearReason: String? = nil

    var body: some View {
        VStack(spacing: 8) {
            ZStack {
                Circle()
                    .fill((notClearReason == nil ? Color.cavnarGreen : Color.cavnarAmber).opacity(0.12))
                    .frame(width: 40, height: 40)
                Image(systemName: notClearReason == nil ? "checkmark" : "clock")
                    .foregroundStyle(notClearReason == nil ? Color.cavnarGreen : Color.cavnarAmber)
            }
            Text(notClearReason == nil ? "All clear" : "Nothing flagged")
                .font(.cavnarBody(15, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text(notClearReason.map { $0.replacingOccurrences(of: "Nothing flagged \u{2014} but ", with: "But ") }
                 ?? "Nothing needs your attention right now")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
    }
}

/// The "swipe hint" arrow — no text label (the gesture is discoverable
/// enough visually; "SWIPE" in tiny tracked-out caps was dead weight).
/// A single chevron drifts a few points to the right while fading, then
/// eases back — one slow, symmetric breath, no spring/bounce/scale. The
/// first attempt here (a capsule shaft popping up and extending) read as
/// busy/gimmicky; this reads closer to a native iOS coach-mark nudge.
/// Not private — HomePulseStrip reuses this exact motion for its own
/// horizontally-scrollable row rather than inventing a second nudge.
struct PulsingSwipeArrow: View {
    var size: CGFloat = 11
    @State private var start = Date()

    // Wall-clock driven (TimelineView), not a PhaseAnimator — the phase
    // animator's own transaction could be interrupted by a tab switch or
    // the Home tree re-rendering after a lock/unlock, after which it never
    // resumed and the chevron sat frozen. A sine over real time can't get
    // stuck: whatever frame this renders on, the position is just a
    // function of the current time.
    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30.0)) { timeline in
            let t = timeline.date.timeIntervalSince(start)
            // 0 -> 1 -> 0 over one 2.3s breath, ease-in-out shaped.
            let phase = 0.5 - 0.5 * cos(t * 2 * .pi / 2.3)
            Image(systemName: "chevron.right")
                .font(.system(size: size, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber2.opacity(0.65 - 0.47 * phase))
                .offset(x: (size * 0.45) * phase)
        }
    }
}
