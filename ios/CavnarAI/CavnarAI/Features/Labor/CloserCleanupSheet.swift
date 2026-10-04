import SwiftUI

/// Roster & rules → Closers: the one-time cleanup (schedule audit 10/3/26
/// D-9, L-9). A closer is the one person chosen to close for their role —
/// on until close and the last of their role to leave — not a key holder.
/// The sheet shows the closers per role as the rules read them, warns when
/// so many are marked that the rule checks nothing, lets the owner choose
/// which roles close, and offers keep / unmark / add from who was really
/// the last of their role out. Nothing changes until the one Apply.
struct CloserCleanupSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel

    /// Suggestions the owner picked to apply, by name.
    @State private var picked: Set<String> = []
    /// The roles that close, as the owner is choosing them; nil = untouched.
    @State private var roles: [String]? = nil
    @State private var seeded = false

    private var store: TeamSetupStore { viewModel.teamSetup }
    private var review: CloserReview? { store.closerReview }
    private var canEdit: Bool { review?.canEdit ?? false }

    /// The suggestions that change something (keep is already so).
    private var actionable: [CloserReview.Suggestion] {
        (review?.suggestions ?? []).filter { $0.action == "unmark" || $0.action == "add" }
    }

    private var changes: [TeamSetupStore.CloserChange] {
        actionable.filter { picked.contains($0.name) }
            .map { TeamSetupStore.CloserChange(name: $0.name, canClose: $0.action == "add") }
    }

    private var rolesChanged: Bool {
        guard let roles else { return false }
        return Set(roles.map { $0.lowercased() }) != Set((review?.closerRoles ?? []).map { $0.lowercased() })
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Closers") {
                        GlowBadge(systemImage: "lock.fill", size: 52)
                    } subtitle: {
                        Text(subtitle)
                    }
                    Text("A closer is the one person chosen to close for their role \u{2014} on until close and the last of their role to leave. Not a key holder. Cavnar AI checks every night has one per role that closes.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    if store.isLoadingClosers && review == nil {
                        CavnarSkeletonBar(height: 3)
                            .padding(.vertical, 10)
                            .accessibilityLabel("Loading the closers")
                    } else if let error = store.closerError, review == nil {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let review {
                        if let warning = review.warning {
                            CavnarCaveat(title: "Too many marked to close", detail: warning)
                        }
                        byRoleSection(review)
                        rolesSection(review)
                        if !review.pendingAdmin.isEmpty { pendingSection(review) }
                        suggestionsSection(review)
                        applyRow
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Closers")
        }
        .task {
            await store.loadClosers()
            seed()
        }
    }

    private var subtitle: String {
        guard let r = review else { return "Who closes for each role" }
        if r.roster == 0 { return "Nobody on the roster yet" }
        return "\(r.flagged) of \(r.roster) marked to close"
    }

    /// The suggestions start picked — the cleanup is one Apply — and the
    /// owner takes out any they disagree with.
    private func seed() {
        guard !seeded, let r = review else { return }
        seeded = true
        picked = Set(r.suggestions.filter { $0.action == "unmark" || $0.action == "add" }.map(\.name))
    }

    // MARK: Closers by role

    private func byRoleSection(_ r: CloserReview) -> some View {
        AccountSection(kicker: "Closers by role") {
            VStack(alignment: .leading, spacing: 10) {
                if r.byRole.isEmpty {
                    Text("No role has a closer yet.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                } else {
                    ForEach(r.byRole) { group in
                        VStack(alignment: .leading, spacing: 6) {
                            HomeMixedText.make("\(group.role) \u{00B7} \(group.closers.count)", size: 14.5, weight: 700,
                                               color: .cavnarInk)
                            AccountFlowLayout(spacing: 6) {
                                ForEach(group.closers, id: \.self) { AccountChip(text: $0, muted: true) }
                            }
                        }
                    }
                }
                if !r.outsideRoles.isEmpty {
                    SetupHelp(text: "Marked to close but in no role that closes: " + r.outsideRoles.joined(separator: ", ")
                              + " \u{2014} their flag checks nothing.", color: .cavnarAmber)
                }
                if let line = r.inForceLine {
                    SetupHelp(text: line)
                }
            }
            .padding(.vertical, 10)
        }
    }

    // MARK: Which roles close

    private func rolesSection(_ r: CloserReview) -> some View {
        let chosen = roles ?? r.closerRoles
        var options: [String] = []
        for role in r.closerRoles + r.closerRolesInForce + viewModel.roleChoices()
            where !options.contains(where: { $0.caseInsensitiveCompare(role) == .orderedSame }) {
            options.append(role)
        }
        return AccountSection(kicker: "Roles that close") {
            VStack(alignment: .leading, spacing: 8) {
                AccountFlowLayout(spacing: 6) {
                    ForEach(options, id: \.self) { role in
                        let on = chosen.contains { $0.caseInsensitiveCompare(role) == .orderedSame }
                        Button {
                            guard canEdit else { return }
                            Haptic.selection()
                            roles = on ? chosen.filter { $0.caseInsensitiveCompare(role) != .orderedSame } : chosen + [role]
                        } label: { AccountChip(text: role, muted: !on) }
                            .buttonStyle(.plain)
                            .disabled(!canEdit)
                            .accessibilityAddTraits(on ? .isSelected : [])
                    }
                }
                SetupHelp(text: chosen.isEmpty
                          ? "None picked: the rule holds for the roles your punches show on until close."
                          : "The closer rule holds for these roles only \u{2014} a cook marked to close isn\u{2019}t held to the bar\u{2019}s close.")
                if canEdit && !chosen.isEmpty {
                    Button {
                        Haptic.selection()
                        roles = []
                    } label: {
                        Text("Go by my punches instead")
                            .font(.cavnarBody(13.5, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 32)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.vertical, 10)
        }
    }

    // MARK: Marked through support

    private func pendingSection(_ r: CloserReview) -> some View {
        AccountSection(kicker: "Marked through Cavnar AI support") {
            VStack(alignment: .leading, spacing: 8) {
                HomeMixedText.make("\(r.pendingAdmin.count) closer flag\(r.pendingAdmin.count == 1 ? "" : "s") set through support "
                                   + "don\u{2019}t count until you count them as yours: " + r.pendingAdmin.prefix(8).joined(separator: ", ")
                                   + (r.pendingAdmin.count > 8 ? ", \u{2026}" : "") + ".",
                                   size: 14, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                if viewModel.canEditOwnerFacts {
                    Button {
                        Task {
                            if await store.adoptSupportRatings() {
                                await store.loadClosers()
                                await viewModel.loadRoster()
                            }
                        }
                    } label: {
                        Group {
                            if store.isAdopting { CavnarShimmerText(text: "Counting\u{2026}") } else { Text("Count them as mine") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: store.isAdopting))
                    .disabled(store.isAdopting)
                    SetupHelp(text: "Also counts the ratings entered through support as yours.")
                }
                if let m = store.adoptMessage { Text(m).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarGreen) }
                if let e = store.adoptError { Text(e).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed) }
            }
            .padding(.vertical, 10)
        }
    }

    // MARK: Suggestions

    private func suggestionsSection(_ r: CloserReview) -> some View {
        AccountSection(kicker: "From who really closes") {
            VStack(alignment: .leading, spacing: 0) {
                if r.suggestions.isEmpty {
                    Text("Nothing to suggest \u{2014} the punches agree with who is marked.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.vertical, 10)
                } else {
                    ForEach(Array(r.suggestions.enumerated()), id: \.element.id) { index, s in
                        suggestionRow(s)
                        if index < r.suggestions.count - 1 { AccountRowDivider() }
                    }
                }
            }
        }
    }

    private func suggestionRow(_ s: CloserReview.Suggestion) -> some View {
        let changes = s.action == "unmark" || s.action == "add"
        let on = picked.contains(s.name)
        let verb = s.action == "unmark" ? "Unmark" : (s.action == "add" ? "Mark to close" : "Keep")
        return Button {
            guard changes, canEdit else { return }
            Haptic.selection()
            if on { picked.remove(s.name) } else { picked.insert(s.name) }
        } label: {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: changes ? (on ? "checkmark.square.fill" : "square") : "checkmark.circle")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(changes ? (on ? Color.cavnarEmber2 : Color.cavnarInk3) : Color.cavnarGreen)
                    .padding(.top, 2)
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make("\(verb): \(s.name)" + (s.role.map { " \u{00B7} \($0)" } ?? ""),
                                       size: 15, weight: 600, color: .cavnarInk)
                    if let reason = s.reason {
                        HomeMixedText.make(reason.prefix(1).uppercased() + reason.dropFirst(), size: 13, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 9)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!changes || !canEdit)
        .accessibilityAddTraits(on ? .isSelected : [])
    }

    // MARK: Apply

    @ViewBuilder
    private var applyRow: some View {
        if canEdit {
            VStack(alignment: .leading, spacing: 8) {
                let n = changes.count
                Button {
                    Task {
                        if await store.applyClosers(changes, closerRoles: rolesChanged ? roles : nil) {
                            picked = []
                            roles = nil
                            seeded = false
                            seed()
                            await viewModel.loadRoster()
                        }
                    }
                } label: {
                    Group {
                        if store.isApplyingClosers {
                            CavnarShimmerText(text: "Applying\u{2026}")
                        } else {
                            Text(n == 0 ? "Save the roles that close" : "Apply \(n) change\(n == 1 ? "" : "s")")
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.isApplyingClosers || (n == 0 && !rolesChanged)))
                .disabled(store.isApplyingClosers || (n == 0 && !rolesChanged))
                if let m = store.closerMessage { Text(m).font(.cavnarBody(14)).foregroundStyle(Color.cavnarGreen) }
                if let e = store.closerError { Text(e).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true) }
            }
        } else {
            Text("Only the account owner chooses the closers.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarAmber)
        }
    }
}
