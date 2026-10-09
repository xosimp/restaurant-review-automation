import SwiftUI

// The employee's own account, as screens the Me tab presents (employee audit
// wave 2, I1). Each takes the StaffSessionStore and talks to the server only
// through it, so there is one copy of every rule:
//
//   StaffForgotPinView(staff:code:phone:)        H10: a code by text → a new PIN, signed in
//   StaffChangePinView(staff:)                   UX-23: current → new → again, on the pad
//   StaffDeleteAccountView(staff:)               C10: delete my login here
//   StaffEmailEditView(staff:current:onSaved:)   the address notices fall back to
//   StaffLocationSwitcherView(staff:)            M12: another location, with its own PIN
//   StaffNotificationAskCard(staff:)             C4: the one-line ask before the system prompt
//
// Present each as a sheet. A view that ends the session (a PIN change, a
// deletion) needs no navigation of its own: RootView swaps to the staff PIN
// pad, which says what happened.

// MARK: - Forgot PIN (H10)

/// "Forgot PIN?" — start → verify → set (staff_account_routes.py). A code is
/// texted to the number on file; the person chooses a new PIN, types it
/// twice, and is signed in with it. The server answers the first step the
/// same whether or not the number has a login, and so does this screen.
struct StaffForgotPinView: View {
    let staff: StaffSessionStore

    @Environment(\.dismiss) private var dismiss

    private enum Step { case number, verify, pin, confirm }

    private let codeKnown: Bool
    @State private var step: Step = .number
    @State private var code: String
    @State private var phone: String
    @State private var smsCode = ""
    @State private var devCode: String?
    @State private var sentLine: String?
    @State private var resetToken: String?
    @State private var who = ""
    @State private var restaurant = ""
    @State private var pin = ""
    @State private var confirmPin = ""
    @State private var error: String?
    @State private var shake = 0
    @State private var loading = false

    init(staff: StaffSessionStore, code: String? = nil, phone: String = "") {
        self.staff = staff
        let known = (code ?? "").trimmingCharacters(in: .whitespaces)
        self.codeKnown = !known.isEmpty
        _code = State(initialValue: known)
        _phone = State(initialValue: phone)
    }

    var body: some View {
        StaffSheetFrame(title: "Reset your PIN", onBack: step == .number ? nil : { back() },
                        onClose: { dismiss() }) {
            switch step {
            case .number:  numberStep
            case .verify:  verifyStep
            case .pin:     pinStep(confirming: false)
            case .confirm: pinStep(confirming: true)
            }
        }
    }

