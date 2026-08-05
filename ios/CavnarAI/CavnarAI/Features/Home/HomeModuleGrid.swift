import SwiftUI

/// One tile in Home's KPI grid — mirrors the web dashboard's stat-tile
/// pattern (big Space Grotesk number, small ember eyebrow label). Purely
/// data-driven off a ModuleSummary, so a 6th module needs zero layout code
/// here — it just becomes one more grid item.
struct KPITile: View {
    let module: ModuleSummary

    var body: some View {
        VStack(spacing: 6) {
            Image(systemName: ModuleIcon.symbolName(for: module.icon))
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
            Text(module.kpi?.value ?? "—")
                .font(.cavnarNumber(26, weight: 500))
                .foregroundStyle(Color.cavnarInk)
                .cavnarNumberGlow()
            Text(module.label)
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.0)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarEmber)
            if let sublabel = module.kpi?.sublabel {
                Text(sublabel)
                    .font(.cavnarBody(10))
                    .foregroundStyle(Color.cavnarInk3)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
        .cavnarStatCell()
    }
}

/// Adaptive grid of every active module's KPI tile — reflows from one
/// full-width tile (1 module) to a multi-column grid (6+) with no per-count
/// layout branching, unlike the fixed 3-slot row this replaces. No tiles are
/// shown for modules the client doesn't have — that upsell framing stays a
/// desktop/Account-tab thing (see the architecture plan). Tapping a tile
/// navigates straight into that module's screen.
struct HomeModuleGrid: View {
    let modules: [ModuleSummary]

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
            ForEach(modules) { module in
                NavigationLink {
                    ModuleDestinationView(moduleKey: module.key, moduleLabel: module.label)
                } label: {
                    KPITile(module: module)
                }
                .buttonStyle(.plain)
            }
        }
    }
}
