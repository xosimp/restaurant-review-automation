import SwiftUI

/// Restaurant code → name → PIN. A few taps and a 4-8 digit PIN, because this
/// runs at the start of a shift with a queue forming, not at a desk.
///
/// A phone that has signed in before opens straight on the last person's PIN
/// pad for its restaurant, with "Not you?" (H10 / UX-02, UX-21): the code is
/// one-time setup and the name grid is for the shared phone. The same screen
/// is the idle lock (15 minutes away, M4) and where a PIN change, a reset or
/// an ended session lands, with the sentence that says why.
struct StaffLoginView: View {
    @Environment(StaffSessionStore.self) private var staff

    /// Presented over the owner sign-in ("I'm staff"): a Close button.
    var onClose: (() -> Void)? = nil
    /// The root of a staff phone: a quiet way to the owner sign-in.
    var onOwnerSignIn: (() -> Void)? = nil

    private enum Step { case code, roster, pin }

    @State private var step: Step = .code
    @State private var portalCode = ""
    @State private var restaurantName = ""
    @State private var roster: [StaffRosterEntry] = []
    @State private var rosterLoaded = false
    @State private var selected: StaffRosterEntry?
    /// The pad was opened from the device's memory, not a tap on the grid:
    /// its way out is "Not you?", not Back.
    @State private var fromMemory = false
    @State private var search = ""
    @State private var pin = ""
    @State private var error: String?
    @State private var shake = 0
    @State private var loading = false
    @State private var showingSignup = false
    @State private var showingForgot = false

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.cavnarPaper.ignoresSafeArea()
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    switch step {
                    case .code:   codeEntry
                    case .roster: rosterPicker
                    case .pin:
                        if let selected { pinEntry(for: selected) } else { rosterPicker }
                    }
                    ownerDoor
                }
                .padding(.horizontal, 22)
                .padding(.top, 30)
                .padding(.bottom, 24)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity)
            }
            .scrollBounceBehavior(.basedOnSize)
            .scrollDismissesKeyboard(.interactively)

            if let onClose {
                Button("Close", action: onClose)
                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(minWidth: 44, minHeight: 44)
                    .padding(.horizontal, 12)
                    .contentShape(Rectangle())
            }
        }
        .task { await start() }
        .onChange(of: staff.isLocked) { _, locked in
            if locked { Task { await start() } }
        }
        .fullScreenCover(isPresented: $showingSignup) {
            // Full screen, like the staff sign-in itself: this is the whole
            // of getting into the product, not a detour inside it.
            StaffSignupView(knownJoinCode: Self.joinCode(staff.portalToken))
        }
        .sheet(isPresented: $showingForgot) {
            StaffForgotPinView(staff: staff, code: portalCode.isEmpty ? staff.portalToken : portalCode)
        }
    }

    // MARK: - Steps

    private var codeEntry: some View {
        VStack(alignment: .leading, spacing: 14) {
            StaffSignInKicker(text: "STAFF SIGN IN")
            Text("Enter your restaurant code")
                .font(.cavnar(.title))
                .foregroundStyle(Color.cavnarInk)
                .accessibilityAddTraits(.isHeader)
            Text("It's on the poster in the back, or ask your manager. This phone remembers it.")
                .font(.cavnarBody(CavnarType.body))
                .foregroundStyle(Color.cavnarInk3)

            TextField("Restaurant code", text: $portalCode)
                .textInputAutocapitalization(.characters)
                .autocorrectionDisabled()
                .submitLabel(.continue)
                .onSubmit { Task { await loadRoster(code: portalCode) } }
                .font(.cavnar(.figureM))
                .padding(14)
                .frame(minHeight: 50)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                .foregroundStyle(Color.cavnarInk)
                .accessibilityLabel("Restaurant code")

            StaffErrorLine(text: error)

            Button {
                Task { await loadRoster(code: portalCode) }
            } label: {
                StaffBusyLabel(title: "Continue", busy: loading)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(portalCode.trimmingCharacters(in: .whitespaces).isEmpty || loading)

            if !staff.savedLocations.isEmpty {
                // Several restaurants on one phone (M12): each is one tap.
                StaffSignInKicker(text: "ON THIS PHONE").padding(.top, 14)
                ForEach(staff.savedLocations) { saved in
                    Button {
                        portalCode = saved.code
                        Task { await loadRoster(code: saved.code) }
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(saved.restaurant.isEmpty ? saved.code : saved.restaurant)
                                    .font(.cavnarBody(CavnarType.body, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                if let name = saved.employeeName {
                                    Text(name)
                                        .font(.cavnarBody(CavnarType.caption))
                                        .foregroundStyle(Color.cavnarInk3)
                                }
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.system(size: 13, weight: .semibold))
                                .foregroundStyle(Color.cavnarInk3)
                                .accessibilityHidden(true)
                        }
                        .padding(.horizontal, 14)
                        .frame(minHeight: 52)
                        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(loading)
                }
            }

            newHere
        }
    }

    private var rosterPicker: some View {
        VStack(alignment: .leading, spacing: 14) {
            StaffSignInKicker(text: restaurantName.uppercased())
            Text("Who's signing in?")
                .font(.cavnar(.title))
                .foregroundStyle(Color.cavnarInk)
                .accessibilityAddTraits(.isHeader)

            if staff.isLocked {
                StaffNoticeLine(text: staff.signInNotice)
            }

            if !rosterLoaded && loading {
                VStack(alignment: .leading, spacing: 8) {
                    CavnarSkeletonBar(height: 3).frame(width: 180)
                    Text("Loading your team")
                        .font(.cavnarBody(CavnarType.secondary))
                        .foregroundStyle(Color.cavnarInk3)
                }
                .accessibilityElement(children: .combine)
            } else if rosterLoaded && roster.isEmpty {
                Text("Nobody has a PIN here yet. If you're new, create your account below.")
                    .font(.cavnarBody(CavnarType.body))
                    .foregroundStyle(Color.cavnarInk3)
            }

            StaffErrorLine(text: error)

            if roster.count > StaffNameSearch.threshold {
                StaffNameSearch(text: $search)
            }

            LazyVGrid(columns: [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)],
                      spacing: 10) {
                ForEach(StaffNameSearch.filter(roster, by: search, name: \.name)) { person in
                    Button {
                        choose(person, fromMemory: false)
                    } label: {
                        Text(person.name)
                            .font(.cavnar(.label))
                            .foregroundStyle(Color.cavnarInk)
                            .multilineTextAlignment(.leading)
                            .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
                            .padding(.vertical, 15)
                            .padding(.horizontal, 14)
                            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 4)

            newHere

            Button("Use a different restaurant code") {
                search = ""
                roster = []
                rosterLoaded = false
                restaurantName = ""
                portalCode = ""
                error = nil
                step = .code
            }
            .font(.cavnarBody(CavnarType.secondary))
            .foregroundStyle(Color.cavnarInk3)
            .frame(maxWidth: .infinity, minHeight: 44)
            .contentShape(Rectangle())
        }
    }

    private func pinEntry(for person: StaffRosterEntry) -> some View {
        VStack(spacing: 18) {
            HStack(spacing: 6) {
                if !fromMemory && !staff.isLocked {
                    StaffBackButton { backToRoster(forget: false) }
                }
                VStack(alignment: .leading, spacing: 2) {
                    if !restaurantName.isEmpty {
                        StaffSignInKicker(text: restaurantName.uppercased())
                    }
                    Text(person.name)
                        .font(.cavnar(.title))
                        .foregroundStyle(Color.cavnarInk)
                        .accessibilityAddTraits(.isHeader)
                }
                Spacer(minLength: 8)
                if fromMemory || staff.isLocked {
                    // The shared phone's way out (UX-21): whoever is holding
                    // it picks their own name instead.
                    Button("Not you?") { backToRoster(forget: true) }
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                        .frame(minWidth: 44, minHeight: 44)
                        .contentShape(Rectangle())
                        .accessibilityHint("Shows everyone's names")
                }
            }

            StaffNoticeLine(text: staff.signInNotice)

            StaffPinField(pin: $pin, isError: error != nil, shakeTrigger: shake, disabled: loading)

            StaffErrorLine(text: error, centered: true)

            // Submitted by the person, not by the fourth digit. Auto-submit
            // at four meant a 5-8 digit PIN was always sent cut short and
            // could never sign in (CLIENT-3).
            Button {
                Task { await submit(person) }
            } label: {
                StaffBusyLabel(title: "Sign in", busy: loading)
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
        .onChange(of: pin) { _, value in
            if !value.isEmpty, error != nil { error = nil }
        }
    }

    /// The way in for someone who has no account yet. Offered on the code
    /// screen and the name screen, because "my name isn't on this list" is
    /// exactly when a new hire realises they need it.
    private var newHere: some View {
        Button("First time here? Create your account") {
            showingSignup = true
        }
        .font(.cavnarBody(CavnarType.secondary, weight: 700))
        .foregroundStyle(Color.cavnarEmber2)
        .frame(maxWidth: .infinity, minHeight: 44)
        .contentShape(Rectangle())
        .padding(.top, 12)
    }

    /// A staff phone opens here, not on the owner sign-in; an owner or
    /// manager holding it gets there in one tap.
    @ViewBuilder
    private var ownerDoor: some View {
        if let onOwnerSignIn {
            Button("Owner or manager? Sign in here", action: onOwnerSignIn)
                .font(.cavnarBody(CavnarType.caption, weight: 600))
                .foregroundStyle(Color.cavnarInk3)
                .frame(maxWidth: .infinity, minHeight: 44)
                .contentShape(Rectangle())
                .padding(.top, 20)
        }
    }

    /// The saved code when it is the 6-character restaurant code. Signup
    /// resolves only that one (staff_routes.signup_where / signup_claim); a
    /// long portal token saved by a PIN reset or a switch would read as
    /// "we don't recognise that code", so signup asks for the code instead.
    static func joinCode(_ saved: String?) -> String? {
        guard let saved = saved?.trimmingCharacters(in: .whitespaces), !saved.isEmpty, saved.count <= 8 else {
            return nil
        }
        return saved
    }

    // MARK: - Actions

    /// Where the screen opens: this phone's restaurant and, when it has one,
    /// the last person's PIN pad — at once, from memory, while the roster
    /// (and its sign-in nonce) loads behind it.
    private func start() async {
        guard let saved = staff.portalToken, !saved.isEmpty else {
            step = .code
            return
        }
        portalCode = saved
        restaurantName = staff.restaurantName(for: saved) ?? restaurantName
        if let person = staff.lastPerson(for: saved) {
            selected = person
            fromMemory = true
            pin = ""
            step = .pin
        } else if step == .code {
            step = .roster
        }
        await loadRoster(code: saved)
    }

    private func loadRoster(code raw: String) async {
        let code = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !code.isEmpty, !loading else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.roster(portal: code)
            guard resp.ok else {
                error = resp.error ?? "We don't recognise that code."
                return
            }
            restaurantName = resp.restaurant ?? restaurantName
            roster = resp.roster ?? []
            rosterLoaded = true
            staff.portalToken = code
            if let current = selected, !roster.contains(where: { $0.membershipID == current.membershipID }) {
                // The remembered person has no PIN here any more.
                selected = nil
                fromMemory = false
                staff.forgetPerson(for: code)
            }
            if selected == nil { step = .roster }
        } catch let apiError as APIClient.APIError {
            // The server's own sentence, never "Could not reach the server"
            // for a code it answered (M10). A code it no longer knows leaves
            // this phone's memory.
            if apiError.status == 404 {
                staff.forgetCode(code)
                selected = nil
                fromMemory = false
                roster = []
                rosterLoaded = false
                restaurantName = ""
                step = .code
                error = (apiError.message) + " Ask your manager for the current restaurant code."
            } else {
                error = apiError.message
            }
        } catch {
            self.error = "Couldn't reach the server — check your connection and try again."
        }
    }

    private func choose(_ person: StaffRosterEntry, fromMemory: Bool) {
        selected = person
        self.fromMemory = fromMemory
        pin = ""
        error = nil
        step = .pin
        // A fresh one-shot nonce for this sign-in: the roster's may have
        // been spent by a mistyped PIN or aged out on this screen (C3).
        let code = portalCode
        Task { await staff.refreshNonce(portal: code) }
    }

    private func backToRoster(forget: Bool) {
        if forget {
            // Signs a locked session out; either way the phone stops opening
            // on this person and their cached screens go with them.
            staff.notMe()
        }
        selected = nil
        fromMemory = false
        pin = ""
        error = nil
        step = .roster
        if !rosterLoaded { Task { await loadRoster(code: portalCode) } }
    }

    private func submit(_ person: StaffRosterEntry) async {
        loading = true
        defer { loading = false }
        let ok = await staff.signIn(portal: portalCode, membershipID: person.membershipID, pin: pin,
                                    name: person.name, restaurant: restaurantName)
        if ok {
            Haptic.success()
        } else {
            Haptic.error()
            pin = ""
            error = staff.lastError
            shake += 1
        }
    }
}

// MARK: - The PIN controls (shared by sign-in, signup, forgot PIN, Change PIN
// and the location switch, so every PIN is typed on the same control)

/// A numeric pad rather than the system keyboard: a phone on a pass gets used
/// with one thumb and often a glove, and the keyboard's number row is the
/// wrong target size for that.
struct StaffPinPad: View {
    let onDigit: (String) -> Void
    let onDelete: () -> Void
    let onClear: () -> Void

    private let rows = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]]

    var body: some View {
        VStack(spacing: 11) {
            ForEach(rows, id: \.self) { row in
                HStack(spacing: 11) {
                    ForEach(row, id: \.self) { digit in
                        key(digit, accessibility: digit) { onDigit(digit) }
                    }
                }
            }
            HStack(spacing: 11) {
                key("Clear", small: true, accessibility: "Clear", action: onClear)
                key("0", accessibility: "0") { onDigit("0") }
                key(nil, symbol: "delete.left", accessibility: "Delete", action: onDelete)
            }
        }
        .frame(maxWidth: 320)
    }

    private func key(_ label: String?, small: Bool = false, symbol: String? = nil,
                     accessibility: String, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Group {
                if let symbol {
                    Image(systemName: symbol)
                        .font(.system(size: 20, weight: .medium))
                        .foregroundStyle(Color.cavnarInk2)
                } else {
                    Text(label ?? "")
                        .font(small ? .cavnar(.secondary) : .cavnar(.figureM))
                        .foregroundStyle(small ? Color.cavnarInk3 : Color.cavnarInk)
                }
            }
            .frame(maxWidth: .infinity)
            .frame(height: 58)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 14))
            .contentShape(RoundedRectangle(cornerRadius: 14))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibility)
    }
}

