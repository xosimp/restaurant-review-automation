import SwiftUI

/// Needs Attention as a horizontal-swipe carousel of self-contained floating
/// cards — replaces the old vertical row-list entirely. Chosen over a
/// drifted-stack alternative because it frees up the vertical space the new
/// value chart above it needs, and reads as genuinely separate objects
/// rather than rows in a form (approved mockup, "Option A").
struct NeedsAttentionCarousel: View {
    let items: [NeedsAttentionItem]
    let onTap: (NeedsAttentionItem) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                ForEach(items) { item in
                    Button {
                        onTap(item)
                    } label: {
                        NeedsAttentionFloatCard(item: item)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 2)
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
