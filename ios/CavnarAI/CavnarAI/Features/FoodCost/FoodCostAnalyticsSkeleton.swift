import SwiftUI

/// Full-page loading skeleton for the Analytics tab's first load.
///
/// Reshaped to match what this page ACTUALLY renders now: hero card (with
/// its AI strip), stat strip, recoverable gauge, the two ledgers (Waste
/// Ledger and Tied-Up Capital), the action rows, then the trend chart. It
/// still described the old layout — a benchmark bar and two donut rows —
/// none of which is on this page any more, so the placeholder was
/// resolving to a shape the real content never landed in and everything
/// jumped once it arrived.
///
/// Blocks are sized to the real components' own heights (the gauge is 230,
/// a ledger is 44 + 40/row + 8, the trend chart 200) so the page barely
/// moves when the data lands.
struct FoodCostAnalyticsSkeleton: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            heroBlock
            statStripBlock
            chartBlock(kickerWidth: 0.30, titleWidth: 0.52, height: 230)   // Recoverable Gauge
            ledgerBlock(rows: 4)                                          // Waste Ledger
            ledgerBlock(rows: 3)                                          // Tied-Up Capital
            actionBlock
            chartBlock(kickerWidth: 0.26, titleWidth: 0.44, height: 200)   // Trend chart
        }
    }

    /// "Counting the Pantry" — the hero's placeholder is a ledger filling in
    /// category by category (the over-budget one in ember) instead of three
    /// anonymous bars, so the wait reads as the audit actually happening
    /// (see CavnarMotion).
    private var heroBlock: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("COUNTING THE PANTRY")
                .font(.cavnarBody(14, weight: 600))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarInk3)
            CavnarLedgerFill()
            // The hero carries the AI consultant strip along its bottom
            // edge, after a divider — reserved here so it doesn't appear
            // out of nowhere.
            Rectangle().fill(Color.cavnarEmber.opacity(0.25)).frame(height: 1)
            CavnarSkeletonBar(height: 15, widthFraction: 0.8)
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

    /// Every chart on this page opens with a CavnarChartHeader (kicker +
    /// title, sometimes a detail line) above the canvas stage, so the
    /// placeholder carries the same three parts.
    private func chartBlock(kickerWidth: CGFloat, titleWidth: CGFloat, height: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 7) {
                CavnarSkeletonBar(height: 10, widthFraction: kickerWidth)
                CavnarSkeletonBar(height: 19, widthFraction: titleWidth)
            }
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(Color.cavnarPaper2.opacity(0.55))
                .frame(height: height)
        }
    }

    /// A ledger's own geometry: header, then one pill-shaped bar per row at
    /// the real 40pt row pitch, each already carrying the shimmer.
    private func ledgerBlock(rows: Int) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 7) {
                CavnarSkeletonBar(height: 10, widthFraction: 0.28)
                CavnarSkeletonBar(height: 19, widthFraction: 0.46)
            }
            VStack(spacing: 14) {
                // Descending widths — a ledger is sorted by cost, so the
                // placeholder leans the way the real bars will.
                ForEach(0..<rows, id: \.self) { i in
                    CavnarSkeletonBar(height: 26, widthFraction: 1.0 - Double(i) * 0.14)
                }
            }
            .padding(.vertical, 8)
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
