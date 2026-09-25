import SwiftUI

// The employee's side of time off and shift changes, on the phone
// (Friction audit #49, U3-21). The web portal has had both forms; staff on
// the app were sent to the web to ask for the things their manager then
// answers in the app. Same /staff/api routes as the web portal, one for
// one — two clients on one set of routes can't drift apart.

struct StaffTimeOff: Decodable, Identifiable, Hashable {
    let id: Int
    let startDate: String
    let endDate: String
    let reason: String?
    let status: String
    let decisionNote: String?

    enum CodingKeys: String, CodingKey {
        case id, reason, status
        case startDate = "start_date"
        case endDate = "end_date"
        case decisionNote = "decision_note"
    }

    var statusLabel: String {
        switch status {
        case "approved": return "Approved"
        case "denied": return "Not approved"
        case "withdrawn": return "Withdrawn"
        default: return "Waiting for an answer"
        }
    }
}

struct StaffTimeOffResponse: Decodable {
    let ok: Bool
    let requests: [StaffTimeOff]?
}

struct StaffShiftChange: Decodable, Identifiable, Hashable {
    let id: Int
    let employeeName: String?
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let reason: String?
    let status: String
    let kind: String?
    let targetName: String?
    let targetDate: String?
    let targetStart: String?
    let replacementName: String?

    enum CodingKeys: String, CodingKey {
        case id, date, role, reason, status, kind
        case employeeName = "employee_name"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case targetName = "target_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
        case replacementName = "replacement_name"
    }

    var isSwap: Bool { (kind ?? "drop") == "swap" }

