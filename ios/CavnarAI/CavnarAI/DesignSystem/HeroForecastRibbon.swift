import SwiftUI

/// Carries a hero card's bottom-center anchor, and the screen's header
/// (segmented-control) bottom edge alongside it, up to whatever ancestor
/// attaches cavnarHeroForecastRibbon — originally built for Labor's
/// Overview tab, now shared so any module's hero card can get the exact
/// same floating "FORECAST" pill.
///
/// The ribbon has to be hoisted OUT of the hero card's own overlay and
/// positioned by anchor at a shared ancestor instead of living inside the
/// card directly, for two independently-discovered reasons:
///
/// 1. Z-ordering: an .overlay attached to the hero card (nested inside the
///    ScrollView) always draws *underneath* a separate .overlay attached
///    higher up on the outer container, no matter what order either one is
///    declared in — an ancestor's overlay composites on top of its entire
///    rendered subtree. That meant a scrim declared at the hero card's own
///    level dimmed everything BUT the ribbon that triggered it, backwards
///    from the intent. Hoisting the ribbon to a shared root via this
///    anchor, with the scrim declared in the SAME overlay pass, puts both
///    in one stacking context where declaration order actually controls
///    what's on top.
/// 2. ScrollView clipping: living outside the ScrollView means the ribbon
///    isn't naturally clipped away by the ScrollView's own scroll clipping
///    the way in-ScrollView content is — without the header anchor it kept
///    rendering wherever its hero-card anchor's geometry said to, even
///    once that point had scrolled up above the visible content area and
///    into the header. Comparing the two anchors' Y position tells the
///    overlay when that's happened, and drives a continuous fade instead
///    of a hard cutoff.
struct CavnarRibbonAnchorKey: PreferenceKey {
    struct Anchors {
        var heroBottom: Anchor<CGPoint>?
        var headerBottom: Anchor<CGPoint>?
    }
    static var defaultValue: Anchors { Anchors() }
    static func reduce(value: inout Anchors, nextValue: () -> Anchors) {
        let next = nextValue()
        if let h = next.heroBottom { value.heroBottom = h }
        if let hd = next.headerBottom { value.headerBottom = hd }
    }
}

extension View {
    /// Attach to the screen's header/segmented-control (the sibling
    /// directly above the ScrollView) — marks where a scrolled-up ribbon
    /// should finish fading out.
    func cavnarRibbonHeaderAnchor() -> some View {
        anchorPreference(key: CavnarRibbonAnchorKey.self, value: .bottom) {
            CavnarRibbonAnchorKey.Anchors(headerBottom: $0)
        }
    }

    /// Attach to the hero card itself — the ribbon straddles this
    /// bottom-center edge, centered on it.
    func cavnarRibbonHeroAnchor() -> some View {
        anchorPreference(key: CavnarRibbonAnchorKey.self, value: .bottom) {
            CavnarRibbonAnchorKey.Anchors(heroBottom: $0)
        }
    }

    /// Renders the floating "FORECAST" pill at the hero card's anchored
    /// bottom edge, expanding into a scrim-backed panel on tap. Attach to
    /// the same container that holds both the header (cavnarRibbonHeaderAnchor)
    /// and, somewhere in its ScrollView content, the hero card
    /// (cavnarRibbonHeroAnchor) — matching where .cavnarModuleBackground()
    /// already sits.
    func cavnarHeroForecastRibbon<Panel: View>(
        isExpanded: Binding<Bool>,
        tone: Color,
        icon: String = "sparkles",
        badgeCount: Int? = nil,
        @ViewBuilder panel: @escaping () -> Panel
    ) -> some View {
        modifier(HeroForecastRibbonModifier(isExpanded: isExpanded, tone: tone, icon: icon, badgeCount: badgeCount, panel: panel))
            .animation(.easeOut(duration: 0.2), value: isExpanded.wrappedValue)
    }
}

private struct HeroForecastRibbonModifier<Panel: View>: ViewModifier {
    @Binding var isExpanded: Bool
    var tone: Color
    var icon: String
    var badgeCount: Int?
    @ViewBuilder var panel: () -> Panel

