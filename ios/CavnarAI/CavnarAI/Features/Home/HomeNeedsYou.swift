import SwiftUI

/// "Needs you (N)" — Home's ONE ranked list of what the owner must act on
/// (iOS readability round, 10/8/26, #5). It replaces the eight lists an
/// evening scroll used to cross: Needs attention (the action deck), the
/// header's quick chips, the brief lines that carry an action, Open issues,
/// Still open, the check-ins, the comps/voids flags and the proposed and
/// missed goals.
///
/// Presentation only — every row keeps its source's own action, endpoint
/// and server-side record: a publish still asks first (HomeView's confirm
/// card), Not today / Hide still post /home/dismiss, an issue still
/// resolves through its Undo window, a Still-open step still opens its
/// confirm card before anything goes out, a goal still confirms through
/// /goals, a check-in still posts /recs/checkin, a loss flag's Done / Pass
/// still records under its own key.
///
/// Each row: a status dot, the title, ONE line of why, ONE 44pt primary,
/// a "…" menu (Not today, Hide, Ask and the row's other answers) and a swipe
/// for the safe second action. The list shows the first few — as many
/// attention items as the server logs as shown (home_brief
/// HOME_ATTENTION_SHOWN, the one-thing card counting as one) — and
/// "+N more" opens the rest in place.
struct HomeNeedsYou: View {
    /// Needs attention, less the item the one-thing card leads with, plus
    /// every cross-module link as a row.
    let items: [NeedsAttentionItem]
    /// True when the one-thing card led with an attention item — it is the
    /// first of the four the server records as shown.
    var leadTookAttention: Bool = false
    /// The header's quick actions no attention row already carries.
    var quick: [HomeQuickAction] = []
    /// Brief lines that carry their own action (the rest stay in the brief).
    var briefActions: [HomeDayViewModel.BriefLine] = []
    /// The open recommendations, less the one Today's focus leads with —
    /// decisions, so rows here, never inside the closed More group (iOS
    /// re-audit H7, DESIGN_SYSTEM §12). Their record stays in More.
    var recommendations: [HomeRecommendation] = []
    var assignees: [HomeAssignee] = []
    let day: HomeDayViewModel
    let followThrough: HomeFollowThroughViewModel
    var busyPublishing: Bool = false
    /// The all-clear's reason when nothing is flagged on stale or no data.
    var notClearReason: String? = nil
    /// The one-thing card took the only attention item.
    var leadTookOnlyItem: Bool = false

    var onPrimary: (NeedsAttentionItem) -> Void
    var onSecondary: (NeedsAttentionItem) -> Void
    var onDismiss: ((NeedsAttentionItem, String) -> Void)? = nil
    var onQuick: (HomeQuickAction) -> Void = { _ in }
    /// A brief line's own action (`action.nav`).
    var onOpenNav: (String) -> Void = { _ in }
    /// A Still-open row's place.
    var onOpenPath: (NavPath) -> Void = { _ in }
    var onOpenModule: (String) -> Void = { _ in }
    var onChanged: () -> Void = {}
    /// The list's count, whenever it changes — the Home tab's badge (H8).
    var onCount: (Int) -> Void = { _ in }
    /// A recommendation answer's sentence, for the screen's posted check.
    var onPosted: (String) -> Void = { _ in }
    /// An issue a push named (parity audit #11): scrolled to and pulsed.
    var focusIssue: HomeIssueFocus? = nil
    var onFocus: (Int) -> Void = { _ in }

    @Environment(DeepLinkRouter.self) private var router
    @State private var openIssue: HomeDayViewModel.Issue?
    @State private var askingCover: HomeDayViewModel.CoverAsk?
    @State private var pulsing: Int?
    @State private var proposing: HomeActionProposal?
    @State private var sendingAnyway: ActionItem?
    @State private var checkingIn: RecOutcome?
    @State private var closingGoal: ProposedGoal?
    @State private var openLine: HomeDayViewModel.BriefLine?
    @State private var note: String?
    @State private var expanded = false
    /// Recommendations answered here, dropped before the reload lands.
    @State private var answeredRecs: Set<String> = []
    /// The recommendation whose Not for us (or second Hide) is asking why.
    @State private var recAskingWhy: HomeRecommendation?
    @State private var recAskingWhyIsHide = false
    @State private var showingRecWhy = false
    /// A recommendation's full card (why, what it rests on, the other
    /// answers), opened from its row.
    @State private var recDetail: HomeRecommendation?
    /// A request's Deny, asking first (M4).
    @State private var denying: HomeDenyAsk?

    /// The scroll id HomeView's tab badge scrolls to.
    static let anchor = "home-needs-you"

    /// How many rows show before "+N more": the server's attention cap
    /// (HomeActionDeck.shownByDefault, = home_brief HOME_ATTENTION_SHOWN),
    /// less the one the one-thing card already shows.
    static func shownCount(leadTookAttention: Bool) -> Int {
        HomeActionDeck.shownByDefault - (leadTookAttention ? 1 : 0)
    }

    private var entries: [HomeNeedsYouEntry] {
        HomeNeedsYouEntry.ranked(
            attention: items, issues: day.visibleIssues, stillOpen: followThrough.actions,
            brief: briefActions, proposed: followThrough.proposedGoals, missed: followThrough.missedGoals,
            checkIns: followThrough.checkInsDue, loss: followThrough.lossFlags, quick: quick,
            recommendations: recommendations.filter { !answeredRecs.contains($0.key) })
    }

