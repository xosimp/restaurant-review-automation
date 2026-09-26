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
    /// The newest recommendation this login hid (home_brief `dismissed`),
    /// and the undo that brings it back — the web's "Restore hidden".
    var restorable: HomeDismissedRec? = nil
    var onRestore: ((HomeDismissedRec) async -> Bool)? = nil

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
    /// Rows whose Details are open (density #23).
    @State private var expanded: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HomeSectionHeader(kicker: "Worth your time", title: "Cavnar AI recommends")
            VStack(spacing: 0) {
                let shown = recommendations.filter { !answered.contains($0.key) }.prefix(3)
                if shown.isEmpty {
                    Text("Nothing to recommend yet \u{2014} that changes as your data grows.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.vertical, 8)
                }
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, rec in
                    row(rec, number: index + 1,
                        showsDivider: index < shown.count - 1)
                }
            }
            .cavnarCard(.ai)
            if let hidden = restorable, let onRestore {
                restoreButton(hidden, onRestore)
            }
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

    /// "Restore hidden": the last card this login hid comes back (web
    /// `data-undo`, parity audit #1).
    private func restoreButton(_ hidden: HomeDismissedRec,
                               _ restore: @escaping (HomeDismissedRec) async -> Bool) -> some View {
        Button {
            Haptic.light()
            Task {
                if await restore(hidden) { withAnimation { toast = "It will show again" } }
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "arrow.uturn.backward").font(.system(size: 11, weight: .bold))
                Text("Restore hidden").font(.cavnarBody(12.5, weight: 700))
            }
            .foregroundStyle(Color.cavnarEmber2)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Restore hidden: \(hidden.title)")
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
            // The figure's kind picks its word when the server sends one.
            out.append("$\(d.commaFormatted)/mo \(OwnerCopy.kindWord(rec.dollarsKind) ?? "at stake")" + (rec.dollarsNote.map { " (\($0))" } ?? ""))
        }
        // What that figure covers (B4 H7).
        if rec.statedDollars != nil, let b = rec.dollarsBasis { out.append(b) }
        if let t = rec.timeframe { out.append(t) }
        if let i = rec.impact { out.append(i) }
        return out
    }

    /// The row's one answer (density #23): Reprice when the server offers
    /// it, else Track this for a recommendation that names a metric, else
    /// Done. Everything else is behind Details.
    enum PrimaryAnswer: Equatable { case reprice, track, done }

    static func primaryAnswer(_ rec: HomeRecommendation) -> PrimaryAnswer {
        if let a = rec.action, a.kind == "reprice" { return .reprice }
        if rec.metric != nil { return .track }
        return .done
    }

    /// "$420/mo at stake" — the first chip when the card carries dollars;
    /// nil otherwise. The row's only figure; the rest of the chips (basis,
    /// timeframe, impact) are in Details.
    static func stake(_ rec: HomeRecommendation) -> String? {
        rec.statedDollars == nil ? nil : chips(rec).first
    }

    /// A shortlist row, not a report (density #23): the verb-first title,
    /// the dollars at stake, ONE confidence (a percentage with "Why?") and
    /// one answer — then "Details" opens why, what it rests on, what
    /// happens if it is ignored, "Could also be…", Assign, Not for us and
    /// Hide in place. Every answer is still one tap from the row.
    private func row(_ rec: HomeRecommendation, number: Int, showsDivider: Bool) -> some View {
        let open = expanded.contains(rec.key)
        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Text(String(format: "%02d", number))
                    .font(.cavnarNumber(17, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .frame(width: 26, alignment: .leading)
                VStack(alignment: .leading, spacing: 6) {
                    HomeMixedText.make(rec.title, size: CavnarType.body, weight: 600, color: .cavnarInk)
                        .fixedSize(horizontal: false, vertical: true)
                    if let stake = Self.stake(rec) {
                        HomeMixedText.make(stake, size: CavnarType.secondary, weight: 700,
                                           color: .cavnarInk2, numberColor: .cavnarInk)
                            .lineLimit(2)
                    }
                    if let c = rec.confidence {
                        ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: "home",
                                       compact: true)
                    }
                    HStack(spacing: 16) {
                        primaryButton(rec)
                        Button {
                            Haptic.light()
                            withAnimation(.easeOut(duration: 0.2)) {
                                if open { expanded.remove(rec.key) } else { expanded.insert(rec.key) }
                            }
                            if !open { RecEvidenceLog.viewed(key: rec.key, surface: "home", module: "home") }
                        } label: {
                            HStack(spacing: 4) {
                                Text(open ? "Less" : "Details")
                                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                                Image(systemName: open ? "chevron.up" : "chevron.down")
                                    .font(.system(size: 10, weight: .bold))
                            }
                            .foregroundStyle(Color.cavnarInk3)
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint(open ? "Hides the detail" : "Shows why, what it rests on and the other answers")
                        Spacer(minLength: 0)
                    }
                    .padding(.top, 2)
                    if open {
                        details(rec)
                            .transition(.opacity)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 12)
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
            }
        }
    }

    /// Everything the row used to carry open, now behind Details.
    @ViewBuilder
    private func details(_ rec: HomeRecommendation) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if rec.modelWritten == true {
                ClaimKindTag(kind: nil, modelWritten: true)
            }
            if let why = rec.why {
                HomeMixedText.make(why, size: CavnarType.secondary, weight: 500, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let meta = Array(Self.chips(rec).dropFirst(Self.stake(rec) == nil ? 0 : 1))
            if !meta.isEmpty {
                HomeMixedText.make(meta.joined(separator: " · "), size: 11.5, weight: 600, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let evidence = rec.evidence {
                HomeMixedText.make(evidence, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let ignored = rec.ifIgnored {
                (Text(OwnerCopy.ifIgnoredLabel).font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundColor(.cavnarInk2)
                 + Text(ignored).font(.cavnarBody(CavnarType.caption, weight: 500)).foregroundColor(.cavnarInk3))
                    .fixedSize(horizontal: false, vertical: true)
            }
            actions(rec, excluding: Self.primaryAnswer(rec))
                .padding(.top, 2)
            answers(rec, excluding: Self.primaryAnswer(rec))
        }
        .padding(.top, 4)
    }

    @ViewBuilder
    private func primaryButton(_ rec: HomeRecommendation) -> some View {
        switch Self.primaryAnswer(rec) {
        case .reprice:
            Button {
                Haptic.light()
                Task {
                    if let message = await viewModel.reprice(rec) {
                        withAnimation { answered.insert(rec.key); toast = message }
                    }
                }
            } label: {
                Text(rec.action?.label ?? "Reprice")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(.plain)
        case .track:
            Button {
                Haptic.light()
                Task {
                    if let message = await viewModel.track(rec) {
                        withAnimation { toast = message }
                    }
                }
            } label: {
                Text(viewModel.tracked.contains(rec.key) ? "Measuring" : "Measure it")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(viewModel.tracked.contains(rec.key)
                                     ? Color.cavnarGreen : Color.cavnarEmber2)
            }
            .buttonStyle(.plain)
            .disabled(viewModel.tracked.contains(rec.key))
            .accessibilityHint("Takes a baseline now and measures the result in a few weeks")
        case .done:
            Button {
                Haptic.light()
                submit(rec, kind: "done")
            } label: {
                Text("Done")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(.plain)
        }
    }

    @ViewBuilder
    private func actions(_ rec: HomeRecommendation, excluding primary: PrimaryAnswer? = nil) -> some View {
        HStack(spacing: 14) {
            if primary != .reprice, let a = rec.action, a.kind == "reprice" {
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
            if primary != .track, rec.metric != nil {
                Button {
                    Haptic.light()
                    Task {
                        if let message = await viewModel.track(rec) {
                            withAnimation { toast = message }
                        }
                    }
                } label: {
                    Text(viewModel.tracked.contains(rec.key) ? "Measuring" : "Measure it")
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
    private func answers(_ rec: HomeRecommendation, excluding primary: PrimaryAnswer? = nil) -> some View {
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
            ForEach(primary == .done ? ["not_for_us"] : ["done", "not_for_us"], id: \.self) { kind in
                Button {
                    Haptic.light()
                    if kind == "not_for_us" {
                        askWhy(rec, isHide: false)
                    } else {
                        submit(rec, kind: kind)
                    }
                } label: {
                    Text(kind == "done" ? "Done" : "Pass")
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
