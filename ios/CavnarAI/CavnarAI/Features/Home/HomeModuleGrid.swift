import SwiftUI

/// One tile in Home's KPI grid — mirrors the web dashboard's stat-tile
/// pattern (big Space Grotesk number, small ember eyebrow label). Purely
/// data-driven off a ModuleSummary, so a 6th module needs zero layout code
/// here — it just becomes one more grid item.
struct KPITile: View {
    let module: ModuleSummary

    var body: some View {
        VStack(spacing: 8) {
            GlowBadge(systemImage: ModuleIcon.symbolName(for: module.icon), size: 40)
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
    // Always-shown, non-interactive placeholders for modules that aren't a
    // real, backend-gated feature yet (Waitlist & Reservations, Bar &
    // Alcohol) — every client sees these regardless of what's actually
    // active for their account, unlike `modules` above.
    var comingSoon: [ModuleSummary] = []
    var onSelect: (ModuleSummary) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
            ForEach(modules) { module in
                // A Button calling back into the parent's NavigationPath,
                // not a NavigationLink — keeps the haptic on a deterministic
                // action closure instead of a simultaneousGesture racing
                // NavigationLink's own tap handling.
                Button {
                    onSelect(module)
                } label: {
                    KPITile(module: module)
                }
                .buttonStyle(.plain)
            }
            ForEach(comingSoon) { module in
                ComingSoonModuleTile(module: module)
            }
        }
    }
}

/// A muted, non-interactive placeholder tile — no Button wrapper at all
/// (not just a disabled one), since these aren't a "not yet enabled for
/// you" upsell tied to entitlement, they're "doesn't exist as a real
/// feature yet" for every client. Reuses .cavnarStatCell's exact card
/// language (gradient wash, top highlight, border) with a gray tint
/// instead of ember, so it reads as clearly distinct from an active module
/// tile without needing a whole separate visual style.
struct ComingSoonModuleTile: View {
    let module: ModuleSummary

    var body: some View {
        VStack(spacing: 8) {
            GlowBadge(systemImage: ModuleIcon.symbolName(for: module.icon), size: 40)
                .opacity(0.45)
                .grayscale(0.7)
            Text(module.label)
                .font(.cavnarBody(10, weight: 700))
                .tracking(1.0)
                .textCase(.uppercase)
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
            Text("Coming Soon")
                .font(.cavnarBody(10, weight: 600))
                .foregroundStyle(Color.cavnarInk3.opacity(0.75))
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
        .cavnarStatCell(tint: Color.cavnarInk3)
    }
}
