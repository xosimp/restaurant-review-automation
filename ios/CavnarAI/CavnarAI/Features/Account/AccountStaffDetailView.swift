import SwiftUI

/// Opened from Account's "Staff accounts" row (owner-only, same gate the API
/// enforces).
///
/// Most employees sign themselves up with the join code, so this screen is
/// mostly for watching and correcting rather than creating: who claimed which
/// name, who on the roster still hasn't, and the day-two operations — rename,
/// retitle, new PIN, unlock, unlink — that otherwise need a database console.
struct AccountStaffDetailView: View {
    let viewModel: AccountViewModel

    @State private var showingAdd = false
    @State private var renaming: AccountViewModel.StaffAccount?
    @State private var retitling: AccountViewModel.StaffAccount?
    @State private var resettingPin: AccountViewModel.StaffAccount?
    @State private var pendingUnlink: AccountViewModel.StaffAccount?
    @State private var pendingRotate = false
    @State private var copied = false
    /// The one person record (Friction audit #25).
    @State private var person: PersonSheetTarget?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    AccountHero(title: "Staff accounts") {
                        GlowBadge(systemImage: "person.badge.key", size: 64)
                    } subtitle: {
                        Text("\(viewModel.staffAccounts.filter(\.active).count)")
                            .font(.cavnarNumber(15.5, weight: 600))
                            + Text(viewModel.staffAccounts.filter(\.active).count == 1
                                   ? " employee signed up" : " employees signed up")
                    }

                    if let error = viewModel.staffError {
                        Text(error).font(.cavnarBody(15)).foregroundStyle(Color.cavnarRed)
                    }

                    joinCodeSection
                    if !viewModel.staffUnclaimed.isEmpty { unclaimedSection }
                    if !viewModel.staffAccounts.isEmpty { rosterSection }

                    Button {
                        Haptic.light()
                        showingAdd = true
                    } label: {
                        Text("Add an employee yourself").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())

                    Text("Only needed if someone can't sign themselves up — you'll have to read them the PIN.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Staff accounts")
            .task { await viewModel.loadStaff() }
            .sheet(isPresented: $showingAdd) { AddStaffSheet(viewModel: viewModel) }
            .sheet(item: $person) { target in PersonSheet(target: target) }
            .sheet(item: $renaming) { staff in
                StaffTextEditSheet(
                    title: "Name", field: "Name",
                    help: "Spell it the way it appears on the schedule — that's what links them to their shifts.",
                    initial: staff.name) { value in
                        await viewModel.renameStaff(staff.membershipID, to: value)
                    }
            }
            .sheet(item: $retitling) { staff in
                StaffTextEditSheet(
                    title: "Job", field: "Job",
                    help: "This decides which task checklist they see.",
                    initial: staff.jobRole ?? "") { value in
                        await viewModel.retitleStaff(staff.membershipID, to: value)
                    }
            }
            .sheet(item: $resettingPin) { staff in
                StaffPinResetSheet(name: staff.name) { pin in
                    await viewModel.resetStaffPin(staff.membershipID, pin: pin)
                }
            }
            .confirmationDialog(
                "Take this name back?",
                isPresented: Binding(get: { pendingUnlink != nil },
                                     set: { if !$0 { pendingUnlink = nil } }),
                titleVisibility: .visible
            ) {
                Button("Unlink \(pendingUnlink?.name ?? "")", role: .destructive) {
                    if let staff = pendingUnlink {
                        Task { await viewModel.unlinkStaff(staff.membershipID) }
                    }
                    pendingUnlink = nil
                }
                Button("Cancel", role: .cancel) { pendingUnlink = nil }
            } message: {
                Text("Whoever claimed it is signed out, the name goes back on the list for the right person, and that phone can't claim it again.")
            }
            .confirmationDialog(
                "Make a new join code?",
                isPresented: $pendingRotate,
                titleVisibility: .visible
            ) {
                Button("New code", role: .destructive) {
                    Task { await viewModel.rotateStaffJoinCode() }
                }
                Button("Cancel", role: .cancel) { }
            } message: {
                Text("The old code and link stop working for everyone immediately — including anyone mid-signup.")
            }
        }
    }

    // MARK: - Sections

    private var joinCodeSection: some View {
        AccountSection(kicker: "Join code") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 16) {
                    Text(viewModel.staffJoinCode.isEmpty ? "—" : viewModel.staffJoinCode)
                        .font(.cavnarNumber(30, weight: 600))
                        .kerning(3)
                        .foregroundStyle(Color.cavnarEmber)
                    Spacer()
                    AccountActionChip(symbol: copied ? "checkmark" : "doc.on.doc",
                                      accessibilityLabel: "Copy join code") {
                        UIPasteboard.general.string = viewModel.staffJoinCode
                        Haptic.success()
                        copied = true
                        Task {
                            try? await Task.sleep(for: .seconds(1.6))
                            copied = false
                        }
                    }
                    AccountActionChip(symbol: "arrow.triangle.2.circlepath",
                                      tone: .cavnarRed,
                                      accessibilityLabel: "New join code") {
                        pendingRotate = true
                    }
                }
                Text("Post this in the back of house. A new employee downloads Cavnar AI, taps Create your account, and types it — then picks their own name off your roster and sets their own PIN. You don't have to do anything.")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
    }

    private var unclaimedSection: some View {
        AccountSection(kicker: "Not signed up yet") {
            Text(viewModel.staffUnclaimed.joined(separator: ", "))
                .font(.cavnarBody(15))
                .foregroundStyle(Color.cavnarInk2)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var rosterSection: some View {
        AccountSection(kicker: "Who has an account") {
            ForEach(Array(viewModel.staffAccounts.enumerated()), id: \.element.id) { index, staff in
                AccountKVRow(label: staff.name,
                             showsDivider: index < viewModel.staffAccounts.count - 1) {
                    HStack(spacing: 8) {
                        if !staff.active {
                            AccountPill(text: "Removed", on: false)
                        } else if staff.locked {
                            AccountPill(text: "Locked", on: false)
                        } else if let job = staff.jobRole, !job.isEmpty {
                            AccountPill(text: job, on: true)
                        }
                        Menu {
                            Button("Person record") { person = PersonSheetTarget(key: nil, name: staff.name) }
                            Button("Rename") { renaming = staff }
                            Button("Change job") { retitling = staff }
                            Button("New PIN") { resettingPin = staff }
                            if staff.locked {
                                Button("Unlock") {
                                    Task { await viewModel.unlockStaff(staff.membershipID) }
                                }
                            }
                            if staff.active {
                                if staff.selfSignup {
                                    Button("Not them — unlink", role: .destructive) {
                                        pendingUnlink = staff
                                    }
                                } else {
                                    Button("Remove", role: .destructive) {
                                        Task { await viewModel.setStaffActive(staff.membershipID, active: false) }
                                    }
                                }
                            } else {
                                Button("Restore") {
                                    Task { await viewModel.setStaffActive(staff.membershipID, active: true) }
                                }
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                                .foregroundStyle(Color.cavnarInk3)
                        }
                    }
                }
                if staff.selfSignup, let phone = staff.claimedByPhone {
                    // The phone is the whole owner-side control for self-signup:
                    // a number you don't recognise next to a name you do.
                    Text("Signed up from \(formatted(phone))")
                        .font(.cavnarBody(12))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.bottom, 4)
                }
            }
        }
    }

    private func formatted(_ phone: String) -> String {
        var digits = phone.filter(\.isNumber)
        if digits.count == 11, digits.hasPrefix("1") { digits.removeFirst() }
        guard digits.count == 10 else { return phone }
        let area = digits.prefix(3)
        let mid = digits.dropFirst(3).prefix(3)
        let last = digits.suffix(4)
        return "(\(area)) \(mid)-\(last)"
    }
}

/// One text field, one save — used for both the name and the job, because
/// they are the same interaction with different copy.
private struct StaffTextEditSheet: View {
    let title: String
    let field: String
    let help: String
    let initial: String
    let save: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var value: String = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(help)
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                    TextField(field, text: $value)
                        .font(.cavnarBody(17))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    Button {
                        Haptic.light()
                        saving = true
                        Task {
                            let ok = await save(value.trimmingCharacters(in: .whitespaces))
                            saving = false
                            if ok { dismiss() }
                        }
                    } label: {
                        Text(saving ? "Saving…" : "Save").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(saving)
                }
                .padding(20)
            }
            .accountSheetChrome(title)
            .onAppear { value = initial }
        }
    }
}

