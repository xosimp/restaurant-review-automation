import SwiftUI

// MARK: - Payloads (/mobile/api/task-sheets*)

struct TSLine: Codable, Hashable, Identifiable {
    let id: Int
    var label: String
    var section: String?
    var dueOffsetMin: Int?
    var proof: String
    var proofLabel: String?
    var minValue: Double?
    var maxValue: Double?
    var critical: Bool

    enum CodingKeys: String, CodingKey {
        case id, label, section, proof, critical
        case dueOffsetMin = "due_offset_min"
        case proofLabel = "proof_label"
        case minValue = "min_value"
        case maxValue = "max_value"
    }
}

struct TSSheet: Decodable, Hashable, Identifiable {
    let id: Int
    let jobCode: String
    let shiftKind: String
    let shiftLabel: String
    let daysOfWeek: [Int]
    let daysLabel: String
    let requiresSignoff: Bool
    let carriedOver: Bool?
    let lines: [TSLine]

    enum CodingKeys: String, CodingKey {
        case id, lines
        case jobCode = "job_code"
        case shiftKind = "shift_kind"
        case shiftLabel = "shift_label"
        case daysOfWeek = "days_of_week"
        case daysLabel = "days_label"
        case requiresSignoff = "requires_signoff"
        case carriedOver = "carried_over"
    }
}

struct TSKind: Decodable, Hashable { let key: String; let label: String }

struct TSSheetsResponse: Decodable {
    let ok: Bool
    let sheets: [TSSheet]?
    let jobCodes: [String]?
    let shiftKinds: [TSKind]?
    let canEdit: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, sheets, error
        case jobCodes = "job_codes"
        case shiftKinds = "shift_kinds"
        case canEdit = "can_edit"
    }
}

struct TSSheetResponse: Decodable {
    let ok: Bool
    let sheet: TSSheet?
    let error: String?
    let similar: [TSSimilar]?
}

struct TSSimilar: Decodable, Hashable { let id: Int; let label: String }

struct TSDraftLine: Codable, Hashable {
    let label: String
    let section: String?
    let proof: String?
    let proofLabel: String?
    let critical: Bool?

    enum CodingKeys: String, CodingKey {
        case label, section, proof, critical
        case proofLabel = "proof_label"
    }
}

struct TSDraftResponse: Decodable { let ok: Bool; let lines: [TSDraftLine]?; let error: String? }

struct TSDaySummary: Decodable, Hashable {
    let sheets: Int, lines: Int, done: Int, complete: Int, overdue: Int
    let criticalOpen: Int, late: Int, flagged: Int, unassigned: Int

    enum CodingKeys: String, CodingKey {
        case sheets, lines, done, complete, overdue, late, flagged, unassigned
        case criticalOpen = "critical_open"
    }
}

struct TSDayResponse: Decodable {
    let ok: Bool
    let date: String?
    let dateLabel: String?
    let isToday: Bool?
    let sheets: [StaffSheet]?
    let summary: TSDaySummary?
    let schedulePublished: Bool?
    let signoffs: [StaffSignoff]?
    let signoffNeeded: [String]?
    let hasSheets: Bool?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, date, sheets, summary, signoffs, error
        case dateLabel = "date_label"
        case isToday = "is_today"
        case schedulePublished = "schedule_published"
        case signoffNeeded = "signoff_needed"
        case hasSheets = "has_sheets"
    }
}

struct TSRate: Decodable, Hashable, Identifiable {
    let name: String
    let jobCode: String?
    let sheets: Int
    let lines: Int
    let done: Int
    let criticalMissed: Int
    let enough: Bool
    let completionPct: Int?
    let onTimePct: Int?
    var id: String { name }

    enum CodingKeys: String, CodingKey {
        case name, sheets, lines, done, enough
        case jobCode = "job_code"
        case criticalMissed = "critical_missed"
        case completionPct = "completion_pct"
        case onTimePct = "on_time_pct"
    }
}

struct TSReportResponse: Decodable {
    let ok: Bool
    let windowLabel: String?
    let sheets: Int?
    let minSheets: Int?
    let byPerson: [TSRate]?
    let byJobCode: [TSRate]?
    let managers: [TSRate]?
    let signoffsMissed: Int?
    let signoffsExpected: Int?
    let error: String?

    enum CodingKeys: String, CodingKey {
        case ok, sheets, managers, error
        case windowLabel = "window_label"
        case minSheets = "min_sheets"
        case byPerson = "by_person"
        case byJobCode = "by_job_code"
        case signoffsMissed = "signoffs_missed"
        case signoffsExpected = "signoffs_expected"
    }
}