    private var numberStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            StaffSheetHelper(text: "We'll text a code to the mobile number on file for you — the one you signed up with, or the one your manager has.")
            if !codeKnown {
                StaffSheetField(text: $code, placeholder: "Restaurant code", label: "Restaurant code")
                    .textInputAutocapitalization(.characters)
                    .autocorrectionDisabled()
            }
            StaffSheetField(text: $phone, placeholder: "(555) 014-2233", label: "Mobile number")
                .keyboardType(.phonePad)
                .textContentType(.telephoneNumber)
            Text("Message and data rates may apply.")
                .font(.cavnarBody(CavnarType.caption))
                .foregroundStyle(Color.cavnarInk3)
            StaffErrorLine(text: error)
            Button {
                Task { await start() }
            } label: {
                StaffBusyLabel(title: "Text me a code", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(loading || phone.filter(\.isNumber).count < 10
                      || code.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var verifyStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            StaffSheetHelper(text: sentLine ?? "If that number is on file here, we've texted it a code.")
            if let devCode { StaffDevCode(code: devCode) }
            StaffSheetField(text: $smsCode, placeholder: "000000", label: "Six-digit code")
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .font(.cavnar(.figureM))
                .multilineTextAlignment(.center)
                .onChange(of: smsCode) { _, value in
                    let digits = String(value.filter(\.isNumber).prefix(6))
                    if digits != value { smsCode = digits; return }
                    if !digits.isEmpty { error = nil }
                    if digits.count == 6, !loading { Task { await verify() } }
                }
            StaffErrorLine(text: error)
            Button {
                Task { await verify() }
            } label: {
                StaffBusyLabel(title: "Continue", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(loading || smsCode.count < 6)
            Button("Send it again") { Task { await start() } }
                .disabled(loading)
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
        }
    }

    private func pinStep(confirming: Bool) -> some View {
        VStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 6) {
                if !restaurant.isEmpty { StaffSignInKicker(text: restaurant.uppercased()) }
                Text(confirming ? "Type it again" : (who.isEmpty ? "Choose a new PIN" : "A new PIN for \(who)"))
                    .font(.cavnar(.headline))
                    .foregroundStyle(Color.cavnarInk)
                    .accessibilityAddTraits(.isHeader)
                StaffSheetHelper(text: confirming
                                 ? "The same PIN once more."
                                 : "4 to 8 digits. Not one people would guess, like 1234 or a year.")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            StaffPinField(pin: confirming ? $confirmPin : $pin, isError: error != nil,
                          shakeTrigger: shake, disabled: loading)
            StaffErrorLine(text: error, centered: true)
            Button {
                if confirming {
                    Task { await setPin() }
                } else {
                    error = nil
                    confirmPin = ""
                    step = .confirm
                }
            } label: {
                StaffBusyLabel(title: confirming ? "Set PIN and sign in" : "Next", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled((confirming ? confirmPin.count : pin.count) < 4 || loading)
            .frame(maxWidth: 320)
        }
    }

    private func back() {
        error = nil
        switch step {
        case .number:  dismiss()
        case .verify:  step = .number
        case .pin:     step = .verify
        case .confirm: confirmPin = ""; step = .pin
        }
    }

    private func start() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.forgotStart(code: code.trimmingCharacters(in: .whitespaces), phone: phone)
            guard resp.ok else {
                error = resp.error ?? "Couldn't send a code."
                return
            }
            sentLine = resp.message
            devCode = resp.devCode
            smsCode = ""
            step = .verify
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func verify() async {
        guard !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.forgotVerify(phone: phone, code: smsCode)
            resetToken = resp.resetToken
            who = resp.employeeName ?? ""
            restaurant = resp.restaurant ?? ""
            pin = ""
            step = .pin
        } catch let apiError as APIClient.APIError {
            smsCode = ""
            error = apiError.message
        } catch {
            smsCode = ""
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func setPin() async {
        guard confirmPin == pin else {
            Haptic.error()
            shake += 1
            error = "Those didn't match — try again."
            pin = ""
            confirmPin = ""
            step = .pin
            return
        }
        guard let resetToken else { step = .number; return }
        loading = true
        error = nil
        defer { loading = false }
        switch await staff.forgotSet(resetToken: resetToken, pin: pin, fallbackCode: code) {
        case .signedIn:
            Haptic.success()
            dismiss()
        case .expired(let message):
            Haptic.error()
            self.resetToken = nil
            error = message
            pin = ""
            confirmPin = ""
            step = .number
        case .refused(let message):
            Haptic.error()
            shake += 1
            error = message
            pin = ""
            confirmPin = ""
            step = .pin
        }
    }
}

// MARK: - Change PIN (UX-23)

/// Current PIN → new PIN → the new one again, on the same pad as sign-in.
/// A success ends this session (the server's rule), so the person lands on
/// their own PIN pad reading "PIN changed — sign in with your new PIN."
struct StaffChangePinView: View {
    let staff: StaffSessionStore

    @Environment(\.dismiss) private var dismiss

    private enum Step { case current, new, confirm }

    @State private var step: Step = .current
    @State private var current = ""
    @State private var newPin = ""
    @State private var confirmPin = ""
    @State private var error: String?
    @State private var shake = 0
    @State private var loading = false

    init(staff: StaffSessionStore) {
        self.staff = staff
    }

    var body: some View {
        StaffSheetFrame(title: "Change your PIN", onBack: step == .current ? nil : { back() },
                        onClose: { dismiss() }) {
            VStack(spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(heading)
                        .font(.cavnar(.headline))
                        .foregroundStyle(Color.cavnarInk)
                        .accessibilityAddTraits(.isHeader)
                    StaffSheetHelper(text: helper)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                StaffPinField(pin: binding, isError: error != nil, shakeTrigger: shake, disabled: loading)
                StaffErrorLine(text: error, centered: true)

                Button {
                    Task { await next() }
                } label: {
                    StaffBusyLabel(title: step == .confirm ? "Change PIN" : "Next", busy: loading)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(binding.wrappedValue.count < 4 || loading)
                .frame(maxWidth: 320)
            }
        }
    }

    private var heading: String {
        switch step {
        case .current: return "Your current PIN"
        case .new:     return "Choose a new PIN"
        case .confirm: return "Type it again"
        }
    }

    private var helper: String {
        switch step {
        case .current: return "So nobody else holding this phone can change it. Three wrong tries signs you out."
        case .new:     return "4 to 8 digits. Not one people would guess, like 1234 or a year."
        case .confirm: return "The same new PIN once more. Then you'll sign in with it."
        }
    }

    private var binding: Binding<String> {
        switch step {
        case .current: return $current
        case .new:     return $newPin
        case .confirm: return $confirmPin
        }
    }

    private func back() {
        error = nil
        switch step {
        case .current: dismiss()
        case .new:     newPin = ""; step = .current
        case .confirm: confirmPin = ""; step = .new
        }
    }

    private func next() async {
        error = nil
        switch step {
        case .current:
            step = .new
        case .new:
            confirmPin = ""
            step = .confirm
        case .confirm:
            guard confirmPin == newPin else {
                Haptic.error()
                shake += 1
                error = "Those didn't match — try again."
                newPin = ""
                confirmPin = ""
                step = .new
                return
            }
            loading = true
            defer { loading = false }
            switch await staff.changePin(current: current, new: newPin) {
            case .changed:
                Haptic.success()
                dismiss()
            case .signedOut:
                Haptic.error()
                dismiss()
            case .wrongCurrent(let message):
                Haptic.error()
                shake += 1
                error = message
                current = ""
                newPin = ""
                confirmPin = ""
                step = .current
            case .refused(let message):
                Haptic.error()
                shake += 1
                error = message
                newPin = ""
                confirmPin = ""
                step = .new
            }
        }
    }
}

// MARK: - Delete my account (C10)

/// App Store 5.1.1(v): an account made in the app can be deleted in it. Says
/// exactly what goes and what stays, then asks once more.
struct StaffDeleteAccountView: View {
    let staff: StaffSessionStore

    @Environment(\.dismiss) private var dismiss
    @State private var confirming = false
    @State private var error: String?
    @State private var loading = false

    init(staff: StaffSessionStore) {
        self.staff = staff
    }

    private var restaurant: String {
        staff.restaurantName(for: staff.portalToken) ?? "this restaurant"
    }

    var body: some View {
        StaffSheetFrame(title: "Delete your account", onBack: nil, onClose: { dismiss() }) {
            VStack(alignment: .leading, spacing: 14) {
                StaffSheetHelper(text: "This deletes your login at \(restaurant):")
                VStack(alignment: .leading, spacing: 8) {
                    bullet("Your PIN, and this phone's sign-in.")
                    bullet("Your consent to schedule texts, and the number you signed up with.")
                    bullet("The schedule links we've sent you stop working.")
                }
                StaffSheetHelper(text: "You stay on the schedule — that's your manager's call, and they're told you deleted your login. If you work at other locations, those logins stay.")
                StaffErrorLine(text: error)
                Button(role: .destructive) {
                    confirming = true
                } label: {
                    StaffBusyLabel(title: "Delete my account", busy: loading)
                        .foregroundStyle(Color.cavnarRed)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: loading))
                .disabled(loading)
                .padding(.top, 6)
            }
        }
        .confirmationDialog("Delete your account at \(restaurant)?", isPresented: $confirming,
                            titleVisibility: .visible) {
            Button("Delete my account", role: .destructive) {
                Task { await delete() }
            }
            Button("Keep it", role: .cancel) {}
        } message: {
            Text("You can't undo this. To use the app here again you'd create a new account.")
        }
    }

    private func bullet(_ text: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle().fill(Color.cavnarInk3).frame(width: 5, height: 5).accessibilityHidden(true)
            Text(text)
                .font(.cavnarBody(CavnarType.body))
                .foregroundStyle(Color.cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func delete() async {
        loading = true
        error = nil
        defer { loading = false }
        if let failure = await staff.deleteAccount() {
            Haptic.error()
            error = failure
        } else {
            Haptic.success()
            dismiss()
        }
    }
}

// MARK: - Email

/// The address notices fall back to — the same one the owner sees on the
/// roster card (staff_contacts), so one person has one email. "" clears it.
struct StaffEmailEditView: View {
    let staff: StaffSessionStore
    let current: String
    var onSaved: ((String) -> Void)?

    @Environment(\.dismiss) private var dismiss
    @State private var email: String
    @State private var error: String?
    @State private var loading = false

    init(staff: StaffSessionStore, current: String = "", onSaved: ((String) -> Void)? = nil) {
        self.staff = staff
        self.current = current
        self.onSaved = onSaved
        _email = State(initialValue: current)
    }

    private var trimmed: String { email.trimmingCharacters(in: .whitespacesAndNewlines) }

    var body: some View {
        StaffSheetFrame(title: current.isEmpty ? "Add your email" : "Your email", onBack: nil,
                        onClose: { dismiss() }) {
            VStack(alignment: .leading, spacing: 14) {
                StaffSheetHelper(text: "If a notice can't reach this phone, it comes here. Your manager sees the same address.")
                StaffSheetField(text: $email, placeholder: "you@example.com", label: "Email")
                    .keyboardType(.emailAddress)
                    .textContentType(.emailAddress)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.done)
                StaffErrorLine(text: error)
                Button {
                    Task { await save(trimmed) }
                } label: {
                    StaffBusyLabel(title: "Save", busy: loading)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(loading || trimmed.isEmpty || trimmed == current)
                if !current.isEmpty {
                    Button("Remove my email") { Task { await save("") } }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarRed)
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .contentShape(Rectangle())
                        .disabled(loading)
                }
            }
        }
    }

    private func save(_ value: String) async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let saved = try await staff.saveEmail(value)
            Haptic.success()
            onSaved?(saved)
            dismiss()
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch let ended as APIClient.SessionExpiredError {
            error = ended.errorDescription
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }
}

// MARK: - Switch location (M12)

/// The locations this person works at. Opening another one shows that
/// location's PIN pad with their name chosen: each restaurant's PIN is its
/// own credential, and a session is never minted from another's (fix_B1's
/// decision). Signing in there replaces this session; RootView rebuilds the
/// portal for it.
struct StaffLocationSwitcherView: View {
    let staff: StaffSessionStore

    @Environment(\.dismiss) private var dismiss
    @State private var details: StaffAccountDetails?
    @State private var loadError: String?
    @State private var target: StaffSwitchResponse?
    @State private var opening: Int?
    @State private var pin = ""
    @State private var error: String?
    @State private var shake = 0
    @State private var loading = false
    @State private var showingForgot = false

    init(staff: StaffSessionStore) {
        self.staff = staff
    }

    var body: some View {
        StaffSheetFrame(title: target == nil ? "Your locations" : "Switch location",
                        onBack: target == nil ? nil : { target = nil; pin = ""; error = nil },
                        onClose: { dismiss() }) {
            if let target {
                pinPad(target)
            } else {
                list
            }
        }
        .task { await load() }
        .sheet(isPresented: $showingForgot) {
            StaffForgotPinView(staff: staff, code: target.flatMap { $0.portalToken ?? $0.joinCode })
        }
    }

    @ViewBuilder
    private var list: some View {
        if let details {
            VStack(alignment: .leading, spacing: 10) {
                if details.locations.count <= 1 {
                    StaffSheetHelper(text: "You work at \(details.restaurant.isEmpty ? "one location" : details.restaurant) on Cavnar AI. Another location's logins show up here once you have one there.")
                }
                ForEach(details.locations) { location in
                    Button {
                        Task { await open(location) }
                    } label: {
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(location.restaurant)
                                    .font(.cavnarBody(CavnarType.body, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                Text(location.current ? "You're here" : "Sign in with your PIN there")
                                    .font(.cavnarBody(CavnarType.caption))
                                    .foregroundStyle(location.current ? Color.cavnarGreen : Color.cavnarInk3)
                            }
                            Spacer()
                            if opening == location.restaurantID {
                                CavnarSkeletonBar(height: 3).frame(width: 40).accessibilityHidden(true)
                            } else if location.current {
                                Image(systemName: "checkmark")
                                    .font(.system(size: 14, weight: .bold))
                                    .foregroundStyle(Color.cavnarGreen)
                                    .accessibilityHidden(true)
                            } else {
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 13, weight: .semibold))
                                    .foregroundStyle(Color.cavnarInk3)
                                    .accessibilityHidden(true)
                            }
                        }
                        .padding(.horizontal, 14)
                        .frame(minHeight: 56)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(location.current || opening != nil)
                    .accessibilityElement(children: .combine)
                    .accessibilityAddTraits(location.current ? .isSelected : [])
                }
                StaffErrorLine(text: error)
            }
        } else if let loadError {
            VStack(alignment: .leading, spacing: 12) {
                StaffErrorLine(text: loadError)
                Button("Try again") { Task { await load() } }
                    .buttonStyle(CavnarSecondaryButtonStyle())
            }
        } else {
            VStack(alignment: .leading, spacing: 8) {
                CavnarSkeletonBar(height: 3).frame(width: 180)
                Text("Loading your locations")
                    .font(.cavnarBody(CavnarType.secondary))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .accessibilityElement(children: .combine)
        }
    }

    private func pinPad(_ target: StaffSwitchResponse) -> some View {
        VStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                StaffSignInKicker(text: (target.restaurant ?? "").uppercased())
                Text(target.employeeName ?? "Your PIN")
                    .font(.cavnar(.headline))
                    .foregroundStyle(Color.cavnarInk)
                    .accessibilityAddTraits(.isHeader)
                StaffSheetHelper(text: "Each location has its own PIN. Enter the one you use at \(target.restaurant ?? "this location").")
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            StaffPinField(pin: $pin, isError: error != nil, shakeTrigger: shake, disabled: loading)
            StaffErrorLine(text: error, centered: true)
            Button {
                Task { await signIn(target) }
            } label: {
                StaffBusyLabel(title: "Sign in there", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(pin.count < 4 || loading)
            .frame(maxWidth: 320)
            Button("Forgot PIN?") { showingForgot = true }
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(minHeight: 44)
                .contentShape(Rectangle())
        }
    }

    private func load() async {
        loadError = nil
        do {
            details = try await staff.loadAccount()
        } catch let apiError as APIClient.APIError {
            loadError = apiError.message
        } catch {
            loadError = error.localizedDescription
        }
    }

    private func open(_ location: StaffAccountLocation) async {
        guard !location.current else { return }
        opening = location.restaurantID
        error = nil
        defer { opening = nil }
        do {
            let resp = try await staff.switchTarget(restaurantID: location.restaurantID)
            if resp.current == true || resp.requiresPin == false {
                dismiss()
                return
            }
            pin = ""
            target = resp
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = error.localizedDescription
        }
    }

    private func signIn(_ target: StaffSwitchResponse) async {
        guard let membershipID = target.membershipID,
              let code = (target.portalToken?.isEmpty == false ? target.portalToken : target.joinCode) else {
            error = "Couldn't open that location. Try again."
            return
        }
        loading = true
        defer { loading = false }
        let ok = await staff.signIn(portal: code, membershipID: membershipID, pin: pin,
                                    name: target.employeeName, restaurant: target.restaurant)
        if ok {
            Haptic.success()
            dismiss()
        } else {
            Haptic.error()
            shake += 1
            pin = ""
            error = staff.lastError
        }
    }
}

// MARK: - Notifications ask (C4)

/// Asked once after a sign-in, in one line, before iOS's own prompt: why
/// this app wants to buzz (UX-03). "Not now" waits a week. RootView shows
/// it over the portal while `staff.notificationAsk` is true.
struct StaffNotificationAskCard: View {
    let staff: StaffSessionStore

    init(staff: StaffSessionStore) {
        self.staff = staff
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "bell.badge")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
                Text("Get a heads-up when your schedule is posted or a request is answered.")
                    .font(.cavnarBody(CavnarType.body, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 12) {
                Button("Not now") { staff.declineNotifications() }
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk2)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
                Spacer()
                Button("Turn on notifications") {
                    Task { await staff.enableNotifications() }
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
            }
        }
        .padding(16)
        .cavnarCard(.floating)
        .padding(.horizontal, 16)
        // Clear of the portal's bottom tab bar, which it floats over.
        .padding(.bottom, 64)
        .accessibilityElement(children: .contain)
    }
}

// MARK: - Sheet chrome shared by the views above

struct StaffSheetFrame<Content: View>: View {
    let title: String
    var onBack: (() -> Void)?
    var onClose: () -> Void
    @ViewBuilder var content: Content

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(spacing: 4) {
                        if let onBack {
                            StaffBackButton(action: onBack)
                        }
                        Text(title)
                            .cavnarText(.headline)
                            .accessibilityAddTraits(.isHeader)
                        Spacer()
                        Button("Close", action: onClose)
                            .font(.cavnar(.label))
                            .foregroundStyle(Color.cavnarInk2)
                            .frame(minWidth: 44, minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    content
                }
                .padding(.horizontal, 22)
                .padding(.top, 14)
                .padding(.bottom, 24)
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollDismissesKeyboard(.interactively)
        }
    }
}

struct StaffSheetHelper: View {
    let text: String

    var body: some View {
        Text(text)
            .cavnarText(.body)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct StaffSheetField: View {
    @Binding var text: String
    let placeholder: String
    let label: String

    var body: some View {
        TextField(placeholder, text: $text)
            .font(.cavnar(.lead))
            .padding(14)
            .frame(minHeight: 50)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .foregroundStyle(Color.cavnarInk)
            .accessibilityLabel(label)
    }
}
