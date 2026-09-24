import SwiftUI

/// "Why labor ran over" — labor.diagnose on the phone, the same anatomy as
/// Reviews' diagnosis (most likely cause, what else fits, what would tell
/// them apart, what it was cross-checked against) and the web's
/// `renderDiagnosis`. The check it names is the recommendation, so it
/// carries Done / Not for us / Track under it (`diag_labor:<driver>`,
/// surface `labor`); an answered one keeps its evidence and drops the
/// controls. Renders nothing when there is no cause (labor at or under
/// target has nothing to diagnose).
struct LaborDiagnosisCard: View {
    let diagnosis: LaborDiagnosis

    var body: some View {
        if diagnosis.hasCause, let cause = diagnosis.cause {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("WHY LABOR RAN OVER")
                        .font(.cavnarBody(11, weight: 700))
                        .tracking(1.1)
                        .foregroundStyle(Color.cavnarEmber)
                    Spacer(minLength: 8)
                    if let band = diagnosis.confidence {
                        Text(band.uppercased())
                            .font(.cavnarBody(10, weight: 700))
                            .tracking(0.7)
                            .foregroundStyle(tint(band))
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(tint(band).opacity(0.14), in: Capsule())
                    }
                }
                if let summary = diagnosis.summary {
                    HomeMixedText.make(summary, size: 13, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                row("Most likely cause", cause)
                if let alt = diagnosis.alternativeCause {
                    row("It could also be", alt, quiet: true)
                }
                if let confirm = diagnosis.whatWouldConfirm {
                    VStack(alignment: .leading, spacing: 6) {
                        row("Check this", confirm)
                        if diagnosis.showsAnswers, let key = diagnosis.recKey {
                            RecAnswerRow(key: key, surface: "labor", module: "labor")
                        }
                    }
                }
                if !diagnosis.operationalEvidence.isEmpty {
                    row("Cross-checked against",
                        diagnosis.operationalEvidence
                            .compactMap { e in e.metric.map { "\($0) \(e.value ?? "")" } }
                            .joined(separator: "  \u{00B7}  "),
                        quiet: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard(.ai)
        }
    }

    private func row(_ label: String, _ text: String, quiet: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label.uppercased())
                .font(.cavnarBody(10, weight: 700))
                .tracking(0.9)
                .foregroundStyle(Color.cavnarInk3)
            HomeMixedText.make(text, size: 14, weight: quiet ? 500 : 600, color: quiet ? .cavnarInk2 : .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func tint(_ band: String) -> Color {
        switch band {
        case "high": return .cavnarGreen
        case "medium": return .cavnarAmber
        default: return .cavnarInk3
        }
    }
}
