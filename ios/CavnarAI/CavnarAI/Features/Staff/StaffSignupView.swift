import SwiftUI

/// An employee makes their own account, with no manager involved.
///
/// Phone → texted code → the restaurant code → which name on the roster is
/// you → a PIN, typed twice. One question per screen, because this is filled
/// in standing up on a phone by someone who is about to start a shift.
///
/// The only real rule underneath it: a name can be claimed once, and only from
/// the roster the owner already keeps. Signing up is open — an account with no
/// membership can see nothing — but claiming "Jordan P." is not, because that
/// name is what the schedule, the shift list and every task completion record
/// are keyed by. A phone that already has a login here is sent to Forgot PIN,
/// never a second claim (409 `has_account`).
struct StaffSignupView: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss

    /// Prefilled when the employee arrived through their restaurant's own
    /// link, in which case the restaurant-code step is skipped in both
    /// directions.
    let knownJoinCode: String?

    private enum Step { case phone, code, restaurant, name, pin, confirm }

    @State private var step: Step = .phone
    @State private var phone = ""
    @State private var optedIn = false
    @State private var smsCode = ""
    @State private var devCode: String?
    @State private var joinCode = ""
    @State private var restaurantName = ""
    @State private var names: [StaffClaimableName] = []
    @State private var noneLeft = false
    @State private var search = ""
    @State private var chosen: StaffClaimableName?
    @State private var pin = ""
    @State private var confirmPin = ""
    @State private var shake = 0
    @State private var error: String?
    @State private var loading = false
    /// The verified phone ran out: the screen offers Start again, with the
    /// number kept (UX-30).
    @State private var expired = false
    /// 409 `has_account`: this phone already has a login here.
    @State private var hasAccount = false
    @State private var showingForgot = false
    @State private var showingNotListed = false

    init(knownJoinCode: String? = nil) {
        self.knownJoinCode = knownJoinCode
    }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    header
                    if expired {
                        expiredBlock
                    } else {
                        switch step {
                        case .phone:      phoneStep
                        case .code:       codeStep
                        case .restaurant: restaurantStep
                        case .name:       nameStep
                        case .pin:        pinStep(confirming: false)
                        case .confirm:    pinStep(confirming: true)
                        }
                    }
                }
                .padding(.horizontal, 22)
                .padding(.bottom, 24)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollDismissesKeyboard(.interactively)
        }
        .onAppear {
            if let knownJoinCode, !knownJoinCode.isEmpty, joinCode.isEmpty { joinCode = knownJoinCode }
        }
        .sheet(isPresented: $showingForgot) {
            StaffForgotPinView(staff: staff, code: joinCode, phone: phone)
        }
        .sheet(isPresented: $showingNotListed) {
            notListedSheet
                .presentationDetents([.medium])
                .presentationDragIndicator(.visible)
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(spacing: 4) {
                StaffBackButton(label: step == .phone ? "Close" : "Back") { back() }
                Text(chosen?.name ?? (restaurantName.isEmpty ? "Create your account" : restaurantName))
                    .cavnarText(.headline)
                    .lineLimit(2)
                Spacer()
            }
            // Where they are in it: the PIN and its retyping are one step,
            // and a restaurant's own link skips the code step.
            if !expired, let (n, total) = stepNumber {
                HomeMixedText.make("Step \(n) of \(total)", role: .caption, color: .cavnarInk2)
                    .accessibilityLabel("Step \(n) of \(total)")
            }
        }
        .padding(.top, 20)
        .padding(.bottom, 20)
    }

    private var stepNumber: (Int, Int)? {
        let skipsRestaurant = !(knownJoinCode ?? "").isEmpty
        let order: [Step] = skipsRestaurant ? [.phone, .code, .name, .pin]
                                            : [.phone, .code, .restaurant, .name, .pin]
        guard let i = order.firstIndex(of: step == .confirm ? .pin : step) else { return nil }
        return (i + 1, order.count)
    }

    // MARK: - Steps

    private var phoneStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("What's your mobile number?")
            helper("We'll text you a one-time code to check it's you. Your manager sees this number next to your name. No marketing texts, ever.")

            field($phone, placeholder: "(555) 014-2233", label: "Mobile number")
                .keyboardType(.phonePad)
                .textContentType(.telephoneNumber)
                .font(.cavnar(.figureS))

            // Unchecked by default, on purpose — this is the consent record
            // Twilio's A2P 10DLC review requires, and a pre-selected box is a
            // documented rejection reason, not just bad UX.
            Button {
                optedIn.toggle()
                if optedIn, error == Self.consentNeeded { error = nil }
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: optedIn ? "checkmark.square.fill" : "square")
                        .foregroundStyle(optedIn ? Color.cavnarEmber : Color.cavnarInk3)
                        .font(.system(size: 20))
                        .padding(.top, 1)
                        .accessibilityHidden(true)
                    // Wording verbatim (the A2P consent record); only its
                    // contrast moved, Ink3 → Ink2, so it can be read.
                    Text("I consent to receive a one-time SMS verification code from Cavnar AI at this number. Message and data rates may apply.")
                        .font(.cavnarBody(CavnarType.caption))
                        .foregroundStyle(Color.cavnarInk2)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(minHeight: 44, alignment: .top)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            // The legal A2P record: VoiceOver says whether it is ticked
            // (UX-18).
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isToggle)
            .accessibilityValue(optedIn ? "Checked" : "Not checked")

            StaffErrorLine(text: error)
            // Tappable without the box ticked, so the answer is a sentence
            // under it rather than a button that does nothing (UX-04).
            primary("Text me a code") {
                guard optedIn else {
                    error = Self.consentNeeded
                    Haptic.warning()
                    return
                }
                Task { await sendCode() }
            }
            .disabled(loading || phone.filter(\.isNumber).count < 10)
        }
    }

    private static let consentNeeded = "Tick the box to agree to the text first."

    private var codeStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("Enter the code")
            Text("We texted a 6-digit code to \(PhoneFormat.display(phone)).")
                .cavnarText(.body)

            if let devCode {
                StaffDevCode(code: devCode)
            }

            field($smsCode, placeholder: "000000", label: "Six-digit code")
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .font(.cavnar(.figureM))
                .multilineTextAlignment(.center)
                // Submit on the sixth digit, the way every OTP field does now
                // — including when iOS autofills the whole code from the SMS,
                // which arrives as one change rather than six.
                .onChange(of: smsCode) { _, value in
                    let digits = String(value.filter(\.isNumber).prefix(6))
                    if digits != value { smsCode = digits; return }
                    // Only while they are typing. Clearing on empty would
                    // wipe the message a failed attempt just set, since that
                    // failure resets the field.
                    if !digits.isEmpty { error = nil }
                    if digits.count == 6, !loading {
                        Task { await verifyCode() }
                    }
                }

            StaffErrorLine(text: error)
            primary("Continue") {
                Task { await verifyCode() }
            }
            .disabled(loading || smsCode.count < 6)

            // Disabled while a send or check is in flight: every tap is
            // another verification SMS (CLIENT-57).
            Button("Send it again") { Task { await sendCode() } }
                .disabled(loading)
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
        }
    }

    private var restaurantStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("Your restaurant code")
            helper("Six characters. It's on the poster in the back, or ask your manager.")

            field($joinCode, placeholder: "ABC123", label: "Restaurant code")
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .font(.cavnar(.figureM))
                .multilineTextAlignment(.center)

            StaffErrorLine(text: error)
            primary("Continue") {
                Task { await findRestaurant() }
            }
            .disabled(loading || joinCode.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var nameStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("Which one are you?")
            helper(names.isEmpty
                   ? "There are no names left to claim here. If you're new, your manager adds you to the schedule first."
                   : "Tap your name so your shifts line up.")

            if names.count > StaffNameSearch.threshold {
                StaffNameSearch(text: $search)
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                      spacing: 10) {
                ForEach(StaffNameSearch.filter(names, by: search, name: \.name)) { entry in
                    Button {
                        chosen = entry
                        pin = ""
                        confirmPin = ""
                        error = nil
                        step = .pin
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(entry.name)
                                .cavnarText(.label)
                            if let job = entry.jobRole {
                                Text(job)
                                    .font(.cavnarBody(CavnarType.caption))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                        }
                        .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
                        .padding(.vertical, 15)
                        .padding(.horizontal, 14)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(.top, 4)

            StaffErrorLine(text: error)

            // The new hire who isn't on the list yet (WF-27, UX-30).
            Button("My name isn't here") { showingNotListed = true }
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
        }
    }

    private var notListedSheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("Not on the list yet?")
            helper("Your name shows up here once your manager adds you to the schedule. Ask them, then check again — your phone stays verified for 30 minutes.")
            Button {
                showingNotListed = false
                Task { await loadNames() }
            } label: {
                StaffBusyLabel(title: "Check again", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(loading)
            Button("Close") { showingNotListed = false }
                .cavnarText(.label, color: .cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
        }
        .padding(22)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(Color.cavnarPaper.ignoresSafeArea())
    }

    private func pinStep(confirming: Bool) -> some View {
        VStack(spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                title(confirming ? "Type it again" : "Choose a PIN")
                helper(confirming
                       ? "The same PIN once more, so a slip on the pad doesn't lock you out."
                       : "4 to 8 digits. You'll use it to sign in to this app — don't share it.")
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            StaffPinField(pin: confirming ? $confirmPin : $pin, isError: error != nil,
                          shakeTrigger: shake, disabled: loading)

            StaffErrorLine(text: error, centered: true)

            if hasAccount {
                Button("Reset my PIN") { showingForgot = true }
                    .buttonStyle(CavnarPrimaryButtonStyle())
            } else {
                Button {
                    if confirming {
                        Task { await claim() }
                    } else {
                        error = nil
                        confirmPin = ""
                        step = .confirm
                    }
                } label: {
                    StaffBusyLabel(title: confirming ? "Create my account" : "Next", busy: loading)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled((confirming ? confirmPin.count : pin.count) < 4 || loading)
                .frame(maxWidth: 320)
            }
        }
        .onChange(of: confirming ? confirmPin : pin) { _, value in
            if !value.isEmpty, error != nil, !hasAccount { error = nil }
        }
    }

    /// The verified phone ran out (30 minutes): one way forward, with the
    /// number kept (UX-30).
    private var expiredBlock: some View {
        VStack(alignment: .leading, spacing: 14) {
            title("Let's check your number again")
            helper("Your phone check ran out after 30 minutes. We'll text a new code to \(PhoneFormat.display(phone)).")
            StaffErrorLine(text: error)
            primary("Start again") {
                expired = false
                hasAccount = false
                smsCode = ""
                pin = ""
                confirmPin = ""
                chosen = nil
                error = nil
                step = .phone
            }
        }
    }

    // MARK: - Pieces

    private func title(_ text: String) -> some View {
        Text(text)
            .cavnarText(.title)
            .accessibilityAddTraits(.isHeader)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func helper(_ text: String) -> some View {
        Text(text)
            .cavnarText(.body)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func field(_ text: Binding<String>, placeholder: String, label: String) -> some View {
        TextField(placeholder, text: text)
            .font(.cavnar(.lead))
            .padding(14)
            .frame(minHeight: 50)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .foregroundStyle(Color.cavnarInk)
            .accessibilityLabel(label)
    }

    /// The step's one primary. The style's own press haptic is the only
    /// one (M2: a single haptic per tap).
    private func primary(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            StaffBusyLabel(title: title, busy: loading)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
    }

    // MARK: - Actions

    /// The back arrow walks the same list backwards, skipping the
    /// restaurant-code step when the link already named the restaurant, so
    /// nobody is stranded on a screen they cannot answer.
    private func back() {
        error = nil
        if expired { dismiss(); return }
        switch step {
        case .phone:      dismiss()
        case .code:       step = .phone
        case .restaurant: step = .code
        case .name:       step = (knownJoinCode?.isEmpty == false) ? .code : .restaurant
        case .pin:        chosen = nil; hasAccount = false; step = .name
        case .confirm:    confirmPin = ""; step = .pin
        }
    }

    private func sendCode() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.startSignup(phone: phone, optin: optedIn)
            guard resp.ok else {
                error = resp.error ?? "Couldn't send a code."
                return
            }
            devCode = resp.devCode
            smsCode = ""
            step = .code
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func verifyCode() async {
        guard !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            _ = try await staff.verifySignupCode(phone: phone, code: smsCode)
            if joinCode.isEmpty {
                step = .restaurant
            } else {
                await loadNamesNow()
            }
        } catch let apiError as APIClient.APIError {
            // Cleared so the next attempt is six fresh presses rather than
            // editing a wrong code in place — and so the auto-submit above
            // does not re-fire on the same wrong value.
            smsCode = ""
            error = apiError.message
        } catch {
            smsCode = ""
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func findRestaurant() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            restaurantName = try await staff.restaurantFor(joinCode: joinCode.trimmingCharacters(in: .whitespaces))
            await loadNamesNow()
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func loadNames() async {
        loading = true
        defer { loading = false }
        await loadNamesNow()
    }

    private func loadNamesNow() async {
        do {
            let resp = try await staff.claimableNames(joinCode: joinCode.trimmingCharacters(in: .whitespaces))
            guard resp.ok else {
                error = resp.error ?? "Couldn't load the list."
                return
            }
            restaurantName = resp.restaurant ?? restaurantName
            names = resp.names ?? []
            noneLeft = resp.noneLeft ?? names.isEmpty
            search = ""
            step = .name
        } catch is StaffSignupExpiredError {
            expired = true
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func claim() async {
        guard let chosen else { return }
        guard confirmPin == pin else {
            // UX-22: a slip on the pad is caught here, not at the next shift.
            Haptic.error()
            shake += 1
            error = "Those didn't match — try again."
            pin = ""
            confirmPin = ""
            step = .pin
            return
        }
        loading = true
        error = nil
        defer { loading = false }
        switch await staff.claim(joinCode: joinCode.trimmingCharacters(in: .whitespaces),
                                 employeeName: chosen.name, pin: pin, restaurant: restaurantName) {
        case .signedIn:
            Haptic.success()
            // RootView watches the staff token and swaps to the portal, so
            // there is nothing to navigate to from here.
            dismiss()
        case .hasAccount(_, let message):
            Haptic.error()
            hasAccount = true
            error = message
            step = .confirm
        case .expired(let message):
            Haptic.error()
            error = message
            expired = true
        case .failed(let message):
            Haptic.error()
            shake += 1
            pin = ""
            confirmPin = ""
            error = message
            step = .pin
        }
    }
}

/// The local-only code a dev backend hands back (STAFF_SIGNUP_DEV_CODE=1, no
/// Twilio). The server decides; it never appears in production.
struct StaffDevCode: View {
    let code: String

    var body: some View {
        Text("Texting isn't set up on this server, so here's your code: \(code)")
            .cavnarText(.caption, color: .cavnarEmber2)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(RoundedRectangle(cornerRadius: 10)
                .strokeBorder(Color.cavnarEmber, style: StrokeStyle(lineWidth: 1, dash: [4])))
    }
}
