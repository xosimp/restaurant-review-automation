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
    /// A remove waiting on its confirm — it used to fire from the menu.
    @State private var pendingRemove: AccountViewModel.StaffAccount?
    @State private var pendingRotate = false
    @State private var showingHowItWorks = false
    @State private var showingSignInMore = false
    @State private var copied = false
    /// The one person record (Friction audit #25).
    @State private var person: PersonSheetTarget?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    // "12 of 18 signed up" and a meter — the answer first
                    // (re-audit M16).
                    AccountHero(title: "Staff accounts") {
                        GlowBadge(systemImage: "person.badge.key", size: 64)
                    } subtitle: {
                        HomeMixedText.make(Self.signedUpLine(signed: signedUp, notYet: viewModel.staffUnclaimed.count),
                                           role: .secondary)
                    }
                    if signedUp + viewModel.staffUnclaimed.count > 0 {
                        signupMeter
                    }

                    if let error = viewModel.staffError, !showingAdd, renaming == nil, retitling == nil, resettingPin == nil {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    }

                    joinCodeSection
                    if !viewModel.staffUnclaimed.isEmpty { unclaimedSection }
                    if !viewModel.staffAccounts.isEmpty { rosterSection }

                    // Rarely needed, so secondary (M16).
                    Button {
                        Haptic.light()
                        showingAdd = true
                    } label: {
                        Text("Add an employee yourself").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())

                    Text("Only needed if someone can't sign themselves up — you'll have to read them the PIN.")
                        .cavnarText(.caption, color: .cavnarInk2)

                    // The web's sign-in notice and its Recent failed PINs
                    // (parity #70), behind one disclosure.
                    disclosure(showingSignInMore ? "Hide sign-in notices" : "Sign-in notices & failed PINs",
                               isOpen: $showingSignInMore)
                    if showingSignInMore {
                        AccountSection(kicker: "Sign-ins") {
                            AccountSwitchRow(
                                label: "Tell me when someone opens the staff app",
                                detail: "A push and a bell entry per staff sign-in. Off by default so it never becomes noise.",
                                isOn: Binding(get: { viewModel.summary?.account.staffSignInNotify ?? false },
                                              set: { on in Task { await viewModel.toggleStaffSignInNotify(on) } }),
                                busy: viewModel.isTogglingStaffSignIn,
                                showsDivider: false
                            )
                        }
                        if !viewModel.pinEvents.isEmpty { pinEventsSection }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
            }
            .accountSheetChrome("Staff accounts")
            .task { await viewModel.loadStaff() }
            .cavnarEmberRefreshable { await viewModel.loadStaff() }
            .sheet(isPresented: $showingAdd) { AddStaffSheet(viewModel: viewModel) }
            .sheet(item: $person) { target in PersonSheet(target: target) }
            .sheet(item: $renaming) { staff in
                StaffTextEditSheet(
                    viewModel: viewModel,
                    title: "Name", field: "Name",
                    help: "Spell it the way it appears on the schedule — that's what links them to their shifts.",
                    initial: staff.name) { value in
                        await viewModel.renameStaff(staff.membershipID, to: value)
                    }
            }
            .sheet(item: $retitling) { staff in
                StaffTextEditSheet(
                    viewModel: viewModel,
                    title: "Job", field: "Job",
                    help: "This decides which task checklist they see.",
                    initial: staff.jobRole ?? "") { value in
                        await viewModel.retitleStaff(staff.membershipID, to: value)
                    }
            }
            .sheet(item: $resettingPin) { staff in
                StaffPinResetSheet(viewModel: viewModel, name: staff.name) { pin in
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
            .confirmationDialog(
                pendingRemove.map { "Remove \($0.name)?" } ?? "",
                isPresented: Binding(get: { pendingRemove != nil },
                                     set: { if !$0 { pendingRemove = nil } }),
                titleVisibility: .visible
            ) {
                Button("Remove", role: .destructive) {
                    if let staff = pendingRemove {
                        Task { await viewModel.setStaffActive(staff.membershipID, active: false) }
                    }
                    pendingRemove = nil
                }
                Button("Cancel", role: .cancel) { pendingRemove = nil }
            } message: {
                Text("They can't sign in to the staff app until you restore them. Their shifts and history stay.")
            }
        }
    }

    // MARK: - Sections

    private var signedUp: Int { viewModel.staffAccounts.filter(\.active).count }

    /// "12 of 18 signed up" — the roster's names that have an account, out
    /// of those plus the ones still waiting; "3 signed up" with nobody waiting.
    static func signedUpLine(signed: Int, notYet: Int) -> String {
        notYet == 0 ? "\(signed) signed up" : "\(signed) of \(signed + notYet) signed up"
    }

    /// The share signed up, as a bar under the hero.
    private var signupMeter: some View {
        let total = max(signedUp + viewModel.staffUnclaimed.count, 1)
        let fraction = CGFloat(signedUp) / CGFloat(total)
        return GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                Capsule()
                    .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2], startPoint: .leading, endPoint: .trailing))
                    .frame(width: geo.size.width * fraction)
            }
        }
        .frame(height: 6)
        .accessibilityElement()
        .accessibilityLabel(Self.signedUpLine(signed: signedUp, notYet: viewModel.staffUnclaimed.count))
    }

    /// A 44pt "show more" line in the "How it works" look.
    private func disclosure(_ title: String, isOpen: Binding<Bool>) -> some View {
        Button {
            Haptic.light()
            withAnimation(.easeOut(duration: 0.2)) { isOpen.wrappedValue.toggle() }
        } label: {
            HStack(spacing: 6) {
                Text(title).cavnarText(.label, color: .cavnarEmber2)
                Image(systemName: "chevron.down")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarEmber2)
                    .rotationEffect(.degrees(isOpen.wrappedValue ? 180 : 0))
                    .accessibilityHidden(true)
                Spacer(minLength: 0)
            }
            .cavnarHitTarget()
        }
        .buttonStyle(.plain)
        .accessibilityValue(isOpen.wrappedValue ? "Expanded" : "Collapsed")
    }

    private var joinCodeSection: some View {
        AccountSection(kicker: "Join code") {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .center, spacing: 16) {
                    Text(viewModel.staffJoinCode.isEmpty ? "—" : viewModel.staffJoinCode)
                        .font(.cavnar(.figureL))
                        .kerning(3)
                        .foregroundStyle(Color.cavnarEmber2)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
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
                // One line; the steps behind "How it works" (iOS
                // readability round — it was a 40-word paragraph).
                Text("Post it in the back of house \u{2014} new staff sign themselves up with it.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.2)) { showingHowItWorks.toggle() }
                } label: {
                    HStack(spacing: 6) {
                        Text(showingHowItWorks ? "Hide how it works" : "How it works")
                            .cavnarText(.label, color: .cavnarEmber2)
                        Image(systemName: "chevron.down")
                            .font(.cavnar(.caption))
                            .foregroundStyle(Color.cavnarEmber2)
                            .rotationEffect(.degrees(showingHowItWorks ? 180 : 0))
                            .accessibilityHidden(true)
                        Spacer(minLength: 0)
                    }
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                if showingHowItWorks {
                    Text("A new employee downloads Cavnar AI, taps Create your account and types the code \u{2014} then picks their own name off your roster and sets their own PIN. You don't have to do anything.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// Recent failed and locked-out staff PINs (auth.get_pin_security_events)
    /// — someone guessing at a PIN shows here, newest first.
    private var pinEventsSection: some View {
        AccountSection(kicker: "Recent failed PINs") {
            ForEach(Array(viewModel.pinEvents.prefix(8).enumerated()), id: \.element.id) { i, e in
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Circle().fill(e.isLockout ? Color.cavnarRed : Color.cavnarAmber).frame(width: 7, height: 7)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(e.name ?? "Someone").cavnarText(.label)
                        Text(e.isLockout ? "Locked out" : "Wrong PIN")
                            .cavnarText(.secondary, color: e.isLockout ? .cavnarRedText : .cavnarInk2)
                    }
                    Spacer(minLength: 8)
                    HomeMixedText.make(Self.when(e.createdAt), role: .caption)
                }
                .padding(.vertical, 8)
                .accessibilityElement(children: .combine)
                if i < min(viewModel.pinEvents.count, 8) - 1 { AccountRowDivider() }
            }
        }
    }

    /// "10/7/26 · 6:45pm" on the restaurant's clock (the stamp is UTC).
    static func when(_ stamp: String) -> String {
        CavnarDate.timestamp(stamp).map { CavnarDate.mdyTime($0, in: RestaurantClock.timeZone) } ?? CavnarDate.mdy(stamp)
    }

    /// The names still waiting, as chips — the first eight, the rest one tap
    /// away (M16: it was one comma-joined paragraph).
    private var unclaimedSection: some View {
        let names = viewModel.staffUnclaimed
        return AccountSection(kicker: "Not signed up yet") {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                AccountFlowLayout(spacing: 6) {
                    ForEach(names.prefix(Self.chipCap), id: \.self) { AccountChip(text: $0, muted: true) }
                }
                if names.count > Self.chipCap {
                    CavnarMoreDisclosure(hiddenCount: names.count - Self.chipCap) {
                        AccountFlowLayout(spacing: 6) {
                            ForEach(names.dropFirst(Self.chipCap), id: \.self) { AccountChip(text: $0, muted: true) }
                        }
                    }
                }
            }
            .padding(.vertical, 9)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private static let chipCap = 8

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
                                    Button("Remove\u{2026}", role: .destructive) {
                                        pendingRemove = staff
                                    }
                                }
                            } else {
                                Button("Restore") {
                                    Task { await viewModel.setStaffActive(staff.membershipID, active: true) }
                                }
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                                .font(.cavnar(.lead))
                                .foregroundStyle(Color.cavnarInk2)
                                .cavnarHitTarget()
                                .padding(.vertical, -7)
                        }
                        .accessibilityLabel("More for \(staff.name)")
                    }
                }
                if staff.selfSignup, let phone = staff.claimedByPhone {
                    // The phone is the whole owner-side control for self-signup:
                    // a number you don't recognise next to a name you do.
                    HomeMixedText.make("Signed up from \(formatted(phone))", role: .caption)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.bottom, 4)
                }
            }
        }
    }

    private func formatted(_ phone: String) -> String { PhoneFormat.display(phone) }
}

