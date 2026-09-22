import Foundation
import Observation

// MARK: - Roster

/// What the owner has said about one person that the generator obeys:
/// on the roster or not, full or part time, an hours band, whether they
/// are a minor, and which part of each day they can work.
struct RosterSettings: Codable, Equatable {
    var active: Bool?
    var employmentType: String?
    var minHours: Double?
    var maxHours: Double?
    var daypartAvailability: [String: String]?
    var isMinor: Bool?

    enum CodingKeys: String, CodingKey {
        case active
        case employmentType = "employment_type"
        case minHours = "min_hours"
        case maxHours = "max_hours"
        case daypartAvailability = "daypart_availability"
        case isMinor = "is_minor"
    }
}

/// How often somebody has not turned up, measured from the shifts they
/// were on. Nil when there is nothing to measure it from.
struct RosterReliability: Codable, Equatable {
    let noShowRate: Double?
    let shortRate: Double?
    let shifts: Int?

    enum CodingKeys: String, CodingKey {
        case shifts
        case noShowRate = "no_show_rate"
        case shortRate = "short_rate"
    }

    /// "4% no-show" — a rate is stored as a fraction (0.04) or, on some
    /// rows, as a percentage already; anything above 1 is taken as one.
    var noShowLabel: String? {
        guard let rate = noShowRate else { return nil }
        let pct = rate > 1 ? rate : rate * 100
        return "\(Int(pct.rounded()))% no-show"
    }
}

struct RosterMember: Codable, Identifiable, Equatable {
    let name: String
    let role: String?
    let shifts: Int?
    let lastWorked: String?
    let isManual: Bool?
    var active: Bool?
    var settings: RosterSettings?
    let score: Int?
    let canClose: Bool?
    let reliability: RosterReliability?

    var id: String { name }
    var isActive: Bool { active ?? settings?.active ?? true }

    enum CodingKeys: String, CodingKey {
        case name, role, shifts, active, settings, score, reliability
        case lastWorked = "last_worked"
        case isManual = "is_manual"
        case canClose = "can_close"
    }
}

/// Two people the owner wants together, or apart.
struct StaffPair: Codable, Identifiable, Equatable {
    let id: Int
    let a: String
    let b: String
    let kind: String
    let note: String?

    var isPrefer: Bool { kind == "prefer" }
}

struct RosterChoices: Codable, Equatable {
    let employmentType: [String]?
    let daypart: [String]?
    let days: [String]?

    enum CodingKeys: String, CodingKey {
        case daypart, days
        case employmentType = "employment_type"
    }
}

// MARK: - Rules

/// Per-role headcount floors: at least this many on a morning, this many
/// on a night, with optional per-day overrides.
struct DayFloor: Codable, Equatable {
    var morning: Int?
    var night: Int?
}

struct RoleFloor: Codable, Equatable {
    var morning: Int?
    var night: Int?
    var days: [String: DayFloor]?
}

// MARK: - Demand signals

/// A dated reason to expect more (or fewer) covers — a private party, a
/// block of reservations, a street closure.
struct DemandSignal: Codable, Identifiable, Equatable {
    let id: Int
    let date: String
    let kind: String
    let label: String?
    let covers: Int?
    let liftPct: Double?
    let source: String?

    enum CodingKeys: String, CodingKey {
        case id, date, kind, label, covers, source
        case liftPct = "lift_pct"
    }

    var kindLabel: String { kind == "reservations" ? "Reservations" : "Event" }
}

// MARK: - Shift requests

/// A shift somebody has asked to give up (`pending`) or that nobody has
/// picked up yet (`open`).
struct ShiftRequest: Codable, Identifiable, Equatable {
    let id: Int
    let employeeName: String?
    let date: String
    let shiftStart: String?
    let shiftEnd: String?
    let role: String?
    let reason: String?
    let status: String
    let createdAt: String?

    enum CodingKeys: String, CodingKey {
        case id, date, role, reason, status
        case employeeName = "employee_name"
        case shiftStart = "shift_start"
        case shiftEnd = "shift_end"
        case createdAt = "created_at"
    }

