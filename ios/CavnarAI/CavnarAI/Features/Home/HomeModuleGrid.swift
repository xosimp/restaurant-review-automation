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
                .cavnarSensitive()
            // Clash Display, not Apfel — this names the module, the same
            // heading role a screen's own centered title plays once you
            // tap in (see cavnarTitleToolbar). Dropped the uppercase/
            // tracking treatment that made sense for a small tracked
            // eyebrow caption but reads oddly on a real headline face —
            // this sits under the number as this tile's own name, not a
            // kicker label above something else.
            Text(module.label)
                .font(.cavnarHeadline(14))
                .foregroundStyle(Color.cavnarEmber)
                .multilineTextAlignment(.center)
            if let sublabel = module.kpi?.sublabel {
                Text(sublabel)
                    .font(.cavnarBody(14))
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

    // Tiles rise in one after another on first load (see
    // CavnarEntranceClock) — one clock shared by every tile in this grid.
    @State private var clock = CavnarEntranceClock()
    // Which tile is showing its tap flash right now — see tap(_:) below.
    @State private var flashingKey: String?

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 150), spacing: 10)], spacing: 10) {
            ForEach(Array(modules.enumerated()), id: \.element.id) { index, module in
                // A Button calling back into the parent's NavigationPath,
                // not a NavigationLink — keeps the haptic on a deterministic
                // action closure instead of a simultaneousGesture racing
                // NavigationLink's own tap handling.
                Button {
                    tap(module)
                } label: {
                    KPITile(module: module)
                        .cavnarTileFlash(flashingKey == module.id)
                }
                .buttonStyle(.plain)
                .cavnarRowEntrance(index: index, clock: clock)
            }
            ForEach(Array(comingSoon.enumerated()), id: \.element.id) { index, module in
                ComingSoonModuleTile(module: module)
                    .cavnarRowEntrance(index: modules.count + index, clock: clock)
            }
        }
    }

    /// Lights the tile, holds for one short beat, then navigates. Firing
    /// both at once (no delay at all) made the flash imperceptible — the
    /// NavigationStack push transition is a bigger, faster motion that
    /// starts in the same instant and visually swallows a same-frame
    /// flash before the eye can register it. 70ms is the shortest gap
    /// that reliably reads as an intentional highlight rather than
    /// nothing (roughly what UITableViewCell's own selection flash uses)
    /// while staying well under the ~130ms a first attempt used, which
    /// read as lag rather than feedback.
    private static let flashHoldMs: UInt64 = 70

    private func tap(_ module: ModuleSummary) {
        flashingKey = module.id
        Task {
            try? await Task.sleep(for: .milliseconds(Self.flashHoldMs))
            onSelect(module)
            if flashingKey == module.id { flashingKey = nil }
        }
    }
}

/// A muted, non-interactive placeholder tile — no Button wrapper at all
/// (not just a disabled one), since these aren't a "not yet enabled for
/// you" upsell tied to entitlement, they're "doesn't exist as a real
/// feature yet" for every client. Reuses .cavnarStatCell's exact card
/// language (gradient wash, top highlight, border) with a gray tint
/// instead of ember, so it reads as clearly distinct from an active module
/// tile without needing a whole separate visual style. Mirrors KPITile's
/// exact 4-row structure (icon → big number line → label → sublabel), not
/// just a similar-looking 3-row version — a real tile is never actually
/// shorter than this since its number line always renders (falling back to
/// "—" only if kpi itself is nil), so matching that same row made these
/// tiles visibly shorter than the real ones before.
struct ComingSoonModuleTile: View {
    let module: ModuleSummary

    var body: some View {
        VStack(spacing: 8) {
            GlowBadge(systemImage: ModuleIcon.symbolName(for: module.icon), size: 40)
                .opacity(0.45)
                .grayscale(0.7)
            Text("—")
                .font(.cavnarNumber(26, weight: 500))
                .foregroundStyle(Color.cavnarInk3)
            Text(module.label)
                .font(.cavnarHeadline(14))
                .foregroundStyle(Color.cavnarInk3)
                .multilineTextAlignment(.center)
            Text("Coming Soon")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3.opacity(0.75))
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 16)
        .cavnarStatCell(tint: Color.cavnarInk3)
    }
}
