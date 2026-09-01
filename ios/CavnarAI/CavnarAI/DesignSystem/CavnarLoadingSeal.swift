import SwiftUI

/// Branded loading state for a tab root's first, full-screen data fetch —
/// replaces a bare system ProgressView with the seal mark on a slow,
/// symmetric breathing pulse (same easeInOut-both-ways feel as
/// PulsingSwipeArrow/PulsingEndpointDot elsewhere in this file, not a
/// spring/bounce). Reads as "the brand is working on it" rather than a
/// generic spinner, on the three tab roots (Home, Modules, Account) where
/// this is the very first thing a session sees.
struct CavnarLoadingSeal: View {
    var size: CGFloat = 40

    private enum Phase: CaseIterable { case dim, bright }

    var body: some View {
        PhaseAnimator(Phase.allCases) { phase in
            Image("SealRing")
                .renderingMode(.template)
                .resizable()
                .frame(width: size, height: size)
                .foregroundStyle(Color.cavnarEmber2)
                .opacity(phase == .bright ? 1 : 0.35)
        } animation: { _ in
            .easeInOut(duration: 0.9)
        }
    }
}
