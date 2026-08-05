import SwiftUI

/// One row in the Home tab's "needs attention" list — same three checks and
/// copy as the web dashboard's home-attention-list (see mobile_api.py's
/// _do_mobile_home docstring).
struct NeedsAttentionRow: View {
    let item: NeedsAttentionItem

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: iconName)
                .foregroundStyle(iconColor)
                .font(.system(size: 16, weight: .semibold))
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.title)
                    .font(.cavnarBody(13, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                Text(item.detail)
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
        }
        .padding(12)
        .background(iconColor.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private var iconName: String {
        switch item.type {
        case "reviews_awaiting_approval": return "star.fill"
        case "labor_overtime": return "exclamationmark.triangle.fill"
        case "low_response_rate": return "chart.bar.fill"
        default: return "bell.fill"
        }
    }

    private var iconColor: Color {
        switch item.type {
        case "labor_overtime": return .cavnarRed
        case "low_response_rate": return .cavnarAmber
        default: return .cavnarEmber
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