// MARK: - The screen

/// Task sheets (task_sheets.py) for the owner: the day — every sheet, every
/// line, who ticked it and when, read-only (owners do not tick) — the
/// consistency report with the managers side by side, and the editor.
struct TaskSheetsScreen: View {
    @Environment(\.dismiss) private var dismiss
    enum View3: String, CaseIterable { case day = "The day", report = "Consistency", edit = "Edit sheets" }
    @State private var view: View3 = .day

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                CavnarSegmentedControl(selection: $view, options: View3.allCases) { $0.rawValue }
                .padding(.horizontal, 20)
                .padding(.vertical, 10)
                switch view {
                case .day: TSDayView()
                case .report: TSReportView()
                case .edit: TSEditorList()
                }
            }
            .background(Color.cavnarPaper.ignoresSafeArea())
            .navigationTitle("Task sheets")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) { Button("Done") { dismiss() } }
            }
        }
    }
}

// MARK: The day

private struct TSDayView: View {
    @State private var day: String?
    @State private var data: TSDayResponse?
    @State private var failed: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                if let d = data {
                    HStack(spacing: 12) {
                        Button { shift(-1) } label: { Image(systemName: "chevron.left") }
                        Text((d.isToday == true ? "Today · " : "") + (d.dateLabel ?? ""))
                            .font(.cavnarBody(16.5, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Button { shift(1) } label: { Image(systemName: "chevron.right") }
                            .disabled(d.isToday == true)
                        Spacer()
                    }
                    .tint(Color.cavnarEmber2)
                    content(d)
                } else if let failed {
                    Text(failed).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                } else {
                    CavnarSkeletonBar(height: 3).frame(width: 180)
                }
            }
            .padding(20)
        }
        .task(id: day) { await load() }
        .cavnarEmberRefreshable { await load() }
    }

    @ViewBuilder
    private func content(_ d: TSDayResponse) -> some View {
        if d.hasSheets != true {
            note("No task sheets yet. Write one for each job code and shift under Edit sheets — each person sees theirs when they sign in to the staff app.")
        } else if (d.sheets ?? []).isEmpty {
            note("No sheets went out this day — nobody on the published schedule worked a job code that has a sheet.")
        } else if let s = d.summary {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                tile("SHEETS FINISHED", "\(s.complete)", "of \(s.sheets)", s.complete == s.sheets ? .cavnarGreen : .cavnarInk)
                tile("LINES DONE", "\(s.done)", "of \(s.lines)", .cavnarInk)
                if d.isToday == true {
                    tile("OVERDUE NOW", "\(s.overdue)", s.criticalOpen > 0 ? "\(s.criticalOpen) critical open" : "", s.overdue > 0 ? .cavnarRed : .cavnarGreen)
                }
                tile("LATE TICKS", "\(s.late)", "after the due time", s.late > 0 ? .cavnarAmber : .cavnarInk)
                tile("OUT OF RANGE", "\(s.flagged)", "a reading outside its limits", s.flagged > 0 ? .cavnarRed : .cavnarInk)
            }
            if d.schedulePublished == false {
                note("No schedule was published for this day, so every sheet went out unassigned — anyone on that job code could tick it, and nobody owned it.", warn: true)
            }
            let signed = Set((d.signoffs ?? []).map(\.shiftKind))
            ForEach(d.signoffs ?? [], id: \.shiftKind) { so in
                note("\(StaffSheetFormat.kind(so.shiftKind)) signed off by \(so.signedBy ?? "a manager").")
            }
            ForEach((d.signoffNeeded ?? []).filter { !signed.contains($0) }, id: \.self) { k in
                note("The \(StaffSheetFormat.kind(k).lowercased()) shift isn't signed off yet.", warn: true)
            }
            ForEach(d.sheets ?? []) { sheet in TSReadOnlySheet(sheet: sheet) }
        }
    }

    private func tile(_ k: String, _ v: String, _ sub: String, _ tone: Color) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(k).font(.cavnarBody(11, weight: 700)).kerning(1.1).foregroundStyle(Color.cavnarInk3)
            Text(v).font(.cavnarNumber(26, weight: 600)).foregroundStyle(tone)
            if !sub.isEmpty { Text(sub).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
    }

    private func note(_ text: String, warn: Bool = false) -> some View {
        Text(text)
            .font(.cavnarBody(14.5))
            .foregroundStyle(Color.cavnarInk2)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(13)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(warn ? Color.cavnarAmber : Color.clear, lineWidth: 1))
    }

    private func shift(_ days: Int) {
        guard let cur = data?.date ?? day else { return }
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        guard let d = f.date(from: cur), let next = Calendar(identifier: .gregorian).date(byAdding: .day, value: days, to: d) else { return }
        day = f.string(from: next)
    }

    private func load() async {
        do {
            data = try await APIClient.shared.send("/mobile/api/task-sheets/day", query: day.map { ["date": $0] } ?? [:])
            failed = data?.ok == false ? (data?.error ?? "Task sheets couldn't be read.") : nil
        } catch {
            failed = (error as? APIClient.APIError)?.message ?? "Task sheets couldn't be read just now."
        }
    }
}

