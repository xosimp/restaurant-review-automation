import SwiftUI

/// Needs Attention as a center-focused paging carousel of self-contained
/// floating cards — replaces the old vertical row-list entirely. Only the
/// centered card is sharp and full-size; its neighbors sit smaller, blurred,
/// and dimmed to either side, and swiping to a new card snaps it to center
/// with a small "locked in" haptic — the same cover-flow feel as Apple
/// Music's Now Playing or the App Store's featured cards. GeometryReader
/// supplies the side inset needed so the first and last cards can actually
/// reach dead center rather than stopping at the scroll view's edge; the
/// side inset plus .scrollTargetBehavior(.viewAligned) below is what makes
/// each swipe settle on one card centered rather than landing wherever
/// momentum happens to stop.
struct NeedsAttentionCarousel: View {
    let items: [NeedsAttentionItem]
    let onTap: (NeedsAttentionItem) -> Void

    private let cardWidth: CGFloat = 184

    @State private var centeredID: String?

    // With 3+ cards, starting centered on the SECOND one means the very
    // first frame already shows a real card peeking on both sides — the
    // clearest possible signal that this swipes both directions, not just
    // right. With only 1-2 cards there's no card that could ever have a
    // left neighbor, so it starts on the first one same as before (an
    // explicit memberwise init is what lets this depend on `items` at
    // construction time, before the view's first layout pass, rather than
    // visibly jumping there after an .onAppear).
    init(items: [NeedsAttentionItem], onTap: @escaping (NeedsAttentionItem) -> Void) {
        self.items = items
        self.onTap = onTap
        _centeredID = State(initialValue: items.count >= 3 ? items[1].id : nil)
    }

    var body: some View {
        VStack(spacing: 2) {
            GeometryReader { geo in
                let sidePadding = max((geo.size.width - cardWidth) / 2, 0)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 20) {
                        ForEach(items) { item in
                            Button {
                                // A tap on a side card brings it to center
                                // first rather than jumping straight to its
                                // destination — matches how a physical stack
                                // of cards works (you pull the one you want
                                // to the front before opening it). Only a
                                // tap on the ALREADY-centered card navigates.
                                if centeredID == item.id {
                                    onTap(item)
                                } else {
                                    withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
                                        centeredID = item.id
                                    }
                                }
                            } label: {
                                NeedsAttentionFloatCard(item: item)
                            }
                            .buttonStyle(.plain)
                            // .scrollTransition's "identity" phase lands exactly
                            // when an item is centered in the visible scroll
                            // area — paired with .viewAligned snapping below, so
                            // the card the user swiped to is always the one at
                            // full scale/sharp, and every other card sits at the
                            // smaller, blurred "off to the side" state.
                            .scrollTransition(.interactive, axis: .horizontal) { content, phase in
                                content
                                    .scaleEffect(phase.isIdentity ? 1 : 0.82)
                                    .blur(radius: phase.isIdentity ? 0 : 5)
                                    .opacity(phase.isIdentity ? 1 : 0.55)
                            }
                            .id(item.id)
                        }
                    }
                    .scrollTargetLayout()
                    .padding(.horizontal, sidePadding)
                    .padding(.vertical, 14)
                }
                // .always (not the default .automatic) forces exactly one
                // card to change per swipe gesture no matter how short the
                // drag is — .automatic was sizing its "how far is a full
                // page" threshold off the container's full width rather
                // than one card's width, which is what made it take a
                // nearly edge-to-edge drag to register a new card at all.
                .scrollTargetBehavior(.viewAligned(limitBehavior: .always))
                .scrollPosition(id: $centeredID)
                // Fires every time a different card locks into center —
                // guarded only against the very first layout pass (nil ->
                // its id) so it doesn't buzz before the user has swiped or
                // tapped anything.
                .sensoryFeedback(.selection, trigger: centeredID) { old, new in
                    old != nil && new != nil && old != new
                }
            }
            .frame(height: 208)

            if items.count > 1 {
                PulsingSwipeArrow()
            }
        }
    }
}

/// One card in the carousel. Every card shares the same uniform ember wash
/// regardless of alert type — a per-type color mix read as chaotic rather
/// than "at a glance severity" (same call already made for the old row
/// design, still true here). Only the icon glyph varies by type.
struct NeedsAttentionFloatCard: View {
    let item: NeedsAttentionItem

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ZStack {
                // "Alert Fired" — one ember ripple out from the icon as the
                // card lands, once (see CavnarMotion).
                CavnarRippleBurst(color: .cavnarEmber2, fromDiameter: 34, toDiameter: 72, rings: 1, duration: 1.0, delay: 0.6)
                Circle()
                    .fill(Color.cavnarEmber.opacity(0.18))
                    .frame(width: 34, height: 34)
                Image(systemName: iconName)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
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
        .frame(width: 184, alignment: .leading)
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
private struct PulsingSwipeArrow: View {
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
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber2.opacity(0.65 - 0.47 * phase))
                .offset(x: 5 * phase)
        }
    }
}
