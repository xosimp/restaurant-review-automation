import SwiftUI

/// One row in the Home tab's "needs attention" list — same three checks and
/// copy as the web dashboard's home-attention-list (see mobile_api.py's
/// _do_mobile_home docstring). Every row shares one quiet, uniform surface
/// and a single ember accent regardless of alert type — a red/amber/ember
/// mix per row read as chaotic rather than "at a glance severity," and a
/// bold gradient-and-border card per row read closer to a game achievement
/// toast than a premium restaurant dashboard. The icon glyph still varies
/// by type (still useful at a glance), just not its color.
struct NeedsAttentionRow: View {
    let item: NeedsAttentionItem

    var body: some View {
        HStack(spacing: 14) {
            ZStack {
                Circle()
                    .fill(Color.cavnarEmber.opacity(0.14))
                    .frame(width: 34, height: 34)
                Image(systemName: iconName)
                    .foregroundStyle(Color.cavnarEmber)
                    .font(.system(size: 14, weight: .semibold))
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(item.title)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(item.detail)
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            GlassChevronButton()
        }
        .padding(.vertical, 14)
        .padding(.horizontal, 16)
        .background(Color.cavnarPaper2.opacity(0.4))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card)
                .strokeBorder(Color.white.opacity(0.06), lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
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