/// One sheet for the owner: its lines, who ticked each and when — no tick.
private struct TSReadOnlySheet: View {
    let sheet: StaffSheet

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                Text(sheet.title).font(.cavnarBody(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                Spacer()
                Text("\(sheet.done)/\(sheet.total)").font(.cavnarNumber(15, weight: 600)).foregroundStyle(Color.cavnarInk2)
            }
            Text(meta).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3).padding(.top, 3)
            // The web's .ts-bar: ember, red when missed, partial or overdue
            // — never a system ProgressView.
            TSProgressBar(done: sheet.done, total: sheet.total, bad: bad)
                .padding(.vertical, 10)
            ForEach(sheet.lines) { l in
                HStack(alignment: .top, spacing: 10) {
                    Image(systemName: l.done ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(l.done ? Color.cavnarGreen : (l.overdue == true ? Color.cavnarRed : Color.cavnarInk3))
                    VStack(alignment: .leading, spacing: 3) {
                        Text(l.label).font(.cavnarBody(14.5)).foregroundStyle(l.done ? Color.cavnarInk2 : Color.cavnarInk)
                        let tags = [l.critical == true ? "Critical" : nil, l.overdue == true ? "Overdue" : nil,
                                    l.late == true ? "Late" : nil, l.flagged == true ? "Out of range" : nil].compactMap { $0 }
                        if !tags.isEmpty {
                            Text(tags.joined(separator: " · ").uppercased())
                                .font(.cavnarBody(10.5, weight: 700)).kerning(0.8)
                                .foregroundStyle(l.overdue == true || l.flagged == true ? Color.cavnarRed : Color.cavnarEmber2)
                        }
                        if l.done {
                            Text("\(l.completedBy ?? "Someone") · \(StaffSheetFormat.clock(l.completedAt))"
                                 + (l.proofValue.map { " · \(l.proofLabel.map { $0 + ": " } ?? "")\($0)" } ?? ""))
                                .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                            // The proof photo itself, tap for full screen
                            // (iOS parity #50) — it read " · photo".
                            if let token = l.photo, !token.isEmpty {
                                TSProofThumb(token: token, caption: l.label)
                            }
                        } else if let due = l.dueAt {
                            Text("Due \(StaffSheetFormat.clock(due))").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(.vertical, 8)
                .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
            }
        }
        .padding(14)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 14))
    }

    private var bad: Bool { sheet.status == "missed" || sheet.status == "partial" || (sheet.overdue ?? 0) > 0 }

    private var meta: String {
        var bits: [String] = []
        if sheet.shiftStart != nil { bits.append("\(StaffSheetFormat.clock(sheet.shiftStart))–\(StaffSheetFormat.clock(sheet.shiftEnd))") }
        bits.append(sheet.unassigned ? "Unassigned — no schedule published" : (sheet.assignees.isEmpty ? "No one scheduled" : sheet.assignees.joined(separator: ", ")))
        bits.append(sheet.status == "done" ? "Finished" : (sheet.status == "open" ? "In progress" : "Left unfinished"))
        return bits.joined(separator: " · ")
    }
}

// MARK: The consistency report

