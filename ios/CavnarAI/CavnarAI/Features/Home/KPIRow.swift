import SwiftUI

/// One KPI tile in Home's stat row — mirrors the web dashboard's stat-tile
/// pattern (big Space Grotesk number, small ember eyebrow label).
struct KPITile: View {
    let value: String
    let label: String
    let sublabel: String
    var isInactive: Bool = false
    var action: (() -> Void)?

    var body: some View {
        Button {
            action?()
        } label: {
            VStack(spacing: 6) {
                Text(value)
                    .font(.cavnarNumber(28, weight: 500))
                    .foregroundStyle(isInactive ? Color.cavnarInk3 : Color.cavnarInk)
                Text(label)
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.2)
                    .textCase(.uppercase)
                    .foregroundStyle(isInactive ? Color.cavnarInk3 : Color.cavnarEmber)
                Text(sublabel)
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
        .buttonStyle(.plain)
        .disabled(action == nil)
    }
}

struct KPIRow: View {
    let kpis: KPISet
    let modules: ModuleFlags
    var onSelectTab: (AppTab) -> Void

    var body: some View {
        HStack(spacing: 0) {
            if modules.reviews, let reviews = kpis.reviews {
                KPITile(
                    value: "\(reviews.responded)/\(reviews.total)",
                    label: "Reviews",
                    sublabel: "\(Int(reviews.responseRate))% response rate",
                    action: { onSelectTab(.reviews) }
                )
            } else {
                KPITile(value: "—", label: "Reviews", sublabel: "not active", isInactive: true)
            }

            Divider()

            // Labor has no dedicated v1 tab yet (desktop-first — see the
            // architecture plan's v2 scope), so this tile is informational
            // only, not tappable.
            if modules.labor, let labor = kpis.labor {
                KPITile(
                    value: String(format: "%.1f%%", labor.overallLaborPct),
                    label: "Labor",
                    sublabel: labor.onTrack ? "on track" : "over \(Int(labor.target))% target"
                )
            } else {
                KPITile(value: "—", label: "Labor", sublabel: "not active", isInactive: true)
            }

            Divider()

            if modules.inventory, let foodCost = kpis.foodCost {
                KPITile(
                    value: "$\(foodCost.recoverableMonthly)",
                    label: "Food Cost",
                    sublabel: "recoverable / mo",
                    action: { onSelectTab(.foodCost) }
                )
            } else {
                KPITile(value: "—", label: "Food Cost", sublabel: "not active", isInactive: true)
            }
        }
        .frame(maxWidth: .infinity)
    }
}
