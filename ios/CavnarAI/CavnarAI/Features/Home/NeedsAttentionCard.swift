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

    private let cardWidth: CGFloat = 168

    @State private var centeredID: String?

    var body: some View {
        GeometryReader { geo in
            let sidePadding = max((geo.size.width - cardWidth) / 2, 0)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 20) {
                    ForEach(items) { item in
                        Button {
                            onTap(item)
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
                .padding(.vertical, 24)
            }
            .scrollTargetBehavior(.viewAligned)
            .scrollPosition(id: $centeredID)
            // Fires once per card that locks into center — guarded so the
            // very first card (nil -> its id, on initial layout) doesn't
            // buzz before the user has actually swiped anything.
            .sensoryFeedback(.selection, trigger: centeredID) { old, new in
                old != nil && new != nil
            }
        }
        .frame(height: 220)
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
                Circle()
                    .fill(Color.cavnarEmber.opacity(0.18))
                    .frame(width: 34, height: 34)
                Image(systemName: iconName)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(item.title)
                    .font(.cavnarBody(12.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                Text(item.detail)
                    .font(.cavnarBody(10.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 16)
        .padding(.horizontal, 14)
        .frame(width: 168, alignment: .leading)
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
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk)
            Text("Nothing needs your attention right now")
                .font(.cavnarBody(11))
                .foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 20)
    }
}
