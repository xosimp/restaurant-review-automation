import CoreGraphics
import Foundation

// MARK: - Models (GET /staff/api/tasks — task_sheets.staff_view)
//
// Codable, not only Decodable: the staff app keeps the last answer on the
// phone (StaffReadCache, "as of 4:05pm") and re-encodes the sheets as they
// stand after a tick, so a walk-in with no signal still shows them.

struct StaffSheetLine: Codable, Hashable, Identifiable {
    let lineID: Int
    let label: String
    let section: String?
    let dueAt: String?
    let proof: String?
    let proofLabel: String?
    let critical: Bool?
    var done: Bool
    var overdue: Bool?
    var completedBy: String?
    var completedAt: String?
    var late: Bool?
    var flagged: Bool?
    var proofValue: String?
    var photo: String?
    /// The reading's allowed range (task_sheets._snapshot_lines). Used only
    /// to tell someone a critical reading is out of range while the phone is
    /// offline; the server's own check is the record.
    var minValue: Double? = nil
    var maxValue: Double? = nil

    var id: Int { lineID }

    enum CodingKeys: String, CodingKey {
        case label, section, proof, critical, done, overdue, late, flagged, photo
        case lineID = "line_id"
        case dueAt = "due_at"
        case proofLabel = "proof_label"
        case completedBy = "completed_by"
        case completedAt = "completed_at"
        case proofValue = "proof_value"
        case minValue = "min_value"
        case maxValue = "max_value"
    }

    /// "none", "number", "note" or "photo".
    var proofKind: String { proof ?? "none" }
}

extension StaffSheetLine {
    /// Tolerant where an older snapshot may differ: `critical` stored as 0/1,
    /// a range as a string. Everything else as the server sends it.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        lineID = try c.decode(Int.self, forKey: .lineID)
        label = try c.decode(String.self, forKey: .label)
        section = try c.decodeIfPresent(String.self, forKey: .section)
        dueAt = try c.decodeIfPresent(String.self, forKey: .dueAt)
        proof = try c.decodeIfPresent(String.self, forKey: .proof)
        proofLabel = try c.decodeIfPresent(String.self, forKey: .proofLabel)
        critical = Self.flexibleBool(c, .critical)
        done = Self.flexibleBool(c, .done) ?? false
        overdue = Self.flexibleBool(c, .overdue)
        completedBy = try c.decodeIfPresent(String.self, forKey: .completedBy)
        completedAt = try c.decodeIfPresent(String.self, forKey: .completedAt)
        late = Self.flexibleBool(c, .late)
        flagged = Self.flexibleBool(c, .flagged)
        if let s = try? c.decodeIfPresent(String.self, forKey: .proofValue) {
            proofValue = s
        } else if let d = try? c.decodeIfPresent(Double.self, forKey: .proofValue) {
            proofValue = String(format: "%g", d)
        } else {
            proofValue = nil
        }
        photo = try c.decodeIfPresent(String.self, forKey: .photo)
        minValue = Self.flexibleDouble(c, .minValue)
        maxValue = Self.flexibleDouble(c, .maxValue)
    }

    private static func flexibleBool(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> Bool? {
        if let b = try? c.decodeIfPresent(Bool.self, forKey: key) { return b }
        if let i = try? c.decodeIfPresent(Int.self, forKey: key) { return i != 0 }
        return nil
    }

    private static func flexibleDouble(_ c: KeyedDecodingContainer<CodingKeys>, _ key: CodingKeys) -> Double? {
        if let d = try? c.decodeIfPresent(Double.self, forKey: key) { return d }
        if let s = try? c.decodeIfPresent(String.self, forKey: key) { return Double(s) }
        return nil
    }
}

struct StaffSheet: Codable, Hashable, Identifiable {
    let id: Int
    let title: String
    let shiftKind: String
    let shiftStart: String?
    let shiftEnd: String?
    let assignees: [String]
    let unassigned: Bool
    var status: String
    var done: Int
    let total: Int
    var overdue: Int?
    var lines: [StaffSheetLine]
    /// The business date the sheet was issued for (ISO).
    var taskDate: String? = nil

    enum CodingKeys: String, CodingKey {
        case id, title, assignees, unassigned, status, done, total, overdue, lines
        case shiftKind = "shift_kind"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case taskDate = "task_date"
    }
}

struct StaffSignoff: Codable, Hashable {
    let shiftKind: String
    let signedBy: String?
    var note: String? = nil

    enum CodingKeys: String, CodingKey {
        case shiftKind = "shift_kind"
        case signedBy = "signed_by"
        case note
    }
}

/// Last night's closing sign-off note, for today's opening manager
/// (task_sheets.last_night_note, COM-16). Only ever sent to a manager with an
/// opening sheet.
struct StaffLastNightNote: Codable, Hashable {
    let note: String
    let signedBy: String?
    let signedAt: String?
    let shiftKind: String?
    let taskDate: String?
    let dateLabel: String?