    func body(content: Content) -> some View {
        content.overlayPreferenceValue(CavnarRibbonAnchorKey.self) { anchors in
            // Only exists at all once the hero card is actually on screen
            // (its anchor present) — no dangling scrim/pill with nothing
            // to focus attention on.
            if let anchor = anchors.heroBottom {
                GeometryReader { geo in
                    let heroY = geo[anchor].y
                    let fadeRange: CGFloat = 24
                    let opacity: Double = {
                        guard let headerY = anchors.headerBottom.map({ geo[$0].y }) else { return 1 }
                        let delta = heroY - headerY
                        if delta >= fadeRange { return 1 }
                        if delta <= 0 { return 0 }
                        return Double(delta / fadeRange)
                    }()
                    ZStack {
                        if isExpanded {
                            Color.black.opacity(0.78)
                                .ignoresSafeArea()
                                .onTapGesture {
                                    Haptic.light()
                                    isExpanded = false
                                }
                                .transition(.opacity)
                        }
                        // .position centers the pill's own center exactly on
                        // the anchor point (hero card's bottom-center edge).
                        CavnarForecastPill(isExpanded: $isExpanded, tone: tone, icon: icon, badgeCount: badgeCount, panel: panel)
                            .position(geo[anchor])
                    }
                    .opacity(opacity)
                    .allowsHitTesting(opacity > 0.01)
                }
            }
        }
    }
}

private struct CavnarForecastPill<Panel: View>: View {
    @Binding var isExpanded: Bool
    var tone: Color
    var icon: String
    var badgeCount: Int?
    @ViewBuilder var panel: () -> Panel

    // This view's own rendered height when collapsed — used only to push
    // the expanded panel down clear of the tab. Safe to hardcode: this
    // view fully owns the tab's font/padding, so its height can't drift
    // out from under this number.
    private static var tabHeight: CGFloat { 34 }

    var body: some View {
        collapsedTab
            .overlay(alignment: .top) {
                if isExpanded {
                    panel()
                        .padding(.top, Self.tabHeight + 10)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
    }

    private var collapsedTab: some View {
        Button {
            Haptic.light()
            isExpanded.toggle()
        } label: {
            HStack(spacing: 7) {
                Image(systemName: icon)
                    .font(.system(size: 11, weight: .semibold))
                Text("FORECAST")
                    .font(.cavnarBody(10, weight: 700))
                    .tracking(1.4)
                if let badgeCount, badgeCount > 1 {
                    Text("\(badgeCount)")
                        .font(.cavnarNumber(10, weight: 700))
                        .frame(width: 15, height: 15)
                        .background(Color.cavnarInk.opacity(0.18))
                        .clipShape(Circle())
                }
                // Signals the pill itself is tappable, not just
                // informational — points down while collapsed (more to
                // reveal) and flips to point up once expanded, matching
                // the panel dropping open below it.
                Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 9, weight: .bold))
            }
            .foregroundStyle(Color.cavnarInk)
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            // Same gradient stops as CavnarGlassCardStyle (the hero card's
            // own background: tint 0.55 fading to 0.22) so the pill reads
            // as the same "lit corner fading toward dark" sheen the card
            // has. Paper2 sits here at FULL opacity, not the card's own
            // translucent 0.35 — keeps the fade from ever revealing
            // whatever's actually behind the pill.
            .background(
                LinearGradient(
                    colors: [tone.opacity(0.55), tone.opacity(0.22)],
                    startPoint: .topLeading, endPoint: .bottomTrailing
                )
            )
            .background(Color.cavnarPaper2)
            .clipShape(Capsule())
            .overlay(Capsule().strokeBorder(tone.opacity(0.6), lineWidth: 1.2))
            // Reads as floating above the hero card behind it, not flush
            // against it.
            .shadow(color: .black.opacity(0.35), radius: 8, y: 4)
        }
        .buttonStyle(.plain)
    }
}

/// Shared chrome for a forecast panel's own content — header row (icon +
/// title + close button) over whatever body a caller supplies. Matches
/// the fixed-width floating-card look (250pt, rounded, tone-tinted border,
/// drop shadow) both Labor's events list and a plain forecast sentence use
/// identically.
struct CavnarForecastPanel<Body: View>: View {
    let title: String
    let tone: Color
    var icon: String = "sparkles"
    @Binding var isExpanded: Bool
    @ViewBuilder var content: () -> Body

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .semibold))
                Text(title)
                    .font(.cavnarBody(12, weight: 700))
                    .tracking(0.6)
                Spacer()
                Button {
                    Haptic.light()
                    isExpanded = false
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            .foregroundStyle(tone)

            content()
        }
        .padding(16)
        .frame(width: 250, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: 18))
        .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(tone.opacity(0.3), lineWidth: 1))
        .shadow(color: .black.opacity(0.35), radius: 16, y: 8)
    }
}
