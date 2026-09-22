import SwiftUI

/// An employee makes their own account, with no manager involved.
///
/// Phone → texted code → the restaurant's join code → which name on the roster
/// is you → a PIN. Five screens, one question each, because this is filled in
/// standing up on a phone by someone who is about to start a shift.
///
/// The only real rule underneath it: a name can be claimed once, and only from
/// the roster the owner already keeps. Signing up is open — an account with no
/// membership can see nothing — but claiming "Jordan P." is not, because that
/// name is what the schedule, the shift list and every task completion record
/// are keyed by.
struct StaffSignupView: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss

    /// Prefilled when the employee arrived through their restaurant's own
    /// link, in which case the join-code step is skipped in both directions.
    let knownJoinCode: String?

    private enum Step { case phone, code, restaurant, name, pin }

    @State private var step: Step = .phone
    @State private var phone = ""
    @State private var optedIn = false
    @State private var smsCode = ""
    @State private var devCode: String?
    @State private var joinCode = ""
    @State private var restaurantName = ""
    @State private var names: [StaffClaimableName] = []
    @State private var noneLeft = false
    @State private var chosen: StaffClaimableName?
    @State private var pin = ""
    @State private var error: String?
    @State private var loading = false

    init(knownJoinCode: String? = nil) {
        self.knownJoinCode = knownJoinCode
    }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(alignment: .leading, spacing: 0) {
                header
                switch step {
                case .phone:      phoneStep
                case .code:       codeStep
                case .restaurant: restaurantStep
                case .name:       nameStep
                case .pin:        pinStep
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 22)
        }
        .onAppear {
            if let knownJoinCode, !knownJoinCode.isEmpty { joinCode = knownJoinCode }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Button {
                Haptic.light()
                back()
            } label: {
                Image(systemName: "chevron.left").foregroundStyle(Color.cavnarInk3)
            }
            Text(chosen?.name ?? (restaurantName.isEmpty ? "Create your account" : restaurantName))
                .font(.cavnarHeadline(20))
                .foregroundStyle(Color.cavnarInk)
            Spacer()
        }
        .padding(.top, 30)
        .padding(.bottom, 24)
    }

    // MARK: - Steps

    private var phoneStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("What's your mobile number?")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text("We'll text you a one-time code to verify it's you. Your manager sees this number next to your name. No marketing texts, ever.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)

            field($phone, placeholder: "(555) 014-2233")
                .keyboardType(.phonePad)
                .textContentType(.telephoneNumber)

            // Unchecked by default, on purpose — this is the consent record
            // Twilio's A2P 10DLC review requires, and a pre-selected box is a
            // documented rejection reason, not just bad UX.
            Button {
                Haptic.light()
                optedIn.toggle()
            } label: {
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: optedIn ? "checkmark.square.fill" : "square")
                        .foregroundStyle(optedIn ? Color.cavnarEmber : Color.cavnarInk3)
                        .font(.system(size: 17))
                        .padding(.top, 2)
                    Text("I consent to receive a one-time SMS verification code from Cavnar AI at this number. Message and data rates may apply.")
                        .font(.cavnarBody(12.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .multilineTextAlignment(.leading)
                }
            }
            .buttonStyle(.plain)

            errorLine
            primary(loading ? "Sending…" : "Text me a code") {
                Task { await sendCode() }
            }
            .disabled(loading || phone.filter(\.isNumber).count < 10 || !optedIn)
        }
    }

    private var codeStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Enter the code")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text("We texted a 6-digit code to \(phone).")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)

            if let devCode {
                // Only ever present when the server has no Twilio configured
                // and is not deployed. The server decides this, so it cannot
                // appear in production.
                Text("Texting isn't set up on this server, so here's your code: \(devCode)")
                    .font(.cavnarBody(13))
                    .foregroundStyle(Color.cavnarEmber2)
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .overlay(RoundedRectangle(cornerRadius: 10)
                        .strokeBorder(Color.cavnarEmber, style: StrokeStyle(lineWidth: 1, dash: [4])))
            }

            field($smsCode, placeholder: "000000")
                .keyboardType(.numberPad)
                .textContentType(.oneTimeCode)
                .font(.cavnarNumber(22, weight: 600))
                .multilineTextAlignment(.center)
                // Submit on the sixth digit, the way every OTP field does now
                // — including when iOS autofills the whole code from the SMS,
                // which arrives as one change rather than six.
                .onChange(of: smsCode) { _, value in
                    let digits = value.filter(\.isNumber)
                    if digits != value { smsCode = digits; return }
                    // Only while they are typing. Clearing on empty would
                    // wipe the message a failed attempt just set, since that
                    // failure resets the field.
                    if !digits.isEmpty { error = nil }
                    if digits.count == 6, !loading {
                        Task { await verifyCode() }
                    }
                }

            errorLine
            primary(loading ? "Checking…" : "Continue") {
                Task { await verifyCode() }
            }
            .disabled(loading || smsCode.count < 6)

            // Disabled while a send or check is in flight: every tap is
            // another verification SMS (CLIENT-57).
            Button("Send it again") { Task { await sendCode() } }
                .disabled(loading)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(maxWidth: .infinity)
                .padding(.top, 4)
        }
    }

    private var restaurantStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Your restaurant's code")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text("Six characters. It's posted in the back of house, or your manager has it.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)

            field($joinCode, placeholder: "ABC123")
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .font(.cavnarNumber(22, weight: 600))
                .multilineTextAlignment(.center)

            errorLine
            primary(loading ? "Checking…" : "Continue") {
                Task { await findRestaurant() }
            }
            .disabled(loading || joinCode.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var nameStep: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Which one are you?")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text(noneLeft
                 ? "Everyone on this restaurant's list already has an account. Ask your manager to add you."
                 : "Tap your name so your shifts line up.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)

            ScrollView {
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(names) { entry in
                        Button {
                            Haptic.light()
                            chosen = entry
                            pin = ""
                            error = nil
                            step = .pin
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(entry.name)
                                    .font(.cavnarBody(15.5, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                if let job = entry.jobRole {
                                    Text(job)
                                        .font(.cavnarBody(12))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.vertical, 15)
                            .padding(.horizontal, 14)
                            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
                .padding(.top, 4)
            }
            errorLine
        }
    }

    private var pinStep: some View {
        VStack(spacing: 18) {
            Text("Choose a PIN")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text("4 digits. You'll use this here and on the restaurant's tablet — don't share it.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)
                .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 12) {
                ForEach(0..<4, id: \.self) { index in
                    Circle()
                        .fill(index < pin.count ? Color.cavnarEmber : Color.cavnarPaper3)
                        .frame(width: 14, height: 14)
                }
            }
            .padding(.vertical, 10)

            if let error {
                Text(error)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarRed)
                    .multilineTextAlignment(.center)
            }

            StaffPinPad(
                onDigit: { digit in
                    guard pin.count < 8, !loading else { return }
                    pin.append(digit)
                    if pin.count == 4 { Task { await claim() } }
                },
                onDelete: { if !pin.isEmpty { pin.removeLast() } },
                onClear: { pin = ""; error = nil }
            )
        }
    }

    // MARK: - Pieces

    private func field(_ text: Binding<String>, placeholder: String) -> some View {
        TextField(placeholder, text: text)
            .font(.cavnarBody(17))
            .padding(14)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
            .foregroundStyle(Color.cavnarInk)
    }

    @ViewBuilder
    private var errorLine: some View {
        if let error {
            Text(error)
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarRed)
        }
    }

    private func primary(_ title: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Text(title).frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarPrimaryButtonStyle())
    }

    // MARK: - Actions

    /// The back arrow walks the same list backwards, skipping the join-code
    /// step when the link already named the restaurant, so nobody is stranded
    /// on a screen they cannot answer.
    private func back() {
        error = nil
        switch step {
        case .phone:      dismiss()
        case .code:       step = .phone
        case .restaurant: step = .code
        case .name:       step = (knownJoinCode?.isEmpty == false) ? .code : .restaurant
        case .pin:        chosen = nil; step = .name
        }
    }

    private func sendCode() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.startSignup(phone: phone, optin: optedIn)
            guard resp.ok else {
                error = resp.error ?? "Could not send a code."
                return
            }
            devCode = resp.devCode
            step = .code
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Could not reach the server."
        }
    }

    private func verifyCode() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            _ = try await staff.verifySignupCode(phone: phone, code: smsCode)
            if joinCode.isEmpty {
                step = .restaurant
            } else {
                await loadNames()
            }
        } catch let apiError as APIClient.APIError {
            // Cleared so the next attempt is six fresh presses rather than
            // editing a wrong code in place — and so the auto-submit above
            // does not re-fire on the same wrong value.
            smsCode = ""
            error = apiError.message
        } catch {
            smsCode = ""
            self.error = "Could not reach the server."
        }
    }

    private func findRestaurant() async {
        loading = true
        error = nil
        defer { loading = false }
        do {
            restaurantName = try await staff.restaurantFor(joinCode: joinCode)
            await loadNames()
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Could not reach the server."
        }
    }

    private func loadNames() async {
        do {
            let resp = try await staff.claimableNames(joinCode: joinCode)
            guard resp.ok else {
                error = resp.error ?? "Could not load the list."
                return
            }
            restaurantName = resp.restaurant ?? restaurantName
            names = resp.names ?? []
            noneLeft = resp.noneLeft ?? names.isEmpty
            step = .name
        } catch let apiError as APIClient.APIError {
            error = apiError.message
        } catch {
            self.error = "Could not reach the server."
        }
    }

    private func claim() async {
        guard let chosen else { return }
        loading = true
        error = nil
        defer { loading = false }
        let ok = await staff.claim(joinCode: joinCode, employeeName: chosen.name, pin: pin)
        if ok {
            Haptic.success()
            // RootView watches the staff token and swaps to the portal, so
            // there is nothing to navigate to from here.
            dismiss()
        } else {
            Haptic.error()
            pin = ""
            error = staff.lastError ?? "Could not finish setting up your account."
        }
    }
}
