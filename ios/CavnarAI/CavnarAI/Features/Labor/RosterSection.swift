import SwiftUI

/// The roster as the generator sees it — everyone with a role, their
/// Operational Score, how reliably they turn up, and whether they are
/// on the roster at all. Tap a person for the settings the engine
/// obeys; below the list, the pairs to keep together or apart.
struct RosterSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil
    /// A labor/closers link opens the closer cleanup straight away
    /// (iOS parity #49); spent once used.
    var openClosers: Binding<Bool>? = nil
    /// Add a hand-entered person; remove one (iOS parity #45).
    @State private var addingPerson = false
    @State private var removingPerson: RosterMember?

    @State private var selected: RosterMember?
    @State private var showingPairEditor = false
    @State private var showingClosers = false
    @State private var showingSectionNames = false
    /// Each pair row's measured height — see CavnarFittedList.
    @State private var pairRowHeights: [StaffPair.ID: CGFloat] = [:]

    var body: some View {
        CavnarDropdown(
            title: "Roster & rules",
            subtitle: subtitle,
            badge: viewModel.roster.filter { !$0.isActive }.isEmpty ? nil : viewModel.roster.filter { !$0.isActive }.count,
            tone: .neutral,
            isExpanded: $viewModel.rosterExpanded,
            onExpand: {
                onExpand?()
                Task {
                    await viewModel.loadRoster()
                    await viewModel.loadLearnedPatterns()
                    // "Trained up" chips on the detail sheet read from intel.
                    if viewModel.intel == nil { await viewModel.loadIntel() }
                    // The closed days and the section cap edit here.
                    if !viewModel.rulesLoaded { await viewModel.loadRules() }
                }
            }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                if !viewModel.canEditRoster {
                    Text("Read-only on this login — an owner or manager can change these.")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if viewModel.isLoadingRoster && viewModel.roster.isEmpty {
                    CavnarSkeletonLines(widths: [1.0, 0.85, 0.7])
                } else if let error = viewModel.rosterError, viewModel.roster.isEmpty {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                } else if viewModel.roster.isEmpty {
                    Text("The roster fills from your shift history \u{2014} upload a shifts CSV on the web (Labor \u{2192} Schedule Studio) or connect your POS, and everyone appears here. Add someone new by hand below.")
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    // Who stopped working (schedule audit 10/3/26 E-3): left
                    // off every draft until the owner answers on their row.
                    let away = viewModel.dormantRoster.count
                    if away > 0 {
                        HomeMixedText.make("\(away) \(away == 1 ? "person hasn\u{2019}t" : "people haven\u{2019}t") worked in six weeks "
                                           + "\u{2014} open each to deactivate them or keep them on the roster.",
                                           size: CavnarType.secondary, weight: 600, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(spacing: 0) {
                        ForEach(viewModel.roster) { member in
                            HStack(spacing: 0) {
                                memberRow(member)
                                // Only a hand-entered name comes off here —
                                // shift history keeps everyone else on — with
                                // a visible ⋯, never a long press to learn.
                                if member.isManual == true && viewModel.canEditRoster {
                                    Menu {
                                        Button(role: .destructive) {
                                            removingPerson = member
                                        } label: { Label("Remove from the roster", systemImage: "person.badge.minus") }
                                    } label: {
                                        Image(systemName: "ellipsis")
                                            .font(.cavnar(.body))
                                            .foregroundStyle(Color.cavnarEmber2)
                                            .cavnarHitTarget()
                                    }
                                    .accessibilityLabel("More for \(member.name)")
                                }
                            }
                            if member.id != viewModel.roster.last?.id {
                                Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                            }
                        }
                    }
                }
                if viewModel.canEditRoster {
                    Button {
                        Haptic.light()
                        addingPerson = true
                    } label: {
                        Label("Add a person", systemImage: "person.badge.plus")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                if let error = viewModel.teamError {
                    Text(error).cavnarText(.secondary, color: .cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // The two rules an owner changes from the floor stay here —
                // the days you don't open, and the most front-of-house
                // people at once; the rest of the rules, the closer cleanup
                // and the roles each job code belongs to are set on the web
                // (iOS readability round, 10/8/26).
                if viewModel.rulesLoaded {
                    RulesClosedDaysSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules)
                    RulesDiningSectionsSection(store: viewModel.teamSetup, canEdit: viewModel.canEditRules) {
                        showingSectionNames = true
                    }
                }
                VStack(spacing: 0) {
                    CavnarWebLinkRow(title: "Schedule rules",
                                     subtitle: "Rest, shift length, minors, floors, arrivals, certifications",
                                     path: "labor/team")
                    CavnarWebLinkRow(title: "Closers", subtitle: closersDetail, path: "labor/closers")
                    CavnarWebLinkRow(title: "Roles and job codes",
                                     subtitle: "Which job codes are one role", path: "labor/team")
                }

                pairsBlock

                if let error = viewModel.pairError {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !viewModel.openSuggestions.isEmpty { suggestedPairsBlock }

                // Edits made through Cavnar AI support teach the draft
                // nothing until the account holder counts them as theirs
                // (schedule audit 10/3/26 L-8).
                if let saves = viewModel.adminSaves, saves.versions > 0, viewModel.canAdoptPatterns {
                    adminSavesBanner(saves)
                }

                if !viewModel.learnedPatterns.isEmpty { learnedPatternsBlock }

                if !viewModel.standingPatterns.isEmpty || !viewModel.patternConflicts.isEmpty {
                    standingPatternsBlock
                }

                // What the last answer about a pattern did, said once under
                // every pattern block (Make it a rule, keep, let go, adopt).
                if let message = viewModel.patternMessage {
                    Text(message).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarGreen)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = viewModel.patternError {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .sheet(item: $selected) { member in
            RosterDetailSheet(viewModel: viewModel, name: member.name)
        }
        .sheet(isPresented: $showingPairEditor) {
            PairEditorSheet(viewModel: viewModel)
        }
        // A labor/closers link (a push, the review's link) still opens the
        // cleanup here; the row for it is on the web.
        .sheet(isPresented: $showingClosers, onDismiss: { Task { await viewModel.loadRoster() } }) {
            CloserCleanupSheet(viewModel: viewModel)
        }
        .sheet(isPresented: $showingSectionNames) {
            FloorSectionsSheet()
                .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $addingPerson) { AddTeamMemberSheet(viewModel: viewModel) }
        .confirmationDialog(removingPerson.map { "Remove \($0.name) from your team?" } ?? "",
                            isPresented: Binding(get: { removingPerson != nil }, set: { if !$0 { removingPerson = nil } }),
                            titleVisibility: .visible) {
            Button("Remove", role: .destructive) {
                if let m = removingPerson { Task { await viewModel.removeTeamMember(m.name) } }
                removingPerson = nil
            }
            Button("Cancel", role: .cancel) { removingPerson = nil }
        } message: {
            Text("This only removes the hand-entered name \u{2014} their staff app access ends with it. Anyone with real shift history stays on the roster.")
        }
        .onAppear {
            guard let open = openClosers, open.wrappedValue else { return }
            open.wrappedValue = false
            showingClosers = true
        }
    }

    /// "4 marked to close of 35 — 2 roles" from the roster itself.
    private var closersDetail: String {
        let marked = viewModel.activeRoster.filter { $0.canClose == true }.count
        let waiting = viewModel.activeRoster.filter { $0.canClosePending == true }.count
        var s = marked == 0 ? "Nobody is marked to close" : "\(marked) of \(viewModel.activeRoster.count) marked to close"
        if waiting > 0 { s += " \u{00B7} \(waiting) waiting on you" }
        return s
    }

    private var subtitle: String {
        let active = viewModel.activeRoster.count
        let total = viewModel.roster.count
        if total == 0 { return "Everyone Cavnar AI can schedule" }
        if active == total { return "\(total) on the roster" }
        return "\(active) of \(total) on the roster"
    }

    // MARK: Rows

    private func memberRow(_ member: RosterMember) -> some View {
        Button {
            Haptic.light()
            selected = member
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        Text(member.name)
                            .font(.cavnar(.label))
                            .foregroundStyle(member.isActive ? Color.cavnarInk : Color.cavnarInk3)
                        if let type = member.settings?.employmentType, type == "part" {
                            ScheduleRowTag(text: "PT", tone: .cavnarInk2)
                        }
                        if member.settings?.isMinor == true {
                            ScheduleRowTag(text: "Minor", tone: .cavnarBlue)
                        }
                        // In training for a role (schedule audit 10/3/26
                        // D-16): those shifts are not coverage.
                        if member.isTraining {
                            ScheduleRowTag(text: "Training", tone: .cavnarInk2)
                        }
                    }
                    if member.isDormant {
                        // "Not worked since 8/14/26 — deactivate?" (E-3).
                        HStack(spacing: 5) {
                            Image(systemName: "moon.zzz")
                                .font(.system(size: 10, weight: .semibold))
                            HomeMixedText.make(member.dormantText ?? "Not worked in six weeks \u{2014} deactivate?",
                                               size: CavnarType.secondary, color: .cavnarAmber)
                                .lineLimit(1)
                        }
                        .foregroundStyle(Color.cavnarAmber)
                    } else if member.isActive {
                        HomeMixedText.make(detailLine(member), size: CavnarType.secondary, color: .cavnarInk3)
                            .lineLimit(1)
                    } else {
                        Text("Not on the roster")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 8)
                if let score = member.score, member.isActive {
                    Text("\(score)")
                        .font(.cavnarNumber(CavnarText.figureS.size, weight: 700))
                        .foregroundStyle(Color.cavnarEmber)
                        .frame(width: 26, alignment: .trailing)
                        .accessibilityLabel("Operational Score \(score)")
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
            .opacity(member.isActive ? 1 : 0.55)
        }
        .buttonStyle(.plain)
    }

    private func detailLine(_ member: RosterMember) -> String {
        var parts: [String] = []
        if let role = member.role, !role.isEmpty { parts.append(role) }
        if member.floorManager?.counts == true { parts.append("runs the floor") }
        if let r = member.reliability?.noShowLabel { parts.append(r) }
        if member.canClose == true { parts.append("can close") }
        if let s = member.settings, let lo = s.minHours, let hi = s.maxHours, hi > 0 {
            parts.append("\(Int(lo))–\(Int(hi))h")
        }
        return parts.isEmpty ? "No role on file" : parts.joined(separator: " · ")
    }

    // MARK: Pairs

    private var pairsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Pairs")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
                if viewModel.canEditRoster && viewModel.activeRoster.count >= 2 {
                    Button {
                        Haptic.light()
                        showingPairEditor = true
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "plus").font(.system(size: 11, weight: .bold))
                            Text("Add").font(.cavnarBody(CavnarType.secondary, weight: 600))
                        }
                        .foregroundStyle(Color.cavnarEmber)
                    }
                    .buttonStyle(.plain)
                }
            }
            Text("Two people to keep on the same shift, or apart.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
            if viewModel.pairs.isEmpty {
                Text("No pairs yet.")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                // A List for the system swipe-to-delete, sized to its rows
                // because it sits inside the page's own ScrollView.
                List {
                    ForEach(viewModel.pairs) { pair in
                        pairRow(pair)
                            .cavnarReportsRowHeight(pair.id, into: $pairRowHeights)
                            .listRowBackground(Color.clear)
                            .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                            .listRowSeparatorTint(Color.cavnarPaper3.opacity(0.5))
                    }
                    .onDelete { offsets in
                        guard viewModel.canEditRoster else { return }
                        let ids = offsets.map { viewModel.pairs[$0].id }
                        Task { for id in ids { await viewModel.deletePair(id: id) } }
                    }
                    .deleteDisabled(!viewModel.canEditRoster)
                }
                .listStyle(.plain)
                .scrollContentBackground(.hidden)
                .scrollDisabled(true)
                .frame(height: CavnarFittedList.height(ids: viewModel.pairs.map(\.id),
                                                       measured: pairRowHeights, verticalInsets: 12))
            }
        }
    }

    private func pairRow(_ pair: StaffPair) -> some View {
        HStack(spacing: 10) {
            Image(systemName: pair.isPrefer ? "person.2.fill" : "person.2.slash.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(pair.isPrefer ? Color.cavnarGreen : Color.cavnarRed)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(pair.a) \(pair.isPrefer ? "with" : "apart from") \(pair.b)")
                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if let note = pair.note, !note.isEmpty {
                    Text(note).font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
        }
    }
}

// MARK: - Suggested pairs & learned patterns

extension RosterSection {
    /// Pairs the record suggests. Never applied on their own: Add creates
    /// the pair through the same POST the editor uses; Ignore hides it.
    fileprivate var suggestedPairsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Suggested pairs")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            Text("Two people whose shared dayparts ran clean. Nothing is applied until you add it.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.openSuggestions) { pair in
                SuggestedPairRow(pair: pair, busy: viewModel.isSavingPair) {
                    Task { await viewModel.addSuggestedPair(pair) }
                } onIgnore: {
                    viewModel.ignoreSuggestion(pair)
                }
            }
        }
    }

    /// What the draft has learned from the manager's edits, each with
    /// "Stop using this" / "Use again".
    fileprivate var learnedPatternsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Learned from your edits")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            Text("Moves you keep making become the draft's defaults. Stop one here and the next draft ignores it.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            VStack(spacing: 0) {
                ForEach(viewModel.learnedPatterns) { pattern in
                    learnedPatternRow(pattern)
                    if pattern.id != viewModel.learnedPatterns.last?.id {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
    }

    /// "6 edits made through Cavnar AI support on 2 weeks don't teach the
    /// draft yet — count them as yours?"
    private func adminSavesBanner(_ saves: AdminSavesPending) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HomeMixedText.make("\(saves.versions) edit\(saves.versions == 1 ? "" : "s") made through Cavnar AI support on "
                               + "\(saves.weeks) week\(saves.weeks == 1 ? "" : "s") don\u{2019}t teach the draft yet \u{2014} "
                               + "count them as yours?", size: CavnarType.secondary, weight: 600, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            Text("Only where those edits were your decisions.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
            Button {
                Task { await viewModel.adoptPatternWork(saves: true) }
            } label: {
                Group {
                    if viewModel.isAdoptingPatterns { CavnarShimmerText(text: "Counting\u{2026}") } else { Text("Count them as mine") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.isAdoptingPatterns))
            .disabled(viewModel.isAdoptingPatterns)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.08)))
    }

    private func learnedPatternRow(_ pattern: LearnedPattern) -> some View {
        let dismissed = pattern.dismissed == true
        let busy = viewModel.patternBusyKey == pattern.key
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: pattern.kind == "moved_off" ? "person.fill.xmark" : "person.fill.checkmark")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(dismissed ? Color.cavnarInk3 : (pattern.kind == "moved_off" ? Color.cavnarAmber : Color.cavnarGreen))
                    .frame(width: 18)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(pattern.text ?? "", size: CavnarType.secondary, color: dismissed ? .cavnarInk3 : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    HStack(spacing: 6) {
                        // Its denominator: "2 of 3 weeks", and how sure
                        // that makes it (schedule audit 10/3/26 L-6).
                        if let evidence = pattern.evidenceLine {
                            HomeMixedText.make(evidence, size: CavnarType.caption, color: .cavnarInk3)
                        }
                        Text(dismissed ? "not in use" : (pattern.active == true ? "in use" : "not enough weeks yet"))
                            .font(.cavnarBody(CavnarType.caption, weight: 600))
                            .foregroundStyle(dismissed ? Color.cavnarInk3 : (pattern.active == true ? Color.cavnarGreen : Color.cavnarInk3))
                    }
                    // A dismissal made through support counts once the
                    // account holder says it's theirs (L-10).
                    if pattern.dismissedByAdmin == true {
                        HStack(spacing: 10) {
                            Text("Dismissed through Cavnar AI support \u{2014} not counted")
                                .font(.cavnarBody(CavnarType.caption, weight: 600))
                                .foregroundStyle(Color.cavnarAmber)
                                .fixedSize(horizontal: false, vertical: true)
                            if viewModel.canAdoptPatterns {
                                Button {
                                    Task { await viewModel.adoptPatternWork(saves: false) }
                                } label: {
                                    Text("Count as mine")
                                        .font(.cavnarBody(CavnarType.caption, weight: 700))
                                        .foregroundStyle(Color.cavnarEmber2)
                                        .frame(minHeight: 32)
                                }
                                .buttonStyle(.plain)
                                .disabled(viewModel.isAdoptingPatterns)
                            }
                        }
                    }
                }
            }
            if viewModel.canEditPatterns {
                Button {
                    Haptic.light()
                    Task { await viewModel.setPattern(pattern.key, dismissed: !dismissed) }
                } label: {
                    Group {
                        if busy { CavnarShimmerText(text: "Saving…") } else { Text(dismissed ? "Use again" : "Stop using this") }
                    }
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(dismissed ? Color.cavnarEmber : Color.cavnarInk3)
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .padding(.leading, 26)
            }
        }
        .padding(.vertical, 9)
        .opacity(dismissed ? 0.7 : 1)
    }
}