private struct TSReportView: View {
    @State private var days = 14
    @State private var data: TSReportResponse?
    @State private var failed: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Picker("Window", selection: $days) {
                    ForEach([7, 14, 28, 90], id: \.self) { Text("\($0) days").tag($0) }
                }
                .pickerStyle(.segmented)
                if let d = data {
                    Text("\(d.windowLabel ?? "") · sheets whose shift has closed")
                        .font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                    if (d.sheets ?? 0) == 0 {
                        Text("Nothing to compare yet. The report reads each sheet once its shift has closed.")
                            .font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                    } else {
                        if let m = d.managers, !m.isEmpty {
                            label("YOUR MANAGERS, SIDE BY SIDE")
                            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                                ForEach(m) { rate($0, min: d.minSheets ?? 3) }
                            }
                        }
                        if let e = d.signoffsExpected, e > 0 {
                            Text("\(d.signoffsMissed ?? 0) of \(e) shifts that need a sign-off went unsigned.")
                                .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk2)
                        }
                        label("BY PERSON")
                        ForEach(d.byPerson ?? []) { row($0, min: d.minSheets ?? 3) }
                        label("BY JOB CODE")
                        ForEach(d.byJobCode ?? []) { row($0, min: d.minSheets ?? 3) }
                        Text("Rates show once a person or job code has \(d.minSheets ?? 3) closed sheets; below that, the counts.")
                            .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    }
                } else if let failed {
                    Text(failed).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                } else {
                    CavnarSkeletonBar(height: 3).frame(width: 180)
                }
            }
            .padding(20)
        }
        .task(id: days) { await load() }
    }

    private func label(_ t: String) -> some View {
        Text(t).font(.cavnarBody(11, weight: 700)).kerning(1.3).foregroundStyle(Color.cavnarEmber2).padding(.top, 8)
    }

    private func rate(_ r: TSRate, min: Int) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(r.name).font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarInk3)
            Text(r.enough ? (r.completionPct.map { "\($0)%" } ?? "—") : "\(r.sheets) sheets")
                .font(.cavnarNumber(24, weight: 600))
                .foregroundStyle(!r.enough ? Color.cavnarInk : ((r.completionPct ?? 0) >= 90 ? Color.cavnarGreen : ((r.completionPct ?? 0) < 70 ? Color.cavnarRed : Color.cavnarAmber)))
            Text(r.enough ? "\(r.onTimePct.map { "\($0)% on time" } ?? "no timed lines") · \(r.criticalMissed) critical missed"
                          : "rates after \(min) sheets · \(r.criticalMissed) critical missed")
                .font(.cavnarBody(12)).foregroundStyle(Color.cavnarInk3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(13)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
    }

    private func row(_ r: TSRate, min: Int) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(r.name).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                Text("\(r.sheets) sheets · \(r.done)/\(r.lines) lines · \(r.criticalMissed) critical missed")
                    .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(r.enough ? (r.completionPct.map { "\($0)%" } ?? "—") : "—").font(.cavnarNumber(17, weight: 600)).foregroundStyle(Color.cavnarInk)
                Text(r.enough ? (r.onTimePct.map { "\($0)% on time" } ?? "") : "after \(min) sheets")
                    .font(.cavnarBody(11.5)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(12)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
    }

    private func load() async {
        do {
            data = try await APIClient.shared.send("/mobile/api/task-sheets/report", query: ["days": String(days)])
            failed = data?.ok == false ? (data?.error ?? "The report couldn't be read.") : nil
        } catch {
            failed = (error as? APIClient.APIError)?.message ?? "The report couldn't be read just now."
        }
    }
}

// MARK: The editor

private struct TSEditorList: View {
    @State private var data: TSSheetsResponse?
    @State private var failed: String?
    @State private var creating = false