/// One text field, one save — used for both the name and the job, because
/// they are the same interaction with different copy.
private struct StaffTextEditSheet: View {
    let viewModel: AccountViewModel
    let title: String
    let field: String
    let help: String
    let initial: String
    let save: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var value: String = ""
    @State private var saving = false
    @State private var loaded = false

    /// Typed and not saved: Back asks first (re-audit M9).
    private var isDirty: Bool {
        loaded && value.trimmingCharacters(in: .whitespaces) != initial.trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text(help)
                        .cavnarText(.secondary)
                    TextField(field, text: $value)
                        .font(.cavnar(.lead))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    // The save's refusal, here where it was tapped — it only
                    // showed on the sheet underneath (re-audit M10).
                    if let error = viewModel.staffError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
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
            .accountSheetChrome(title, isDirty: isDirty)
            .onAppear {
                value = initial
                loaded = true
                viewModel.staffError = nil
            }
        }
    }
}

/// The server's PIN length (auth.PIN_MIN_LENGTH…PIN_MAX_LENGTH, digits only);
/// the server also refuses easy ones (repeats, runs, years) and says why.
enum StaffPinRule {
    static let range = 4...8
    static func plausible(_ pin: String) -> Bool { range.contains(pin.count) && pin.allSatisfy(\.isNumber) }
}

