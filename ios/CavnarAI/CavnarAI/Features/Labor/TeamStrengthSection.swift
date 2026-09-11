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
    var onExpand: (() -> Void)? = nil

    var body: some View {
        CavnarDropdown(
            title: "Operational Score",
            subtitle: subtitle,
            badge: viewModel.team.filter { $0.score == nil }.count,
            tone: .neutral,
            isExpanded: $viewModel.teamExpanded,
            onExpand: { onExpand?(); Task { await viewModel.loadTeam() } }
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
                    if let cov = viewModel.teamCoverage, cov.rated < cov.total {
                        coverageNote(cov)
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
            scorePicker(member)
            closerToggle(member)
        }
        .padding(.vertical, 9)
    }

    /// Authorised to close. Registered in the capability layer from day one
    /// and reachable from no interface until now, which meant a leadership
    /// requirement could only ever be answered by a score — so an
    /// experienced closer rated 3 never qualified, and "every closing shift
    /// needs somebody authorised to close" could not be satisfied at all.
    private func closerToggle(_ member: RatedEmployee) -> some View {
        Button {
            Haptic.selection()
            Task { await viewModel.setCloser(for: member.name, to: !(member.canClose ?? false)) }
        } label: {
            HStack(spacing: 6) {
                Image(systemName: (member.canClose ?? false) ? "checkmark.square.fill" : "square")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle((member.canClose ?? false) ? Color.cavnarGreen : Color.cavnarInk3)
                Text("Authorised to close")
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