    var body: some View {
        List {
            if let d = data {
                if d.canEdit == true {
                    Button { creating = true } label: { Label("New sheet", systemImage: "plus") }
                        .tint(Color.cavnarEmber)
                } else {
                    Text("Only a login that manages the team edits sheets.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                }
                let groups = Dictionary(grouping: d.sheets ?? [], by: \.jobCode)
                ForEach(groups.keys.sorted { $0.lowercased() < $1.lowercased() }, id: \.self) { code in
                    Section(code) {
                        ForEach(groups[code] ?? []) { s in
                            NavigationLink {
                                TSSheetEditor(sheetID: s.id, canEdit: d.canEdit == true, kinds: d.shiftKinds ?? [],
                                              codes: d.jobCodes ?? [], onChange: { await load() })
                            } label: {
                                HStack {
                                    Text(s.shiftLabel + (s.daysOfWeek.isEmpty ? "" : " · " + s.daysLabel)).font(.cavnarBody(15))
                                    Spacer()
                                    Text("\(s.lines.count) lines").font(.cavnarNumber(13)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                        }
                    }
                }
            } else if let failed {
                Text(failed).foregroundStyle(Color.cavnarRed)
            } else {
                CavnarSkeletonBar(height: 3).frame(width: 180)
            }
        }
        .scrollContentBackground(.hidden)
        .task { await load() }
        .cavnarEmberRefreshable { await load() }
        .sheet(isPresented: $creating) {
            TSNewSheet(kinds: data?.shiftKinds ?? [], codes: data?.jobCodes ?? []) { await load() }
        }
    }

    private func load() async {
        do {
            data = try await APIClient.shared.send("/mobile/api/task-sheets")
            failed = data?.ok == false ? (data?.error ?? "Sheets couldn't be read.") : nil
        } catch {
            failed = (error as? APIClient.APIError)?.message ?? "Sheets couldn't be read just now."
        }
    }
}

private struct TSNewSheet: View {
    @Environment(\.dismiss) private var dismiss
    let kinds: [TSKind]
    let codes: [String]
    let onDone: () async -> Void
    @State private var jobCode = ""
    @State private var kind = "opening"
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Job code, as your schedule spells it", text: $jobCode)
                    if !codes.isEmpty {
                        Menu("Pick from your schedule") {
                            ForEach(codes, id: \.self) { c in Button(c) { jobCode = c } }
                        }
                    }
                    Picker("Shift", selection: $kind) {
                        ForEach(kinds, id: \.key) { Text($0.label).tag($0.key) }
                    }
                } footer: {
                    Text("Opening goes to whoever the published schedule has starting first on that job code; closing to whoever finishes last; all day to everyone on it.")
                }
                if let error { Text(error).foregroundStyle(Color.cavnarRed) }
            }
            .navigationTitle("New sheet")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create") { Task { await create() } }.disabled(saving || jobCode.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
        }
    }

    private func create() async {
        struct Body: Encodable { let job_code: String; let shift_kind: String }
        saving = true
        defer { saving = false }
        do {
            let r: TSSheetResponse = try await APIClient.shared.send("/mobile/api/task-sheets", method: .post,
                                                                     body: Body(job_code: jobCode, shift_kind: kind))
            if r.ok { await onDone(); dismiss() } else { error = r.error }
        } catch {
            self.error = (error as? APIClient.APIError)?.message ?? "That sheet wasn't created."
        }
    }
}

private struct TSSheetEditor: View {
    let sheetID: Int
    let canEdit: Bool
    let kinds: [TSKind]
    let codes: [String]
    let onChange: () async -> Void

    @State private var sheet: TSSheet?
    @State private var editing: TSLine?
    @State private var adding = false
    @State private var note: String?
    @State private var drafts: [TSDraftLine]?
    @State private var drafting = false
    @State private var kind = "any"
    @State private var days: Set<Int> = []
    @State private var signoff = false

