import SwiftUI

/// "Why labor ran over" — labor.diagnose on the phone, on the iPhone answer
/// card (CavnarAnswerCard, iOS readability round 10/8/26): the most likely
/// cause as the headline, the check it names as "Do this" with Done / Not
/// for us / Track under it (`diag_labor:<driver>`, surface `labor`), ONE
/// visible "Could also be" line, the measured confidence — and the summary,
/// what it was cross-checked against and what would tell the two readings
/// apart behind "See the evidence". The web's `renderDiagnosis` carries
/// the same parts. An answered one keeps its evidence and drops the
/// controls. Renders nothing when there is no cause (labor at or under
/// target has nothing to diagnose).
///
/// The same card carries Marketing's guest-text diagnosis
/// (guest_marketing.diagnose, one shape — the web's renderDiagnosis draws
/// both): `title`, `surface` and `module` name whose it is.
struct LaborDiagnosisCard: View {
    let diagnosis: LaborDiagnosis
    var title: String = "WHY LABOR RAN OVER"
    var surface: String = "labor"

    var body: some View {
        if diagnosis.hasCause, let cause = diagnosis.cause {
            CavnarAnswerCard(
                kicker: title,
                headline: cause,
                alternativeCause: diagnosis.alternativeCause,
                // How sure, as a percentage with what it rests on (K1/K6).
                confidence: diagnosis.confidence.map {
                    ConfidenceLine(confidence: $0, recKey: diagnosis.recKey, surface: surface, module: surface)
                },
                detailLabel: "See the evidence"
            ) {
                if let confirm = diagnosis.whatWouldConfirm {
                    (Text("Do this: ").font(.cavnarBody(CavnarType.body, weight: 700)).foregroundStyle(Color.cavnarInk)
                     + HomeMixedText.make(confirm, role: .body))
                        .fixedSize(horizontal: false, vertical: true)
                    if diagnosis.showsAnswers, let key = diagnosis.recKey {
                        RecAnswerRow(key: key, surface: surface, module: surface)
                    }
                }
            } detail: {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    if let summary = diagnosis.summary {
                        CavnarMixedText(summary, role: .secondary)
                    }
                    if diagnosis.alternativeCause != nil, let confirm = diagnosis.whatWouldConfirm {
                        reasoningRow("What would tell them apart", confirm)
                    }
                    if !diagnosis.operationalEvidence.isEmpty {
                        reasoningRow("Cross-checked against",
                                     diagnosis.operationalEvidence
                                        .compactMap { e in e.metric.map { "\($0) \(e.value ?? "")" } }
                                        .joined(separator: "  \u{00B7}  "))
                    }
                }
            }
        }
    }

    private func reasoningRow(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
            CavnarKicker(label)
            CavnarMixedText(text, role: .secondary)
        }
    }
}