/// The PIN's dots: at least four, one more for each digit past four (a PIN
/// is 4-8 digits and the pad cannot know which length this person chose).
/// An empty dot is an ink3 ring, not a near-black fill (UX-29: 1.29:1); a
/// wrong PIN turns them red and shakes the row once — still under Reduce
/// Motion, where only the colour changes (CavnarPasscodePad's motion).
struct StaffPinDots: View {
    let count: Int
    var isError: Bool = false
    var shakeTrigger: Int = 0

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var offset: CGFloat = 0

    var body: some View {
        HStack(spacing: 14) {
            ForEach(0..<max(4, count), id: \.self) { index in
                let filled = index < count
                ZStack {
                    Circle()
                        .strokeBorder(isError ? Color.cavnarRed.opacity(0.8) : Color.cavnarInk3,
                                      lineWidth: 1.5)
                    if filled {
                        Circle().fill(isError ? Color.cavnarRed : Color.cavnarEmber)
                            .transition(.scale(scale: 0.4).combined(with: .opacity))
                    }
                }
                .frame(width: 15, height: 15)
            }
        }
        .padding(.vertical, 10)
        .offset(x: offset)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.15), value: count)
        .onChange(of: shakeTrigger) { _, _ in
            guard !reduceMotion else { return }
            Task { @MainActor in
                for dx: CGFloat in [-10, 9, -7, 5, -3, 0] {
                    withAnimation(.easeInOut(duration: 0.055)) { offset = dx }
                    try? await Task.sleep(for: .seconds(0.055))
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("PIN")
        .accessibilityValue(count == 1 ? "1 digit entered" : "\(count) digits entered")
    }
}

/// Dots over the pad, bound to one PIN string — up to 8 digits
/// (auth.PIN_MAX_LENGTH), never submitted by the pad itself.
struct StaffPinField: View {
    @Binding var pin: String
    var isError: Bool = false
    var shakeTrigger: Int = 0
    var disabled: Bool = false

    var body: some View {
        VStack(spacing: 16) {
            StaffPinDots(count: pin.count, isError: isError, shakeTrigger: shakeTrigger)
            StaffPinPad(
                onDigit: { digit in
                    guard pin.count < 8, !disabled else { return }
                    pin.append(digit)
                },
                onDelete: { if !pin.isEmpty { pin.removeLast() } },
                onClear: { pin = "" }
            )
            .disabled(disabled)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Small pieces shared by the staff sign-in screens

/// The house busy state inside a button (DESIGN_SYSTEM §10): the label stays
/// and the 3pt ember pulse runs under it — never "Checking…", never a
/// spinner. The pulse is still under Reduce Motion (CavnarSkeletonBar).
struct StaffBusyLabel: View {
    let title: String
    var busy: Bool

    var body: some View {
        Text(title)
            .frame(maxWidth: .infinity)
            .overlay(alignment: .bottom) {
                if busy {
                    CavnarSkeletonBar(height: 3)
                        .frame(width: 90)
                        .offset(y: 8)
                        .accessibilityHidden(true)
                }
            }
            .accessibilityValue(busy ? "Working" : "")
    }
}

/// The sign-in screens' kicker — now the one kicker, `CavnarKicker` (iOS
/// readability round, 10/8/26).
struct StaffSignInKicker: View {
    let text: String

    var body: some View {
        CavnarKicker(text)
    }
}

/// One plain sentence in red, under the control that caused it.
struct StaffErrorLine: View {
    let text: String?
    var centered: Bool = false

    var body: some View {
        if let text, !text.isEmpty {
            Text(text)
                .font(.cavnarBody(CavnarType.secondary, weight: 600))
                .foregroundStyle(Color.cavnarRedText)
                .multilineTextAlignment(centered ? .center : .leading)
                .frame(maxWidth: .infinity, alignment: centered ? .center : .leading)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// Why the person is looking at a PIN pad again ("PIN changed — sign in
/// with your new PIN.", "Your shift session ended — sign in again.").
struct StaffNoticeLine: View {
    let text: String?

    var body: some View {
        if let text, !text.isEmpty {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: "info.circle")
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
                Text(text)
                    .font(.cavnarBody(CavnarType.secondary, weight: 600))
                    .foregroundStyle(Color.cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
            .accessibilityElement(children: .combine)
        }
    }
}

/// A 44×44 back chevron with a VoiceOver label (UX-18, UX-19).
struct StaffBackButton: View {
    var label: String = "Back"
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: "chevron.left")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

/// The name search above a long grid (UX-21): shown past eight names, on the
/// sign-in roster and the signup list.
struct StaffNameSearch: View {
    @Binding var text: String

    static let threshold = 8

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(Color.cavnarInk3)
                .accessibilityHidden(true)
            TextField("Find your name", text: $text)
                .textInputAutocapitalization(.words)
                .autocorrectionDisabled()
                .font(.cavnar(.body))
                .foregroundStyle(Color.cavnarInk)
            if !text.isEmpty {
                Button { text = "" } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear search")
            }
        }
        .padding(.leading, 14)
        .frame(minHeight: 48)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    /// Names containing every word typed, in any order, ignoring case and
    /// accents ("lop mar" finds "María López").
    static func filter<T>(_ items: [T], by query: String, name: KeyPath<T, String>) -> [T] {
        let words = query.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !words.isEmpty else { return items }
        return items.filter { item in
            let haystack = item[keyPath: name]
            return words.allSatisfy {
                haystack.range(of: $0, options: [.caseInsensitive, .diacriticInsensitive]) != nil
            }
        }
    }
}