private struct StaffPinResetSheet: View {
    let viewModel: AccountViewModel
    let name: String
    let save: (String) async -> Bool

    @Environment(\.dismiss) private var dismiss
    @State private var pin = ""
    @State private var saving = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // One rule, said once: 4 to 8 digits (re-audit L15 — the
                    // line said "4-digit" over a "4–8 digits" field).
                    Text("A new PIN for \(name), 4 to 8 digits. Read it to them — it isn't texted or emailed, and they're signed out until they use it.")
                        .cavnarText(.secondary)
                    TextField("4–8 digits", text: $pin)
                        .keyboardType(.numberPad)
                        .font(.cavnarNumber(CavnarType.tileNumber, weight: 600))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    if let error = viewModel.staffError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
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
                    .disabled(saving || !StaffPinRule.plausible(pin))
                }
                .padding(20)
            }
            .accountSheetChrome("New PIN", isDirty: !pin.isEmpty)
            .onAppear { viewModel.staffError = nil }
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
                        .font(.cavnar(.secondary))
                        .foregroundStyle(Color.cavnarInk2)
                    field($name, "Name, as it appears on the schedule")
                    field($job, "Job (Server)")
                    TextField("PIN, 4–8 digits", text: $pin)
                        .keyboardType(.numberPad)
                        .font(.cavnarNumber(CavnarType.tileNumber, weight: 600))
                        .padding(14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        .foregroundStyle(Color.cavnarInk)
                    if let error = viewModel.staffError {
                        Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
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
                    .disabled(saving || name.trimmingCharacters(in: .whitespaces).isEmpty || !StaffPinRule.plausible(pin))
                }
                .padding(20)
            }
            // Something typed and not added: Back asks first (re-audit M9).
            .accountSheetChrome("Add an employee", isDirty: !name.isEmpty || !job.isEmpty || !pin.isEmpty)
            .onAppear { viewModel.staffError = nil }
        }
    }

    private func field(_ text: Binding<String>, _ placeholder: String) -> some View {
        TextField(placeholder, text: text)
            .font(.cavnar(.body))
            .padding(14)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
            .foregroundStyle(Color.cavnarInk)
    }
}
