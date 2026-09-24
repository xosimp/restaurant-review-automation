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
///
/// Every card answers five questions (the recommendation-trust audit): what
/// to do (the title, verb first), why now, the dollars at stake when they
/// were measured, ONE confidence with what it rests on, and what happens if
/// it is ignored — plus the timeframe and impact. The confidence is the
/// shared `ConfidenceLine` (a percentage with "Why?"); the separate
/// evidence-strength pill is gone. Every answer (Done, Not for us, Hide with its
/// reason, Assign) goes to the server, so it holds on every other surface.
struct HomeRecommendations: View {
    let recommendations: [HomeRecommendation]
    let viewModel: HomeFollowThroughViewModel
    var assignees: [HomeAssignee] = []
    var quieter: [HomeQuietKind] = []
    var onOpenModule: (String) -> Void
    /// Something changed server-side (an answer, a restore) — reload Home.
    var onChanged: () -> Void = {}

    @State private var toast: String?
    /// Keys answered in this session, so the card drops out without a
    /// reload.
    @State private var answered: Set<String> = []
    /// The card whose answer is asking why — its "Not for us", or a hide
    /// on a card that was hidden before. Held apart from the dialog's own
    /// flag so the answer still has the card after the dialog closes.
    @State private var askingWhy: HomeRecommendation?
    @State private var askingWhyIsHide = false
    @State private var showingWhy = false
    @State private var explaining: HomeRecommendation?

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
            if !quieter.isEmpty {
                quieterLine
            }
            if let toast {
                Text(toast)
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarGreen)
                    .transition(.opacity)
            }
        }
        // "Not for us" asks why — the six reasons, one tap. A hide on a
        // card hidden before asks the same, with "Just hide it" beside them.
        .recReasonDialog(isPresented: $showingWhy,
                         title: askingWhyIsHide ? "You\u{2019}ve hidden this before \u{2014} why?"
                                                : "Why isn\u{2019}t it for you?",
                         message: "Tell Cavnar AI why, so it stops suggesting it.",
                         skipLabel: askingWhyIsHide ? "Just hide it for two weeks" : nil,
                         onSkip: askingWhyIsHide ? { answerWhy(kind: "recommendation", reason: nil) } : nil,
                         onPick: { reason in answerWhy(kind: "not_for_us", reason: reason) })
        .alert("What else could explain it", isPresented: Binding(
            get: { explaining != nil }, set: { if !$0 { explaining = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(explaining?.alternative ?? "")
        }
    }

    private func askWhy(_ rec: HomeRecommendation, isHide: Bool) {
        askingWhy = rec
        askingWhyIsHide = isHide
        showingWhy = true
    }

    private func answerWhy(kind: String, reason: RecReason?) {
        guard let rec = askingWhy else { return }
        askingWhy = nil
        submit(rec, kind: kind, reasonCode: reason?.code)
    }

    private func submit(_ rec: HomeRecommendation, kind: String, reasonCode: String? = nil) {
        Task {
            if let message = await viewModel.answer(rec, kind: kind, reasonCode: reasonCode) {
                withAnimation { answered.insert(rec.key); toast = message }
            }
        }
    }

    private var quieterLine: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Quieter: \(quieter.map(\.label).joined(separator: ", ")) — the last four went by unanswered.")
                .font(.cavnarBody(12.5, weight: 500))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(quieter) { q in
                Button {
                    Haptic.light()
                    Task {
                        if await viewModel.restoreKind(q.kind) {
                            withAnimation { toast = "It will show again" }
                            onChanged()
                        }
                    }
                } label: {
                    Text("Show \(q.label.lowercased()) again")
                        .font(.cavnarBody(12.5, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
            }
        }
    }

    /// Dollars at stake, timeframe, impact. The old "evidence strength" pill
    /// is gone (CA4 F1): the card's one confidence line says how sure, with
    /// what it rests on behind "Why?".
    static func chips(_ rec: HomeRecommendation) -> [String] {
        var out: [String] = []
        // The calibrated figure when the server corrected it by this
        // restaurant's measured results (F6), with the note beside it.
        if let d = rec.statedDollars {
            out.append("$\(d.commaFormatted)/mo at stake" + (rec.dollarsNote.map { " (\($0))" } ?? ""))
        }
        if let t = rec.timeframe { out.append(t) }
        if let i = rec.impact { out.append(i) }
        return out
    }

    private func row(_ rec: HomeRecommendation, number: Int, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Text(String(format: "%02d", number))
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .frame(width: 26, alignment: .leading)
                VStack(alignment: .leading, spacing: 5) {
                    HomeMixedText.make(rec.title, size: 15, weight: 600, color: .cavnarInk)
                    if rec.modelWritten == true {
                        ClaimKindTag(kind: nil, modelWritten: true)
                    }
                    if let why = rec.why {
                        HomeMixedText.make(why, size: 13, weight: 500, color: .cavnarInk2)
                    }
                    let meta = Self.chips(rec)
                    if !meta.isEmpty {
                        HomeMixedText.make(meta.joined(separator: " · "), size: 11.5, weight: 600, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let evidence = rec.evidence {
                        HomeMixedText.make(evidence, size: 12.5, weight: 500, color: .cavnarInk3)
                    }
                    // ONE confidence per card: the percentage, what it rests
                    // on, and "Why?" for the three dimensions behind it. An
                    // older server's band-only object (and its low-band
                    // caution) renders through the same line.
                    if let c = rec.confidence {
                        ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: "home")
                    }
                    if let ignored = rec.ifIgnored {
                        (Text("If ignored: ").font(.cavnarBody(12.5, weight: 700)).foregroundColor(.cavnarInk2)
                         + Text(ignored).font(.cavnarBody(12.5, weight: 500)).foregroundColor(.cavnarInk3))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    actions(rec)
                        .padding(.top, 2)
                    answers(rec)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 12)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    @ViewBuilder
    private func actions(_ rec: HomeRecommendation) -> some View {
        HStack(spacing: 14) {
            if let a = rec.action, a.kind == "reprice" {
                Button {
                    Haptic.light()
                    Task {
                        if let message = await viewModel.reprice(rec) {
                            withAnimation { answered.insert(rec.key); toast = message }
                        }
                    }
                } label: {
                    Text(a.label ?? "Reprice")
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
            }
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
                    Text("Open \(module == "inventory" ? "Food Cost" : module.capitalized)")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .buttonStyle(.plain)
            }
            if rec.alternative != nil {
                Button {
                    Haptic.light()
                    explaining = rec
                    // The owner opened the reasoning (#38).
                    RecEvidenceLog.viewed(key: rec.key, surface: "home", module: "home")
                } label: {
                    Text("Could also be…")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .buttonStyle(.plain)
            }
            Spacer(minLength: 0)
        }
    }

    /// Assign, and the three answers that are not "Track this": Done, Not
    /// for us, and Hide — the second hide asks why.
    private func answers(_ rec: HomeRecommendation) -> some View {
        HStack(spacing: 14) {
            if !assignees.isEmpty {
                Menu {
                    ForEach(assignees) { person in
                        Button(person.name) {
                            Task {
                                if let message = await viewModel.assign(rec, to: person) {
                                    withAnimation { answered.insert(rec.key); toast = message }
                                }
                            }
                        }
                    }
                } label: {
                    Text("Assign")
                        .font(.cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer(minLength: 0)
            ForEach(["done", "not_for_us"], id: \.self) { kind in
                Button {
                    Haptic.light()
                    if kind == "not_for_us" {
                        askWhy(rec, isHide: false)
                    } else {
                        submit(rec, kind: kind)
                    }
                } label: {
                    Text(kind == "done" ? "Done" : "Not for us")
                        .font(.cavnarBody(12.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .buttonStyle(.plain)
            }
            Button {
                Haptic.light()
                if (rec.timesHidden ?? 0) >= 1 {
                    askWhy(rec, isHide: true)
                } else {
                    submit(rec, kind: "recommendation")
                }
            } label: {
                Text("Hide")
                    .font(.cavnarBody(12.5, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .buttonStyle(.plain)
            .accessibilityHint("Hides it for two weeks, everywhere Cavnar AI would say it")
        }
    }
}
