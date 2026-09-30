import PhotosUI
import SwiftUI
import UIKit

// MARK: - Models (GET /staff/api/tasks — task_sheets.staff_view)

struct StaffSheetLine: Decodable, Hashable, Identifiable {
    let lineID: Int
    let label: String
    let section: String?
    let dueAt: String?
    let proof: String?
    let proofLabel: String?
    let critical: Bool?
    let done: Bool
    let overdue: Bool?
    let completedBy: String?
    let completedAt: String?
    let late: Bool?
    let flagged: Bool?
    let proofValue: String?
    let photo: String?

    var id: Int { lineID }

    enum CodingKeys: String, CodingKey {
        case label, section, proof, critical, done, overdue, late, flagged, photo
        case lineID = "line_id"
        case dueAt = "due_at"
        case proofLabel = "proof_label"
        case completedBy = "completed_by"
        case completedAt = "completed_at"
        case proofValue = "proof_value"
    }
}

struct StaffSheet: Decodable, Hashable, Identifiable {
    let id: Int
    let title: String
    let shiftKind: String
    let shiftStart: String?
    let shiftEnd: String?
    let assignees: [String]
    let unassigned: Bool
    let status: String
    let done: Int
    let total: Int
    let overdue: Int?
    let lines: [StaffSheetLine]

    enum CodingKeys: String, CodingKey {
        case id, title, assignees, unassigned, status, done, total, overdue, lines
        case shiftKind = "shift_kind"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
    }
}

struct StaffSignoff: Decodable, Hashable {
    let shiftKind: String
    let signedBy: String?

    enum CodingKeys: String, CodingKey {
        case shiftKind = "shift_kind"
        case signedBy = "signed_by"
    }
}

struct StaffTickResponse: Decodable {
    let ok: Bool
    let error: String?
    let late: Bool?
    let flagged: Bool?
}

enum StaffSheetFormat {
    /// "10:30am" from the server's local wall clock ("2026-10-05T10:30:00").
    static func clock(_ iso: String?) -> String {
        guard let iso, iso.count >= 16,
              let h = Int(iso.dropFirst(11).prefix(2)) else { return "" }
        let m = String(iso.dropFirst(14).prefix(2))
        let hour = h % 12 == 0 ? 12 : h % 12
        return "\(hour)\(m == "00" ? "" : ":" + m)\(h >= 12 ? "pm" : "am")"
    }

    static func kind(_ k: String) -> String {
        ["opening": "Opening", "closing": "Closing", "mid": "Mid", "any": "All day"][k] ?? k
    }
}

// MARK: - Your sheets today

/// The staff app's Tasks tab (task_sheets.py): each sheet the published
/// schedule puts this person on, in order, with due times; a line that
/// needs a reading, a note or a photo asks for it before it ticks. A manager
/// also sees the floor and signs a shift off.
struct StaffTaskSheetsSection: View {
    @Environment(StaffSessionStore.self) private var staff
    let response: StaffTasksResponse
    let reload: () async -> Void