    /// "9/26/26 4:00pm" — M/D/YY, then the start.
    var whenLabel: String {
        [CavnarDate.mdy(date), shiftStart ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
    }

    /// The web portal's wording for each state (staff_portal.html).
    var statusLabel: String {
        switch status {
        case "pending": return isSwap && targetName != nil ? "Waiting on your manager and \(targetName!)" : "Waiting for an answer"
        case "open": return "Approved — open for anyone to pick up"
        case "covered": return isSwap ? "Swapped" : (replacementName.map { "Covered by \($0)" } ?? "Covered")
        case "denied": return "Not approved"
        case "withdrawn": return "Withdrawn"
        default: return status.capitalized
        }
    }
}

struct StaffShiftChangesResponse: Decodable {
    let ok: Bool
    let requests: [StaffShiftChange]?
    let open: [StaffShiftChange]?
    let asks: [StaffShiftChange]?
}

struct StaffColleague: Decodable, Hashable, Identifiable {
    struct Shift: Decodable, Hashable, Identifiable {
        let date: String
        let day: String?
        let role: String?
        let shiftStart: String
        let shiftEnd: String?
        var id: String { date + "|" + shiftStart }
        var label: String {
            [day ?? "", CavnarDate.mdy(date), shiftStart + (shiftEnd.map { "–\($0)" } ?? "")]
                .filter { !$0.isEmpty }.joined(separator: " ")
        }
        enum CodingKeys: String, CodingKey {
            case date, day, role
            case shiftStart = "shift_start"
            case shiftEnd = "shift_end"
        }
    }
    let name: String
    let shifts: [Shift]
    var id: String { name }
}

struct StaffColleaguesResponse: Decodable {
    let ok: Bool
    let colleagues: [StaffColleague]?
}

/// Bodies — the exact keys the web portal posts.
struct StaffTimeOffBody: Encodable, Equatable {
    let startDate: String
    let endDate: String
    let reason: String
    enum CodingKeys: String, CodingKey {
        case reason
        case startDate = "start_date"
        case endDate = "end_date"
    }
}

struct StaffShiftChangeBody: Encodable, Equatable {
    var kind: String = "drop"
    let date: String
    let shiftStart: String
    var targetName: String? = nil
    var targetDate: String? = nil
    var targetStart: String? = nil
    let reason: String
    enum CodingKeys: String, CodingKey {
        case kind, date, reason
        case shiftStart = "shift_start"
        case targetName = "target_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
    }
}

enum StaffISODay {
    /// yyyy-MM-dd on the phone's own calendar day, POSIX.
    static func string(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}

// MARK: - The Requests tab

struct StaffRequestsView: View {
    @Environment(StaffSessionStore.self) private var staff
    @State private var timeOff: [StaffTimeOff]?
    @State private var changes: StaffShiftChangesResponse?
    @State private var showingTimeOff = false
    @State private var busy: Set<String> = []
    @State private var message: String?
    /// The open shift "Pick this up" is asking about — claiming it puts
    /// the shift on this person's schedule, so it is confirmed first (F3-18).
    @State private var claiming: StaffShiftChange?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            label("TIME OFF")
            Button {
                Haptic.light()
                showingTimeOff = true
            } label: {
                Text("Ask for time off").frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            if let timeOff {
                if timeOff.isEmpty {
                    card("No time-off requests yet. Your manager's next schedule keeps an approved range off.")
                }
                ForEach(timeOff) { req in timeOffRow(req) }
            } else {
                CavnarSkeletonLines(widths: [1.0, 0.7])
            }

            if let asks = changes?.asks, !asks.isEmpty {
                label("SWAPS ASKED OF YOU")
                ForEach(asks) { ask in askRow(ask) }
            }

            label("YOUR SHIFT CHANGES")
            if let mine = changes?.requests {
                if mine.isEmpty {
                    card("Can't make a shift? Tap it on your Schedule to hand it back or swap it.")
                }
                ForEach(mine) { req in changeRow(req) }
            } else {
                CavnarSkeletonLines(widths: [1.0, 0.6])
            }

            if let open = changes?.open, !open.isEmpty {
                label("OPEN SHIFTS")
                ForEach(open) { shift in openRow(shift) }
            }

            if let message {
                Text(message)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await reload() }
        .sheet(isPresented: $showingTimeOff, onDismiss: { Task { await reload() } }) {
            StaffTimeOffSheet()
        }
        .confirmationDialog(claiming.map { "Pick up \($0.whenLabel)?" } ?? "",
                            isPresented: Binding(get: { claiming != nil }, set: { if !$0 { claiming = nil } }),
                            titleVisibility: .visible, presenting: claiming) { shift in
            Button("Pick it up") {
                Task { await post("open\(shift.id)", "/staff/api/open-shifts/\(shift.id)/claim") }
            }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("It goes on your schedule.")
        }
    }

    func reload() async {
        let t: StaffTimeOffResponse? = try? await staff.authed("/staff/api/time-off")
        let c: StaffShiftChangesResponse? = try? await staff.authed("/staff/api/shift-requests")
        timeOff = t?.requests ?? []
        changes = c ?? StaffShiftChangesResponse(ok: false, requests: [], open: [], asks: [])
    }

    private func post(_ key: String, _ path: String, body: (any Encodable)? = nil) async {
        busy.insert(key)
        defer { busy.remove(key) }
        message = nil
        do {
            let r: StaffOKResponse = try await staff.authed(path, method: .post, body: body ?? [String: String]())
            // The success haptic only for a success (F3-18): it played over
            // "That didn't go through" too.
            if r.ok {
                Haptic.success()
            } else {
                message = r.error ?? "That didn't go through."
            }
        } catch let error as APIClient.APIError {
            message = error.message
        } catch {
            message = "That didn't go through — check your connection."
        }
        await reload()
    }

    private func timeOffRow(_ req: StaffTimeOff) -> some View {
        row {
            HomeMixedText.make(CavnarDate.mdyRange(req.startDate, req.endDate), size: 15, weight: 700, color: .cavnarInk)
            Text(req.statusLabel)
                .font(.cavnarBody(13, weight: 600))
                .foregroundStyle(req.status == "approved" ? Color.cavnarGreen
                                 : (req.status == "denied" ? Color.cavnarRed : Color.cavnarInk3))
            if let note = req.decisionNote, !note.isEmpty {
                Text(note).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk2)
            }
            if req.status == "pending" {
                textButton("Withdraw", busy: busy.contains("to\(req.id)")) {
                    await post("to\(req.id)", "/staff/api/time-off/\(req.id)/withdraw")
                }
            }
        }
    }

