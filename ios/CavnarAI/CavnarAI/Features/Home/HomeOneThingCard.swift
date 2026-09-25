import SwiftUI

/// "If you only do one thing" — Home's cross-module pick
/// (GET /mobile/api/cross-module → fix_first), in the slot DESIGN_SYSTEM
/// §11b gives it: after Needs attention, before the recommendations. The
/// web's focus card has always led with it; the phone never showed it, so
/// the one recommendation both Homes present was the one iOS could not
/// answer (rec-ROI #26).
///
/// Always an action (business_intelligence.pick_one_thing). The modules it
/// joins are drawn with the ember thread only when there are two — a real
/// link in the payload, never decoration. "Could also be…" opens what else
/// would explain it, and records that the owner looked (#38).
struct HomeOneThingCard: View {
    let viewModel: HomeFollowThroughViewModel
    @State private var explaining = false

    var body: some View {
        if let ff = viewModel.fixFirst, let what = ff.what, !what.isEmpty {
            VStack(alignment: .leading, spacing: 14) {
                HomeSectionHeader(kicker: "Start here", title: "If you only do one thing")
                VStack(alignment: .leading, spacing: 10) {
                    if let modules = ff.modules, modules.count > 1 {
                        HStack(spacing: 8) {
                            ForEach(Array(modules.enumerated()), id: \.offset) { index, module in
                                if index > 0 { EmberThread(axis: .horizontal, length: 22) }
                                Text(RecSummaryFormat.moduleLabel(module).uppercased())
                                    .font(.cavnarBody(10.5, weight: 700))
                                    .tracking(1.0)
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    HomeMixedText.make(what, size: 18, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    // What kind of claim this is (K4) — measured, computed,
                    // forecast, inferred.
                    ClaimKindTag(kind: ff.claimKind)
                    if let why = ff.why, !why.isEmpty {
                        HomeMixedText.make(why.prefix(1).uppercased() + why.dropFirst(), size: 13.5, weight: 500,
                                           color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let confirm = ff.confirmBy, !confirm.isEmpty, confirm != what {
                        HomeMixedText.make("To confirm: " + confirm, size: 13, weight: 600, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let evidence = ff.evidence?.prefix(3), !evidence.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            ForEach(Array(evidence.enumerated()), id: \.offset) { _, line in
                                HomeMixedText.make("\u{00B7} " + line, size: 12.5, weight: 500, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                    // How sure — the most prominent item on Home carried a
                    // confidence it never showed (CA4, CA1 H8).
                    if let c = ff.confidence {
                        ConfidenceLine(confidence: c, recKey: ff.answerKey, surface: "home", module: "home")
                    }
                    // The money fallback is a range with its label — never
                    // one figure pulled out of it (CA4 F3).
                    if let range = ff.moneyRange {
                        VStack(alignment: .leading, spacing: 2) {
                            HomeMixedText.make(range, size: 19, weight: 600, color: .cavnarInk,
                                               numberWeight: 600)
                            if let label = ff.money?.label, !label.isEmpty {
                                HomeMixedText.make(label, size: 12.5, weight: 600, color: .cavnarInk3)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 14) {
                        // Calibrated by this restaurant's measured results
                        // when the server sent it (F6), and said so.
                        if let dollars = ff.statedDollars {
                            VStack(alignment: .leading, spacing: 2) {
                                (Text("$" + dollars.commaFormatted).font(.cavnarNumber(19, weight: 600))
                                    .foregroundColor(.cavnarInk)
                                 + Text("/month").font(.cavnarBody(12.5, weight: 600)).foregroundColor(.cavnarInk3))
                                if let note = ff.dollarsNote {
                                    HomeMixedText.make(note, size: 12, weight: 500, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                                // What the figure covers (B4 H7).
                                if let basis = ff.dollarsBasis {
                                    HomeMixedText.make(basis, size: 12, weight: 500, color: .cavnarInk3)
                                        .fixedSize(horizontal: false, vertical: true)
                                }
                            }
                        }
                        Spacer(minLength: 0)
                        if ff.alternative != nil {
                            Button {
                                Haptic.light()
                                explaining = true
                                RecEvidenceLog.viewed(key: ff.answerKey, surface: "home", module: "home")
                            } label: {
                                Text("Could also be\u{2026}")
                                    .font(.cavnarBody(13, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    HomeAskLink(question: "Walk me through this: \(what)")
                    if ff.answerable == true, let key = ff.answerKey {
                        RecAnswerRow(key: key, surface: "home", module: "home")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .cavnarCard(.hero)
            }
            .alert("What else could explain it", isPresented: $explaining) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(ff.alternative ?? "")
            }
        }
    }
}