    @State private var values: [String: String] = [:]
    @State private var busy: Set<String> = []
    @State private var message: String?
    @State private var messageIsBad = false
    @State private var photoItem: PhotosPickerItem?
    @State private var photoTarget: (sheet: Int, line: Int)?
    @State private var signingOff: String?
    @State private var signoffNote = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let message {
                Text(message)
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(messageIsBad ? Color.cavnarRed : Color.cavnarInk2)
            }
            let sheets = response.sheets ?? []
            if sheets.isEmpty {
                Text("No task sheet is yours today. Your sheets show here on the days the schedule puts you on a shift that has one.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(16)
                    .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 12))
            }
            ForEach(sheets) { sheet in sheetCard(sheet) }
            if response.manager == true { floor }
        }
        .onChange(of: photoItem) { _, item in
            guard let item, let target = photoTarget else { return }
            Task { await upload(item, sheet: target.sheet, line: target.line) }
        }
        .alert("Sign off the \(StaffSheetFormat.kind(signingOff ?? "").lowercased()) shift?",
               isPresented: Binding(get: { signingOff != nil }, set: { if !$0 { signingOff = nil } })) {
            TextField("Anything to note (optional)", text: $signoffNote)
            Button("Sign off") { Task { await signOff() } }
            Button("Cancel", role: .cancel) { signingOff = nil }
        } message: {
            Text("It records every sheet on the shift as it stands now.")
        }
    }

    // MARK: one sheet

    private func sheetCard(_ s: StaffSheet) -> some View {
        let open = s.status == "open"
        return VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(s.title).font(.cavnarBody(16.5, weight: 700)).foregroundStyle(Color.cavnarInk)
                    Text(meta(s)).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                }
                Spacer(minLength: 8)
                Text("\(s.done)/\(s.total)").font(.cavnarNumber(16, weight: 700)).foregroundStyle(Color.cavnarInk2)
            }
            ProgressView(value: Double(s.done), total: Double(max(s.total, 1)))
                .tint(s.overdue ?? 0 > 0 ? Color.cavnarRed : Color.cavnarGreen)
                .padding(.vertical, 10)
            if !open {
                Text("This sheet closed at the end of the shift.")
                    .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3).padding(.bottom, 6)
            }
            ForEach(Array(s.lines.enumerated()), id: \.element.id) { i, line in
                if let sec = line.section, !sec.isEmpty, i == 0 || s.lines[i - 1].section != sec {
                    Text(sec.uppercased())
                        .font(.cavnarBody(11, weight: 700)).kerning(1.2)
                        .foregroundStyle(Color.cavnarInk3)
                        .padding(.top, 10).padding(.bottom, 2)
                }
                lineRow(s, line, open: open)
            }
        }
        .padding(14)
        .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 14))
    }

    private func meta(_ s: StaffSheet) -> String {
        var bits: [String] = []
        if s.shiftStart != nil {
            bits.append("\(StaffSheetFormat.clock(s.shiftStart))–\(StaffSheetFormat.clock(s.shiftEnd))")
        }
        if s.unassigned { bits.append("no schedule published — yours if you're on it") }
        if s.assignees.count > 1 { bits.append("with " + s.assignees.joined(separator: ", ")) }
        return bits.joined(separator: " · ")
    }

    @ViewBuilder
    private func lineRow(_ s: StaffSheet, _ l: StaffSheetLine, open: Bool) -> some View {
        let proof = l.proof ?? "none"
        let key = "\(s.id)-\(l.lineID)"
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .top, spacing: 12) {
                if open && (proof == "none" || l.done) {
                    Button {
                        Haptic.light()
                        Task { await tick(s.id, l.lineID, done: !l.done) }
                    } label: {
                        Image(systemName: l.done ? "checkmark.circle.fill" : "circle")
                            .font(.system(size: 24))
                            .foregroundStyle(l.done ? Color.cavnarGreen : (l.overdue == true ? Color.cavnarRed : Color.cavnarInk3))
                    }
                    .disabled(busy.contains(key))
                    .accessibilityLabel(l.done ? "Mark not done: \(l.label)" : "Mark done: \(l.label)")
                } else {
                    Image(systemName: l.done ? "checkmark.circle.fill" : "circle")
                        .font(.system(size: 20))
                        .foregroundStyle(l.done ? Color.cavnarGreen : (l.overdue == true ? Color.cavnarRed : Color.cavnarInk3))
                        .accessibilityHidden(true)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(l.label)
                        .font(.cavnarBody(15.5))
                        .foregroundStyle(l.done ? Color.cavnarInk3 : Color.cavnarInk)
                    tags(l)
                    if let sub = subline(l) {
                        Text(sub).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                Spacer(minLength: 0)
            }
            if open && !l.done && (proof == "number" || proof == "note") {
                HStack(spacing: 8) {
                    TextField(l.proofLabel ?? (proof == "number" ? "Reading" : "Your note"),
                              text: Binding(get: { values[key] ?? "" }, set: { values[key] = $0 }))
                        .keyboardType(proof == "number" ? .decimalPad : .default)
                        .font(.cavnarBody(15))
                        .padding(10)
                        .background(Color.cavnarPaper, in: RoundedRectangle(cornerRadius: 9))
                    Button("Save") {
                        Task { await tick(s.id, l.lineID, done: true, value: values[key]) }
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle())
                    .disabled(busy.contains(key) || (values[key] ?? "").trimmingCharacters(in: .whitespaces).isEmpty)
                }
                .padding(.leading, 36)
            }
            if open && !l.done && proof == "photo" {
                PhotosPicker(selection: $photoItem, matching: .images) {
                    Text(busy.contains(key) ? "Uploading…" : "Add photo")
                        .font(.cavnarBody(14, weight: 600))
                }
                .simultaneousGesture(TapGesture().onEnded { photoTarget = (s.id, l.lineID) })
                .buttonStyle(CavnarPrimaryButtonStyle())
                .disabled(busy.contains(key))
                .padding(.leading, 36)
            }
        }
        .padding(.vertical, 10)
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
    }

    @ViewBuilder
    private func tags(_ l: StaffSheetLine) -> some View {
        let items: [(String, Color)] = [
            l.critical == true ? ("CRITICAL", Color.cavnarEmber2) : nil,
            l.overdue == true ? ("OVERDUE", Color.cavnarRed) : nil,
            l.late == true ? ("LATE", Color.cavnarAmber) : nil,
            l.flagged == true ? ("OUT OF RANGE", Color.cavnarRed) : nil,
        ].compactMap { $0 }
        if !items.isEmpty {
            HStack(spacing: 6) {
                ForEach(items, id: \.0) { t in
                    Text(t.0)
                        .font(.cavnarBody(10.5, weight: 700)).kerning(0.8)
                        .foregroundStyle(t.1)
                        .padding(.horizontal, 7).padding(.vertical, 2)
                        .overlay(Capsule().stroke(t.1, lineWidth: 1))
                }
            }
        }
    }

    private func subline(_ l: StaffSheetLine) -> String? {
        if l.done {
            var s = "\(l.completedBy ?? "") · \(StaffSheetFormat.clock(l.completedAt))"
            if let v = l.proofValue { s += " · \(l.proofLabel.map { $0 + ": " } ?? "")\(v)" }
            if l.photo != nil { s += " · photo added" }
            return s
        }
        if let due = l.dueAt { return "Due \(StaffSheetFormat.clock(due))" }
        return nil
    }

    // MARK: the floor (managers)

    @ViewBuilder
    private var floor: some View {
        Text("THE FLOOR · TODAY")
            .font(.cavnarBody(11, weight: 700)).kerning(1.3)
            .foregroundStyle(Color.cavnarEmber2)
            .padding(.top, 10)
        let others = response.floor ?? []
        if others.isEmpty {
            Text("No other sheets went out today.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
        }
        ForEach(others) { s in
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(s.title).font(.cavnarBody(15, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Text(s.unassigned ? "Unassigned" : s.assignees.joined(separator: ", "))
                        .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                }
                Spacer()
                if let o = s.overdue, o > 0 {
                    Text("\(o) overdue").font(.cavnarBody(12, weight: 700)).foregroundStyle(Color.cavnarRed)
                }
                Text("\(s.done)/\(s.total)").font(.cavnarNumber(15, weight: 600)).foregroundStyle(Color.cavnarInk2)
            }
            .padding(13)
            .background(Color.cavnarPaper2, in: RoundedRectangle(cornerRadius: 10))
        }
        let signed = Dictionary((response.signoffs ?? []).map { ($0.shiftKind, $0) }, uniquingKeysWith: { a, _ in a })
        ForEach(response.canSignOff ?? [], id: \.self) { kind in
            if let s = signed[kind] {
                Text("\(StaffSheetFormat.kind(kind)) signed off by \(s.signedBy ?? "a manager").")
                    .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
            } else {
                Button("Sign off the \(StaffSheetFormat.kind(kind).lowercased()) shift") {
                    signoffNote = ""
                    signingOff = kind
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
            }
        }
    }

    // MARK: writes

    private func say(_ text: String?, bad: Bool = false) {
        message = text
        messageIsBad = bad
    }

    private func tick(_ sheet: Int, _ line: Int, done: Bool, value: String? = nil) async {
        struct Body: Encodable {
            let assignment_id: Int
            let line_id: Int
            let done: Bool
            let value: String?
        }
        let key = "\(sheet)-\(line)"
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            let r: StaffTickResponse = try await staff.authed("/staff/api/tasks/complete", method: .post,
                                                              body: Body(assignment_id: sheet, line_id: line, done: done, value: value))
            if !r.ok { say(r.error ?? "That didn't save — try again.", bad: true); return }
            if r.flagged == true { say("Saved — that reading is outside its limits, so your manager will see it flagged.", bad: true) }
            else if r.late == true { say("Saved, after its due time.") }
            else { say(nil) }
            values[key] = nil
            await reload()
        } catch {
            say((error as? APIClient.APIError)?.message ?? "That didn't save — check your connection.", bad: true)
        }
    }

    private func upload(_ item: PhotosPickerItem, sheet: Int, line: Int) async {
        struct Body: Encodable {
            let assignment_id: Int
            let line_id: Int
            let image_b64: String
            let mime: String
        }
        let key = "\(sheet)-\(line)"
        busy.insert(key)
        defer { busy.remove(key); photoItem = nil; photoTarget = nil }
        guard let data = try? await item.loadTransferable(type: Data.self),
              let image = UIImage(data: data),
              let jpeg = image.jpegData(compressionQuality: 0.8) else {
            say("That photo couldn't be read — try another.", bad: true)
            return
        }
        do {
            let r: StaffTickResponse = try await staff.authed("/staff/api/tasks/photo", method: .post,
                                                              body: Body(assignment_id: sheet, line_id: line,
                                                                         image_b64: jpeg.base64EncodedString(), mime: "image/jpeg"))
            if !r.ok { say(r.error ?? "That photo didn't upload.", bad: true); return }
            say("Photo saved.")
            await reload()
        } catch {
            say((error as? APIClient.APIError)?.message ?? "That photo didn't upload — check your connection.", bad: true)
        }
    }

    private func signOff() async {
        struct Body: Encodable {
            let shift_kind: String
            let note: String
        }
        guard let kind = signingOff else { return }
        signingOff = nil
        do {
            let r: StaffTickResponse = try await staff.authed("/staff/api/tasks/signoff", method: .post,
                                                              body: Body(shift_kind: kind, note: signoffNote))
            if !r.ok { say(r.error ?? "That didn't sign off.", bad: true); return }
            say("Signed off.")
            await reload()
        } catch {
            say((error as? APIClient.APIError)?.message ?? "That didn't sign off — check your connection.", bad: true)
        }
    }
}