    private func changeRow(_ req: StaffShiftChange) -> some View {
        row {
            HStack(spacing: 6) {
                HomeMixedText.make(req.whenLabel, size: 15, weight: 700, color: .cavnarInk)
                Text(req.isSwap ? "SWAP" : "DROP")
                    .font(.cavnarBody(9.5, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
            }
            if req.isSwap, let who = req.targetName {
                HomeMixedText.make("With \(who) for \([req.targetDate.map(CavnarDate.mdy), req.targetStart].compactMap { $0 }.joined(separator: " "))",
                                   size: 13.5, color: .cavnarInk2)
            }
            Text(req.statusLabel).font(.cavnarBody(13, weight: 600)).foregroundStyle(Color.cavnarInk3)
            if req.status == "pending" {
                textButton("Withdraw", busy: busy.contains("sr\(req.id)")) {
                    await post("sr\(req.id)", "/staff/api/shift-requests/\(req.id)/withdraw")
                }
            }
        }
    }

    private func askRow(_ ask: StaffShiftChange) -> some View {
        row {
            HomeMixedText.make("\(ask.employeeName ?? "A colleague") asked to swap: their \(ask.whenLabel) for your "
                               + [ask.targetDate.map(CavnarDate.mdy), ask.targetStart].compactMap { $0 }.joined(separator: " "),
                               size: 14.5, weight: 600, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Button {
                    Haptic.light()
                    Task { await post("ask\(ask.id)", "/staff/api/shift-requests/\(ask.id)/respond", body: ["accept": false]) }
                } label: { Text("Decline").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle())
                Button {
                    Haptic.light()
                    Task { await post("ask\(ask.id)", "/staff/api/shift-requests/\(ask.id)/respond", body: ["accept": true]) }
                } label: { Text("Accept").frame(maxWidth: .infinity) }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .disabled(busy.contains("ask\(ask.id)"))
        }
    }

    private func openRow(_ shift: StaffShiftChange) -> some View {
        row {
            HomeMixedText.make(shift.whenLabel + (shift.role.map { " · \($0)" } ?? ""), size: 15, weight: 700,
                               color: .cavnarInk)
            textButton("Pick this up", busy: busy.contains("open\(shift.id)")) {
                claiming = shift
            }
        }
    }

    // MARK: pieces

    private func label(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(11, weight: 700))
            .kerning(1.3)
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.top, 6)
    }

    private func card(_ text: String) -> some View {
        Text(text)
            .font(.cavnarBody(14))
            .foregroundStyle(Color.cavnarInk3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
    }

    private func row<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) { content() }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(13)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
    }

    private func textButton(_ title: String, busy: Bool, action: @escaping () async -> Void) -> some View {
        Button {
            Haptic.light()
            Task { await action() }
        } label: {
            Group {
                if busy { CavnarShimmerText(text: "…") } else { Text(title) }
            }
            .font(.cavnarBody(14, weight: 700))
            .foregroundStyle(Color.cavnarEmber2)
            .frame(minHeight: 44, alignment: .leading)
        }
        .buttonStyle(.plain)
        .disabled(busy)
    }
}

// MARK: - Ask for time off

struct StaffTimeOffSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var start = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var end = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("Your manager sees this in Cavnar and answers it. An approved range stays off the next schedule.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    DatePicker("First day off", selection: $start, in: Date()..., displayedComponents: .date)
                        .font(.cavnarBody(15))
                        .tint(Color.cavnarEmber)
                    DatePicker("Last day off", selection: $end, in: start..., displayedComponents: .date)
                        .font(.cavnarBody(15))
                        .tint(Color.cavnarEmber)
                    TextField("Why (optional)", text: $reason)
                        .cavnarTextFieldStyle()
                    Button {
                        Haptic.light()
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { CavnarShimmerText(text: "Sending…", color: .white) } else { Text("Ask for these days") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending))
                    .disabled(sending)
                    if let error {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Time off")
        }
        .onChange(of: start) { _, new in if end < new { end = new } }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let body = StaffTimeOffBody(startDate: StaffISODay.string(start), endDate: StaffISODay.string(end),
                                    reason: reason.trimmingCharacters(in: .whitespacesAndNewlines))
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/time-off", method: .post, body: body)
            if r.ok {
                Haptic.success()
                dismiss()
            } else {
                error = r.error ?? "That didn't go through."
            }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That didn't go through — check your connection."
        }
    }
}

