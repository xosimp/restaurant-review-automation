import SwiftUI

/// Full-page loading skeleton for the Analytics tab's first load —
/// replaces the plain ProgressView() with blocks roughly shaped like the
/// real content underneath (hero, stat strip, benchmark bar, two donut
/// rows, action rows, trend chart), each using the house CavnarSkeletonBar
/// sliding-highlight shimmer (DesignSystem/ViewModifiers.swift) rather than
/// a spinner — the same convention other loading states in this app use,
/// just composed into a full-screen layout instead of a single line.
struct FoodCostAnalyticsSkeleton: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            heroBlock
            statStripBlock
            CavnarSkeletonBar(height: 22, widthFraction: 1.0)
            donutRowBlock
            donutRowBlock
            actionBlock
            CavnarSkeletonBar(height: 120, widthFraction: 1.0)
        }
    }

    private var heroBlock: some View {
        VStack(alignment: .leading, spacing: 14) {
            CavnarSkeletonBar(height: 14, widthFraction: 0.4)
            CavnarSkeletonBar(height: 34, widthFraction: 0.7)
            CavnarSkeletonBar(height: 14, widthFraction: 0.85)
        }
        .padding(18)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.cavnarPaper2)
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card))
    }

    private var statStripBlock: some View {
        HStack(spacing: 12) {
            ForEach(0..<3, id: \.self) { _ in
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 20, widthFraction: 0.6)
                    CavnarSkeletonBar(height: 10, widthFraction: 0.9)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private var donutRowBlock: some View {
        HStack(alignment: .center, spacing: 20) {
            Circle()
                .fill(Color.cavnarPaper3.opacity(0.5))
                .frame(width: 92, height: 92)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(0..<3, id: \.self) { _ in
                    CavnarSkeletonBar(height: 12, widthFraction: 0.75)
                }
            }
        }
    }

    private var actionBlock: some View {
        VStack(alignment: .leading, spacing: 12) {
            CavnarSkeletonBar(height: 14, widthFraction: 0.35)
            ForEach(0..<3, id: \.self) { _ in
                CavnarSkeletonBar(height: 40, widthFraction: 1.0)
            }
        }
    }
}
