import SwiftUI

/// What the generator is actually doing, while it does it — only what the
/// job REPORTED (schedule re-audit 10/11/26 TRUST-21).
///
/// The steps are schedule-status's `progress.stages` (schedule_engine.
/// GENERATION_STAGES, the field the web's Building steps read): a stage is
/// ticked only once the job said it moved past it, and the live one is the
/// stage it said it is in. Until the first report the sliding ember line
/// stands in, with no step named — there used to be a 75-second timed
/// script here that ticked "Weighing operational scores" for a team nobody
/// had rated and stages the solver-first pipeline never runs. The one other
/// real measure is the days drafted (`progress`), shown under it; the
/// server reports no percentage, and this never invents one.
struct ScheduleProgressSteps: View {
    @Environment(\.colorSchemeContrast) private var contrast
    /// Whether last year's same days are an input to this draft (the
    /// labor payload's `last_year_available`): the inputs stage says so only
    /// then (NS1 #21).
    var lastYearAvailable: Bool = false
    /// The job's own report; nil (or no stages) before it sent one.
    var progress: GenerationProgress? = nil

    /// The inputs stage's words: last year's same days only when that input
    /// exists; every other stage in the server's own words.
    static func label(_ stage: GenerationProgress.Stage, lastYearAvailable: Bool) -> String {
        stage.key == "inputs" ? OwnerCopy.firstScheduleStep(lastYearAvailable: lastYearAvailable) : stage.label
    }

    var body: some View {
        let stages = progress?.stages ?? []
        if let current = progress?.currentIndex {
            VStack(alignment: .leading, spacing: 7) {
                ForEach(Self.window(count: stages.count, around: current), id: \.self) { index in
                    row(stages[index], live: index == current)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .animation(.easeOut(duration: 0.3), value: current)
        } else {
            // Nothing reported yet: the pulse, never a tick the job did not
            // send (DESIGN_SYSTEM §10's loading line).
            CavnarSkeletonBar(height: 3)
                .frame(width: 180)
                .accessibilityLabel("Starting the draft")
        }
    }

    /// A window rather than the whole list: the finished steps scroll away
    /// so the block stays the same height and the eye stays on the live one.
    static func window(count: Int, around current: Int) -> [Int] {
        guard count > 0 else { return [] }
        let lower = max(0, current - 2)
        let upper = min(count - 1, current + 1)
        return lower <= upper ? Array(lower...upper) : []
    }

    private func row(_ stage: GenerationProgress.Stage, live: Bool) -> some View {
        let done = stage.done && !live
        let text = Self.label(stage, lastYearAvailable: lastYearAvailable)
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
            .accessibilityHidden(true)

            if live {
                // The live step shimmers; the others are plain, so exactly
                // one thing on screen is moving.
                CavnarShimmerText(text: text, color: .cavnarInk)
                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
            } else {
                Text(text)
                    .font(.cavnar(.secondary))
                    .foregroundStyle(done ? Color.cavnarInk3 : Color.cavnarInk3Muted(contrast))
            }
            Spacer(minLength: 0)
        }
        .opacity(done ? 0.7 : 1)
        .transition(.opacity)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(text + (done ? ", done" : (live ? ", in progress" : "")))
    }
}