// MARK: - Hand back or swap a shift

/// Opened from a shift on the Schedule tab: "Can't work this" (a drop) or
/// "Swap with a colleague" (their shift for yours — it needs your manager's
/// yes and theirs).
struct StaffShiftChangeSheet: View {
    enum Mode: Hashable { case drop, swap }

    let day: StaffWeekDay
    let mode: Mode
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var colleagues: [StaffColleague]?
    @State private var colleague = ""
    @State private var theirShiftId = ""
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?

    private var shiftStart: String { day.shift?.shiftStart ?? "" }
    private var chosen: StaffColleague? { colleagues?.first { $0.name == colleague } }
    private var theirShift: StaffColleague.Shift? { chosen?.shifts.first { $0.id == theirShiftId } }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    HomeMixedText.make("\(day.weekday) \(CavnarDate.mdy(day.date)) · \(day.shift?.timeRange ?? "")",
                                       size: 16, weight: 700, color: .cavnarInk)
                    if mode == .swap { swapPickers }
                    TextField(mode == .swap ? "Why (optional)" : "Why can't you work it? (optional)", text: $reason)
                        .cavnarTextFieldStyle()
                    Button {
                        Haptic.light()
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { CavnarShimmerText(text: "Sending…", color: .white) }
                            else { Text(mode == .swap ? "Ask for the swap" : "Ask to hand it back") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending || (mode == .swap && theirShift == nil)))
                    .disabled(sending || (mode == .swap && theirShift == nil))
                    Text(mode == .swap
                         ? "Both shifts move once your manager approves and your colleague says yes."
                         : "Your manager decides. If they approve, anyone on the team can pick it up.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if let error {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome(mode == .swap ? "Swap a shift" : "Can't work this")
        }
        .task {
            guard mode == .swap else { return }
            let r: StaffColleaguesResponse? = try? await staff.authed("/staff/api/colleagues")
            colleagues = r?.colleagues ?? []
        }
    }

    @ViewBuilder
    private var swapPickers: some View {
        if let colleagues {
            if colleagues.isEmpty {
                Text("Nobody else is on the published week yet.")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            } else {
                Picker("Swap with", selection: $colleague) {
                    Text("Choose a colleague").tag("")
                    ForEach(colleagues) { c in Text(c.name).tag(c.name) }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber)
                .onChange(of: colleague) { _, _ in theirShiftId = chosen?.shifts.first?.id ?? "" }
                if let chosen {
                    Picker("For their shift", selection: $theirShiftId) {
                        ForEach(chosen.shifts) { s in Text(s.label).tag(s.id) }
                    }
                    .pickerStyle(.menu)
                    .tint(Color.cavnarEmber)
                }
            }
        } else {
            CavnarSkeletonLines(widths: [0.8, 0.6])
        }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        var body = StaffShiftChangeBody(date: day.date, shiftStart: shiftStart, reason: why)
        if mode == .swap {
            guard let theirShift else { return }
            body.kind = "swap"
            body.targetName = colleague
            body.targetDate = theirShift.date
            body.targetStart = theirShift.shiftStart
        }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/shift-requests", method: .post, body: body)
            if r.ok {
                Haptic.success()
                dismiss()
            } else {
                error = r.error ?? "That didn't go through."
            }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That didn't go through — check your connection."
        }
    }
}