    var body: some View {
        List {
            if let s = sheet {
                if s.carriedOver == true && s.shiftKind == "any" {
                    Text("Carried over from your old checklist as an All day sheet, so it goes to everyone on \(s.jobCode). Set it to Opening or Closing and it goes to just the opener or the closer.")
                        .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk2)
                }
                if let note { Text(note).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarAmber) }
                if canEdit {
                    Section("Settings") {
                        Picker("Shift", selection: $kind) { ForEach(kinds, id: \.key) { Text($0.label).tag($0.key) } }
                        HStack(spacing: 6) {
                            ForEach(0..<7, id: \.self) { i in
                                let on = days.contains(i)
                                Text(["M", "T", "W", "T", "F", "S", "S"][i])
                                    .font(.cavnarBody(13, weight: 700))
                                    .frame(width: 32, height: 32)
                                    .background(on ? Color.cavnarEmber.opacity(0.18) : Color.cavnarPaper3, in: Circle())
                                    .foregroundStyle(on ? Color.cavnarEmber : Color.cavnarInk2)
                                    .onTapGesture { if on { days.remove(i) } else { days.insert(i) } }
                                    .accessibilityLabel(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][i])
                                    .accessibilityAddTraits(on ? .isSelected : [])
                            }
                        }
                        Text(days.isEmpty ? "Every day" : "Only the days picked").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                        Toggle("The manager on duty signs this shift off", isOn: $signoff).tint(Color.cavnarEmber)
                        Button("Save settings") { Task { await saveSettings() } }
                    }
                }
                Section("Lines, in the order the work is done") {
                    ForEach(s.lines) { l in
                        Button { if canEdit { editing = l } } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(l.label).font(.cavnarBody(15)).foregroundStyle(Color.cavnarInk)
                                Text(meta(l, kind: s.shiftKind)).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                            }
                        }
                    }
                    .onMove(perform: canEdit ? { from, to in Task { await move(from: from, to: to) } } : nil)
                    .onDelete(perform: canEdit ? { idx in Task { await remove(idx) } } : nil)
                    if canEdit {
                        Button { adding = true } label: { Label("Add a line", systemImage: "plus") }
                        Button { Task { await draft() } } label: {
                            Label(drafting ? "Drafting…" : "Draft lines with Cavnar AI", systemImage: "sparkles")
                        }
                        .disabled(drafting)
                    }
                }
                if let drafts {
                    Section("Cavnar AI's draft — add the ones you want") {
                        if drafts.isEmpty { Text("Nothing new to suggest for this sheet.").foregroundStyle(Color.cavnarInk3) }
                        ForEach(Array(drafts.enumerated()), id: \.offset) { i, d in
                            HStack {
                                Text(d.label).font(.cavnarBody(14.5))
                                Spacer()
                                Button("Add") { Task { await addDraft(i) } }
                                    .buttonStyle(CavnarChipButtonStyle(tone: .cavnarEmber))
                            }
                        }
                    }
                }
            } else {
                CavnarSkeletonBar(height: 3).frame(width: 180)
            }
        }
        .scrollContentBackground(.hidden)
        .navigationTitle(sheet.map { "\($0.jobCode) · \($0.shiftLabel)" } ?? "Sheet")
        .toolbar { if canEdit { EditButton() } }
        .task { await load() }
        .sheet(item: $editing) { l in
            TSLineForm(line: l, kind: sheet?.shiftKind ?? "any") { fields in await save(line: l.id, fields) }
        }
        .sheet(isPresented: $adding) {
            TSLineForm(line: nil, kind: sheet?.shiftKind ?? "any") { fields in await add(fields) }
        }
    }

    private func meta(_ l: TSLine, kind: String) -> String {
        var bits: [String] = []
        if l.critical { bits.append("Critical") }
        if let s = l.section, !s.isEmpty { bits.append(s) }
        if let d = l.dueOffsetMin { bits.append("due \(d) min " + (kind == "closing" ? "before the end" : "into the shift")) }
        if l.proof != "none" { bits.append(["photo": "a photo", "number": "a number", "note": "a note"][l.proof] ?? l.proof) }
        return bits.isEmpty ? "Any time in the shift · no proof" : bits.joined(separator: " · ")
    }

    private func apply(_ s: TSSheet?) {
        guard let s else { return }
        sheet = s
        kind = s.shiftKind
        days = Set(s.daysOfWeek)
        signoff = s.requiresSignoff
    }

    private func load() async {
        let r: TSSheetsResponse? = try? await APIClient.shared.send("/mobile/api/task-sheets")
        apply(r?.sheets?.first { $0.id == sheetID })
    }

    private func similarNote(_ r: TSSheetResponse) {
        if let sim = r.similar, !sim.isEmpty {
            note = "Nearly the same as " + sim.map { "“\($0.label)”" }.joined(separator: ", ") + " on this sheet — worth keeping only one."
        } else { note = nil }
    }

    private func saveSettings() async {
        struct Body: Encodable { let shift_kind: String; let days_of_week: [Int]; let requires_signoff: Bool }
        if let r: TSSheetResponse = try? await APIClient.shared.send("/mobile/api/task-sheets/\(sheetID)", method: .post,
                                                                      body: Body(shift_kind: kind, days_of_week: days.sorted(), requires_signoff: signoff)) {
            if r.ok { apply(r.sheet); note = "Saved. Today's sheets keep what they went out with; the change applies from tomorrow."; await onChange() }
            else { note = r.error }
        }
    }

    private func add(_ fields: [String: String]) async -> String? {
        do {
            let r: TSSheetResponse = try await APIClient.shared.send("/mobile/api/task-sheets/\(sheetID)/lines", method: .post, body: fields)
            if !r.ok { return r.error ?? "That line didn't save." }
            apply(r.sheet); similarNote(r); await onChange()
            return nil
        } catch { return (error as? APIClient.APIError)?.message ?? "That line didn't save." }
    }

    private func save(line: Int, _ fields: [String: String]) async -> String? {
        do {
            let r: TSSheetResponse = try await APIClient.shared.send("/mobile/api/task-sheets/lines/\(line)", method: .post, body: fields)
            if !r.ok { return r.error ?? "That line didn't save." }
            apply(r.sheet); similarNote(r); await onChange()
            return nil
        } catch { return (error as? APIClient.APIError)?.message ?? "That line didn't save." }
    }

    private func remove(_ idx: IndexSet) async {
        struct Body: Encodable { let active: Bool }
        guard let s = sheet else { return }
        for i in idx {
            let _: TSSheetResponse? = try? await APIClient.shared.send("/mobile/api/task-sheets/lines/\(s.lines[i].id)", method: .post,
                                                                        body: Body(active: false))
        }
        await load(); await onChange()
    }

    private func move(from: IndexSet, to: Int) async {
        struct Body: Encodable { let line_ids: [Int] }
        guard var lines = sheet?.lines else { return }
        lines.move(fromOffsets: from, toOffset: to)
        if let r: TSSheetResponse = try? await APIClient.shared.send("/mobile/api/task-sheets/\(sheetID)/order", method: .post,
                                                                      body: Body(line_ids: lines.map(\.id))) {
            apply(r.sheet)
        }
    }

    private func draft() async {
        struct Empty: Encodable {}
        drafting = true
        defer { drafting = false }
        let r: TSDraftResponse? = try? await APIClient.shared.send("/mobile/api/task-sheets/\(sheetID)/starter", method: .post,
                                                                    body: Empty(), timeout: 90)
        drafts = r?.lines ?? []
        if r?.ok == false { note = r?.error }
    }

    private func addDraft(_ i: Int) async {
        guard let d = drafts?[i] else { return }
        var f: [String: String] = ["label": d.label, "proof": d.proof ?? "none", "critical": d.critical == true ? "1" : "0"]
        if let s = d.section { f["section"] = s }
        if let p = d.proofLabel { f["proof_label"] = p }
        if await add(f) == nil { drafts?.remove(at: i) }
    }
}