    /// "9/21/26 · 4:00pm–close · Server"
    var whenLabel: String {
        var parts = [CavnarDate.mdy(date)]
        if let s = shiftStart, !s.isEmpty {
            parts.append((shiftEnd ?? "").isEmpty ? s : "\(s)–\(shiftEnd ?? "")")
        }
        if let role, !role.isEmpty { parts.append(role) }
        return parts.joined(separator: " · ")
    }
}

/// Everything the generator reads that is not the shift history itself:
/// who is on the roster and how they may be used, the pairs to keep
/// together or apart, the compliance rules and role floors, dated demand
/// signals, and the shifts staff have asked to hand back.
///
/// Separate from LaborViewModel because that one is the generation loop
/// and its result; this is the set-up around it, and LaborView keeps one
/// of each outside its Overview/Analytics branch so section state
/// survives a tab switch (see LaborViewModel.scheduleResultExpanded).
@Observable
@MainActor
final class ScheduleSetupViewModel {
    private let client: APIClient

    init(client: APIClient = .shared) {
        self.client = client
    }

    private typealias OKResponse = APIClient.OKResponse

    // Expand/collapse for each section, lifted here for the same reason
    // LaborViewModel holds its own — see its comment.
    var rosterExpanded = false
    var demandExpanded = false
    var requestsExpanded = false

    // MARK: Roster

    var roster: [RosterMember] = []
    var pairs: [StaffPair] = []
    var choices: RosterChoices?
    var canEditRoster = true
    var isLoadingRoster = false
    var rosterError: String?
    // Whose settings are mid-flight, so only that person's controls dim.
    var savingFor: String?
    // A one-line failure for the sheet's toast; cleared when the next save
    // starts. Optimistic writes roll back on their own before setting it.
    var settingsToast: String?

    var activeRoster: [RosterMember] { roster.filter(\.isActive) }
    var activeNames: [String] { activeRoster.map(\.name) }
    var employmentTypes: [String] { choices?.employmentType ?? ["full", "part"] }
    var dayparts: [String] { choices?.daypart ?? ["any", "morning", "night", "off"] }
    var days: [String] { choices?.days ?? LaborDayOfWeek.allNames }

