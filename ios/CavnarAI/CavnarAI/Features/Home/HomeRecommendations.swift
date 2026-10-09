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
    var onOpenModule: (String) -> Void
    /// Something changed server-side (an answer, a restore) — reload Home.
    var onChanged: () -> Void = {}
    /// The newest recommendation this login hid (home_brief `dismissed`),
    /// and the undo that brings it back — the web's "Restore hidden".
    var restorable: HomeDismissedRec? = nil
    var onRestore: ((HomeDismissedRec) async -> Bool)? = nil
    /// Opens every row's Details at once — the single-card sheet a Needs
    /// you row opens (H7).
    var startsExpanded = false

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
            Text("Cavnar AI recommends")
                .cavnarText(.headline)
                .accessibilityAddTraits(.isHeader)
            VStack(spacing: 0) {
                let shown = recommendations.filter { !answered.contains($0.key) }.prefix(3)
                if shown.isEmpty {
                    Text("Nothing to recommend yet \u{2014} that changes as your data grows.")
                        .cavnarText(.body)
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
            if let toast {
                Text(toast)
                    .cavnarText(.secondary, color: .cavnarGreen)
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
        .onAppear { if startsExpanded { expanded = Set(recommendations.map(\.key)) } }
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
                Image(systemName: "arrow.uturn.backward").font(.cavnar(.caption))
                Text("Restore hidden").font(.cavnar(.label))
            }
            .foregroundStyle(Color.cavnarEmber2)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(HomeTextButtonStyle())
        .accessibilityLabel("Restore hidden: \(hidden.title)")
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
    /// the dollars at stake and one answer, at phone reading size — then
    /// "Details" opens how sure (a percentage with "Why?", moved there
    /// 10/8/26), why, what it rests on, what happens if it is ignored,
    /// "Could also be…", Assign, Not for us and Hide in place. Every answer
    /// is still one tap from the row.
    private func row(_ rec: HomeRecommendation, number: Int, showsDivider: Bool) -> some View {
        let open = expanded.contains(rec.key)
        return VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                Text(String(format: "%02d", number))
                    .cavnarText(.figureS, color: .cavnarEmber2)
                    .frame(width: 30, alignment: .leading)
                VStack(alignment: .leading, spacing: 6) {
                    HomeMixedText.make(rec.title, role: .lead)
                        .fixedSize(horizontal: false, vertical: true)
                    // What another module knows against it (M3's trim
                    // guard) and what was said before about it (memory
                    // round 9/29/26, M1) — on the row, never behind
                    // Details: they change how the owner answers.
                    if let caution = rec.caution {
                        RecCautionLine(text: caution)
                    }
                    RecMemoryNote(previous: rec.previousAnswer, delegate: rec.delegateAnswer,
                                  retest: rec.retest == true)
                    if let stake = Self.stake(rec) {
                        HomeMixedText.make(stake, role: .label,
                                           color: .cavnarInk2, numberColor: .cavnarInk)
                            .lineLimit(2)
                    }
                    // How sure, as a measured %, on the row (iOS re-audit
                    // M13) — no longer behind Details.
                    if let c = rec.confidence {
                        ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: "home",
                                       compact: true)
                    }
                    // Advice that pulls against other advice: a decision,
                    // so it stays on the row (DS §12).
                    if let conflict = rec.conflict {
                        RecConflictPanel(conflict: conflict, onSettled: onChanged)
                    }
                    HStack(spacing: 22) {
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
                                    .font(.cavnar(.label))
                                Image(systemName: open ? "chevron.up" : "chevron.down")
                                    .font(.cavnar(.caption))
                            }
                            .foregroundStyle(Color.cavnarInk2)
                        }
                        .buttonStyle(HomeTextButtonStyle())
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
        VStack(alignment: .leading, spacing: 8) {
            if rec.modelWritten == true {
                Text("Written by Cavnar AI from your numbers").cavnarText(.caption)
            }
            if let why = rec.why {
                HomeMixedText.make(why, role: .body)
                    .fixedSize(horizontal: false, vertical: true)
            }
            let meta = Array(Self.chips(rec).dropFirst(Self.stake(rec) == nil ? 0 : 1))
            if !meta.isEmpty {
                HomeMixedText.make(meta.joined(separator: " · "), role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let evidence = rec.evidence {
                HomeMixedText.make(evidence, role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let ignored = rec.ifIgnored {
                (Text(OwnerCopy.ifIgnoredLabel).font(.cavnar(.label)).foregroundColor(.cavnarInk2)
                 + Text(ignored).font(.cavnar(.secondary)).foregroundColor(.cavnarInk2))
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
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(HomeTextButtonStyle())
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
                    .font(.cavnar(.label))
                    .foregroundStyle(viewModel.tracked.contains(rec.key)
                                     ? Color.cavnarGreen : Color.cavnarEmber2)
            }
            .buttonStyle(HomeTextButtonStyle())
            .disabled(viewModel.tracked.contains(rec.key))
            .accessibilityHint("Takes a baseline now and measures the result in a few weeks")
        case .done:
            Button {
                Haptic.light()
                submit(rec, kind: "done")
            } label: {
                Text("Done")
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .buttonStyle(HomeTextButtonStyle())
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
                        .cavnarText(.label, color: .cavnarEmber2)
                }
                .buttonStyle(HomeTextButtonStyle())
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
                        .cavnarText(.label, color: viewModel.tracked.contains(rec.key)
                                    ? Color.cavnarGreen : Color.cavnarEmber2)
                }
                .buttonStyle(HomeTextButtonStyle())
                .disabled(viewModel.tracked.contains(rec.key))
                .accessibilityHint("Takes a baseline now and measures the result in a few weeks")
            }
            if let module = rec.module {
                Button {
                    Haptic.light()
                    onOpenModule(module)
                } label: {
                    Text("Open \(module == "inventory" ? "Food Cost" : module.capitalized)")
                        .cavnarText(.label, color: .cavnarInk2)
                }
                .buttonStyle(HomeTextButtonStyle())
            }
            if rec.alternative != nil {
                Button {
                    Haptic.light()
                    explaining = rec
                    // The owner opened the reasoning (#38).
                    RecEvidenceLog.viewed(key: rec.key, surface: "home", module: "home")
                } label: {
                    Text("Could also be…")
                        .cavnarText(.label, color: .cavnarInk2)
                }
                .buttonStyle(HomeTextButtonStyle())
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
                        .cavnarText(.label, color: .cavnarInk2)
                        .cavnarHitTarget()
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
                    Text(kind == "done" ? "Done" : RecAnswer.notForUs.label)
                        .cavnarText(.label, color: .cavnarInk2)
                }
                .buttonStyle(HomeTextButtonStyle())
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
                    .cavnarText(.label, color: .cavnarInk2)
            }
            .buttonStyle(HomeTextButtonStyle())
            .accessibilityHint("Hides it for two weeks, everywhere Cavnar AI would say it")
        }
    }
}
