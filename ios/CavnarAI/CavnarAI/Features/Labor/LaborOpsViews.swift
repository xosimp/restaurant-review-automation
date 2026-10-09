import SwiftUI
import Observation

// Labor's day-to-day moves on the phone (iOS parity #25, #45, #69, 10/7/26):
// post an open shift or offer it to one person, add someone to the roster by
// hand, and tonight's covers at close. Every outward move (a shift posted or
// offered reaches staff) goes through a confirm.

// MARK: - Post an open shift (#25)

/// "Post a shift": the date, time wheels, the role, whose shift it was (blank
/// for an extra one) and who to offer it to (blank for the whole team) — the
/// web's form, every key POST /labor/open-shifts reads.
struct OpenShiftPostSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss
    // The restaurant's business date, not the phone's day (re-audit
    // 10/8/26 #6): a phone in another zone, or a 1am post for last night's
    // still-running shift, used to open on the wrong date.
    @State private var date = RestaurantClock.businessDate()
    @State private var start = "5:00pm"
    @State private var end = "10:00pm"
    @State private var role = ""
    @State private var whose = ""
    @State private var offerTo = ""
    @State private var note = ""
    @State private var error: String?
    @State private var posting = false
    @State private var confirming = false

    /// The earliest postable day: the business date (a shift of last
    /// night's service can still be running past midnight).
    private static var earliest: Date {
        CavnarDateChip.day(RestaurantClock.businessDate()) ?? Date()
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Somebody's shift on the published week, or an extra one. The team picks it up in the Cavnar AI app \u{2014} or only the person you offer it to.")
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    AccountSection(kicker: "The shift") {
                        AccountKVRow(label: "Date") { CavnarDateChip(iso: $date, earliest: Self.earliest, accessibilityName: "Date") }
                        AccountKVRow(label: "Starts") { CavnarTimeChip(time: $start, accessibilityName: "Starts") }
                        AccountKVRow(label: "Ends") { CavnarTimeChip(time: $end, accessibilityName: "Ends") }
                        AccountKVRow(label: "Role", showsDivider: false) {
                            Picker("Role", selection: $role) {
                                Text("Any").tag("")
                                ForEach(viewModel.rosterRoles, id: \.self) { Text($0).tag($0) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                    }
                    AccountSection(kicker: "Who") {
                        AccountKVRow(label: "Whose shift") {
                            Picker("Whose shift", selection: $whose) {
                                Text("An extra shift").tag("")
                                ForEach(viewModel.activeNames, id: \.self) { Text($0).tag($0) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                        AccountKVRow(label: "Offer to", showsDivider: false) {
                            Picker("Offer to", selection: $offerTo) {
                                Text("The whole team").tag("")
                                ForEach(viewModel.activeNames.filter { $0 != whose }, id: \.self) { Text($0).tag($0) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                    }
                    TextField("Note (e.g. Bears game, all hands)", text: $note)
                        .cavnarTextFieldStyle()
                    if let error {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        Haptic.light()
                        confirming = true
                    } label: {
                        Group {
                            if posting { CavnarShimmerText(text: "Posting\u{2026}") } else { Text(offerTo.isEmpty ? "Post it" : "Offer it to \(offerTo)") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: posting || date.isEmpty))
                    .disabled(posting || date.isEmpty)
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .accountSheetChrome("Post a shift")
        }
        .presentationDetents([.large])
        .confirmationDialog(confirmTitle, isPresented: $confirming, titleVisibility: .visible) {
            Button(offerTo.isEmpty ? "Post it to the team" : "Offer it to \(offerTo)") { Task { await post() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(offerTo.isEmpty ? "Everyone who can work it hears about it in the Cavnar AI app."
                                 : "\(offerTo) hears about it in the Cavnar AI app and can take it.")
        }
    }

    private var confirmTitle: String {
        "\(LaborWaitingOnYou.dayLabel(date)) \(start)\u{2013}\(end)" + (role.isEmpty ? "" : " \u{00B7} \(role)")
    }

    private func post() async {
        posting = true
        error = nil
        defer { posting = false }
        let body = OpenShiftPostBody(date: date, shiftStart: start, shiftEnd: end, role: role, employee: whose,
                                     offerTo: offerTo, note: note)
        if let why = await viewModel.postOpenShift(body) { error = why } else { dismiss() }
    }
}

/// Offer one open shift to a named person (the web row's "Offer it to…").
struct OpenShiftOfferSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    let shift: ShiftRequest
    @Environment(\.dismiss) private var dismiss
    @State private var pick: String?
    @State private var confirming = false
    @State private var error: String?

    private var names: [String] { viewModel.activeNames.filter { $0 != shift.employeeName } }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HomeMixedText.make(shift.whenLabel, size: 15, weight: 600, color: .cavnarInk2)
                    AccountKicker(text: "Offer it to")
                    AccountFlowLayout(spacing: 6) {
                        ForEach(names, id: \.self) { name in
                            Button {
                                Haptic.selection()
                                pick = pick == name ? nil : name
                            } label: { AccountChip(text: name, muted: pick != name) }
                            .buttonStyle(.plain)
                        }
                    }
                    if let error {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        confirming = true
                    } label: {
                        Group {
                            if viewModel.requestBusyId == shift.id { CavnarShimmerText(text: "Offering\u{2026}") }
                            else { Text(pick.map { "Offer it to \($0)" } ?? "Pick someone") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: pick == nil))
                    .disabled(pick == nil || viewModel.requestBusyId != nil)
                }
                .padding(20)
            }
            .accountSheetChrome("Offer the shift")
        }
        .presentationDetents([.medium, .large])
        .confirmationDialog(pick.map { "Offer it to \($0)?" } ?? "", isPresented: $confirming, titleVisibility: .visible) {
            Button("Offer it") {
                guard let pick else { return }
                Task {
                    if let why = await viewModel.offerOpenShift(shift.id, to: pick) { error = why } else { dismiss() }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("They hear about it in the Cavnar AI app and can take it.")
        }
    }
}

// MARK: - Add someone to the roster (#45)

struct AddTeamMemberSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var role = ""
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Someone new, before their first shift is in the POS. Cavnar AI can schedule them from now on; once they have shift history, that record takes over.")
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    TextField("Their name, as the POS will spell it", text: $name)
                        .cavnarTextFieldStyle()
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                    AccountSection(kicker: "Role") {
                        AccountKVRow(label: "Role", showsDivider: false) {
                            Picker("Role", selection: $role) {
                                Text("Pick a role").tag("")
                                ForEach(viewModel.rosterRoles, id: \.self) { Text($0).tag($0) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                    }
                    if let error {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        Task {
                            if let why = await viewModel.addTeamMember(name: name, role: role) { error = why } else { dismiss() }
                        }
                    } label: {
                        Group {
                            if viewModel.teamBusy == "add" { CavnarShimmerText(text: "Adding\u{2026}") } else { Text("Add to the roster") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: name.trimmingCharacters(in: .whitespaces).isEmpty))
                    .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || viewModel.teamBusy != nil)
                }
                .padding(20)
            }
            .accountSheetChrome("Add a person")
        }
        .presentationDetents([.medium, .large])
    }
}

// MARK: - Tonight's covers (#69)

/// The one figure that tells a lean day from a short one, read by the labor
/// analysis: the POS fills it each night where it can, a count typed here
/// always wins (GET/POST /labor/covers).
@Observable
@MainActor
final class CoversModel {
    private(set) var payload: CoversPayload?
    private(set) var saving = false
    private(set) var error: String?
    private(set) var saved: String?
    /// The night the count is for: the restaurant's business date — last
    /// night's before its day starts — never the phone's calendar day
    /// (re-audit 10/8/26 #6). The server's `business_date` replaces the
    /// local estimate until the owner picks a night themselves.
    var date = RestaurantClock.businessDate() {
        didSet { if date != oldValue && !settingDefault { datePicked = true } }
    }
    var count = ""
    private var datePicked = false
    private var settingDefault = false

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    /// The count on file for the picked night, if any.
    var onFile: CoversDay? { payload?.days.first { $0.date == date } }

    func load() async {
        if let r: CoversPayload = try? await client.send("/mobile/api/labor/covers", hapticOnError: false), r.ok {
            payload = r
            if !datePicked, let day = r.businessDate, day.count == 10 {
                settingDefault = true
                date = day
                settingDefault = false
            }
        }
    }

    func save() async {
        guard let n = Int(count.trimmingCharacters(in: .whitespaces)), n >= 0 else {
            error = "Enter a whole number of covers."
            return
        }
        saving = true
        error = nil
        saved = nil
        defer { saving = false }
        do {
            let r: CoversSaveResponse = try await client.send(
                "/mobile/api/labor/covers", method: .post,
                body: CoversSaveBody(rows: [.init(date: date, covers: n)]))
            if r.ok, (r.written ?? 0) > 0 {
                saved = "Saved \(n) covers for \(CavnarDate.mdy(date))"
                count = ""
                Haptic.success()
                await load()
            } else {
                error = r.errors?.first ?? r.error ?? "Couldn\u{2019}t save that count."
            }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t save that count."
        }
    }
}

struct CoversTile: View {
    @Bindable var model: CoversModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: "person.2.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                Text("Covers")
                    .font(.cavnarBody(15, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Spacer(minLength: 6)
                if let line = model.payload?.averageLine {
                    HomeMixedText.make(line, size: 12.5, color: .cavnarInk3)
                } else if model.payload != nil {
                    Text("no counts yet").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                }
            }
            if let day = model.onFile {
                HomeMixedText.make("\(day.covers) covers on file for \(CavnarDate.mdy(day.date))"
                                   + (day.source == "pos" ? " from your POS \u{2014} a number you enter replaces it" : ""),
                                   size: 13, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 8) {
                CavnarDateChip(iso: $model.date, accessibilityName: "Night")
                TextField("covers", text: $model.count)
                    .keyboardType(.numberPad)
                    .font(.cavnarNumber(16, weight: 700))
                    .focused($focused)
                    .cavnarTextFieldStyle()
                    .frame(maxWidth: 120)
                    .accessibilityLabel("Covers")
                Spacer(minLength: 0)
                Button {
                    focused = false
                    Task { await model.save() }
                } label: {
                    Group {
                        if model.saving { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save") }
                    }
                    .frame(minWidth: 64)
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.count.isEmpty || model.saving))
                .disabled(model.count.isEmpty || model.saving)
            }
            if let error = model.error {
                Text(error).font(.cavnarBody(13)).foregroundStyle(Color.cavnarRed)
            } else if let saved = model.saved {
                HomeMixedText.make(saved, size: 13, weight: 600, color: .cavnarGreen)
            }
            Text("The one figure that tells a lean day from a short one \u{2014} read by the labor analysis.")
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .cavnarCard()
        .toolbar {
            if focused {
                cavnarKeyboardTrailing {
                    Button("Done") { focused = false }
                        .foregroundStyle(Color.cavnarEmber2)
                }
            }
        }
        .task { if model.payload == nil { await model.load() } }
    }
}

// MARK: - Another week is being built (a 409, #15)

/// A press refused because another generation is running: that week, the
/// server's own sentence, and Watch it — the run is followed here and its
/// week lands when it is done.
struct GenerationBusyCard: View {
    let busy: GenerationBusy
    let onFollow: () -> Void
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 7) {
                Image(systemName: "hourglass")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarAmber)
                HomeMixedText.make(busy.headline, size: 14.5, weight: 700, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = busy.error, !line.isEmpty {
                HomeMixedText.make(line, size: 13, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                if busy.jobId != nil {
                    Button {
                        Haptic.light()
                        onFollow()
                    } label: { Text("Watch it").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }
                Button {
                    onDismiss()
                } label: {
                    Text("OK")
                        .font(.cavnarBody(14, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(minWidth: 44, minHeight: 44)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .fill(Color.cavnarAmber.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .strokeBorder(Color.cavnarAmber.opacity(0.3), lineWidth: 1))
    }
}
