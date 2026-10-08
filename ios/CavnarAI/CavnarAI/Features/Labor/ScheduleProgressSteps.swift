import SwiftUI

/// What the generator is actually doing, while it does it.
///
/// A spinner for minutes tells a restaurant owner that something is slow.
/// These lines tell them something intelligent is happening, and every one
/// of them names a real stage: the prompt genuinely carries availability,
/// labor targets, operational scores and leadership rules, and the
/// deterministic passes afterwards genuinely repair rows, check strength
/// and score quality.
///
/// The timings are estimates, not progress. A week is several model calls
/// and runs for minutes, so the step clock is a 75-second sketch stretched
/// to the draft time measured here (`typical`, schedule_engine.
/// typical_generation_seconds) — or three times over when nothing is
/// measured yet — exactly as the web's scheduleTiming stretches it (iOS
/// parity #15). It runs from when the job started, not from when this view
/// appeared, so coming back to Labor mid-run picks up where it was. The one
/// real measure is the days drafted (`progress`), shown under it; the server
/// reports no percentage, and this never invents one.
struct ScheduleProgressSteps: View {
    /// Whether last year's same days are an input to this draft (the
    /// labor payload's `last_year_available`, read tolerantly). A progress
    /// line is a claim about the inputs, and most restaurants have no year
    /// of history (NS1 #21).
    var lastYearAvailable: Bool = false
    /// When the job started (LaborViewModel.generationStartedAt).
    var startedAt: Date? = nil
    /// How long a full week usually takes here, measured; nil until two
    /// have run.
    var typical: GenerationTypical? = nil

    private var steps: [(text: String, after: Double)] { Self.steps(lastYearAvailable: lastYearAvailable) }

    /// Each stage and roughly how long the one before it runs.
    static func steps(lastYearAvailable: Bool) -> [(text: String, after: Double)] { [
        (OwnerCopy.firstScheduleStep(lastYearAvailable: lastYearAvailable), 0),
        ("Checking who's available", 7),
        ("Balancing against your labor target", 15),
        ("Weighing operational scores", 26),
        ("Checking leadership requirements", 36),
        ("Building the strongest team it can", 46),
        ("Scoring every shift for quality", 62),
    ] }

    @State private var appeared = Date()

    var body: some View {
        TimelineView(.periodic(from: .now, by: 0.5)) { timeline in
            let elapsed = timeline.date.timeIntervalSince(startedAt ?? appeared)
            let scale = GenerationCopy.stepScale(typical: typical)
            let current = steps.lastIndex(where: { elapsed >= $0.after * scale }) ?? 0
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
        let upper = min(steps.count - 1, current + 1)
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
                CavnarShimmerText(text: steps[index].text, color: .cavnarInk)
                    .font(.cavnarBody(13.5, weight: 600))
            } else {
                Text(steps[index].text)
                    .font(.cavnarBody(13.5, weight: done ? 400 : 400))
                    .foregroundStyle(done ? Color.cavnarInk3 : Color.cavnarInk3.opacity(0.55))
            }
            Spacer(minLength: 0)
        }
        .opacity(done ? 0.7 : 1)
        .transition(.opacity)
    }
}