    enum CodingKeys: String, CodingKey {
        case note
        case signedBy = "signed_by"
        case signedAt = "signed_at"
        case shiftKind = "shift_kind"
        case taskDate = "task_date"
        case dateLabel = "date_label"
    }
}

/// A critical reading out of its range (task_sheets._flag_critical):
/// "Tell your manager now". `managerAlerted` is false when no manager could
/// be routed or texted — the person is then told to say it in person.
struct StaffTaskAlert: Codable, Hashable {
    let critical: Bool?
    let title: String
    let message: String?
    let managerAlerted: Bool?

    enum CodingKeys: String, CodingKey {
        case critical, title, message
        case managerAlerted = "manager_alerted"
    }
}

/// The answer to /tasks/complete, /tasks/photo and /tasks/signoff. A tick
/// answers with the sheet as it now stands (PERF-08): the app replaces it in
/// place by id and does not read /tasks again. `sheet` is nil only when the
/// server saved the tick but could not build the view — then it re-reads.
struct StaffTickResponse: Decodable {
    let ok: Bool
    let error: String?
    let late: Bool?
    let flagged: Bool?
    var alert: StaffTaskAlert? = nil
    var sheet: StaffSheet? = nil
    var taskDate: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, late, flagged, alert, sheet
        case taskDate = "task_date"
    }
}

/// The whole /tasks answer as this screen reads it, with the fields the
/// shared StaffTasksResponse doesn't carry (`last_night_note`, `task_date`).
/// The legacy flat `tasks` list is never asked for (X-Staff-Tasks-Version: 2).
struct StaffTasksPayload: Codable, Equatable {
    var ok: Bool
    var role: String?
    var taskDate: String?
    var sheets: [StaffSheet]
    var manager: Bool
    var floor: [StaffSheet]
    var canSignOff: [String]
    var signoffs: [StaffSignoff]
    var lastNightNote: StaffLastNightNote?

    enum CodingKeys: String, CodingKey {
        case ok, role, sheets, manager, floor, signoffs
        case taskDate = "task_date"
        case canSignOff = "can_sign_off"
        case lastNightNote = "last_night_note"
    }

    init(ok: Bool = true, role: String? = nil, taskDate: String? = nil, sheets: [StaffSheet] = [],
         manager: Bool = false, floor: [StaffSheet] = [], canSignOff: [String] = [],
         signoffs: [StaffSignoff] = [], lastNightNote: StaffLastNightNote? = nil) {
        self.ok = ok
        self.role = role
        self.taskDate = taskDate
        self.sheets = sheets
        self.manager = manager
        self.floor = floor
        self.canSignOff = canSignOff
        self.signoffs = signoffs
        self.lastNightNote = lastNightNote
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decodeIfPresent(Bool.self, forKey: .ok)) ?? true
        role = try? c.decodeIfPresent(String.self, forKey: .role)
        taskDate = try? c.decodeIfPresent(String.self, forKey: .taskDate)
        sheets = try c.decodeIfPresent([StaffSheet].self, forKey: .sheets) ?? []
        manager = (try? c.decodeIfPresent(Bool.self, forKey: .manager)) ?? false
        floor = try c.decodeIfPresent([StaffSheet].self, forKey: .floor) ?? []
        canSignOff = (try? c.decodeIfPresent([String].self, forKey: .canSignOff)) ?? []
        signoffs = (try? c.decodeIfPresent([StaffSignoff].self, forKey: .signoffs)) ?? []
        lastNightNote = try? c.decodeIfPresent(StaffLastNightNote.self, forKey: .lastNightNote)
    }

    /// The portal's own read (StaffTasksResponse), as a first paint before
    /// this screen's read lands.
    init(seed r: StaffTasksResponse) {
        self.init(ok: r.ok, role: r.role, sheets: r.sheets ?? [], manager: r.manager ?? false,
                  floor: r.floor ?? [], canSignOff: r.canSignOff ?? [], signoffs: r.signoffs ?? [])
    }
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

    /// "4:05pm" today, "9/30/26 4:05pm" another day — for "as of …".
    static func asOf(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.dateFormat = "h:mma"
        let time = f.string(from: date).lowercased().replacingOccurrences(of: ":00", with: "")
        if calendar.isDate(date, inSameDayAs: now) { return time }
        f.dateFormat = "M/d/yy"
        return f.string(from: date) + " " + time
    }
}

// MARK: - Pure sheet logic (unit-tested)

/// A line's state the app is showing before the server has said so: a tick
/// in flight, or one parked offline (StaffOfflineQueue).
struct StaffLineOverlay: Equatable {
    enum State: Equatable { case sending, queued }
    let done: Bool
    let value: String?
    let state: State
}

enum StaffSheetMerge {
    /// "<assignment>-<line>" — the one key a line's busy state, note, overlay
    /// and queued tick share.
    static func key(_ sheet: Int, _ line: Int) -> String { "\(sheet)-\(line)" }