/// One line's fields — the web's form, as a sheet.
private struct TSLineForm: View {
    @Environment(\.dismiss) private var dismiss
    let line: TSLine?
    let kind: String
    let onSave: ([String: String]) async -> String?

    @State private var label = ""
    @State private var section = ""
    @State private var due = ""
    @State private var proof = "none"
    @State private var proofLabel = ""
    @State private var minV = ""
    @State private var maxV = ""
    @State private var critical = false
    @State private var error: String?
    @State private var saving = false

    var body: some View {
        NavigationStack {
            Form {
                Section("What needs doing") {
                    TextField("Log the walk-in temperature", text: $label, axis: .vertical)
                    TextField("Section (optional) — Bar, Line 1", text: $section)
                }
                Section {
                    TextField(kind == "closing" ? "Minutes before the shift ends" : "Minutes after the shift starts", text: $due)
                        .keyboardType(.numberPad)
                } header: { Text("Due") } footer: { Text("Leave empty for any time in the shift.") }
                Section("Proof") {
                    Picker("Proof", selection: $proof) {
                        Text("No proof").tag("none"); Text("A photo").tag("photo"); Text("A number").tag("number"); Text("A note").tag("note")
                    }
                    if proof == "number" || proof == "note" {
                        TextField("What to record — Walk-in °F", text: $proofLabel)
                    }
                    if proof == "number" {
                        TextField("Lowest allowed", text: $minV).keyboardType(.decimalPad)
                        TextField("Highest allowed", text: $maxV).keyboardType(.decimalPad)
                    }
                }
                Section {
                    Toggle("Critical", isOn: $critical).tint(Color.cavnarEmber)
                } footer: { Text("A critical line not done by its due time texts the manager.") }
                if let error { Text(error).foregroundStyle(Color.cavnarRed) }
            }
            .navigationTitle(line == nil ? "Add a line" : "Edit line")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task { await save() } }.disabled(saving || label.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            }
            .onAppear {
                guard let l = line else { return }
                label = l.label; section = l.section ?? ""; due = l.dueOffsetMin.map(String.init) ?? ""
                proof = l.proof; proofLabel = l.proofLabel ?? ""; critical = l.critical
                minV = l.minValue.map { String(format: "%g", $0) } ?? ""; maxV = l.maxValue.map { String(format: "%g", $0) } ?? ""
            }
        }
    }

    private func save() async {
        saving = true
        defer { saving = false }
        let f: [String: String] = ["label": label, "section": section, "due_offset_min": due, "proof": proof,
                                   "proof_label": proofLabel, "min_value": minV, "max_value": maxV, "critical": critical ? "1" : "0"]
        if let e = await onSave(f) { error = e } else { dismiss() }
    }
}


// MARK: - Progress and proof photos (iOS parity, 10/7/26)