    var body: some View {
        let all = entries
        let cap = Self.shownCount(leadTookAttention: leadTookAttention)
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            header(count: all.count)
            if let pending = day.pendingResolve {
                StaffUndoCapsule(text: "Resolved \u{2014} \(pending.title)") { day.undoResolve() }
            }
            if let error = day.issueError {
                Text(error)
                    .cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if all.isEmpty {
                emptyState
            } else {
                VStack(spacing: 0) {
                    ForEach(Array(all.prefix(cap).enumerated()), id: \.element.id) { index, entry in
                        row(entry, showsDivider: index < min(all.count, cap) - 1 || (all.count > cap && expanded))
                    }
                    if all.count > cap {
                        if expanded {
                            ForEach(Array(all.dropFirst(cap).enumerated()), id: \.element.id) { index, entry in
                                row(entry, showsDivider: index < all.count - cap - 1)
                            }
                        }
                        CavnarMoreToggle(hiddenCount: all.count - cap, isExpanded: $expanded)
                    }
                }
                .padding(.horizontal, CavnarSpace.m)
                .background(Color.cavnarPaper2.opacity(0.85))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous))
            }
            if let note {
                Text(note)
                    .cavnarText(.secondary, color: .cavnarGreen)
                    .transition(.opacity)
            }
        }
        .id(Self.anchor)
        .onChange(of: all.count, initial: true) { _, count in
            if count <= cap { expanded = false }
            onCount(count)
        }
        .recReasonDialog(isPresented: $showingRecWhy,
                         title: recAskingWhyIsHide ? "You\u{2019}ve hidden this before \u{2014} why?"
                                                   : "Why isn\u{2019}t it for you?",
                         message: "Tell Cavnar AI why, so it stops suggesting it.",
                         skipLabel: recAskingWhyIsHide ? "Just hide it for two weeks" : nil,
                         onSkip: recAskingWhyIsHide ? { answerRecWhy(kind: "recommendation", reason: nil) } : nil,
                         onPick: { reason in answerRecWhy(kind: "not_for_us", reason: reason) })
        .confirmationDialog(denying.map { "Deny \u{201C}\($0.item.title)\u{201D}?" } ?? "",
                            isPresented: Binding(get: { denying != nil }, set: { if !$0 { denying = nil } }),
                            titleVisibility: .visible, presenting: denying) { ask in
            Button(ask.step.label, role: .destructive) { perform(ask.step, on: ask.item) }
            Button("Not yet", role: .cancel) {}
        } message: { ask in
            Text(ask.item.detail.map { $0 + " \u{2014} they\u{2019}re told it was declined." }
                 ?? "They\u{2019}re told it was declined.")
        }
        .sheet(item: $recDetail, onDismiss: onChanged) { rec in
            HomeRecommendationSheet(rec: rec, viewModel: followThrough, assignees: assignees,
                                    onOpenModule: { module in recDetail = nil; onOpenModule(module) },
                                    onChanged: onChanged)
        }
        .modifier(HomeNeedsYouChrome(
            day: day, followThrough: followThrough, openIssue: $openIssue, askingCover: $askingCover,
            proposing: $proposing, sendingAnyway: $sendingAnyway, checkingIn: $checkingIn,
            closingGoal: $closingGoal, openLine: $openLine, note: $note,
            onOpenNav: onOpenNav, onChanged: onChanged))
        .task(id: focusIssue) { await focus() }
        .onDisappear { day.keepPendingResolve() }
    }

    // MARK: - Header and empty state

    private func header(count: Int) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
            Text("Needs you")
                .cavnarText(.headline)
                .accessibilityAddTraits(.isHeader)
            if count > 0 {
                Text("(\(count))")
                    .cavnarText(.figureS, color: .cavnarEmber2)
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(count > 0 ? "Needs you, \(count)" : "Needs you")
    }

    @ViewBuilder
    private var emptyState: some View {
        if leadTookOnlyItem {
            // The one-thing card took the only item (web: "Nothing else
            // needs you") — never an "All clear" beside an item.
            Text("Nothing else needs you \u{2014} Cavnar AI is watching.")
                .cavnarText(.body)
        } else {
            AllClearRow(notClearReason: notClearReason)
        }
    }

    // MARK: - Rows

    @ViewBuilder
    private func row(_ entry: HomeNeedsYouEntry, showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            switch entry {
            case .attention(let item): attentionRow(item)
            case .issue(let issue): issueRow(issue)
            case .stillOpen(let item): stillOpenRow(item)
            case .brief(let line): briefRow(line)
            case .proposedGoal(let goal): proposedRow(goal)
            case .missedGoal(let goal): missedRow(goal)
            case .checkIn(let outcome): checkInRow(outcome)
            case .loss(let flag, let first): lossRow(flag, showsNote: first)
            case .quick(let q): quickRow(q)
            case .recommendation(let rec): recommendationRow(rec)
            }
            if showsDivider {
                Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
            }
        }
    }

    private func ask(_ question: String) {
        Haptic.light()
        router.pendingAskScreen = AskScreen(panel: "home")
        router.pendingAskAutoSend = true
        router.pendingAskPrompt = question
    }

    private func attentionRow(_ item: NeedsAttentionItem) -> some View {
        let busy = busyPublishing && item.isPublishAction
        // A cross-module link the owner can answer (M20): its confirm step
        // (Done / Not for us) is the row's answer; Evidence is secondary.
        let linkKey = item.action == "link_evidence"
            ? HomeFollowThroughViewModel.linkAnswerKey(followThrough.link(for: item)) : nil
        return HomeNeedsYouRow(
            tone: Self.tone(item), title: item.title, why: item.detail.isEmpty ? nil : item.detail,
            primary: linkKey != nil ? nil
                : item.cta.map { HomeNeedsYouPrimary(label: $0, busy: busy) { onPrimary(item) } },
            swipe: item.dismissable == true && onDismiss != nil
                ? HomeSwipeAction(label: "Not today", systemImage: "moon", tint: .cavnarInk3) {
                    onDismiss?(item, "snooze")
                } : nil
        ) {
            if linkKey != nil {
                Button { onPrimary(item) } label: { Label("See the evidence", systemImage: "doc.text.magnifyingglass") }
            }
            if let secondary = item.secondary {
                Button { onSecondary(item) } label: { Label(secondary, systemImage: "arrow.up.right") }
            }
            if item.dismissable == true, let onDismiss {
                Button { onDismiss(item, "snooze") } label: { Label("Not today", systemImage: "moon") }
                Button { onDismiss(item, "recommendation") } label: {
                    Label("Hide for two weeks", systemImage: "eye.slash")
                }
            }
            Button { ask("What should I do about this: \(item.title)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            // What was said before about it, and advice it pulls against —
            // both change how the owner answers, so they stay on the row.
            RecMemoryNote(previous: item.previousAnswer, delegate: item.delegateAnswer, compact: true)
            if let conflict = item.conflict {
                RecConflictPanel(conflict: conflict, onSettled: onChanged)
            }
            if let linkKey {
                RecAnswerRow(key: linkKey, surface: "home", module: "home",
                             answers: [.completed, .notForUs],
                             onAnswered: { _ in onChanged() })
                HomeNeedsYouTextLink(label: "See the evidence") { onPrimary(item) }
            }
        }
    }

    private func issueRow(_ issue: HomeDayViewModel.Issue) -> some View {
        let cover = issue.isGroup ? nil : issue.coversToAsk.first
        let primary: HomeNeedsYouPrimary
        if let cover {
            primary = HomeNeedsYouPrimary(label: "Ask \(HomeDayViewModel.firstName(cover))") {
                askingCover = HomeDayViewModel.CoverAsk(issue: issue, name: cover)
            }
        } else {
            primary = HomeNeedsYouPrimary(label: "Resolve") { day.resolveWithUndo(issue) }
        }
        return HomeNeedsYouRow(
            tone: issue.tone, title: issue.title, why: issue.statusLine,
            primary: issue.status == "resolved" ? nil : primary,
            onTap: { openIssue = issue },
            swipe: issue.status == "resolved" ? nil : HomeSwipeAction(
                label: "Resolve", systemImage: "checkmark", tint: .cavnarGreen) { day.resolveWithUndo(issue) }
        ) {
            Button { openIssue = issue } label: { Label("Open the issue", systemImage: "arrow.up.right") }
            if issue.status != "resolved" {
                Button { day.resolveWithUndo(issue) } label: { Label("Resolve", systemImage: "checkmark") }
            }
            ForEach(issue.isGroup ? issue.groupCoverNames : issue.coversToAsk, id: \.self) { name in
                Button { askingCover = HomeDayViewModel.CoverAsk(issue: issue, name: name) } label: {
                    Label("Ask \(HomeDayViewModel.firstName(name)) to cover", systemImage: "person.badge.plus")
                }
            }
            Button { ask("What should I do about this issue: \(issue.title)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            // A role's call-off, gap by gap, one cover per open gap (E-31).
            if issue.isGroup {
                CoverageGapList(issue: issue) { name in
                    askingCover = HomeDayViewModel.CoverAsk(issue: issue, name: name)
                }
            }
            if let sent = day.coverNote[issue.id] {
                Text(sent).cavnarText(.secondary, color: .cavnarGreen)
            }
            // "Did Zed take it?" — the manager's word counts on their record.
            ForEach(issue.askedNames, id: \.self) { name in
                CoverAnswerRow(issueId: issue.id, name: name)
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill(Color.cavnarEmber.opacity(pulsing == issue.id ? 0.16 : 0))
                .padding(.horizontal, -8)
        )
        .id(HomeDayCard.issueAnchor(issue.id))
    }

    private func stillOpenRow(_ item: ActionItem) -> some View {
        let note = followThrough.rowNote[item.key]
        let busy = followThrough.running.contains(item.key)
        let alt = item.action?.alt
        return HomeNeedsYouRow(
            tone: item.tone, title: item.title, why: item.detail,
            primary: item.action.map { a in HomeNeedsYouPrimary(label: a.label, busy: busy) { perform(a.step, on: item) } },
            onTap: item.destination.map { nav -> () -> Void in { onOpenPath(nav) } },
            swipe: alt.map { a in
                HomeSwipeAction(label: a.label, systemImage: HomeFollowThrough.altGlyph(a),
                                tint: HomeFollowThrough.altTint(a)) { performAlt(a, on: item) }
            } ?? HomeSwipeAction(label: "Not today", systemImage: "moon", tint: .cavnarInk3) {
                Task { await followThrough.snooze(item) }
            }
        ) {
            if let nav = item.destination {
                Button { onOpenPath(nav) } label: { Label("Open it", systemImage: "arrow.up.right") }
            }
            if let alt {
                Button(role: HomeFollowThrough.altTint(alt) == .cavnarRed ? .destructive : nil) {
                    performAlt(alt, on: item)
                } label: { Label(alt.label, systemImage: HomeFollowThrough.altGlyph(alt)) }
            }
            Button { Task { await followThrough.snooze(item) } } label: { Label("Not today", systemImage: "moon") }
            Button { ask("What should I do about this: \(item.title)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            if busy { CavnarSkeletonBar(height: 3) }
            if let note { rowNoteView(item, note) }
        }
    }

    private func briefRow(_ line: HomeDayViewModel.BriefLine) -> some View {
        HomeNeedsYouRow(
            tone: line.toneColor, title: line.text, why: nil,
            primary: line.action.map { act in HomeNeedsYouPrimary(label: act.label) { onOpenNav(act.nav) } },
            onTap: { openLine = line }
        ) {
            Button { openLine = line } label: { Label("See the evidence", systemImage: "doc.text.magnifyingglass") }
            if let q = line.ask, !q.isEmpty {
                Button { ask(q) } label: { Label("Ask Cavnar AI", systemImage: "sparkles") }
            }
        } accessory: {
            EmptyView()
        }
    }

    private func proposedRow(_ goal: ProposedGoal) -> some View {
        let canAnswer = followThrough.canConfirmGoals
        return HomeNeedsYouRow(
            tone: .cavnarAmber, title: goal.summary,
            why: canAnswer ? goal.byLine + " \u{00B7} once confirmed, every module judges against it"
                           : "Waiting for the owner to confirm",
            primary: canAnswer ? HomeNeedsYouPrimary(label: "Confirm", busy: followThrough.answeringGoal == goal.id) {
                answerGoal(goal, confirm: true)
            } : nil
        ) {
            if canAnswer {
                Button { answerGoal(goal, confirm: false) } label: { Label("Decline", systemImage: "xmark") }
            }
            Button { ask("Should I set this goal: \(goal.summary)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            EmptyView()
        }
    }

    private func missedRow(_ goal: ProposedGoal) -> some View {
        let canAnswer = followThrough.canConfirmGoals
        return HomeNeedsYouRow(
            tone: .cavnarAmber, title: goal.summary,
            why: canAnswer ? "Its date passed without reaching it" : "Waiting for the owner to renew or close it",
            primary: canAnswer ? HomeNeedsYouPrimary(label: "Another month", busy: followThrough.answeringGoal == goal.id) {
                Task {
                    if let said = await followThrough.answerMissed(goal, renew: true) { withAnimation { note = said } }
                }
            } : nil
        ) {
            if canAnswer {
                Button(role: .destructive) { closingGoal = goal } label: { Label("Close it", systemImage: "xmark") }
            }
            Button { ask("Why did we miss this goal: \(goal.summary)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            if canAnswer, let error = followThrough.missedGoalError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
            }
        }
    }

    private func checkInRow(_ outcome: RecOutcome) -> some View {
        HomeNeedsYouRow(
            tone: .cavnarInk3,
            title: outcome.resultLine ?? outcome.summary ?? outcome.title ?? "Your change was measured",
            why: "Did you make this change?",
            primary: HomeNeedsYouPrimary(label: "Answer") { checkingIn = outcome },
            onTap: { checkingIn = outcome }
        ) {
            Button { checkingIn = outcome } label: { Label("Answer", systemImage: "checklist") }
        } accessory: {
            EmptyView()
        }
    }

    /// A comps/voids flag. Its innocent explanation is rendered with the
    /// signal, never behind a tap: the flag names a person beside a
    /// percentage, and "they may simply have worked the busiest shifts"
    /// is what keeps a question from reading as an accusation.
    private func lossRow(_ flag: HomeFollowThroughViewModel.LossSignals.Flag, showsNote: Bool) -> some View {
        HomeNeedsYouRow(
            tone: .cavnarAmber, title: flag.headline,
            why: flag.alternative.map { "Could also be: " + $0 },
            whyLines: nil,
            primary: nil
        ) {
            Button { ask("Walk me through this: \(flag.headline)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            // Done / Pass — the key the concentration issue is filed under.
            if flag.answerable == true, let key = flag.recKey ?? flag.key {
                RecAnswerRow(key: key, surface: "home", module: "ops")
            }
            if showsNote, let lossNote = followThrough.lossNote {
                CavnarCaveat(title: "Worth a look", detail: lossNote)
            }
        }
    }

    private func quickRow(_ q: HomeQuickAction) -> some View {
        HomeNeedsYouRow(
            tone: .cavnarInk3, title: q.chipLabel, why: nil,
            primary: HomeNeedsYouPrimary(label: q.kind == "publish_replies" ? "Review" : "Open") { onQuick(q) }
        ) {
            Button { onQuick(q) } label: { Label(q.label, systemImage: "arrow.up.right") }
        } accessory: {
            EmptyView()
        }
    }

    /// An open recommendation (H7): what to do, the dollars at stake (else
    /// why), ONE answer as the primary — Reprice, Measure it or Done, the
    /// card's own rule — and how sure on the row (M13). Not for us, Hide,
    /// Assign and the rest are in "…"; a tap opens the full card.
    private func recommendationRow(_ rec: HomeRecommendation) -> some View {
        let primary = HomeRecommendations.primaryAnswer(rec)
        let measuring = followThrough.tracked.contains(rec.key)
        let label: String = {
            switch primary {
            case .reprice: return rec.action?.label ?? "Reprice"
            case .track: return measuring ? "Measuring" : "Measure it"
            case .done: return "Done"
            }
        }()
        return HomeNeedsYouRow(
            tone: .cavnarAmber, title: rec.title,
            why: HomeRecommendations.stake(rec) ?? rec.why,
            primary: (primary == .track && measuring) ? nil
                : HomeNeedsYouPrimary(label: label) { answerRec(rec, primary) },
            onTap: {
                RecEvidenceLog.viewed(key: rec.key, surface: "home", module: "home")
                recDetail = rec
            },
            swipe: HomeSwipeAction(label: "Hide", systemImage: "eye.slash", tint: .cavnarInk3) { hideRec(rec) }
        ) {
            Button { recDetail = rec } label: { Label("See the evidence", systemImage: "doc.text.magnifyingglass") }
            if primary != .done {
                Button { submitRec(rec, kind: "done") } label: { Label("Done", systemImage: "checkmark") }
            }
            if primary != .track, rec.metric != nil, !measuring {
                Button { answerRec(rec, .track) } label: { Label("Measure it", systemImage: "gauge.with.dots.needle.33percent") }
            }
            Button {
                recAskingWhy = rec
                recAskingWhyIsHide = false
                showingRecWhy = true
            } label: { Label(RecAnswer.notForUs.label, systemImage: "xmark") }
            Button { hideRec(rec) } label: { Label("Hide for two weeks", systemImage: "eye.slash") }
            ForEach(assignees) { person in
                Button {
                    Task {
                        if let said = await followThrough.assign(rec, to: person) {
                            answeredRecs.insert(rec.key)
                            onPosted(said)
                            onChanged()
                        }
                    }
                } label: { Label("Assign to \(person.name)", systemImage: "person.badge.plus") }
            }
            if let module = rec.module {
                Button { onOpenModule(module) } label: {
                    Label("Open \(module == "inventory" ? "Food Cost" : module.capitalized)", systemImage: "arrow.up.right")
                }
            }
            Button { ask("Should I do this: \(rec.title)") } label: {
                Label("Ask Cavnar AI", systemImage: "sparkles")
            }
        } accessory: {
            // What another module knows against it and what was said
            // before — they change the answer, so they stay on the row.
            if let caution = rec.caution {
                RecCautionLine(text: caution)
            }
            RecMemoryNote(previous: rec.previousAnswer, delegate: rec.delegateAnswer,
                          retest: rec.retest == true, compact: true)
            if let conflict = rec.conflict {
                RecConflictPanel(conflict: conflict, onSettled: onChanged)
            }
            // How sure, as a measured %, on the row (M13).
            if let c = rec.confidence {
                ConfidenceLine(confidence: c, recKey: rec.key, surface: "home", module: "home", compact: true)
            }
        }
    }

    private func answerRec(_ rec: HomeRecommendation, _ primary: HomeRecommendations.PrimaryAnswer) {
        switch primary {
        case .reprice:
            Task {
                if let said = await followThrough.reprice(rec) {
                    answeredRecs.insert(rec.key)
                    onPosted(said)
                    onChanged()
                }
            }
        case .track:
            Task {
                if let said = await followThrough.track(rec) { withAnimation { note = said } }
            }
        case .done:
            submitRec(rec, kind: "done")
        }
    }

    /// Hide for two weeks — the second hide asks why first, as the card did.
    private func hideRec(_ rec: HomeRecommendation) {
        if (rec.timesHidden ?? 0) >= 1 {
            recAskingWhy = rec
            recAskingWhyIsHide = true
            showingRecWhy = true
        } else {
            submitRec(rec, kind: "recommendation")
        }
    }

    private func answerRecWhy(kind: String, reason: RecReason?) {
        guard let rec = recAskingWhy else { return }
        recAskingWhy = nil
        submitRec(rec, kind: kind, reasonCode: reason?.code)
    }

    private func submitRec(_ rec: HomeRecommendation, kind: String, reasonCode: String? = nil) {
        Haptic.light()
        Task {
            if let said = await followThrough.answer(rec, kind: kind, reasonCode: reasonCode) {
                withAnimation(.cavnarEase(0.25)) { _ = answeredRecs.insert(rec.key) }
                onPosted(said)
                onChanged()
            }
        }
    }

    /// A row's second answer. A deny (a time-off or shift request) asks
    /// first, naming the request (iOS re-audit M4): approving is the row's
    /// primary; saying no to a person is never one stray tap.
    private func performAlt(_ step: ActionItem.Step, on item: ActionItem) {
        if HomeFollowThrough.altTint(step) == .cavnarRed {
            denying = HomeDenyAsk(item: item, step: step)
        } else {
            perform(step, on: item)
        }
    }

    // MARK: - What a row's actions do

    private func answerGoal(_ goal: ProposedGoal, confirm: Bool) {
        Haptic.light()
        Task {
            if let said = await followThrough.answerGoal(goal, confirm: confirm) {
                withAnimation { note = said }
            }
        }
    }

    /// What a Still-open step does — the web's order (hbQueueActs): the
    /// confirm card, else the route in place, else the item, else Ask, else
    /// the module. An outward step never posts on one tap.
    private func perform(_ step: ActionItem.Step, on item: ActionItem) {
        Haptic.light()
        switch step.kind {
        case .confirm(let confirm):
            proposing = HomeActionProposal(item: item, step: step, confirm: confirm)
        case .post:
            Task { await followThrough.run(item, step: step) }
        case .open:
            if let nav = item.destination(for: step) { onOpenPath(nav) }
        case .ask(let question):
            // An unanswered Ask proposal reopens as itself.
            if let nav = item.proposalId != nil ? item.destination : SystemEntry.askPath(question) {
                onOpenPath(nav)
            }
        case .module(let module):
            if let nav = item.destination(for: step) { onOpenPath(nav) } else { onOpenModule(module) }
        case .none:
            if let nav = item.destination { onOpenPath(nav) }
        }
    }

    /// Under a Still-open row: what happened, or the publish gate's list
    /// with the one button that sends past it knowingly (its own confirm).
    @ViewBuilder
    private func rowNoteView(_ item: ActionItem, _ note: HomeFollowThroughViewModel.RowNote) -> some View {
        let color: Color = note.tone == .good ? .cavnarGreen : (note.tone == .warn ? .cavnarAmber : .cavnarRedText)
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            if note.blockers.isEmpty {
                CavnarMixedText(note.text, role: .secondary, color: color)
            } else {
                CavnarKicker(note.text)
                ForEach(Array(note.blockers.enumerated()), id: \.offset) { _, b in
                    HStack(alignment: .top, spacing: CavnarSpace.xs) {
                        Circle().fill(Color.cavnarRed).frame(width: 6, height: 6).padding(.top, 7)
                        CavnarMixedText(b, role: .secondary)
                    }
                }
                Button {
                    Haptic.warning()
                    sendingAnyway = item
                } label: {
                    Text(HomeFollowThrough.sendAnywayLabel(note.blockers.count))
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Home's severity tones: critical red, important amber, else ink.
    /// A guest waiting or labor over is red; anything else that needs
    /// doing is amber — ember is never a status (B4 L7).
    static func tone(_ item: NeedsAttentionItem) -> Color {
        switch item.type {
        case "urgent_reviews", "labor_overtime": return .cavnarRed
        default: return .cavnarAmber
        }
    }

    /// A push or a row named an issue: open the list far enough, scroll to
    /// it, pulse it once. An issue no longer open says so.
    private func focus() async {
        guard let id = focusIssue?.id else { return }
        if !day.issues.contains(where: { $0.id == id }) { await day.load() }
        guard day.visibleIssues.contains(where: { $0.id == id }) else {
            day.issueError = "That issue isn\u{2019}t open any more."
            onFocus(id)
            return
        }
        let ranked = entries
        if let index = ranked.firstIndex(where: { $0.id == HomeNeedsYouEntry.issueId(id) }),
           index >= Self.shownCount(leadTookAttention: leadTookAttention) {
            expanded = true
        }
        try? await Task.sleep(for: .milliseconds(150))
        onFocus(id)
        try? await Task.sleep(for: .milliseconds(450))
        withAnimation(.easeInOut(duration: 0.35)) { pulsing = id }
        try? await Task.sleep(for: .seconds(1.4))
        withAnimation(.easeOut(duration: 0.6)) { pulsing = nil }
    }
}

/// The sheets and confirms the list's rows open — apart from the list so
/// each type-checks on its own.
private struct HomeNeedsYouChrome: ViewModifier {
    let day: HomeDayViewModel
    let followThrough: HomeFollowThroughViewModel
    @Binding var openIssue: HomeDayViewModel.Issue?
    @Binding var askingCover: HomeDayViewModel.CoverAsk?
    @Binding var proposing: HomeActionProposal?
    @Binding var sendingAnyway: ActionItem?
    @Binding var checkingIn: RecOutcome?
    @Binding var closingGoal: ProposedGoal?
    @Binding var openLine: HomeDayViewModel.BriefLine?
    @Binding var note: String?
    var onOpenNav: (String) -> Void
    var onChanged: () -> Void

    func body(content: Content) -> some View {
        content
            .sheet(item: $openIssue, onDismiss: { Task { await day.load() } }) { issue in
                HomeIssueSheet(issue: issue, viewModel: day) { resolved in
                    openIssue = nil
                    day.resolveWithUndo(resolved)
                }
            }
            .sheet(item: $proposing, onDismiss: { Task { await followThrough.load() } }) { p in
                HomeActionProposalSheet(proposal: p, viewModel: followThrough)
            }
            .sheet(item: $checkingIn, onDismiss: { Task { await followThrough.load() } }) { outcome in
                HomeCheckInSheet(outcome: outcome) { await followThrough.load() }
            }
            .sheet(item: $openLine) { line in
                HomeBriefLineSheet(line: line, day: day, onOpenNav: { nav in
                    openLine = nil
                    onOpenNav(nav)
                })
            }
            .confirmationDialog(askingCover.map { "Ask \($0.name) to cover?" } ?? "",
                                isPresented: Binding(get: { askingCover != nil },
                                                     set: { if !$0 { askingCover = nil } }),
                                titleVisibility: .visible, presenting: askingCover) { ask in
                Button("Ask \(HomeDayViewModel.firstName(ask.name))") {
                    Task { await day.askToCover(ask.issue, name: ask.name) }
                }
                Button("Not yet", role: .cancel) {}
            } message: { ask in
                Text(HomeDayViewModel.coverAskMessage(ask))
            }
            .confirmationDialog(sendingAnyway.map {
                HomeFollowThrough.sendAnywayTitle(followThrough.rowNote[$0.key]?.blockers.count ?? 0)
            } ?? "",
                                isPresented: Binding(get: { sendingAnyway != nil },
                                                     set: { if !$0 { sendingAnyway = nil } }),
                                titleVisibility: .visible, presenting: sendingAnyway) { item in
                Button("Send it anyway", role: .destructive) {
                    guard let rn = followThrough.rowNote[item.key], let step = rn.ackStep else { return }
                    Task { await followThrough.run(item, step: step, acknowledge: rn.acknowledgement) }
                }
                Button("Not yet", role: .cancel) {}
            } message: { _ in
                Text("It goes out with the warnings above. Open the week instead to fix them first.")
            }
            .confirmationDialog("Close this goal?",
                                isPresented: Binding(get: { closingGoal != nil },
                                                     set: { if !$0 { closingGoal = nil } }),
                                titleVisibility: .visible, presenting: closingGoal) { goal in
                Button("Close it", role: .destructive) {
                    Task {
                        if let said = await followThrough.answerMissed(goal, renew: false) {
                            withAnimation { note = said }
                        }
                    }
                }
                Button("Keep it", role: .cancel) {}
            } message: { goal in
                Text("\u{201C}\(goal.summary)\u{201D} stops being a target. You can set a new goal any time.")
            }
    }
}

/// One thing in the list, by source, with its rank. Pure, so the order is
/// pinned by tests.
enum HomeNeedsYouEntry: Identifiable {
    case attention(NeedsAttentionItem)
    case issue(HomeDayViewModel.Issue)
    case stillOpen(ActionItem)
    case brief(HomeDayViewModel.BriefLine)
    case proposedGoal(ProposedGoal)
    case missedGoal(ProposedGoal)
    case checkIn(RecOutcome)
    /// The flag, and whether it is the first (which carries the note).
    case loss(HomeFollowThroughViewModel.LossSignals.Flag, Bool)
    case quick(HomeQuickAction)
    /// An open recommendation, answerable in place (H7).
    case recommendation(HomeRecommendation)

    static func issueId(_ id: Int) -> String { "issue:\(id)" }

    var id: String {
        switch self {
        case .attention(let i): return "attn:" + i.id
        case .issue(let i): return Self.issueId(i.id)
        case .stillOpen(let a): return "open:" + a.key
        case .brief(let l): return "brief:" + l.id
        case .proposedGoal(let g): return "goal:\(g.id)"
        case .missedGoal(let g): return "missed:\(g.id)"
        case .checkIn(let o): return "checkin:\(o.id)"
        case .loss(let f, _): return "loss:" + f.id
        case .quick(let q): return "quick:" + q.id
        case .recommendation(let r): return "rec:" + r.key
        }
    }

    /// Lower is higher on the list. Needs attention keeps the server's own
    /// urgency order at the top (and so the first four are exactly what
    /// home_brief records as shown); then a high-severity issue, what was
    /// left open, the brief's own actions, other issues, the open
    /// recommendations (H7), decisions on goals, check-ins, flags worth a look, and shortcuts last.
    var rank: Int {
        switch self {
        case .attention: return 0
        case .issue(let i): return i.severity == "high" ? 10 : 40
        case .stillOpen(let a): return a.severity == "critical" ? 15 : 20
        case .brief: return 30
        case .proposedGoal, .missedGoal: return 50
        case .checkIn: return 60
        case .loss: return 70
        case .quick: return 80
        case .recommendation: return 45
        }
    }

    static func ranked(attention: [NeedsAttentionItem], issues: [HomeDayViewModel.Issue] = [],
                       stillOpen: [ActionItem] = [], brief: [HomeDayViewModel.BriefLine] = [],
                       proposed: [ProposedGoal] = [], missed: [ProposedGoal] = [],
                       checkIns: [RecOutcome] = [], loss: [HomeFollowThroughViewModel.LossSignals.Flag] = [],
                       quick: [HomeQuickAction] = [],
                       recommendations: [HomeRecommendation] = []) -> [HomeNeedsYouEntry] {
        var all: [HomeNeedsYouEntry] = attention.map { .attention($0) }
        all += issues.map { .issue($0) }
        all += stillOpen.map { .stillOpen($0) }
        all += brief.map { .brief($0) }
        all += proposed.map { .proposedGoal($0) }
        all += missed.map { .missedGoal($0) }
        all += checkIns.map { .checkIn($0) }
        all += loss.enumerated().map { .loss($0.element, $0.offset == 0) }
        all += quick.map { .quick($0) }
        all += recommendations.map { .recommendation($0) }
        // One item, once: a key two sources share is drawn by the first.
        var seen = Set<String>()
        let unique = all.filter { seen.insert($0.id).inserted }
        return unique.enumerated()
            .sorted { a, b in a.element.rank != b.element.rank ? a.element.rank < b.element.rank : a.offset < b.offset }
            .map(\.element)
    }
}

/// A row's one primary action.
struct HomeNeedsYouPrimary {
    let label: String
    var busy: Bool = false
    let action: () -> Void
    init(label: String, busy: Bool = false, action: @escaping () -> Void) {
        self.label = label; self.busy = busy; self.action = action
    }
}

/// One row of "Needs you": a status dot, the title (two lines), one line of
/// why, the row's ONE primary as a 44pt ember button, and a "…" menu with
/// the rest. A tap on the row opens its place when it has one; a swipe left
/// offers the safe second action.
struct HomeNeedsYouRow<Menu: View, Accessory: View>: View {
    let tone: Color
    let title: String
    let why: String?
    /// nil = the full why (a loss flag's innocent explanation); else a cap.
    var whyLines: Int? = 2
    let primary: HomeNeedsYouPrimary?
    var onTap: (() -> Void)? = nil
    var swipe: HomeSwipeAction? = nil
    @ViewBuilder var menu: () -> Menu
    @ViewBuilder var accessory: () -> Accessory

    init(tone: Color, title: String, why: String?, whyLines: Int? = 2, primary: HomeNeedsYouPrimary?,
         onTap: (() -> Void)? = nil, swipe: HomeSwipeAction? = nil,
         @ViewBuilder menu: @escaping () -> Menu, @ViewBuilder accessory: @escaping () -> Accessory) {
        self.tone = tone; self.title = title; self.why = why; self.whyLines = whyLines
        self.primary = primary; self.onTap = onTap; self.swipe = swipe
        self.menu = menu; self.accessory = accessory
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .top, spacing: CavnarSpace.s) {
                Circle().fill(tone).frame(width: 10, height: 10).padding(.top, 6)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                    CavnarMixedText(title, role: .label)
                        .lineLimit(3)
                    if let why, !why.isEmpty {
                        HomeMixedText.make(why, role: .secondary)
                            .lineLimit(whyLines)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
                .onTapGesture {
                    guard let onTap else { return }
                    Haptic.light()
                    onTap()
                }
                .accessibilityAddTraits(onTap != nil ? .isButton : [])
                SwiftUI.Menu {
                    menu()
                } label: {
                    Image(systemName: "ellipsis")
                        .font(.cavnar(.label))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .accessibilityLabel("More for this")
            }
            if let primary {
                Button {
                    Haptic.medium()
                    primary.action()
                } label: {
                    Group {
                        if primary.busy {
                            CavnarShimmerText(text: "Working\u{2026}", color: Color.cavnarEmber2)
                        } else {
                            HStack(spacing: CavnarSpace.xxs + 2) {
                                HomeMixedText.make(primary.label, role: .label, color: .cavnarEmber2)
                                    .lineLimit(2)
                                Image(systemName: "chevron.right")
                                    .font(.cavnar(.caption))
                                    .foregroundStyle(Color.cavnarEmber2)
                                    .accessibilityHidden(true)
                            }
                        }
                    }
                    .padding(.horizontal, CavnarSpace.m)
                    .frame(minHeight: 44)
                    .background(Capsule().fill(Color.cavnarEmber.opacity(0.14)))
                    .overlay(Capsule().strokeBorder(Color.cavnarEmber2.opacity(0.35), lineWidth: 1))
                    .contentShape(Capsule())
                }
                .buttonStyle(.plain)
                .disabled(primary.busy)
                .padding(.leading, 22)
            }
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                accessory()
            }
            .padding(.leading, 22)
        }
        .padding(.vertical, CavnarSpace.s)
        .homeSwipeAction(swipe)
    }
}

/// A request's Deny waiting on its confirm (M4).
struct HomeDenyAsk {
    let item: ActionItem
    let step: ActionItem.Step
}

/// A row's secondary as a plain ember text link — "See the evidence" under
/// a cross-module link's Done / Not for us (M20). 44pt.
struct HomeNeedsYouTextLink: View {
    let label: String
    let action: () -> Void

    var body: some View {
        Button {
            Haptic.light()
            action()
        } label: {
            HStack(spacing: CavnarSpace.xxs) {
                Text(label)
                    .cavnarText(.label, color: .cavnarEmber2)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
    }
}

/// One recommendation's full card in a sheet, opened from its Needs you
/// row (H7): why, what it rests on, what happens if it is ignored, and
/// every answer — the same card Home's recommendations grid drew.
struct HomeRecommendationSheet: View {
    let rec: HomeRecommendation
    let viewModel: HomeFollowThroughViewModel
    var assignees: [HomeAssignee] = []
    var onOpenModule: (String) -> Void
    var onChanged: () -> Void = {}

    var body: some View {
        NavigationStack {
            ScrollView {
                HomeRecommendations(recommendations: [rec], viewModel: viewModel, assignees: assignees,
                                    onOpenModule: onOpenModule, onChanged: onChanged,
                                    startsExpanded: true)
                    .padding(CavnarSpace.gutter)
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .accountSheetChrome("Recommendation")
        }
        .presentationDetents([.medium, .large])
    }
}

/// "Did you make this change?" in its own sheet, opened from a check-in's
/// row in Needs you — the same card, the same /recs/checkin answer.
struct HomeCheckInSheet: View {
    let outcome: RecOutcome
    var onAnswered: () async -> Void = {}

    var body: some View {
        NavigationStack {
            ScrollView {
                RecCheckInCard(outcome: outcome, surface: "home", onAnswered: onAnswered)
                    .padding(CavnarSpace.gutter)
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .accountSheetChrome("Check in")
        }
        .presentationDetents([.medium, .large])
    }
}