    /// The sheets with `updated` put in place of the sheet with its id. A
    /// sheet not in the list leaves it unchanged (the caller re-reads).
    static func replacing(_ updated: StaffSheet, in sheets: [StaffSheet]) -> (sheets: [StaffSheet], found: Bool) {
        guard let i = sheets.firstIndex(where: { $0.id == updated.id }) else { return (sheets, false) }
        var out = sheets
        out[i] = updated
        return (out, true)
    }

    /// The payload with the tick's answer merged in by id — the person's own
    /// sheets, else the floor. Nil when the sheet is in neither (re-read).
    static func merging(_ updated: StaffSheet, into payload: StaffTasksPayload) -> StaffTasksPayload? {
        var p = payload
        let mine = replacing(updated, in: p.sheets)
        if mine.found { p.sheets = mine.sheets; return p }
        let floor = replacing(updated, in: p.floor)
        if floor.found { p.floor = floor.sheets; return p }
        return nil
    }

    /// The sheets as the person should see them now: each overlay sets its
    /// line done or not (with the reading or note), and the sheet's count
    /// follows. The server's answer replaces all of it when it lands.
    static func applying(_ overlays: [String: StaffLineOverlay], to sheets: [StaffSheet]) -> [StaffSheet] {
        guard !overlays.isEmpty else { return sheets }
        return sheets.map { sheet in
            var s = sheet
            var changed = false
            for (i, line) in s.lines.enumerated() {
                guard let o = overlays[key(s.id, line.lineID)] else { continue }
                var l = line
                l.done = o.done
                if o.done {
                    l.overdue = false
                    if let v = o.value, !v.isEmpty { l.proofValue = v }
                } else {
                    l.completedBy = nil
                    l.completedAt = nil
                    l.proofValue = nil
                    l.photo = nil
                    l.late = false
                    l.flagged = false
                }
                s.lines[i] = l
                changed = true
            }
            if changed {
                s.done = s.lines.filter(\.done).count
                s.overdue = s.lines.filter { $0.overdue == true }.count
            }
            return s
        }
    }

    /// The reading as the server will store it, or nil when it isn't a
    /// number (the server refuses those with its own sentence).
    static func reading(_ text: String) -> Double? {
        Double(text.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: "."))
    }

    /// True when a reading falls outside the line's range — the same test
    /// as task_sheets.complete_line (inclusive bounds). False with no range.
    static func outOfRange(_ value: String?, line: StaffSheetLine) -> Bool {
        guard let value, let n = reading(value) else { return false }
        if let lo = line.minValue, n < lo { return true }
        if let hi = line.maxValue, n > hi { return true }
        return false
    }

    /// "33–41", "at least 33", "at most 41" — task_sheets._range_label.
    static func rangeLabel(_ line: StaffSheetLine) -> String? {
        func g(_ d: Double) -> String { String(format: "%g", d) }
        switch (line.minValue, line.maxValue) {
        case let (lo?, hi?): return "\(g(lo))–\(g(hi))"
        case let (lo?, nil): return "at least \(g(lo))"
        case let (nil, hi?): return "at most \(g(hi))"
        default: return nil
        }
    }
}

/// "Message your manager" under a critical reading (employee audit I4 → I3):
/// the thread opens about the sheet's day, with the line named and a draft
/// that says what was read and the range it should be in.
struct StaffLineMessage: Identifiable, Equatable {
    let context: StaffMessageContext
    let draft: String
    var id: String { context.label }

    static func about(sheet: StaffSheet, line: StaffSheetLine, reading: String?) -> StaffLineMessage {
        let label = "\(sheet.title) \u{00B7} \(line.label)"
        var draft = "\(line.label) on the \(sheet.title) sheet"
        if let r = reading?.trimmingCharacters(in: .whitespaces), !r.isEmpty {
            draft += " read \(r)"
        }
        if let range = StaffSheetMerge.rangeLabel(line) {
            draft += " \u{2014} it should be \(range)."
        } else {
            draft += " is out of range."
        }
        return StaffLineMessage(context: StaffMessageContext(shiftDate: sheet.taskDate, label: label),
                                draft: draft + " ")
    }
}

/// On-device sizing for a proof photo. The server refuses an image over
/// 3.5 MB and a body over 5 MB, and re-encodes to 1280 px anyway; a 12–48 MP
/// original at JPEG 0.8 was often 3–5 MB (PERF-06).
enum StaffPhotoPrep {
    static let maxEdge: CGFloat = 1600
    static let quality: CGFloat = 0.7
    /// Under the server's 3.5 MB per image, with room to spare.
    static let maxBytes = 3_000_000

    /// The size to draw at: the long edge at most `maxEdge`, aspect kept,
    /// never upscaled, whole pixels.
    static func targetSize(for size: CGSize, maxEdge: CGFloat = maxEdge) -> CGSize {
        guard size.width > 0, size.height > 0 else { return .zero }
        let long = max(size.width, size.height)
        guard long > maxEdge else { return CGSize(width: size.width.rounded(), height: size.height.rounded()) }
        let scale = maxEdge / long
        return CGSize(width: max(1, (size.width * scale).rounded()), height: max(1, (size.height * scale).rounded()))
    }
}