/// A sheet's progress: the ember capsule StaffEmberProgressBar draws, red
/// when the sheet was missed, left partial or is overdue (the web's .ts-bar).
private struct TSProgressBar: View {
    let done: Int
    let total: Int
    let bad: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var fraction: CGFloat { total > 0 ? min(1, max(0, CGFloat(done) / CGFloat(total))) : 0 }

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.cavnarPaper3.opacity(0.7))
                Capsule()
                    .fill(bad ? LinearGradient(colors: [Color.cavnarRed, Color.cavnarRed.opacity(0.8)],
                                               startPoint: .leading, endPoint: .trailing)
                              : LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2],
                                               startPoint: .leading, endPoint: .trailing))
                    .frame(width: fraction > 0 ? max(6, geo.size.width * fraction) : 0)
            }
        }
        .frame(height: 6)
        .animation(reduceMotion ? nil : .easeOut(duration: 0.35), value: fraction)
        .accessibilityLabel("\(done) of \(total) lines done")
    }
}

/// A proof photo's bytes for the owner (GET /mobile/api/task-sheets/photo/
/// <token>, this restaurant's only): the stored session's bearer, an
/// ephemeral session (nothing on disk) pinned on the production host, and a
/// named timeout. Kept in memory for the screen's life.
enum OwnerProofPhoto {
    @MainActor private static var cache: [String: UIImage] = [:]

    private static let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 15
        config.timeoutIntervalForResource = 30
        config.waitsForConnectivity = false
        let delegate = AppEnvironment.isProductionHost ? PinnedSessionDelegate() : nil
        return URLSession(configuration: config, delegate: delegate, delegateQueue: nil)
    }()

    /// Tokens are URL-safe base64; anything else is not one of ours.
    static func isToken(_ token: String) -> Bool {
        !token.isEmpty && token.count <= 200
            && token.allSatisfy { $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "-" || $0 == "_") }
    }

    @MainActor
    static func image(_ token: String) async -> UIImage? {
        if let hit = cache[token] { return hit }
        guard isToken(token), let bearer = Keychain.get(Keychain.Key.sessionToken) else { return nil }
        var request = URLRequest(url: AppEnvironment.baseURL.appendingPathComponent("/mobile/api/task-sheets/photo/" + token))
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15
        guard let (data, response) = try? await session.data(for: request),
              let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let image = UIImage(data: data) else { return nil }
        cache[token] = image
        return image
    }
}

/// The line's proof photo as a thumbnail; a tap shows it full screen.
private struct TSProofThumb: View {
    let token: String
    let caption: String
    @State private var image: UIImage?
    @State private var failed = false
    @State private var showing = false

    var body: some View {
        Button {
            guard image != nil else { return }
            Haptic.light()
            showing = true
        } label: {
            ZStack {
                RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.cavnarPaper3.opacity(0.5))
                if let image {
                    Image(uiImage: image).resizable().scaledToFill()
                } else if failed {
                    Image(systemName: "photo").font(.system(size: 14, weight: .semibold)).foregroundStyle(Color.cavnarInk3)
                } else {
                    CavnarSkeletonBar(height: 3).frame(width: 30)
                }
            }
            .frame(width: 64, height: 64)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .padding(.top, 4)
        .accessibilityLabel(failed ? "Proof photo couldn't load" : "Proof photo for \(caption) \u{2014} opens full screen")
        .task(id: token) {
            image = await OwnerProofPhoto.image(token)
            failed = image == nil
        }
        .fullScreenCover(isPresented: $showing) {
            TSPhotoViewer(image: image, caption: caption)
        }
    }
}

private struct TSPhotoViewer: View {
    let image: UIImage?
    let caption: String
    @Environment(\.dismiss) private var dismiss
    @State private var scale: CGFloat = 1

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.cavnarPaper.ignoresSafeArea()
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .scaleEffect(scale)
                    .gesture(MagnificationGesture().onChanged { scale = max(1, min(4, $0)) }
                        .onEnded { _ in if scale < 1.05 { scale = 1 } })
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .accessibilityLabel("Proof photo for \(caption)")
            }
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(Color.cavnarInk)
                    .frame(width: 44, height: 44)
                    .background(Circle().fill(Color.cavnarPaper2))
            }
            .buttonStyle(.plain)
            .padding(16)
            .accessibilityLabel("Close")
        }
        .overlay(alignment: .bottom) {
            Text(caption)
                .font(.cavnarBody(14, weight: 600))
                .foregroundStyle(Color.cavnarInk2)
                .padding(16)
        }
    }
}
