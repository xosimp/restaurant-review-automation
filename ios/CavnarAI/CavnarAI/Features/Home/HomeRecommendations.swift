import SwiftUI

/// "Cavnar recommends", with the button that starts measuring it.
///
/// The delight audit's third structural finding: `hbTrack` on the web is
/// what calls `outcomes.record`, and `outcomes.record` is the only thing
/// that eventually produces the best notification in the product — "That
/// one worked, about $420/month" (`strategy_jobs._tell_owners_what_worked`,
/// whose own docstring notes it is the only notification about money the
/// owner ALREADY made). iOS read `/mobile/api/outcomes` and never posted to
/// it, so a phone-first owner — which is every restaurant owner — could
/// read the results of a loop they had no way to start.
///
/// Only a recommendation that names a metric gets the button. One that does
/// not cannot be measured before and after, and a "Track this" that
/// silently measures nothing would be worse than no button: it would
/// produce a tracker that comes back `unknown` forever.
struct HomeRecommendations: View {
    let recommendations: [HomeRecommendation]
    let viewModel: HomeFollowThroughViewModel
    var onOpenModule: (String) -> Void

    @State private var toast: String?
    /// Keys answered in this session, so the card drops out without a
    /// reload.
    @State private var answered: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HomeSectionHeader(kicker: "Worth your time", title: "Cavnar AI recommends")
            VStack(spacing: 0) {
                let shown = recommendations.filter { !answered.contains($0.key) }.prefix(3)
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, rec in
                    row(rec, number: index + 1,
                        showsDivider: index < shown.count - 1)
                }
            }
            .cavnarCard(.ai)
            if let toast {
                Text(toast)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                    .transition(.opacity)
            }
        }
    }

    private func row(_ rec: HomeRecommendation, number: Int, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Text(String(format: "%02d", number))
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .frame(width: 26, alignment: .leading)
                VStack(alignment: .leading, spacing: 4) {
                    HomeMixedText.make(rec.title, size: 15, weight: 600, color: .cavnarInk)
                    if let evidence = rec.evidence ?? rec.why {
                        HomeMixedText.make(evidence, size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                    // Low confidence is said, not hidden: the engine's own
                    // caution sentence, in the accent so it reads as a caveat.
                    if let c = rec.confidence, c.band == "low", let caution = c.caution {
                        Text(caution)
                            .font(.cavnarBody(12.5, weight: 500))
                            .foregroundStyle(Color.cavnarEmber2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 14) {
                        if rec.metric != nil {
                            Button {
                                Haptic.light()
                                Task {
                                    if let message = await viewModel.track(rec) {
                                        withAnimation { toast = message }
                                    }
                                }
                            } label: {
                                Text(viewModel.tracked.contains(rec.key) ? "Tracking" : "Track this")
                                    .font(.cavnarBody(13, weight: 700))
                                    .foregroundStyle(viewModel.tracked.contains(rec.key)
                                                     ? Color.cavnarGreen : Color.cavnarEmber2)
                            }
                            .buttonStyle(.plain)
                            .disabled(viewModel.tracked.contains(rec.key))
                            .accessibilityHint("Takes a baseline now and measures the result in a few weeks")
                        }
                        if let module = rec.module {
                            Button {
                                Haptic.light()
                                onOpenModule(module)
                            } label: {
                                Text("Open \(module.capitalized)")
                                    .font(.cavnarBody(13, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .buttonStyle(.plain)
                        }
                        Spacer(minLength: 0)
                        // Two answers that are not "hide for a fortnight".
                        ForEach(["done", "not_for_us"], id: \.self) { kind in
                            Button {
                                Haptic.light()
                                Task {
                                    if let message = await viewModel.answer(rec, kind: kind) {
                                        withAnimation { answered.insert(rec.key); toast = message }
                                    }
                                }
                            } label: {
                                Text(kind == "done" ? "Done" : "Not for us")
                                    .font(.cavnarBody(12.5, weight: 600))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.top, 2)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 12)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }
}
