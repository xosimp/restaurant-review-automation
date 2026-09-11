import SwiftUI

/// What the generator is actually doing, while it does it.
///
/// A spinner for sixty seconds tells a restaurant owner that something is
/// slow. These lines tell them something intelligent is happening, and
/// every one of them names a real stage: the prompt genuinely carries
/// availability, labor targets, operational scores and leadership rules,
/// and the deterministic passes afterwards genuinely repair rows, check
/// strength and score quality.
///
/// The timings are estimates, not progress. Generation is a single model
/// call and the server cannot report a percentage, so this never claims
/// one — the steps advance on their own clock and the last one holds until
/// the schedule actually arrives. Claiming "87%" would be inventing a
/// number, which is the one thing this product does not do.
struct ScheduleProgressSteps: View {
    /// Each stage and roughly how long the one before it runs.
    private static let steps: [(text: String, after: Double)] = [
        ("Reading last year's same days", 0),
        ("Checking who's available", 7),
        ("Balancing against your labor target", 15),
        ("Weighing operational scores", 26),
        ("Checking leadership requirements", 36),
        ("Building the strongest team it can", 46),
        ("Scoring every shift for quality", 62),
    ]

    @State private var started = Date()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { timeline in
            let elapsed = timeline.date.timeIntervalSince(started)
            let current = Self.steps.lastIndex(where: { elapsed >= $0.after }) ?? 0
            VStack(alignment: .leading, spacing: 7) {
                ForEach(visible(around: current), id: \.self) { index in
                    row(index: index, current: current)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(.easeOut(duration: 0.3), value: current)
        }
    }

    /// A window rather than the whole list: the finished steps scroll away
    /// so the block stays the same height and the eye stays on the live one.
    private func visible(around current: Int) -> [Int] {
        let lower = max(0, current - 2)
        let upper = min(Self.steps.count - 1, current + 1)
        return Array(lower...upper)
    }

    private func row(index: Int, current: Int) -> some View {
        let done = index < current
        let live = index == current
        return HStack(spacing: 8) {
            ZStack {
                if done {
                    Image(systemName: "checkmark")
                        .font(.system(size: 8, weight: .black))
                        .foregroundStyle(Color.cavnarGreen)
                } else if live {
                    Circle().fill(Color.cavnarEmber).frame(width: 6, height: 6)
                } else {
                    Circle().strokeBorder(Color.cavnarPaper3, lineWidth: 1.2)
                        .frame(width: 6, height: 6)
                }
            }
            .frame(width: 12)

            if live {
                // The live step shimmers; the others are plain, so exactly
                // one thing on screen is moving.
                CavnarShimmerText(text: Self.steps[index].text, color: .cavnarInk)
                    .font(.cavnarBody(13.5, weight: 600))
            } else {
                Text(Self.steps[index].text)
                    .font(.cavnarBody(13.5, weight: done ? 400 : 400))
                    .foregroundStyle(done ? Color.cavnarInk3 : Color.cavnarInk3.opacity(0.55))
            }
            Spacer(minLength: 0)
        }
        .opacity(done ? 0.7 : 1)
        .transition(.opacity)
    }
}
