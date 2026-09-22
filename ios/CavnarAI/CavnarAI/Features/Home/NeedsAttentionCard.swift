import SwiftUI

/// One floating Needs Attention card. Every card shares the same uniform ember wash
/// regardless of alert type — a per-type color mix read as chaotic rather
/// than "at a glance severity" (same call already made for the old row
/// design, still true here). Only the icon glyph varies by type.
struct NeedsAttentionFloatCard: View {
    let item: NeedsAttentionItem

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Same ember tile the module grid uses (GlowBadge), not the
            // old tinted circle — so an alert card's icon reads as part of
            // the same family as the tile it links to. The ripple that
            // used to fire here on first unlock (a thin ember ring
            // expanding out from the badge) was asked to go — device
            // feedback called it out by name ("orange thin circles that
            // animate around the badges").
            GlowBadge(systemImage: iconName, size: 36)
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title)
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                Text(item.detail)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 16)
        .padding(.horizontal, 14)
        // The card was never given its own height — only its enclosing
        // GeometryReader was, so the card itself just content-hugged to
        // whatever its badge + two text blocks needed (measured on a real
        // render: ~140pt) and sat centered in the leftover space, reading
        // as short/wide. Giving it an explicit height fixed that, but the
        // first value (180, matched exactly to the outer allowance) left
        // a lot of empty room below the text for the common single-line
        // case — this is trimmed down to the smallest height that still
        // comfortably fits a 2-line title + 2-line detail without either
        // one clipping against the card's own rounded-rect mask. 160 = 188
        // minus the carousel's own 14pt top/bottom scroll padding.
        .frame(width: 184, height: 160, alignment: .leading)
        .background(
            LinearGradient(
                colors: [Color.cavnarEmber.opacity(0.16), Color.cavnarEmber.opacity(0.04)],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
        )
        .overlay(
            RoundedRectangle(cornerRadius: 20)
                .strokeBorder(Color.cavnarEmber.opacity(0.22), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: 20))
        .shadow(color: Color.cavnarEmber.opacity(0.35), radius: 15, x: 0, y: 10)
    }

    private var iconName: String {
        switch item.type {
        case "reviews_awaiting_approval": return "star.fill"
        case "labor_overtime": return "exclamationmark.triangle.fill"
        case "low_response_rate": return "chart.bar.fill"
        default: return "bell.fill"
        }
    }
}

struct AllClearRow: View {
    var body: some View {
        VStack(spacing: 8) {
            ZStack {
                Circle()
                    .fill(Color.cavnarGreen.opacity(0.12))
                    .frame(width: 40, height: 40)
                Image(systemName: "checkmark")
                    .foregroundStyle(Color.cavnarGreen)
            }
            Text("All clear")
                .font(.cavnarBody(15, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("Nothing needs your attention right now")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
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
