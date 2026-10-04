import SwiftUI

/// Operational Score — how strong each employee is, 1 to 5, set by the owner.
///
/// Built from a customer's own question: the scheduler knew two bartenders
/// were available on a Saturday and had no idea they were his two weakest.
/// Availability alone produced a schedule that satisfied every rule and was
/// operationally wrong.
///
/// The rating control is the whole point of this screen, so it is five
/// tappable numbers rather than a picker or a stepper — one tap to set, one
/// tap on the same number to clear. An owner rating twenty people should
/// never open a sheet.
struct TeamStrengthSection: View {
    @Bindable var viewModel: LaborViewModel
    /// The setup store, for "Count them as mine" (support-entered ratings
    /// and closer flags, schedule audit 10/3/26 L-9). Nil hides the button.
    var setup: TeamSetupStore? = nil
    /// The roles each person worked lately (the roster's `recent_roles`),
    /// for rating them per role (D-12).
    var rolesFor: [String: [String]] = [:]
    var onExpand: (() -> Void)? = nil

    var body: some View {
        CavnarDropdown(
            title: "Operational Score",
            subtitle: subtitle,
            badge: viewModel.team.filter { $0.score == nil }.count,
            tone: .neutral,
            isExpanded: $viewModel.teamExpanded,
            onExpand: {
                onExpand?()
                Task {
                    await viewModel.loadTeam()
                    await viewModel.loadUnmatchedRatings()
                }
            }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("Rate each person 1 to 5. The scheduler uses this to avoid putting your weakest people together on your busiest shifts.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                if viewModel.isLoadingTeam && viewModel.team.isEmpty {
                    CavnarWorkingLine().padding(.vertical, 12)
                } else if let note = viewModel.teamNote {
                    Text(note)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else if viewModel.team.isEmpty {
                    Text("No staff on file yet.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                } else {
                    if !viewModel.unmatchedRatings.isEmpty {
                        unmatchedBlock
                    }
                    if let status = viewModel.leaderRulesStatus, !status.lines.isEmpty || status.canAdopt {
                        leaderStatusBlock(status)
                    }
                    if let cov = viewModel.teamCoverage, cov.rated < cov.total {
                        coverageNote(cov)
                    }
                    if let due = viewModel.teamCoverage?.dueForRerate, due > 0 {
                        // Ratings past 90 days (schedule audit 10/3/26 D-12):
                        // they still count; the owner says they still hold.
                        HomeMixedText.make("\(due) rating\(due == 1 ? " is" : "s are") 90+ days old \u{2014} tap Still right "
                                           + "beside each that still holds, or rate again.",
                                           size: 13, weight: 600, color: .cavnarAmber)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    ForEach(viewModel.team) { member in
                        memberRow(member)
                        if member.id != viewModel.team.last?.id {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
                        }
                    }
                }

                if let error = viewModel.teamError {
                    Text(error)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }

                // Who changed a rating or a target, and when (memory round).
                CapabilityHistoryBlock()
            }
        }
    }

    private var subtitle: String {
        guard let cov = viewModel.teamCoverage else { return "Rate your team" }
        if cov.rated == 0 { return "Nobody rated yet" }
        if cov.rated == cov.total { return "All \(cov.total) rated" }
        return "\(cov.rated) of \(cov.total) rated"
    }

    /// Until somebody is rated the whole feature is dormant, and a partly
    /// rated team produces shift-strength figures that undercount. Both are
    /// worth saying rather than leaving the owner to infer.
    private func coverageNote(_ cov: RatingCoverage) -> some View {
        HStack(alignment: .top, spacing: 7) {
            Image(systemName: "info.circle")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
                .padding(.top, 2)
            Text(cov.rated == 0
                 ? "Nothing changes in your schedules until you rate somebody."
                 : "\(cov.total - cov.rated) still unrated. They count as zero toward a shift's strength, so targets will read low until they're rated.")
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.bottom, 2)
    }

    // MARK: Ratings that match nobody

    /// A rating stored under "Kim Tran" judges nobody while the roster says
    /// "Kim T." Each one with a one-tap confirm when a roster name clearly
    /// fits, and a picker — the candidates first, then the team — when not.
    private var unmatchedBlock: some View {
        let count = viewModel.unmatchedRatings.count
        return VStack(alignment: .leading, spacing: 10) {
            HomeMixedText.make("\(count) \(count == 1 ? "rating doesn't" : "ratings don't") match anyone on your roster — they judge nobody until they do.",
                               size: 14, weight: 600, color: .cavnarAmber)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(viewModel.unmatchedRatings) { item in
                unmatchedRow(item)
            }
            if let error = viewModel.matchError {
                Text(error)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.08)))
    }

    private func unmatchedRow(_ item: UnmatchedRating) -> some View {
        let busy = viewModel.matchingRating == item.rated
        let rated = Set(viewModel.unmatchedRatings.map { $0.rated.lowercased() })
        var seen = Set<String>()
        let choices = ((item.candidates ?? []) + viewModel.team.map(\.name)).filter {
            !rated.contains($0.lowercased()) && seen.insert($0.lowercased()).inserted
        }
        return VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Text(item.rated)
                    .font(.cavnarBody(15, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if let score = item.score {
                    HomeMixedText.make("rated \(Int(score.rounded()))", size: 13, color: .cavnarInk3)
                }
                Spacer(minLength: 4)
                if busy { CavnarShimmerText(text: "Matching", color: .cavnarInk3).font(.cavnarBody(12)) }
            }
            HStack(spacing: 8) {
                if let suggestion = item.suggestion {
                    Button {
                        Haptic.medium()
                        Task { await viewModel.matchRating(item.rated, to: suggestion) }
                    } label: {
                        Text("It's \(suggestion)").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy)
                }
                Menu {
                    ForEach(choices, id: \.self) { name in
                        Button(name) { Task { await viewModel.matchRating(item.rated, to: name) } }
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(item.suggestion == nil ? "Pick who this is" : "Someone else")
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(Color.cavnarEmber)
                        Image(systemName: "chevron.up.chevron.down")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(Color.cavnarEmber)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                }
                .disabled(busy || choices.isEmpty)
            }
        }
    }

    private func memberRow(_ member: RatedEmployee) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(member.name)
                        .font(.cavnarBody(15, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                    HStack(spacing: 5) {
                        if let role = member.role, !role.isEmpty {
                            Text(role)
                                .font(.cavnarBody(13))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        if let label = member.scoreLabel {
                            Text("· \(label)")
                                .font(.cavnarBody(13))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
                Spacer(minLength: 8)
                if viewModel.savingFor == member.name {
                    CavnarShimmerText(text: "Saving", color: .cavnarInk3)
                        .font(.cavnarBody(12))
                }
            }
            if member.dormant == true {
                // Not worked in six weeks (E-3) — answered on the roster.
                HomeMixedText.make(member.dormantText ?? "Not worked in six weeks", size: 13, color: .cavnarAmber)
            }
            scorePicker(member)
            if member.ratingDue == true, member.score != nil {
                rerateRow(member)
            }
            let roles = roleOptions(member)
            if roles.count > 1 || !(member.roleScores ?? [:]).isEmpty {
                roleScoreRows(member, roles: roles)
            }
            closerToggle(member)
            if member.canClosePending == true {
                Text("Marked to close through Cavnar AI support \u{2014} counts once you count it as yours.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 9)
    }

    // MARK: Per role (schedule audit 10/3/26 D-12)

    /// Their role, the roles worked lately, and any role already rated —
    /// each once, theirs first.
    private func roleOptions(_ member: RatedEmployee) -> [String] {
        var out: [String] = []
        func add(_ r: String?) {
            guard let r = r?.trimmingCharacters(in: .whitespaces), !r.isEmpty,
                  !out.contains(where: { $0.caseInsensitiveCompare(r) == .orderedSame }) else { return }
            out.append(r)
        }
        add(member.role)
        (rolesFor[member.name] ?? []).forEach(add)
        (member.roleScores ?? [:]).keys.sorted().forEach(add)
        return out
    }

    /// A score for each role they work, under the overall one: somebody can
    /// be a 5 behind the bar and a 3 on the floor. A role left unrated uses
    /// the overall score.
    private func roleScoreRows(_ member: RatedEmployee, roles: [String]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("BY ROLE")
                .font(.cavnarBody(11, weight: 700))
                .tracking(1)
                .foregroundStyle(Color.cavnarInk3)
            ForEach(roles, id: \.self) { role in
                let current = (member.roleScores ?? [:]).first { $0.key.caseInsensitiveCompare(role) == .orderedSame }?.value
                let busy = viewModel.savingRoleFor == member.name + "|" + role
                HStack(spacing: 8) {
                    Text(role)
                        .font(.cavnarBody(13.5, weight: 600))
                        .foregroundStyle(current == nil ? Color.cavnarInk3 : Color.cavnarInk2)
                        .lineLimit(1)
                        .frame(width: 96, alignment: .leading)
                    ForEach(1...5, id: \.self) { value in
                        Button {
                            Haptic.light()
                            Task {
                                await viewModel.setRoleScore(for: member.name, role: role,
                                                             score: current == value ? nil : value)
                            }
                        } label: {
                            Text("\(value)")
                                .font(.cavnarNumber(13, weight: 700))
                                .frame(maxWidth: .infinity, minHeight: 28)
                                .foregroundStyle(current == value ? Color.cavnarPaper : Color.cavnarInk2)
                                .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                                    .fill(current == value ? tone(value) : Color.cavnarPaper2))
                                .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous)
                                    .strokeBorder(Color.cavnarPaper3, lineWidth: current == value ? 0 : 1))
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                        .accessibilityLabel("\(role) \(value)")
                    }
                }
            }
            Text("A role left blank uses the overall score.")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.top, 2)
    }

    /// "Rated 6/1/26 — still right?" with one tap to keep it.
    private func rerateRow(_ member: RatedEmployee) -> some View {
        HStack(spacing: 10) {
            HomeMixedText.make(member.ratingDueText ?? "Rated a while ago \u{2014} still right?", size: 13,
                               color: .cavnarAmber)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 6)
            Button {
                Haptic.light()
                Task { await viewModel.confirmRating(for: member.name) }
            } label: {
                Text("Still right")
                    .font(.cavnarBody(13.5, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 32)
            }
            .buttonStyle(.plain)
            .disabled(viewModel.savingFor == member.name)
        }
    }

    // MARK: Leader rules and support-entered ratings (D-10, L-9)

    /// "1 leader rule that needs a rating is inactive: nobody on the roster
    /// is rated (57 ratings entered through support are waiting…)" — and
    /// the one tap that counts those as the owner's.
    private func leaderStatusBlock(_ status: LeaderRulesStatus) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(status.lines, id: \.self) { line in
                HomeMixedText.make(line, size: 13.5, weight: 600, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if status.canAdopt, let setup {
                if status.lines.isEmpty {
                    HomeMixedText.make("\(status.adminRatings + status.adminClosers) entered through Cavnar AI support "
                                       + "don\u{2019}t count until you count them as yours.",
                                       size: 13.5, weight: 600, color: .cavnarAmber)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Task {
                        if await setup.adoptSupportRatings() { await viewModel.loadTeam() }
                    }
                } label: {
                    Group {
                        if setup.isAdopting { CavnarShimmerText(text: "Counting\u{2026}") } else { Text("Count them as mine") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: setup.isAdopting))
                .disabled(setup.isAdopting)
                if let error = setup.adoptError {
                    Text(error).font(.cavnarBody(13)).foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let message = setup.adoptMessage {
                    Text(message).font(.cavnarBody(13)).foregroundStyle(Color.cavnarGreen)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.08)))
    }

    /// Authorized to close. Registered in the capability layer from day one
    /// and reachable from no interface until now, which meant a leadership
    /// requirement could only ever be answered by a score — so an
    /// experienced closer rated 3 never qualified, and "every closing shift
    /// needs somebody authorized to close" could not be satisfied at all.
    private func closerToggle(_ member: RatedEmployee) -> some View {
        Button {
            Haptic.selection()
            Task { await viewModel.setCloser(for: member.name, to: !(member.canClose ?? false)) }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: (member.canClose ?? false) ? "checkmark.square.fill" : "square")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle((member.canClose ?? false) ? Color.cavnarGreen : Color.cavnarInk3)
                // A closer is chosen to close for their role — on until
                // close, the last of their role out — not a key holder
                // (owner, 10/2/26; schedule audit 10/3/26 D-9).
                Text("Closes for their role")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle((member.canClose ?? false) ? Color.cavnarInk2 : Color.cavnarInk3)
                Spacer()
            }
            .contentShape(Rectangle())
            .padding(.top, 2)
        }
        .buttonStyle(.plain)
        .disabled(viewModel.savingFor == member.name)
    }

    /// Five numbers, one tap each. Tapping the current score clears it,
    /// because "not rated" is a real state the scheduler treats differently
    /// from a low rating — it contributes nothing and gets named in the
    /// explanation rather than quietly counting as average.
    private func scorePicker(_ member: RatedEmployee) -> some View {
        HStack(spacing: 6) {
            ForEach(1...5, id: \.self) { value in
                Button {
                    Haptic.light()
                    Task {
                        await viewModel.setScore(for: member.name,
                                                 score: member.score == value ? nil : value)
                    }
                } label: {
                    Text("\(value)")
                        .font(.cavnarNumber(15, weight: 700))
                        .frame(maxWidth: .infinity, minHeight: 34)
                        .foregroundStyle(member.score == value ? Color.cavnarPaper : Color.cavnarInk2)
                        .background(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .fill(member.score == value ? tone(value) : Color.cavnarPaper2)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 8, style: .continuous)
                                .strokeBorder(Color.cavnarPaper3,
                                              lineWidth: member.score == value ? 0 : 1)
                        )
                }
                .buttonStyle(.plain)
                .disabled(viewModel.savingFor == member.name)
            }
        }
    }

    /// Weak reads warm-to-hot, strong reads green — the same direction every
    /// other number in this app uses, so a 2 never looks like good news.
    private func tone(_ value: Int) -> Color {
        switch value {
        case 1: return Color.cavnarRed
        case 2: return Color.cavnarAmber
        case 3: return Color.cavnarInk3
        case 4: return Color.cavnarGreen.opacity(0.75)
        default: return Color.cavnarGreen
        }
    }
}
