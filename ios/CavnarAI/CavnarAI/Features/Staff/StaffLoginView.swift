import SwiftUI

/// Restaurant → name → PIN. Three taps and four digits, because this runs at
/// the start of a shift with a queue forming, not at a desk.
struct StaffLoginView: View {
    @Environment(StaffSessionStore.self) private var staff

    @State private var portalCode: String = ""
    @State private var restaurantName: String = ""
    @State private var roster: [StaffRosterEntry] = []
    @State private var selected: StaffRosterEntry?
    @State private var pin: String = ""
    @State private var error: String?
    @State private var loading = false

    /// Whether this device already knows its restaurant. Remembered after the
    /// first sign-in so the code is one-time setup, not something retyped
    /// every shift.
    private var needsCode: Bool { roster.isEmpty && restaurantName.isEmpty }

    var body: some View {
        ZStack {
            Color.cavnarPaper.ignoresSafeArea()
            VStack(spacing: 0) {
                if needsCode {
                    codeEntry
                } else if let selected {
                    pinEntry(for: selected)
                } else {
                    rosterPicker
                }
            }
            .padding(.horizontal, 22)
        }
        .task {
            if let saved = staff.portalToken, !saved.isEmpty {
                portalCode = saved
                await loadRoster()
            }
        }
    }

    // MARK: - Steps

    private var codeEntry: some View {
        VStack(alignment: .leading, spacing: 14) {
            Spacer()
            Text("STAFF SIGN IN")
                .font(.cavnarBody(11, weight: 700))
                .kerning(1.6)
                .foregroundStyle(Color.cavnarEmber2)
            Text("Enter your restaurant's staff code")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)
            Text("Your manager has it. You only need to do this once on this device.")
                .font(.cavnarBody(14))
                .foregroundStyle(Color.cavnarInk3)

            TextField("Staff code", text: $portalCode)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .font(.cavnarBody(16))
                .padding(14)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
                .foregroundStyle(Color.cavnarInk)

            if let error {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
            }

            Button {
                Haptic.light()
                Task { await loadRoster() }
            } label: {
                Text(loading ? "Checking…" : "Continue")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(portalCode.trimmingCharacters(in: .whitespaces).isEmpty || loading)
            Spacer()
        }
    }

    private var rosterPicker: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text(restaurantName.uppercased())
                .font(.cavnarBody(11, weight: 700))
                .kerning(1.6)
                .foregroundStyle(Color.cavnarEmber2)
            Text("Who's starting a shift?")
                .font(.cavnarHeadline(25))
                .foregroundStyle(Color.cavnarInk)

            if roster.isEmpty {
                Text("No staff PINs have been set up here yet. A manager can add them from the dashboard.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
            }

            ScrollView {
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    ForEach(roster) { person in
                        Button {
                            Haptic.light()
                            selected = person
                            pin = ""
                            error = nil
                        } label: {
                            Text(person.name)
                                .font(.cavnarBody(15.5, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding(.vertical, 16)
                                .padding(.horizontal, 14)
                                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
                        }
                    }
                }
                .padding(.top, 4)
            }

            Button("Use a different code") {
                roster = []
                restaurantName = ""
                staff.portalToken = nil
            }
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk3)
            .padding(.bottom, 12)
        }
        .padding(.top, 30)
    }

    private func pinEntry(for person: StaffRosterEntry) -> some View {
        VStack(spacing: 18) {
            HStack(spacing: 10) {
                Button {
                    selected = nil
                    pin = ""
                    error = nil
                } label: {
                    Image(systemName: "chevron.left").foregroundStyle(Color.cavnarInk3)
                }
                Text(person.name)
                    .font(.cavnarHeadline(20))
                    .foregroundStyle(Color.cavnarInk)
                Spacer()
            }
            .padding(.top, 30)

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
                    if pin.count == 4 { Task { await submit(person) } }
                },
                onDelete: { if !pin.isEmpty { pin.removeLast() } },
                onClear: { pin = ""; error = nil }
            )
            Spacer()
        }
    }

    // MARK: - Actions

    private func loadRoster() async {
        let code = portalCode.trimmingCharacters(in: .whitespaces)
        guard !code.isEmpty else { return }
        loading = true
        error = nil
        defer { loading = false }
        do {
            let resp = try await staff.roster(portal: code)
            guard resp.ok else {
                error = resp.error ?? "That code isn't valid."
                return
            }
            restaurantName = resp.restaurant ?? ""
            roster = resp.roster ?? []
            staff.portalToken = code
        } catch {
            self.error = "Could not reach the server."
        }
    }

    private func submit(_ person: StaffRosterEntry) async {
        loading = true
        defer { loading = false }
        let ok = await staff.signIn(portal: portalCode, membershipID: person.membershipID, pin: pin)
        if ok {
            Haptic.success()
        } else {
            Haptic.error()
            pin = ""
            error = staff.lastError
        }
    }
}

/// A numeric pad rather than the system keyboard: a phone on a pass gets used
/// with one thumb and often a glove, and the keyboard's number row is the
/// wrong target size for that.
private struct StaffPinPad: View {
    let onDigit: (String) -> Void
    let onDelete: () -> Void
    let onClear: () -> Void

    private let rows = [["1", "2", "3"], ["4", "5", "6"], ["7", "8", "9"]]

    var body: some View {
        VStack(spacing: 11) {
            ForEach(rows, id: \.self) { row in
                HStack(spacing: 11) {
                    ForEach(row, id: \.self) { digit in
                        key(digit) { onDigit(digit) }
                    }
                }
            }
            HStack(spacing: 11) {
                key("Clear", small: true, action: onClear)
                key("0") { onDigit("0") }
                key("⌫", small: true, action: onDelete)
            }
        }
        .frame(maxWidth: 320)
    }

    private func key(_ label: String, small: Bool = false, action: @escaping () -> Void) -> some View {
        Button {
            Haptic.light()
            action()
        } label: {
            Text(label)
                .font(small ? .cavnarBody(14) : .cavnarNumber(24, weight: 600))
                .foregroundStyle(small ? Color.cavnarInk3 : Color.cavnarInk)
                .frame(maxWidth: .infinity)
                .frame(height: 58)
                .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 14))
        }
    }
}