    private struct RosterResponse: Decodable {
        let ok: Bool
        let roster: [RosterMember]?
        let pairs: [StaffPair]?
        let choices: RosterChoices?
        let canEdit: Bool?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, roster, pairs, choices, error
            case canEdit = "can_edit"
        }
    }

    func loadRoster() async {
        isLoadingRoster = roster.isEmpty
        defer { isLoadingRoster = false }
        do {
            let r: RosterResponse = try await client.send("/mobile/api/labor/roster", hapticOnError: false)
            guard r.ok else { rosterError = r.error; return }
            roster = r.roster ?? []
            pairs = r.pairs ?? []
            choices = r.choices
            canEditRoster = r.canEdit ?? true
            rosterError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if roster.isEmpty { rosterError = error.message }
        } catch {
            if roster.isEmpty { rosterError = "Couldn't load the roster." }
        }
    }

    /// One field of one person's settings — only what changed goes over
    /// the wire. Encoded by hand so an unset field is absent, not null.
    struct StaffSettingsPatch: Encodable {
        let employeeName: String
        var active: Bool? = nil
        var employmentType: String? = nil
        var minHours: Double? = nil
        var maxHours: Double? = nil
        var daypartAvailability: [String: String]? = nil
        var isMinor: Bool? = nil

        enum CodingKeys: String, CodingKey {
            case employeeName = "employee_name"
            case active
            case employmentType = "employment_type"
            case minHours = "min_hours"
            case maxHours = "max_hours"
            case daypartAvailability = "daypart_availability"
            case isMinor = "is_minor"
        }

        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(employeeName, forKey: .employeeName)
            try c.encodeIfPresent(active, forKey: .active)
            try c.encodeIfPresent(employmentType, forKey: .employmentType)
            try c.encodeIfPresent(minHours, forKey: .minHours)
            try c.encodeIfPresent(maxHours, forKey: .maxHours)
            try c.encodeIfPresent(daypartAvailability, forKey: .daypartAvailability)
            try c.encodeIfPresent(isMinor, forKey: .isMinor)
        }
    }

    private struct SettingsResponse: Decodable {
        let ok: Bool
        let settings: RosterSettings?
        let error: String?
    }

    /// Writes the change into the local row first and rolls it back on a
    /// refusal — this sheet is a run of switches and pickers, and a spinner
    /// between each would make setting up a team feel like filing forms.
    @discardableResult
    func updateSettings(_ patch: StaffSettingsPatch) async -> Bool {
        guard let index = roster.firstIndex(where: { $0.name == patch.employeeName }) else { return false }
        let previous = roster[index]
        var next = previous
        var settings = previous.settings ?? RosterSettings()
        if let v = patch.active { settings.active = v; next.active = v }
        if let v = patch.employmentType { settings.employmentType = v }
        if let v = patch.minHours { settings.minHours = v }
        if let v = patch.maxHours { settings.maxHours = v }
        if let v = patch.daypartAvailability { settings.daypartAvailability = v }
        if let v = patch.isMinor { settings.isMinor = v }
        next.settings = settings
        roster[index] = next
        savingFor = patch.employeeName
        settingsToast = nil
        defer { savingFor = nil }
        do {
            let r: SettingsResponse = try await client.send(
                "/mobile/api/labor/staff-settings", method: .post, body: patch, hapticOnError: false)
            if r.ok {
                if let saved = r.settings, let i = roster.firstIndex(where: { $0.name == patch.employeeName }) {
                    roster[i].settings = saved
                    if let active = saved.active { roster[i].active = active }
                }
                Haptic.light()
                return true
            }
            roster[index] = previous
            settingsToast = r.error ?? "Couldn't save that."
        } catch let error as APIClient.APIError {
            roster[index] = previous
            settingsToast = error.message
        } catch {
            roster[index] = previous
            settingsToast = "Couldn't save that."
        }
        Haptic.error()
        return false
    }

    private struct PairBody: Encodable {
        let a: String
        let b: String
        let kind: String
        let note: String?
    }

    private struct PairResponse: Decodable {
        let ok: Bool
        let pair: StaffPair?
        let pairs: [StaffPair]?
        let error: String?
    }

    var isSavingPair = false
    var pairError: String?

    @discardableResult
    func addPair(a: String, b: String, kind: String, note: String?) async -> Bool {
        isSavingPair = true
        pairError = nil
        defer { isSavingPair = false }
        do {
            let r: PairResponse = try await client.send(
                "/mobile/api/labor/staff-pairs", method: .post,
                body: PairBody(a: a, b: b, kind: kind, note: note?.isEmpty == true ? nil : note))
            guard r.ok else { pairError = r.error ?? "Couldn't save that pair."; return false }
            if let all = r.pairs { pairs = all } else if let one = r.pair { pairs.append(one) }
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            pairError = error.message
        } catch {
            pairError = "Couldn't save that pair."
        }
        return false
    }

    func deletePair(id: Int) async {
        let previous = pairs
        pairs.removeAll { $0.id == id }
        do {
            let r: PairResponse = try await client.send(
                "/mobile/api/labor/staff-pairs/\(id)", method: .delete, hapticOnError: false)
            if r.ok {
                if let all = r.pairs { pairs = all }
                Haptic.selection()
            } else {
                pairs = previous
                pairError = r.error ?? "Couldn't remove that pair."
            }
        } catch let error as APIClient.APIError {
            pairs = previous
            pairError = error.message
        } catch {
            pairs = previous
            pairError = "Couldn't remove that pair."
        }
    }

    // MARK: Rules

    var rules: [String: LooseValue] = [:]
    var ruleDefaults: [String: LooseValue] = [:]
    var roleFloors: [String: RoleFloor] = [:]
    var ruleRoles: [String] = []
    var isLoadingRules = false
    var isSavingRules = false
    var rulesError: String?

    private struct RulesResponse: Decodable {
        let ok: Bool
        let rules: [String: LooseValue]?
        let defaults: [String: LooseValue]?
        let roleFloors: [String: RoleFloor]?
        let roles: [String]?
        let error: String?
        enum CodingKeys: String, CodingKey {
            case ok, rules, defaults, roles, error
            case roleFloors = "role_floors"
        }
    }

    private struct RulesBody: Encodable {
        let rules: [String: LooseValue]?
        let roleFloors: [String: RoleFloor]?
        enum CodingKeys: String, CodingKey {
            case rules
            case roleFloors = "role_floors"
        }
    }

    func loadRules() async {
        isLoadingRules = true
        defer { isLoadingRules = false }
        do {
            let r: RulesResponse = try await client.send("/mobile/api/labor/rules", hapticOnError: false)
            guard r.ok else { rulesError = r.error; return }
            rules = r.rules ?? [:]
            ruleDefaults = r.defaults ?? [:]
            roleFloors = r.roleFloors ?? [:]
            ruleRoles = r.roles ?? []
            rulesError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            rulesError = error.message
        } catch {
            rulesError = "Couldn't load the rules."
        }
    }

    @discardableResult
    func saveRules(_ newRules: [String: LooseValue], roleFloors newFloors: [String: RoleFloor]) async -> Bool {
        isSavingRules = true
        rulesError = nil
        defer { isSavingRules = false }
        do {
            let r: RulesResponse = try await client.send(
                "/mobile/api/labor/rules", method: .post,
                body: RulesBody(rules: newRules, roleFloors: newFloors))
            guard r.ok else { rulesError = r.error ?? "Couldn't save the rules."; return false }
            rules = r.rules ?? newRules
            roleFloors = r.roleFloors ?? newFloors
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            rulesError = error.message
        } catch {
            rulesError = "Couldn't save the rules."
        }
        return false
    }

    // MARK: Demand signals

    var signals: [DemandSignal] = []
    var isLoadingSignals = false
    var isSavingSignal = false
    var signalError: String?
    // What the last paste or add did, in the server's own count.
    var signalOutcome: String?

    private struct SignalsResponse: Decodable {
        let ok: Bool
        let signals: [DemandSignal]?
        let error: String?
    }

    private struct SignalRow: Encodable {
        let date: String
        let kind: String
        let label: String?
        let covers: Int?
        let liftPct: Double?
        enum CodingKeys: String, CodingKey {
            case date, kind, label, covers
            case liftPct = "lift_pct"
        }
    }

    private struct SignalRowsBody: Encodable { let rows: [SignalRow] }
    private struct SignalCSVBody: Encodable { let csv: String }

    private struct SignalWriteResponse: Decodable {
        let ok: Bool
        let written: Int?
        let skipped: Int?
        let errors: [String]?
        let error: String?
    }

    private static let isoDay: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = Calendar.current.timeZone
        return f
    }()

    /// The next 60 days, which is as far ahead as anybody schedules.
    func loadSignals() async {
        isLoadingSignals = signals.isEmpty
        defer { isLoadingSignals = false }
        let today = Calendar.current.startOfDay(for: Date())
        let end = Calendar.current.date(byAdding: .day, value: 60, to: today) ?? today
        do {
            let r: SignalsResponse = try await client.send(
                "/mobile/api/labor/demand-signals",
                query: ["start": Self.isoDay.string(from: today), "end": Self.isoDay.string(from: end)],
                hapticOnError: false)
            guard r.ok else { signalError = r.error; return }
            signals = (r.signals ?? []).sorted { $0.date < $1.date }
            signalError = nil
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            if signals.isEmpty { signalError = error.message }
        } catch {
            if signals.isEmpty { signalError = "Couldn't load events and reservations." }
        }
    }

    @discardableResult
    func addSignal(date: Date, kind: String, label: String?, covers: Int?, liftPct: Double?) async -> Bool {
        let row = SignalRow(date: Self.isoDay.string(from: date), kind: kind,
                            label: label?.isEmpty == true ? nil : label, covers: covers, liftPct: liftPct)
        return await writeSignals(SignalRowsBody(rows: [row]))
    }

    @discardableResult
    func pasteSignals(csv: String) async -> Bool {
        await writeSignals(SignalCSVBody(csv: csv))
    }

    private func writeSignals(_ body: any Encodable) async -> Bool {
        isSavingSignal = true
        signalError = nil
        signalOutcome = nil
        defer { isSavingSignal = false }
        do {
            let r: SignalWriteResponse = try await client.send(
                "/mobile/api/labor/demand-signals", method: .post, body: body)
            guard r.ok else { signalError = r.error ?? "Couldn't save that."; return false }
            var parts: [String] = []
            parts.append("\(r.written ?? 0) written")
            if let s = r.skipped, s > 0 { parts.append("\(s) skipped") }
            if let e = r.errors, !e.isEmpty { parts.append("\(e.count) with errors") }
            signalOutcome = parts.joined(separator: " · ")
            if let e = r.errors, !e.isEmpty { signalError = e.prefix(3).joined(separator: "\n") }
            Haptic.success()
            await loadSignals()
            return true
        } catch let error as APIClient.APIError {
            signalError = error.message
        } catch {
            signalError = "Couldn't save that."
        }
        return false
    }

    func deleteSignal(id: Int) async {
        let previous = signals
        signals.removeAll { $0.id == id }
        do {
            let _: OKResponse = try await client.send(
                "/mobile/api/labor/demand-signals/\(id)", method: .delete, hapticOnError: false)
            Haptic.selection()
        } catch let error as APIClient.APIError {
            signals = previous
            signalError = error.message
        } catch {
            signals = previous
            signalError = "Couldn't remove that."
        }
    }

    // MARK: Shift requests

    var shiftRequests: [ShiftRequest] = []
    var openShifts: [ShiftRequest] = []
    var isLoadingRequests = false
    var requestBusyId: Int?
    var requestError: String?
    var pendingRequests: [ShiftRequest] { shiftRequests.filter { $0.status == "pending" } }

    private struct RequestsResponse: Decodable {
        let ok: Bool
        let requests: [ShiftRequest]?
        let open: [ShiftRequest]?
        let error: String?
    }

    private struct DecideBody: Encodable {
        let decision: String
        let replacement: String?
    }

    private struct DecideResponse: Decodable {
        let ok: Bool
        let request: ShiftRequest?
        let error: String?
    }

    func loadShiftRequests() async {
        isLoadingRequests = shiftRequests.isEmpty && openShifts.isEmpty
        defer { isLoadingRequests = false }
        do {
            let r: RequestsResponse = try await client.send("/mobile/api/labor/shift-requests", hapticOnError: false)
            guard r.ok else { requestError = r.error; return }
            shiftRequests = r.requests ?? []
            openShifts = r.open ?? []
            requestError = nil
            if !pendingRequests.isEmpty { requestsExpanded = true }
        } catch is CancellationError {
        } catch {
            // Silent, like time off: a secondary section.
        }
    }

    /// Approve or deny a hand-back. Naming a replacement on approve covers
    /// the shift outright; the server checks that person is legal for it
    /// and answers 400 with the reason when they are not.
    func decideShiftRequest(_ id: Int, approve: Bool, replacement: String? = nil) async {
        requestBusyId = id
        requestError = nil
        defer { requestBusyId = nil }
        do {
            let r: DecideResponse = try await client.send(
                "/mobile/api/labor/shift-requests/\(id)/decide", method: .post,
                body: DecideBody(decision: approve ? "approve" : "deny", replacement: replacement))
            if r.ok, let updated = r.request {
                if let i = shiftRequests.firstIndex(where: { $0.id == id }) { shiftRequests[i] = updated }
                Haptic.success()
                await loadShiftRequests()
            } else {
                requestError = r.error ?? "Couldn't decide that."
            }
        } catch let error as APIClient.APIError {
            requestError = error.message
        } catch {
            requestError = "Couldn't decide that."
        }
    }
}
