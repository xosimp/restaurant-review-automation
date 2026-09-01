import SwiftUI

/// Branded loading state for a tab root's first, full-screen data fetch —
/// replaces a bare system ProgressView with the real seal mark (ring +
/// ember) on a slow, symmetric breathing pulse (same easeInOut-both-ways
/// feel as PulsingSwipeArrow/PulsingEndpointDot elsewhere in this file,
/// not a spring/bounce). Reads as "the brand is working on it" rather than
/// a generic spinner, on the three tab roots (Home, Modules, Account)
/// where this is the very first thing a session sees.
///
/// One breath drives two things in sync: the ring dims and brightens
/// (0.35 -> 1), and the ember flares (CavnarSealMark.emberIntensity
/// 0 -> 1 — halo swells, core goes hot). The ember itself never dims —
/// it's the thing that stays lit while everything else waits, which is
/// the whole brand story.
struct CavnarLoadingSeal: View {
    var size: CGFloat = 40

    private enum Phase: CaseIterable { case rest, fanned }

    var body: some View {
        PhaseAnimator(Phase.allCases) { phase in
            CavnarSealMark(
                size: size,
                ringOpacity: phase == .fanned ? 1 : 0.35,
                emberIntensity: phase == .fanned ? 1 : 0
            )
        } animation: { _ in
            .easeInOut(duration: 0.9)
        }
    }
}