// MARK: - Standing patterns (memory round, 9/29/26)

extension RosterSection {
    /// What the draft keeps after the manager stopped correcting it — with
    /// who taught it, when it was learned and last kept — and "Make it a
    /// rule" where the pattern is about one person. Pairs two editors pull
    /// opposite ways come first: the owner settles them, never the model.
    fileprivate var standingPatternsBlock: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Standing patterns")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            Text("What the draft keeps doing because your edits taught it. Two reversals retire one; a rule makes it the person\u{2019}s own availability.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.patternConflicts) { conflict in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "arrow.left.arrow.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .frame(width: 18)
                        .padding(.top, 2)
                    VStack(alignment: .leading, spacing: 3) {
                        HomeMixedText.make(conflict.line, size: CavnarType.secondary, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                        Text("Left out of the draft until you settle it \u{2014} set their availability above.")
                            .font(.cavnarBody(CavnarType.caption, weight: 600))
                            .foregroundStyle(Color.cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.06)))
            }
            VStack(spacing: 0) {
                ForEach(viewModel.standingPatterns) { pattern in
                    standingRow(pattern)
                    if pattern.id != viewModel.standingPatterns.last?.id {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
        }
    }

    private func standingRow(_ pattern: StandingPattern) -> some View {
        let retired = pattern.status == "retired"
        let ruled = pattern.status == "ruled"
        let busy = viewModel.patternBusyKey == pattern.key
        return VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: ruled ? "checkmark.seal.fill" : (retired ? "arrow.uturn.backward" : "repeat"))
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(ruled ? Color.cavnarGreen : (retired ? Color.cavnarInk3 : Color.cavnarEmber2))
                    .frame(width: 18)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 3) {
                    HomeMixedText.make(pattern.text, size: CavnarType.secondary, color: retired ? .cavnarInk3 : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if let history = pattern.historyLine {
                        HomeMixedText.make(history, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // Kept how many of the weeks that tested it, how sure,
                    // and when a manager's hand last confirmed it (L-6, L-30).
                    if let evidence = pattern.evidenceLine {
                        HomeMixedText.make(evidence, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    HStack(spacing: 6) {
                        if pattern.evidenceLine == nil, let applied = pattern.timesApplied {
                            HomeMixedText.make("kept \(applied) \(applied == 1 ? "time" : "times")", size: CavnarType.caption,
                                               color: .cavnarInk3)
                        }
                        if let overridden = pattern.timesOverridden, overridden > 0 {
                            HomeMixedText.make("undone \(overridden)", size: CavnarType.caption, color: .cavnarInk3)
                        }
                        Text(pattern.statusLabel)
                            .font(.cavnarBody(CavnarType.caption, weight: 600))
                            .foregroundStyle(ruled ? Color.cavnarGreen : (retired ? Color.cavnarInk3
                                : (pattern.isRetest ? Color.cavnarAmber : Color.cavnarEmber2)))
                    }
                    if ruled, let note = pattern.rule?.note {
                        HomeMixedText.make("Rule: " + note + (pattern.rule?.by.map { " \u{00B7} by " + $0 } ?? ""),
                                           size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    // A cut stays a habit: Cavnar AI holds minimums, not
                    // maximums (L-33's refusal, said before anyone asks).
                    if pattern.kind == "headcount_cut", !retired {
                        Text("A cut can\u{2019}t be a rule \u{2014} Cavnar AI holds staffing minimums, not maximums. The draft keeps it as a habit.")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            // Re-tested: the next draft leaves it out to check it's still
            // wanted — the owner can answer now instead (L-30).
            if pattern.isRetest {
                VStack(alignment: .leading, spacing: 6) {
                    HomeMixedText.make("Left out of the next draft to check you still want it"
                                       + (pattern.retestSince.map { " (since \($0))" } ?? "") + ".",
                                       size: CavnarType.caption, weight: 600, color: .cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                    if viewModel.canEditPatterns {
                        HStack(spacing: 18) {
                            Button {
                                Task { await viewModel.answerStanding(pattern, keep: true) }
                            } label: {
                                Text("Keep it").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                                    .frame(minHeight: 32)
                            }
                            .buttonStyle(.plain)
                            Button {
                                Task { await viewModel.answerStanding(pattern, keep: false) }
                            } label: {
                                Text("Let it go").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk3)
                                    .frame(minHeight: 32)
                            }
                            .buttonStyle(.plain)
                        }
                        .disabled(busy)
                    }
                }
                .padding(.leading, 26)
            }
            if pattern.canBeRule && viewModel.canEditPatterns {
                Button {
                    Haptic.light()
                    Task { await viewModel.makeRule(pattern) }
                } label: {
                    Group {
                        if busy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Make it a rule") }
                    }
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .padding(.leading, 26)
            }
        }
        .padding(.vertical, 9)
        .opacity(retired ? 0.7 : 1)
    }
}

// MARK: - Detail sheet

/// One person's settings, saved a field at a time as they change. Opened
/// from a roster row, and from the rules sheet's "Which days and hours do
/// … work?" (schedule audit 10/3/26 D-5).
struct RosterDetailSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    let name: String
    @Environment(\.dismiss) private var dismiss

    init(viewModel: ScheduleSetupViewModel, name: String) {
        self.viewModel = viewModel
        self.name = name
    }

    @State private var active = true
    @State private var isMinor = false
    @State private var minorAgeBand = ""
    @State private var experienced = false
    @State private var employmentType = "full"
    @State private var minHours = ""
    @State private var maxHours = ""
    @State private var dayparts: [String: String] = [:]
    @State private var windows: [String: TimeWindow] = [:]
    @State private var certifications: [String] = []
    @State private var toast: String?
    @State private var showingPerson = false
    @FocusState private var focused: Field?

    private enum Field: Hashable, CaseIterable { case minHours, maxHours }
    @FocusState private var windowFocus: String?

    private var member: RosterMember? { viewModel.roster.first { $0.name == name } }
    private var busy: Bool { viewModel.savingFor == name }
    private var editable: Bool { viewModel.canEditRoster }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    hero
                    if let member, !member.alsoRoles.isEmpty {
                        // The role above is the one worked most lately
                        // (D-17); the others they worked follow it.
                        HomeMixedText.make("Also: " + member.alsoRoles.prefix(4).joined(separator: ", "),
                                           size: CavnarType.secondary, color: .cavnarInk3)
                            .padding(.top, -14)
                    }
                    if let member, member.isDormant {
                        RosterDormantNotice(
                            text: member.dormantText ?? "Not worked in six weeks \u{2014} deactivate?",
                            busy: busy, editable: editable,
                            onDeactivate: {
                                active = false
                                Task { await viewModel.updateSettings(.init(employeeName: name, active: false)) }
                            },
                            onStillHere: {
                                Task { await viewModel.updateSettings(.init(employeeName: name, active: true)) }
                            })
                    }
                    // Contact, PIN and pay live in the one person record
                    // (Friction audit #25); this sheet keeps scheduling.
                    Button {
                        Haptic.light()
                        showingPerson = true
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: "person.text.rectangle").font(.system(size: 13, weight: .semibold))
                            Text("Contact, login and pay")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    if !editable {
                        Text("Read-only on this login — an owner or manager can change these.")
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    statusSection
                    // Who runs the floor and who stands in — the account
                    // holder's alone (schedule audit 10/3/26 P-7, E-13).
                    FloorManagerSection(
                        status: member?.floorManager, stored: member?.settings?.floorManager,
                        canEdit: viewModel.canEditOwnerFacts, busy: busy) { choice in
                        Task { await viewModel.updateSettings(.init(employeeName: name, floorManager: choice)) }
                    }
                    ActingManagerSection(
                        ranges: member?.settings?.actingManager ?? [],
                        canEdit: viewModel.canEditOwnerFacts, busy: busy) { next in
                        Task { await viewModel.updateSettings(.init(employeeName: name, actingManager: next)) }
                    }
                    if isOwnerRole {
                        paidHourlySection
                    }
                    StandingShiftsSection(
                        shifts: member?.settings?.standingShifts ?? [],
                        roles: viewModel.roleChoices(for: name),
                        canEdit: editable, busy: busy) { next in
                        Task { await viewModel.updateSettings(.init(employeeName: name, standingShifts: next)) }
                    }
                    TraineeSection(
                        trainee: member?.settings?.trainee, name: name,
                        roles: viewModel.roleChoices(for: name),
                        trainers: viewModel.activeNames,
                        canEdit: editable, busy: busy) { change in
                        Task { await viewModel.updateSettings(.init(employeeName: name, trainee: change)) }
                    }
                    hoursSection
                    daypartSection
                    windowsSection
                    certificationsSection
                    if member?.canClose == true || member?.canClosePending == true {
                        ClosesForSection(
                            role: member?.role, closesFor: member?.settings?.closesFor ?? [],
                            roles: viewModel.roleChoices(for: name),
                            pending: member?.canClosePending == true,
                            canEdit: editable, busy: busy) { next in
                            Task { await viewModel.updateSettings(.init(employeeName: name, closesFor: next)) }
                        }
                    }
                    PersonAttendanceSection(reliability: member?.reliability)
                    preferencesSection
                    trainedUpSection
                    if let toast {
                        Text(toast)
                            .font(.cavnar(.secondary))
                            .foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome(name)
            .keyboardNavToolbar($focused)
        }
        .sheet(isPresented: $showingPerson) {
            PersonSheet(target: PersonSheetTarget(key: nil, name: name))
        }
        .onAppear(perform: sync)
        .onChange(of: viewModel.settingsToast) { _, message in
            guard let message else { return }
            toast = message
            sync()
        }
        .onChange(of: focused) { old, new in
            // Hours commit when the field loses focus, so a half-typed
            // "1" on the way to "16" never becomes the ceiling.
            if old == .minHours, new != .minHours { commitHours(min: true) }
            if old == .maxHours, new != .maxHours { commitHours(min: false) }
        }
        .onChange(of: windowFocus) { old, new in
            // Same rule for a window: "10:0" on the way to "10:00am" is
            // never sent. Commits once the field is left.
            if old != nil, old != new { commitWindows() }
        }
    }

    // MARK: Windows

    /// The earliest they can start and the latest they can finish, per
    /// weekday. Blank means no limit beyond the daypart above.
    private var windowsSection: some View {
        AccountSection(kicker: "Earliest and latest, by day") {
            ForEach(Array(viewModel.days.enumerated()), id: \.element) { index, day in
                HStack(spacing: 10) {
                    Text(String(day.prefix(3)))
                        .font(.cavnarBody(CavnarType.secondary, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(width: 36, alignment: .leading)
                    Spacer(minLength: 4)
                    windowField("from", value: windowBinding(day, earliest: true), id: "\(day)|e")
                    windowField("to", value: windowBinding(day, earliest: false), id: "\(day)|l")
                }
                .frame(minHeight: AccountKVRow<EmptyView>.rowHeight)
                .padding(.vertical, 6)
                .disabled(!editable)
                if index < viewModel.days.count - 1 { AccountRowDivider() }
            }
            Text("Times like 10:00am or 9:00pm. Leave blank for no limit.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func windowField(_ label: String, value: Binding<String>, id: String) -> some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.cavnarBody(CavnarType.caption, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            TextField("—", text: value)
                .font(.cavnarNumber(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
                .multilineTextAlignment(.center)
                .autocorrectionDisabled()
                .textInputAutocapitalization(.never)
                .focused($windowFocus, equals: id)
                .frame(width: 78, height: 32)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(windowFocus == id ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
        }
    }

    private func windowBinding(_ day: String, earliest: Bool) -> Binding<String> {
        Binding(
            get: { (earliest ? windows[day]?.earliest : windows[day]?.latest) ?? "" },
            set: { text in
                var w = windows[day] ?? TimeWindow()
                if earliest { w.earliest = text } else { w.latest = text }
                windows[day] = w
            })
    }

    private func commitWindows() {
        var cleaned: [String: TimeWindow] = [:]
        for (day, w) in windows {
            let e = (w.earliest ?? "").trimmingCharacters(in: .whitespaces).lowercased()
            let l = (w.latest ?? "").trimmingCharacters(in: .whitespaces).lowercased()
            if e.isEmpty && l.isEmpty { continue }
            cleaned[day] = TimeWindow(earliest: e.isEmpty ? nil : e, latest: l.isEmpty ? nil : l)
        }
        let current = (member?.settings?.timeWindows ?? [:]).filter { !$0.value.isEmpty }
        guard cleaned != current else { return }
        Task { await viewModel.updateSettings(.init(employeeName: name, timeWindows: cleaned)) }
    }

    // MARK: Certifications

    private var certificationsSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            AccountKicker(text: "Certifications")
            Text("What they hold. A role that needs one is only given to somebody who has it.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
            if viewModel.certificationChoices.isEmpty {
                Text("No certifications are defined yet.")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .italic()
            } else {
                AccountFlowLayout(spacing: 6) {
                    ForEach(viewModel.certificationChoices, id: \.self) { cert in
                        let on = certifications.contains(cert)
                        Button {
                            guard editable else { return }
                            Haptic.selection()
                            var next = certifications
                            if on { next.removeAll { $0 == cert } } else { next.append(cert) }
                            certifications = next
                            Task { await viewModel.updateSettings(.init(employeeName: name, certifications: next)) }
                        } label: {
                            // "Floor manager (can run the shift)" apart from
                            // the food-safety card (schedule audit 10/3/26 E-15).
                            AccountChip(text: viewModel.certificationLabel(cert), muted: !on)
                        }
                        .buttonStyle(.plain)
                        .disabled(!editable)
                        .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
            }
        }
    }

    // MARK: Paid by the hour (an Owner role)

    /// An Owner role is salaried-style — no overtime, hours not spent from
    /// the hourly budget — unless they are paid by the hour (schedule audit
    /// 10/3/26 E-12). The account holder's to say.
    private var isOwnerRole: Bool {
        guard let member else { return false }
        return ([member.role ?? ""] + (member.recentRoles ?? [])).contains { $0.lowercased().contains("owner") }
    }

    private var paidHourlySection: some View {
        AccountSection(kicker: "Pay") {
            AccountSwitchRow(label: "Paid by the hour",
                             detail: (member?.settings?.paidHourly ?? false)
                                ? "Held to the overtime line like anyone hourly, their hours spent from the hourly budget."
                                : "Off: an owner is salaried-style \u{2014} no overtime, hours outside the hourly budget.",
                             isOn: Binding(get: { member?.settings?.paidHourly ?? false }, set: { newValue in
                                Task { await viewModel.updateSettings(.init(employeeName: name, paidHourly: newValue)) }
                             }),
                             busy: busy, disabled: !viewModel.canEditOwnerFacts, showsDivider: false)
        }
    }

    // MARK: Preferences (read-only, from the portal)

    @ViewBuilder
    private var preferencesSection: some View {
        let s = member?.settings
        let parts = s?.preferredDayparts ?? []
        let hours = s?.desiredHours
        if !parts.isEmpty || (hours ?? 0) > 0 {
            AccountSection(kicker: "What they asked for") {
                if !parts.isEmpty {
                    AccountKVRow(label: "Prefers", showsDivider: (hours ?? 0) > 0) {
                        HStack(spacing: 6) {
                            ForEach(parts, id: \.self) { part in AccountChip(text: part.capitalized, muted: true) }
                        }
                    }
                }
                if let hours, hours > 0 {
                    AccountKVRow(label: "Wants", showsDivider: false) {
                        AccountValue(text: "\(hours.commaFormatted)h a week", isNumber: true)
                    }
                }
                Text("Stated in the staff portal — theirs to change, not yours.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, 8)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: Trained up (from intel)

    @ViewBuilder
    private var trainedUpSection: some View {
        if let roles = viewModel.intel?.couldHold?[name], !roles.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                AccountKicker(text: "Trained up")
                Text("Roles the record says they could hold — enough shifts beside a rated colleague.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                AccountFlowLayout(spacing: 6) {
                    ForEach(roles, id: \.self) { role in AccountChip(text: role) }
                }
            }
        }
    }

    private var hero: some View {
        AccountHero(title: name) {
            GlowBadge(systemImage: "person.fill", size: 52)
        } subtitle: {
            HomeMixedText.make(subtitle, size: CavnarType.body, color: .cavnarInk3)
        }
    }

    private var subtitle: String {
        guard let member else { return "" }
        var parts: [String] = []
        if let role = member.role, !role.isEmpty { parts.append(role) }
        if let score = member.score { parts.append("score \(score)") }
        if let r = member.reliability?.noShowLabel { parts.append(r) }
        if let shifts = member.shifts, shifts > 0 {
            // When they last worked, beside the count (schedule audit 10/3/26 E-3).
            parts.append("\(shifts) shifts" + (member.lastWorkedLabel.map { ", last \($0)" } ?? ""))
        }
        return parts.joined(separator: " · ")
    }

    private var statusSection: some View {
        AccountSection(kicker: "Status") {
            AccountSwitchRow(label: "On the roster",
                             detail: active ? "Cavnar AI may schedule them." : "Not on the roster — skipped by every draft.",
                             isOn: Binding(get: { active }, set: { newValue in
                                active = newValue
                                Task { await viewModel.updateSettings(.init(employeeName: name, active: newValue)) }
                             }),
                             busy: busy, disabled: !editable)
            AccountSwitchRow(label: "Minor",
                             detail: "Under 18 — the minor rules (latest end, daily hours) apply.",
                             isOn: Binding(get: { isMinor }, set: { newValue in
                                isMinor = newValue
                                if !newValue { minorAgeBand = "" }
                                Task { await viewModel.updateSettings(.init(employeeName: name, isMinor: newValue)) }
                             }),
                             busy: busy, disabled: !editable, showsDivider: true)
            // Which limits are checked depends on age (NS5 H4): 14-15 carries
            // the federal school-day limits; 16-17 the restaurant's own rule.
            if isMinor {
                AccountKVRow(label: "Age", showsDivider: true) {
                    HStack(spacing: 6) {
                        ForEach(["14-15", "16-17"], id: \.self) { band in
                            choiceChip(band.replacingOccurrences(of: "-", with: "–"),
                                       on: minorAgeBand == band, enabled: editable) {
                                minorAgeBand = band
                                Task { await viewModel.updateSettings(.init(employeeName: name, minorAgeBand: band)) }
                            }
                        }
                    }
                }
            }
            // The owner's word for it: counted by Experience balance
            // without waiting for twenty shifts of history on file.
            AccountSwitchRow(label: "Experienced — knows the job",
                             detail: "Counts as an experienced hand without waiting for 20 shifts on file.",
                             isOn: Binding(get: { experienced }, set: { newValue in
                                experienced = newValue
                                Task { await viewModel.updateSettings(.init(employeeName: name, experienced: newValue)) }
                             }),
                             busy: busy, disabled: !editable, showsDivider: true)
            AccountKVRow(label: "Employment", showsDivider: false) {
                HStack(spacing: 6) {
                    ForEach(viewModel.employmentTypes, id: \.self) { type in
                        choiceChip(type == "full" ? "Full time" : (type == "part" ? "Part time" : type.capitalized),
                                   on: employmentType == type, enabled: editable) {
                            employmentType = type
                            Task { await viewModel.updateSettings(.init(employeeName: name, employmentType: type)) }
                        }
                    }
                }
            }
        }
    }

    private var hoursSection: some View {
        AccountSection(kicker: "Hours a week") {
            AccountField(label: "At least", text: $minHours, focus: $focused, field: .minHours,
                         keyboardType: .decimalPad, isNumber: true)
                .disabled(!editable)
            AccountField(label: "At most", text: $maxHours, focus: $focused, field: .maxHours,
                         keyboardType: .decimalPad, isNumber: true, showsDivider: false)
                .disabled(!editable)
            Text("Leave blank for no floor or ceiling beyond the week's own limit.")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.top, 8)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var daypartSection: some View {
        AccountSection(kicker: "Which part of each day") {
            ForEach(Array(viewModel.days.enumerated()), id: \.element) { index, day in
                VStack(alignment: .leading, spacing: 8) {
                    Text(day)
                        .font(.cavnarBody(CavnarType.secondary, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    HStack(spacing: 6) {
                        ForEach(viewModel.dayparts, id: \.self) { part in
                            choiceChip(part.capitalized, on: (dayparts[day] ?? "any") == part,
                                       enabled: editable, tone: part == "off" ? .cavnarRed : .cavnarEmber) {
                                var next = dayparts
                                next[day] = part
                                dayparts = next
                                Task { await viewModel.updateSettings(.init(employeeName: name, daypartAvailability: next)) }
                            }
                        }
                    }
                }
                .padding(.vertical, 9)
                if index < viewModel.days.count - 1 { AccountRowDivider() }
            }
        }
    }

    private func choiceChip(_ label: String, on: Bool, enabled: Bool, tone: Color = .cavnarEmber,
                            action: @escaping () -> Void) -> some View {
        Button {
            guard enabled, !on else { return }
            Haptic.selection()
            action()
        } label: {
            Text(label)
                .font(.cavnarBody(CavnarType.caption, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 32)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? tone : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
                .opacity(enabled ? 1 : 0.6)
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
    }

    private func sync() {
        guard let member else { return }
        let s = member.settings
        active = member.isActive
        isMinor = s?.isMinor ?? false
        minorAgeBand = s?.minorAgeBand ?? ""
        experienced = s?.experienced ?? false
        employmentType = s?.employmentType ?? "full"
        minHours = s?.minHours.map(Self.hours) ?? ""
        maxHours = s?.maxHours.map(Self.hours) ?? ""
        var parts: [String: String] = [:]
        for day in viewModel.days { parts[day] = s?.daypartAvailability?[day] ?? "any" }
        dayparts = parts
        windows = s?.timeWindows ?? [:]
        certifications = s?.certifications ?? []
    }

    private static func hours(_ v: Double) -> String {
        v == 0 ? "" : (v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v))
    }

    private func commitHours(min: Bool) {
        let text = (min ? minHours : maxHours).trimmingCharacters(in: .whitespaces)
        let value = text.isEmpty ? 0 : (NumberFormatter.cavnarDecimal.number(from: text)?.doubleValue ?? Double(text))
        guard let value, value >= 0 else {
            toast = "Hours need to be a number."
            sync()
            return
        }
        let current = min ? member?.settings?.minHours : member?.settings?.maxHours
        guard (current ?? 0) != value else { return }
        Task {
            await viewModel.updateSettings(min ? .init(employeeName: name, minHours: value)
                                               : .init(employeeName: name, maxHours: value))
        }
    }
}

// MARK: - Pair editor

private struct PairEditorSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var a = ""
    @State private var b = ""
    @State private var kind = "prefer"
    @State private var note = ""
    @FocusState private var focused: Field?
    private enum Field: Hashable, CaseIterable { case note }

    private var canSave: Bool { !a.isEmpty && !b.isEmpty && a != b && !viewModel.isSavingPair }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    picker("First person", selection: $a, excluding: b)
                    picker("Second person", selection: $b, excluding: a)
                    VStack(alignment: .leading, spacing: 8) {
                        kicker("Keep them")
                        HStack(spacing: 8) {
                            kindChip("Together", value: "prefer", tone: .cavnarGreen)
                            kindChip("Apart", value: "avoid", tone: .cavnarRed)
                        }
                    }
                    CavnarFloatingField(icon: "text.alignleft", placeholder: "Note (optional)", text: $note,
                                        focus: $focused, field: .note)
                    if let error = viewModel.pairError {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRed)
                    }
                    VStack(spacing: 10) {
                        Button {
                            Task {
                                if await viewModel.addPair(a: a, b: b, kind: kind, note: note) { dismiss() }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingPair {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Save pair")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSave))
                        .disabled(!canSave)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("New pair")
            .keyboardNavToolbar($focused)
        }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
            .tracking(0.8)
            .foregroundStyle(Color.cavnarInk3)
    }

    private func picker(_ label: String, selection: Binding<String>, excluding: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            kicker(label)
            AccountFlowLayout(spacing: 6) {
                ForEach(viewModel.activeNames.filter { $0 != excluding }, id: \.self) { person in
                    Button {
                        Haptic.selection()
                        selection.wrappedValue = person
                    } label: {
                        AccountChip(text: person, muted: selection.wrappedValue != person)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func kindChip(_ label: String, value: String, tone: Color) -> some View {
        let on = kind == value
        return Button {
            Haptic.selection()
            kind = value
        } label: {
            Text(label)
                .font(.cavnarBody(CavnarType.secondary, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 36)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? tone : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}