private struct StaffPinResetSheet: View {
    let name: String
    let save: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var pin = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("A new 4-digit PIN for \(name). Read it to them — it isn't texted or emailed, and they're signed out until they use it.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                    TextField("4–8 digits", text: $pin)
                        .keyboardType(.numberPad)
                        .font(.cavnarNumber(22, weight: 600))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    Button {
                        Haptic.light()
                        saving = true
                        Task {
                            let ok = await save(pin)
                            saving = false
                            if ok { dismiss() }
                        }
                    } label: {
                        Text(saving ? "Saving…" : "Set PIN").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(saving || pin.count < 4)
                }
                .padding(20)
            }
            .accountSheetChrome("New PIN")
        }
    }
}

private struct AddStaffSheet: View {
    let viewModel: AccountViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var job = ""
    @State private var pin = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Creates the account and the PIN yourself. Most employees can do this themselves with the join code.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                    field($name, "Name, as it appears on the schedule")
                    field($job, "Job (Server)")
                    TextField("PIN", text: $pin)
                        .keyboardType(.numberPad)
                        .font(.cavnarNumber(22, weight: 600))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    if let error = viewModel.staffError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                    }
                    Button {
                        Haptic.light()
                        saving = true
                        Task {
                            let ok = await viewModel.createStaff(
                                name: name.trimmingCharacters(in: .whitespaces),
                                jobRole: job.trimmingCharacters(in: .whitespaces), pin: pin)
                            saving = false
                            if ok { dismiss() }
                        }
                    } label: {
                        Text(saving ? "Adding…" : "Add employee").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(saving || name.isEmpty || pin.count < 4)
                }
                .padding(20)
            }
            .accountSheetChrome("Add an employee")
        }
    }

    private func field(_ text: Binding<String>, _ placeholder: String) -> some View {
        TextField(placeholder, text: text)
            .font(.cavnarBody(17))
            .padding(14)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
            .foregroundStyle(Color.cavnarInk)
    }
}
