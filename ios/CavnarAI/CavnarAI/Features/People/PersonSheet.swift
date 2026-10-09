import SwiftUI
import Observation

/// One person, one record (Friction audit #25, U2-7 / U3-20 / U4-8): role,
/// PIN, contact, hours, availability, rating, certifications, POS id and
/// pay in one sheet, reached from the roster, the staff list, the command
/// sheet's search and a name in Labor's "Waiting on you" — instead of three
/// screens that each hold a slice of the same employee.
///
/// Reads GET /mobile/api/people/<key> and saves with a partial POST to the
/// same path; the server routes each field to the store it already lives in
/// (people.py). On a server without the people routes the sheet says so and
/// points at the roster, rather than showing blanks as if they were facts.

/// Who to open. `key` when the caller has one (a search hit, a nav path);
/// otherwise the name, resolved against GET /mobile/api/people.
struct PersonSheetTarget: Identifiable, Hashable {
    let key: String?
    let name: String
    var id: String { key ?? "name:" + name.lowercased() }
}

struct PersonRecord: Decodable, Equatable {
    let key: String
    let name: String
    let role: String?
    let active: Bool?
    let pinSet: Bool?
    let phone: String?
    let email: String?
    let hours: JSONValue?
    let availability: JSONValue?
    let rating: JSONValue?
    let certifications: [String]?
    let posId: String?
    /// The rate this person's hours are costed at. Read only here: rates
    /// are per ROLE (people._pay_rate), set in Account → Targets & pay rates
    /// (/account/targets; the web, from the Pay card's row). The server
    /// sends `{role, rate, source}`; a bare number or text is read too.
    /// Decoding only a number left the field blank for everyone (F3-5).
    private(set) var payRate: Double?
    /// The role the rate belongs to, and whether it is that role's own rate
    /// ("role") or the restaurant's blended one ("blended").
    let payRateRole: String?
    let payRateSource: String?
    /// Whether this login may change the record (`_may_rate`); a view-only
    /// login sees the facts without Save (F3-5).
    let canEdit: Bool?
    /// Which fields this login may change, per field (`{"role": false,
    /// "pay_rate": true, …}`). Nil on an older server: role shown as a field,
    /// pay read only.
    let editable: [String: Bool]?
    /// The account holder's (`_principal`): rename, merge, undo a merge and
    /// erase (parity #87). False on an older server.
    var canManageLogin = false
    /// Whether they hold a staff login — erase waits until they don't.
    var hasLogin = false
    /// What else is known about them (memory round, 9/29/26): roles held
    /// beyond the shifts worked, their record of taking covers, the guest
    /// mentions the owner confirmed, and attendance on the shifts somebody
    /// watched — `{known: false}` reads "Not watched yet", never a clean
    /// record. All absent on an older server.
    var rolesHeld: [PersonRole] = []
    var covers: PersonCovers? = nil
    var guestMentions: [GuestMention] = []
    var attendance: PersonAttendance? = nil
    /// How each certificate reads — "Floor manager (can run the shift)"
    /// apart from the food-safety card (schedule audit 10/3/26 E-15).
    var certificationLabels: [String: String] = [:]

    private struct Choices: Decodable {
        let certificationLabels: [String: String]?
        enum CodingKeys: String, CodingKey { case certificationLabels = "certification_labels" }
    }

    enum CodingKeys: String, CodingKey {
        case key, name, role, active, phone, email, hours, availability, rating, certifications, editable
        case covers, attendance, choices
        case rolesHeld = "roles_held"
        case guestMentions = "guest_mentions"
        case pinSet = "pin_set"
        case posId = "pos_id"
        case payRate = "pay_rate"
        case payRateAmount = "pay_rate_amount"
        case canEdit = "can_edit"
        case canManageLogin = "can_manage_login"
        case hasLogin = "has_login"
    }

    /// Erase is offered for someone off the roster with no staff login —
    /// the server's own refusal (people.erase_person), said before asking.
    var mayErase: Bool { canManageLogin && active == false && !hasLogin }

    /// Whether `field` may be changed here: the server's `editable` map when
    /// it sent one, else `fallback`.
    func mayEdit(_ field: String, fallback: Bool) -> Bool {
        guard canEdit != false else { return false }
        return editable?[field] ?? fallback
    }

    private struct PayRate: Decodable {
        let rate: Double?
        let role: String?
        let source: String?
        enum CodingKeys: String, CodingKey { case rate, role, source }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            if let d = try? c.decodeIfPresent(Double.self, forKey: .rate) { rate = d }
            else if let s = try? c.decodeIfPresent(String.self, forKey: .rate) { rate = Double(s) }
            else { rate = nil }
            role = try? c.decodeIfPresent(String.self, forKey: .role)
            source = try? c.decodeIfPresent(String.self, forKey: .source)
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        key = (try? c.decode(String.self, forKey: .key)) ?? ""
        name = (try? c.decode(String.self, forKey: .name)) ?? ""
        role = try? c.decodeIfPresent(String.self, forKey: .role)
        active = try? c.decodeIfPresent(Bool.self, forKey: .active)
        pinSet = try? c.decodeIfPresent(Bool.self, forKey: .pinSet)
        phone = try? c.decodeIfPresent(String.self, forKey: .phone)
        email = try? c.decodeIfPresent(String.self, forKey: .email)
        hours = try? c.decodeIfPresent(JSONValue.self, forKey: .hours)
        availability = try? c.decodeIfPresent(JSONValue.self, forKey: .availability)
        rating = try? c.decodeIfPresent(JSONValue.self, forKey: .rating)
        certifications = try? c.decodeIfPresent([String].self, forKey: .certifications)
        // POS ids arrive as text or a number depending on the POS.
        if let s = try? c.decodeIfPresent(String.self, forKey: .posId) { posId = s }
        else if let n = try? c.decodeIfPresent(Int.self, forKey: .posId) { posId = String(n) }
        else { posId = nil }
        if let d = try? c.decodeIfPresent(Double.self, forKey: .payRate) {
            payRate = d; payRateRole = nil; payRateSource = nil
        } else if let s = try? c.decodeIfPresent(String.self, forKey: .payRate) {
            payRate = Double(s); payRateRole = nil; payRateSource = nil
        } else if let o = try? c.decodeIfPresent(PayRate.self, forKey: .payRate) {
            payRate = o.rate; payRateRole = o.role; payRateSource = o.source
        } else {
            payRate = nil; payRateRole = nil; payRateSource = nil
        }
        canEdit = try? c.decodeIfPresent(Bool.self, forKey: .canEdit)
        editable = try? c.decodeIfPresent([String: Bool].self, forKey: .editable)
        // The flat figure, when the server sends it, is the one to show.
        if let amount = try? c.decodeIfPresent(Double.self, forKey: .payRateAmount) { payRate = amount }
        rolesHeld = ((try? c.decodeIfPresent(HomeLenientList<PersonRole>.self, forKey: .rolesHeld)) ?? nil)?.items ?? []
        covers = try? c.decodeIfPresent(PersonCovers.self, forKey: .covers)
        guestMentions = ((try? c.decodeIfPresent(HomeLenientList<GuestMention>.self, forKey: .guestMentions)) ?? nil)?
            .items ?? []
        attendance = try? c.decodeIfPresent(PersonAttendance.self, forKey: .attendance)
        certificationLabels = ((try? c.decodeIfPresent(Choices.self, forKey: .choices)) ?? nil)?.certificationLabels ?? [:]
        canManageLogin = ((try? c.decodeIfPresent(Bool.self, forKey: .canManageLogin)) ?? nil) ?? false
        hasLogin = ((try? c.decodeIfPresent(Bool.self, forKey: .hasLogin)) ?? nil) ?? false
    }

    /// The certificates in the owner's words.
    var certificationNames: [String] {
        (certifications ?? []).map { certificationLabels[$0] ?? $0.replacingOccurrences(of: "_", with: " ").capitalized }
    }

    /// Whether the server sent the memory fields at all — an older server
    /// sends none, and the section is left off rather than shown empty.
    var hasMemory: Bool { attendance != nil || covers != nil || !rolesHeld.isEmpty || !guestMentions.isEmpty }

    /// "$15.00/h · Server's rate" / "$14.00/h · blended rate" — nil when no
    /// rate is set (never "$0").
    var payRateLine: String? {
        guard let payRate else { return nil }
        let money = "$" + String(format: "%.2f", payRate) + "/h"
        if payRateSource == "role", let role = payRateRole, !role.isEmpty { return money + " \u{00B7} \(role) rate" }
        if payRateSource == "blended" { return money + " \u{00B7} blended rate" }
        return money
    }

    /// A readable line for a fact whose shape the server owns ("20–32 h",
    /// "Mon, Tue nights", "4.5"). Nil when there is nothing to say — an
    /// unset fact is shown as "Not set", never as zero.
    static func describe(_ value: JSONValue?) -> String? {
        guard let value else { return nil }
        switch value {
        case .null: return nil
        case .string(let s): return s.isEmpty ? nil : s
        case .bool(let b): return b ? "Yes" : "No"
        case .number(let n): return n == n.rounded() ? String(Int(n)) : String(format: "%.1f", n)
        case .array(let items):
            let parts = items.compactMap { describe($0) }
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        case .object(let o):
            if let label = o["label"].flatMap({ describe($0) }) { return label }
            // Owner words for the keys the server sends; a key the app
            // doesn't know is left out, never shown raw (re-audit L5: it
            // read "max hours 32 · min hours 20").
            let parts = fieldOrder.compactMap { k -> String? in
                guard let v = o[k] else { return nil }
                return describeField(k, v)
            }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
        }
    }

    private static let fieldOrder = ["employment_type", "min", "max", "min_hours", "max_hours", "desired_hours",
                                     "score", "can_close", "dayparts", "preferred_dayparts"]

    /// One known fact field, said the way an owner would.
    static func describeField(_ key: String, _ value: JSONValue) -> String? {
        switch key {
        case "employment_type":
            guard case .string(let s) = value else { return nil }
            return s == "full" ? "Full-time" : s == "part" ? "Part-time" : nil
        case "min", "min_hours": return describe(value).map { "at least \($0) h" }
        case "max", "max_hours": return describe(value).map { "at most \($0) h" }
        case "desired_hours": return describe(value).map { "wants \($0) h" }
        case "score": return describe(value)
        case "can_close":
            if case .bool(true) = value { return "can close" }
            return nil
        case "preferred_dayparts": return describe(value).map { "prefers \($0)" }
        case "dayparts":
            guard case .object(let days) = value else { return nil }
            let order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            let parts = order.compactMap { d -> String? in
                guard case .string(let v)? = days[d], v != "any", !v.isEmpty else { return nil }
                return "\(d.prefix(3)) \(v == "off" ? "off" : v + "s")"
            }
            return parts.isEmpty ? nil : parts.joined(separator: ", ")
        default: return nil
        }
    }
}

@Observable
@MainActor
final class PersonSheetViewModel {
    enum State: Equatable {
        case loading
        case loaded(PersonRecord)
        /// The server has no people routes yet, or no such person.
        case unavailable(String)
    }

    private(set) var state: State = .loading
    var role = ""
    var phone = ""
    var email = ""
    var posId = ""
    /// Only sent when the server says this login may set the pay rate.
    var payRate = ""
    private(set) var isSaving = false
    private(set) var saveMessage: String?
    private(set) var saveError: String?

    let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    // Name and records (#87): the merges that touch this person, everyone
    // they could be merged into, and the sheet's own status lines.
    var merges: [PeopleMerge] = []
    var canUndoMerges = false
    var people: [PeopleListRow] = []
    /// Whether the merge picker's list has come back, and why it didn't —
    /// the picker shows Try again rather than an endless skeleton
    /// (re-audit 10/8/26).
    var peopleLoaded = false
    var peopleError: String?
    var recordsBusy = false
    var recordsMessage: String?
    var recordsError: String?

    private struct ListResponse: Decodable {
        struct Row: Decodable { let key: String; let name: String }
        let ok: Bool
        let people: [Row]?
    }

    private struct PersonResponse: Decodable {
        let ok: Bool
        let person: PersonRecord?
        let error: String?
        /// The fields the server actually wrote (people.update_person).
        var changed: [String]? = nil
    }

    /// What the sheet sent that the server did not write — each named, so
    /// "Saved." is never said over a change that was dropped (F3-5). The
    /// server's names: a role is written as the login's `job_title`.
    nonisolated static func unsaved(sent: [String], changed: [String]?) -> [String] {
        guard let changed else { return [] }
        let wrote = Set(changed)
        return sent.filter { key in
            key == "role" ? !(wrote.contains("role") || wrote.contains("job_title")) : !wrote.contains(key)
        }.sorted()
    }

    nonisolated static func label(forField key: String) -> String {
        switch key {
        case "role": return "Role"
        case "pay_rate": return "Pay rate"
        case "phone": return "Phone"
        case "email": return "Email"
        case "pos_id": return "POS id"
        default: return key.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    static func path(for key: String) -> String {
        var allowed = CharacterSet.alphanumerics
        allowed.insert(charactersIn: "-._~")
        return "/mobile/api/people/" + (key.addingPercentEncoding(withAllowedCharacters: allowed) ?? key)
    }

    func load(_ target: PersonSheetTarget) async {
        do {
            var key = target.key
            if key == nil {
                let list: ListResponse = try await client.send("/mobile/api/people", hapticOnError: false)
                key = (list.people ?? []).first { $0.name.caseInsensitiveCompare(target.name) == .orderedSame }?.key
            }
            guard let key else {
                state = .unavailable("\(target.name) isn't on the team list yet.")
                return
            }
            let r: PersonResponse = try await client.send(Self.path(for: key), hapticOnError: false)
            guard r.ok, let person = r.person else {
                state = .unavailable(r.error ?? "Couldn't load \(target.name).")
                return
            }
            apply(person)
        } catch let error as APIClient.APIError where error.status == 404 {
            state = .unavailable("One record per person needs the latest Cavnar AI server. Until then, "
                                 + "scheduling lives in Labor → Roster and logins in Account → Staff accounts.")
        } catch is CancellationError {
        } catch let error as APIClient.APIError {
            state = .unavailable(error.message)
        } catch {
            state = .unavailable("Couldn't load \(target.name).")
        }
    }

    private func apply(_ person: PersonRecord) {
        state = .loaded(person)
        role = person.role ?? ""
        phone = person.phone ?? ""
        email = person.email ?? ""
        posId = person.posId ?? ""
        payRate = person.payRate.map { String(format: "%.2f", $0) } ?? ""
    }

    /// Only the fields that changed — each goes to its own store server-side.
    func changes(from person: PersonRecord) -> [String: AnyCodableValue] {
        var out: [String: AnyCodableValue] = [:]
        func trimmed(_ s: String) -> String { s.trimmingCharacters(in: .whitespacesAndNewlines) }
        if person.mayEdit("role", fallback: true), trimmed(role) != (person.role ?? "") {
            out["role"] = .string(trimmed(role))
        }
        if trimmed(phone) != (person.phone ?? "") { out["phone"] = .string(trimmed(phone)) }
        if trimmed(email) != (person.email ?? "") { out["email"] = .string(trimmed(email)) }
        if trimmed(posId) != (person.posId ?? "") { out["pos_id"] = .string(trimmed(posId)) }
        // Pay is sent only where the server says this login may set it;
        // otherwise it is the role's rate, read only here.
        if person.mayEdit("pay_rate", fallback: false) {
            let rate = Double(trimmed(payRate).replacingOccurrences(of: "$", with: ""))
            if let rate, rate != person.payRate { out["pay_rate"] = .double(rate) }
        }
        return out
    }

    // MARK: Roles held (POST /people/<key>/roles)

    /// A role to add ("bartender"), and whether it becomes their role on
    /// the roster (a promotion) rather than one they're trained on.
    var newRole = ""
    var newRolePrimary = false
    /// The day they started in it ("trained on bar from 9/1"), an ISO day
    /// from the date chip; empty is no start date (the web's "From").
    var newRoleSince = ""
    private(set) var roleBusy = false
    private(set) var roleMessage: String?
    private(set) var roleError: String?

    /// POST /people/<key>/roles: `{role, since?, primary?}` adds a role,
    /// `{role, remove: true}` takes one off (strategy_routes._do_person_roles).
    struct RoleBody: Encodable {
        let role: String
        var since: String? = nil
        let primary: Bool?
        let remove: Bool?
    }

    private struct RoleResponse: Decodable {
        let ok: Bool
        let error: String?
        let sinceLabel: String?
        enum CodingKeys: String, CodingKey {
            case ok, error
            case sinceLabel = "since_label"
        }
    }

    static func rolesPath(for key: String) -> String { path(for: key) + "/roles" }

    /// Adds (or with `remove`, takes off) one role, then re-reads the
    /// record so the roster's role and the list agree.
    func changeRole(_ role: String, primary: Bool = false, since: String? = nil, remove: Bool = false) async {
        guard case .loaded(let person) = state else { return }
        let name = role.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty else { return }
        let from = remove ? nil : since.flatMap { $0.isEmpty ? nil : String($0.prefix(10)) }
        roleBusy = true
        roleError = nil
        roleMessage = nil
        defer { roleBusy = false }
        do {
            let r: RoleResponse = try await client.send(
                Self.rolesPath(for: person.key), method: .post,
                body: RoleBody(role: name, since: from, primary: remove ? nil : primary, remove: remove ? true : nil),
                retryTransient: false)
            guard r.ok else { roleError = r.error ?? "Couldn\u{2019}t save that role."; return }
            if !remove { newRole = ""; newRolePrimary = false; newRoleSince = "" }
            let fromLine = from.map { " \u{2014} from " + (r.sinceLabel ?? CavnarDate.mdy($0)) } ?? ""
            roleMessage = remove ? "Removed." : (primary ? "Promoted \u{2014} their role on the roster" + fromLine + "."
                                                         : "Added" + fromLine + ".")
            Haptic.success()
            let refreshed: PersonResponse? = try? await client.send(Self.path(for: person.key), hapticOnError: false)
            if let updated = refreshed?.person { apply(updated) }
        } catch let error as APIClient.APIError {
            roleError = error.message
        } catch {
            roleError = "Couldn\u{2019}t save that role."
        }
    }

    func save() async {
        guard case .loaded(let person) = state else { return }
        let body = changes(from: person)
        guard !body.isEmpty else {
            saveMessage = "Nothing changed."
            return
        }
        isSaving = true
        saveError = nil
        saveMessage = nil
        defer { isSaving = false }
        do {
            let r: PersonResponse = try await client.send(Self.path(for: person.key), method: .post, body: body)
            if r.ok {
                if let updated = r.person { apply(updated) }
                let dropped = Self.unsaved(sent: Array(body.keys), changed: r.changed)
                if dropped.isEmpty {
                    saveMessage = "Saved."
                    Haptic.success()
                } else {
                    let names = dropped.map(Self.label(forField:)).joined(separator: ", ")
                    saveError = "Not saved: \(names). "
                        + (dropped.contains("role") ? "A role is changed in Labor \u{2192} Roster." : "Try again.")
                }
            } else {
                saveError = r.error ?? "Couldn't save that."
            }
        } catch let error as APIClient.APIError {
            saveError = error.message
        } catch {
            saveError = "Couldn't save that."
        }
    }
}

// MARK: - Name and records (parity audit 10/7/26 #87)

extension PersonSheetViewModel {
    static func renamePath(for key: String) -> String { path(for: key) + "/rename" }
    static func erasePath(for key: String) -> String { path(for: key) + "/erase" }
    static let mergePath = "/mobile/api/people/merge"
    static let mergesPath = "/mobile/api/people/merges"
    static func undoPath(_ mergeId: Int) -> String { "/mobile/api/people/merges/\(mergeId)/undo" }

    /// Whether `typed` names this person — the server's own comparison
    /// (whitespace folded, case ignored).
    nonisolated static func typedNameMatches(_ typed: String, _ name: String) -> Bool {
        func fold(_ s: String) -> String { s.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
        return !fold(name).isEmpty && fold(typed) == fold(name)
    }

    /// Re-reads one person by key (after a rename or a merge moved them).
    func reload(key: String) async {
        await load(PersonSheetTarget(key: key, name: ""))
    }

    /// The merges of the last 30 days that touch this person, and whether
    /// this login may undo them.
    func loadMerges(for name: String) async {
        guard let r: PeopleMergesResponse = try? await client.send(Self.mergesPath, hapticOnError: false), r.ok else {
            return
        }
        let n = name.lowercased()
        merges = r.merges.filter { $0.from.lowercased() == n || $0.into.lowercased() == n }
        canUndoMerges = r.canUndo
    }

    /// Everyone else on the list, for "the same person as…".
    func loadPeople(excluding key: String) async {
        peopleError = nil
        do {
            let r: PeopleListResponse = try await client.send("/mobile/api/people", hapticOnError: false)
            guard r.ok else { peopleError = "The list didn\u{2019}t load."; return }
            people = r.people.filter { $0.key != key && !$0.key.isEmpty }
            peopleLoaded = true
        } catch {
            peopleError = PeopleRulesViewModel.loadFailure(error)
        }
    }

    /// Returns the new key on success.
    func rename(_ person: PersonRecord, to newName: String) async -> String? {
        let name = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, name != person.name else { return nil }
        recordsBusy = true
        recordsError = nil
        defer { recordsBusy = false }
        do {
            let r: PersonRenameResponse = try await client.send(Self.renamePath(for: person.key), method: .post,
                                                                body: PersonRenameBody(name: name), retryTransient: false)
            guard r.ok else { recordsError = r.error ?? "Couldn\u{2019}t rename them."; return nil }
            Haptic.success()
            recordsMessage = "Renamed to \(r.to ?? name)."
            return r.key ?? person.key
        } catch let e as APIClient.APIError {
            recordsError = e.message
        } catch {
            recordsError = "Couldn\u{2019}t rename them."
        }
        return nil
    }

    /// `person`'s records all move onto `into`. Returns true when merged.
    func merge(_ person: PersonRecord, into: PeopleListRow) async -> Bool {
        recordsBusy = true
        recordsError = nil
        defer { recordsBusy = false }
        do {
            let r: PeopleMergeResponse = try await client.send(Self.mergePath, method: .post,
                                                               body: PeopleMergeBody(from: person.key, into: into.key),
                                                               retryTransient: false)
            guard r.ok else { recordsError = r.error ?? "Couldn\u{2019}t merge them."; return false }
            Haptic.success()
            recordsMessage = "Merged into \(r.into ?? into.name). You can undo it for 30 days."
            return true
        } catch let e as APIClient.APIError {
            recordsError = e.message
        } catch {
            recordsError = "Couldn\u{2019}t merge them."
        }
        return false
    }

    func undoMerge(_ m: PeopleMerge, name: String) async {
        recordsBusy = true
        recordsError = nil
        defer { recordsBusy = false }
        do {
            let r: PeopleMergeResponse = try await client.send(Self.undoPath(m.mergeId), method: .post,
                                                               body: TeamEmptyBody(), retryTransient: false)
            guard r.ok else { recordsError = r.error ?? "Couldn\u{2019}t undo that merge."; return }
            Haptic.success()
            recordsMessage = "Two people again: \(r.from ?? m.from) and \(r.into ?? m.into) \u{2014} each has their own records back."
            await loadMerges(for: name)
            if case .loaded(let p) = state { await reload(key: p.key) }
        } catch let e as APIClient.APIError {
            recordsError = e.message
        } catch {
            recordsError = "Couldn\u{2019}t undo that merge."
        }
    }

    /// Erases every record about them. True when it went.
    func erase(_ person: PersonRecord, typed: String) async -> Bool {
        recordsBusy = true
        recordsError = nil
        defer { recordsBusy = false }
        do {
            let r: APIClient.OKResponse = try await client.send(Self.erasePath(for: person.key), method: .post,
                                                                body: PersonEraseBody(confirm: typed), retryTransient: false)
            guard r.ok else { recordsError = r.error ?? "Couldn\u{2019}t erase their record."; return false }
            Haptic.success()
            return true
        } catch let e as APIClient.APIError {
            recordsError = e.message
        } catch {
            recordsError = "Couldn\u{2019}t erase their record."
        }
        return false
    }
}

struct PersonSheet: View {
    let target: PersonSheetTarget
    @State private var viewModel = PersonSheetViewModel()
    @Environment(\.dismiss) private var dismiss
    @State private var renaming = false
    @State private var mergePicking = false
    @State private var mergeInto: PeopleListRow?
    @State private var erasing = false
    @State private var undoing: PeopleMerge?
    /// The add-role form and the merges list, opened from the "…" menu
    /// (iOS readability round [75]: they sat on the record itself).
    @State private var addingRole = false
    @State private var showingMerges = false
    /// A role waiting on its "Remove the X role?" confirm (L16).
    @State private var pendingRoleRemoval: String?
    @Environment(\.openURL) private var openURL

    /// Contact fields typed and not saved (M9).
    private var contactDirty: Bool {
        guard case .loaded(let person) = viewModel.state, person.canEdit != false else { return false }
        var changed = viewModel.changes(from: person)
        // The phone field shows "(312) 555-0100" for a stored "3125550100":
        // the same number is not an edit.
        if viewModel.phone.filter(\.isNumber) == (person.phone ?? "").filter(\.isNumber) {
            changed.removeValue(forKey: "phone")
        }
        return !changed.isEmpty
    }

    private func removeRole(_ role: String) async {
        await viewModel.changeRole(role, remove: true)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    switch viewModel.state {
                    case .loading:
                        CavnarSkeletonLines(widths: [0.6, 1.0, 0.8, 0.9])
                    case .unavailable(let why):
                        Text(target.name)
                            .cavnarText(.title)
                        Text(why)
                            .cavnarText(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    case .loaded(let person):
                        loaded(person)
                        if person.canManageLogin { recordsStatus }
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            // Contact edits typed and not saved: Back and swipe-down ask
            // first (re-audit M9).
            .accountSheetChrome("Person", isDirty: contactDirty)
            .confirmationDialog(pendingRoleRemoval.map { "Remove the \($0) role?" } ?? "",
                                isPresented: Binding(get: { pendingRoleRemoval != nil },
                                                     set: { if !$0 { pendingRoleRemoval = nil } }),
                                titleVisibility: .visible) {
                Button("Remove role", role: .destructive) {
                    guard let role = pendingRoleRemoval else { return }
                    pendingRoleRemoval = nil
                    Task { await removeRole(role) }
                }
                Button("Keep it", role: .cancel) { pendingRoleRemoval = nil }
            } message: {
                Text("Schedules stop giving them shifts as this role. You can add it back from the \u{2026} menu.")
            }
            .toolbar {
                if case .loaded(let person) = viewModel.state, person.canManageLogin || person.canEdit != false {
                    cavnarToolbarItem(placement: .topBarTrailing) { overflowMenu(person) }
                }
            }
            .sheet(isPresented: $addingRole) {
                if case .loaded(let person) = viewModel.state {
                    NavigationStack {
                        ScrollView {
                            addRoleForm(person)
                                .padding(CavnarSpace.gutter)
                        }
                        .accountSheetChrome("Add a role")
                    }
                    .presentationDetents([.medium, .large])
                }
            }
            .sheet(isPresented: $showingMerges) {
                if case .loaded(let person) = viewModel.state {
                    NavigationStack {
                        ScrollView {
                            VStack(alignment: .leading, spacing: CavnarSpace.l) {
                                records(person)
                            }
                            .padding(CavnarSpace.gutter)
                        }
                        .accountSheetChrome("Merged lately")
                        // Asked here — a dialog on the record underneath
                        // can't show over this sheet.
                        .confirmationDialog(undoing.map { "Make \($0.from) and \($0.into) two people again?" } ?? "",
                                            isPresented: Binding(get: { undoing != nil }, set: { if !$0 { undoing = nil } }),
                                            titleVisibility: .visible) {
                            Button("Undo the merge", role: .destructive) {
                                guard let m = undoing, case .loaded(let person) = viewModel.state else { return }
                                undoing = nil
                                Task { await viewModel.undoMerge(m, name: person.name) }
                            }
                            Button("Keep them merged", role: .cancel) { undoing = nil }
                        } message: {
                            Text("Each gets back the ratings, notes, roles and shifts they had before.")
                        }
                    }
                    .presentationDetents([.medium, .large])
                }
            }
        }
        .task {
            await viewModel.load(target)
            if case .loaded(let p) = viewModel.state, p.canManageLogin { await viewModel.loadMerges(for: p.name) }
        }
        .sheet(isPresented: $renaming) {
            if case .loaded(let person) = viewModel.state {
                PersonRenameSheet(name: person.name) { newName in
                    guard let key = await viewModel.rename(person, to: newName) else { return false }
                    await viewModel.reload(key: key)
                    return true
                }
                .presentationDetents([.medium])
            }
        }
        .sheet(isPresented: $mergePicking) {
            if case .loaded(let person) = viewModel.state {
                PersonMergePicker(people: viewModel.people, name: person.name,
                                  loaded: viewModel.peopleLoaded, error: viewModel.peopleError,
                                  retry: { await viewModel.loadPeople(excluding: person.key) }) { picked in
                    mergePicking = false
                    mergeInto = picked
                }
                .presentationDetents([.medium, .large])
                .task { await viewModel.loadPeople(excluding: person.key) }
            }
        }
        .sheet(isPresented: $erasing) {
            if case .loaded(let person) = viewModel.state {
                PersonEraseSheet(name: person.name, error: viewModel.recordsError) { typed in
                    guard await viewModel.erase(person, typed: typed) else { return false }
                    dismiss()
                    return true
                }
                .presentationDetents([.medium])
            }
        }
        .confirmationDialog(mergeConfirmTitle, isPresented: Binding(get: { mergeInto != nil },
                                                                    set: { if !$0 { mergeInto = nil } }),
                            titleVisibility: .visible) {
            Button("Merge them") {
                guard let into = mergeInto, case .loaded(let person) = viewModel.state else { return }
                mergeInto = nil
                Task {
                    if await viewModel.merge(person, into: into) {
                        await viewModel.reload(key: into.key)
                        await viewModel.loadMerges(for: into.name)
                    }
                }
            }
            Button("Not yet", role: .cancel) { mergeInto = nil }
        } message: {
            Text(mergeConfirmMessage)
        }
    }

    private var mergeConfirmTitle: String {
        guard let into = mergeInto, case .loaded(let p) = viewModel.state else { return "" }
        return "Merge \(p.name) into \(into.name)?"
    }

    private var mergeConfirmMessage: String {
        guard let into = mergeInto, case .loaded(let p) = viewModel.state else { return "" }
        return "Every rating, note, role and shift of \(p.name) moves to \(into.name), and \(p.name) stays on as "
            + "another name for them. You can undo it for 30 days."
    }

    /// Add a role (anyone who may change the record); rename, "the same
    /// person as…", the merges of the last 30 days and — for someone who has
    /// left and holds no staff login — erase (the account holder's alone).
    private func overflowMenu(_ person: PersonRecord) -> some View {
        Menu {
            if person.canEdit != false {
                Button {
                    addingRole = true
                } label: { Label("Add a role\u{2026}", systemImage: "person.badge.plus") }
            }
            if person.canManageLogin {
                Button {
                    renaming = true
                } label: { Label("Rename", systemImage: "pencil") }
                Button {
                    mergePicking = true
                } label: { Label("Same person as\u{2026}", systemImage: "person.2.badge.gearshape") }
                if !viewModel.merges.isEmpty {
                    Button {
                        showingMerges = true
                    } label: { Label("Merged lately", systemImage: "arrow.uturn.backward") }
                }
                if person.mayErase {
                    Button(role: .destructive) {
                        viewModel.recordsError = nil
                        erasing = true
                    } label: { Label("Erase their record", systemImage: "trash") }
                }
            }
        } label: {
            Image(systemName: "ellipsis")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .cavnarToolbarIconGlass()
        }
        .accessibilityLabel("Name and records")
    }

    /// The status of the last rename, merge or undo, on the record.
    @ViewBuilder
    private var recordsStatus: some View {
        if let m = viewModel.recordsMessage {
            Text(m).cavnarText(.secondary, color: .cavnarGreen)
                .fixedSize(horizontal: false, vertical: true)
        }
        if let e = viewModel.recordsError, !erasing {
            Text(e).cavnarText(.secondary, color: .cavnarRedText)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The merges of the last 30 days that touch them, with Undo while it
    /// can still be undone (in the "…" menu's Merged lately sheet).
    @ViewBuilder
    private func records(_ person: PersonRecord) -> some View {
        recordsStatus
        if !viewModel.merges.isEmpty {
            AccountSection(kicker: "Merged lately") {
                ForEach(Array(viewModel.merges.enumerated()), id: \.element.id) { i, m in
                    VStack(alignment: .leading, spacing: 0) {
                        HStack(alignment: .center, spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("\(m.from) \u{2192} \(m.into)")
                                    .cavnarText(.label, color: m.undone ? .cavnarInk2 : .cavnarInk)
                                CavnarMixedText(Self.mergeLine(m), role: .caption)
                            }
                            Spacer(minLength: 6)
                            if viewModel.canUndoMerges && m.undoable {
                                Button {
                                    Haptic.light()
                                    undoing = m
                                } label: {
                                    Text("Undo")
                                        .cavnarText(.label, color: .cavnarEmber2)
                                        .cavnarHitTarget()
                                }
                                .buttonStyle(.plain)
                                .disabled(viewModel.recordsBusy)
                            }
                        }
                        .padding(.vertical, 9)
                        if i < viewModel.merges.count - 1 { AccountRowDivider() }
                    }
                }
            }
        }
    }

    /// "Merged 10/2/26 · can be undone until 11/1/26", or why not.
    static func mergeLine(_ m: PeopleMerge) -> String {
        var bits: [String] = []
        if let on = m.mergedOn, !on.isEmpty { bits.append("Merged \(on)") }
        if m.undone { bits.append("undone") }
        else if m.undoable, let until = m.undoUntil { bits.append("can be undone until \(until)") }
        else if let why = m.whyNot, !why.isEmpty { bits.append(why) }
        return bits.joined(separator: " \u{00B7} ")
    }

    @ViewBuilder
    private func loaded(_ person: PersonRecord) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text(person.name)
                .cavnarText(.title)
            HStack(spacing: 6) {
                if let role = person.role, !role.isEmpty { AccountChip(text: role) }
                AccountChip(text: person.active == false ? "Not active" : "Active", muted: person.active == false)
            }
            // The two things an owner opens a person for most: call them,
            // text them (iOS readability round [75]).
            if let phone = person.phone, let digits = Self.dialable(phone) {
                HStack(spacing: CavnarSpace.s) {
                    Button {
                        if let url = URL(string: "tel:\(digits)") { openURL(url) }
                    } label: {
                        Label("Call", systemImage: "phone.fill").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .accessibilityLabel("Call \(person.name)")
                    Button {
                        if let url = URL(string: "sms:\(digits)") { openURL(url) }
                    } label: {
                        Label("Text", systemImage: "message.fill").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .accessibilityLabel("Text \(person.name)")
                }
                .padding(.top, CavnarSpace.xxs)
            }
        }

        AccountSection(kicker: "Scheduling") {
            kv("Hours", PersonRecord.describe(person.hours) ?? "Not set")
            kv("Availability", PersonRecord.describe(person.availability) ?? "Not set")
            kv("Rating", PersonRecord.describe(person.rating) ?? "Not rated")
            kv("Certifications", last: true,
                (person.certifications ?? []).isEmpty ? "None on file"
                                : person.certificationNames.joined(separator: ", "))
        }

        if person.hasMemory { memorySection(person) }

        AccountSection(kicker: "Login") {
            kv("PIN", last: true, person.pinSet == true ? "Set" : "Not set")
        }

        // Pay is read here and set on the web (iOS readability round [71]):
        // rates are per role, in Targets & pay rates.
        AccountSection(kicker: "Pay") {
            kv("Pay rate", person.payRateLine ?? "Not set")
            CavnarWebLinkRow(title: "Pay rates", path: "account/restaurant",
                             actionLabel: person.mayEdit("pay_rate", fallback: false) ? "Edit on the web" : "On the web")
        }

        if person.canEdit == false {
            AccountSection(kicker: "Contact") {
                kv("Role", person.role?.isEmpty == false ? person.role! : "Not set")
                kv("Phone", person.phone?.isEmpty == false ? PhoneFormat.display(person.phone) : "Not set")
                kv("Email", person.email?.isEmpty == false ? person.email! : "Not set")
                kv("POS id", last: true, person.posId?.isEmpty == false ? person.posId! : "Not set")
            }
        } else {
            AccountSection(kicker: "Contact") {
                VStack(alignment: .leading, spacing: 12) {
                    if person.mayEdit("role", fallback: true) {
                        field("Role", text: $viewModel.role, keyboard: .default)
                    } else {
                        kv("Role", person.role?.isEmpty == false ? person.role! : "Not set")
                    }
                    field("Phone", text: $viewModel.phone, keyboard: .phonePad)
                        .onChange(of: viewModel.phone) { _, v in let f = PhoneFormat.typing(v); if f != v { viewModel.phone = f } }
                    field("Email", text: $viewModel.email, keyboard: .emailAddress)
                    // The POS id and the pay rate are set on the web (iOS
                    // readability round [75]); the POS id reads here.
                    kv("POS id", last: true, person.posId?.isEmpty == false ? person.posId! : "Not set")
                    Button {
                        Haptic.light()
                        Task { await viewModel.save() }
                    } label: {
                        Group {
                            if viewModel.isSaving { CavnarShimmerText(text: "Saving…") } else { Text("Save") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSaving))
                    .disabled(viewModel.isSaving)
                    if let message = viewModel.saveMessage {
                        Text(message).cavnarText(.secondary, color: .cavnarGreen)
                    }
                    if let error = viewModel.saveError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// What Cavnar AI knows about them beyond the settings: attendance on
    /// watched shifts, covers taken, the roles they hold, and what guests
    /// said (confirmed by the owner). Each fact says its window.
    @ViewBuilder
    private func memorySection(_ person: PersonRecord) -> some View {
        AccountSection(kicker: "What Cavnar AI knows") {
            AccountKVRow(label: "Attendance") {
                HomeMixedText.make((person.attendance ?? PersonAttendance(known: false)).line, role: .secondary,
                                   color: person.attendance?.unreliable == true ? .cavnarAmber : .cavnarInk)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            AccountKVRow(label: "Covers", showsDivider: !person.rolesHeld.isEmpty || !person.guestMentions.isEmpty) {
                HomeMixedText.make((person.covers ?? PersonCovers(taken: 0, declined: 0)).line, role: .secondary, color: .cavnarInk)
                    .multilineTextAlignment(.trailing)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !person.rolesHeld.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Roles held")
                        .cavnarText(.caption, color: .cavnarInk2)
                    ForEach(person.rolesHeld) { role in
                        HStack(alignment: .center, spacing: 8) {
                            CavnarMixedText(role.line, role: .secondary)
                            Spacer(minLength: 4)
                            if person.canEdit != false && !role.primary {
                                // Asked first (re-audit L16): one tap took
                                // the role off.
                                Button {
                                    Haptic.light()
                                    pendingRoleRemoval = role.role
                                } label: {
                                    Text("Remove")
                                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                        .foregroundStyle(Color.cavnarInk2)
                                        .cavnarHitTarget()
                                }
                                .accessibilityLabel("Remove the \(role.role) role")
                                .buttonStyle(.plain)
                                .disabled(viewModel.roleBusy)
                            }
                        }
                    }
                }
                .padding(.vertical, 10)
            }
            if !person.guestMentions.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text("What guests said")
                        .cavnarText(.caption, color: .cavnarInk2)
                    ForEach(person.guestMentions) { m in
                        CavnarMixedText((m.dateLabel.map { $0 + " \u{00B7} " } ?? "")
                                        + "\u{201C}" + (m.snippet ?? "named in a review") + "\u{201D}",
                                        role: .secondary, color: m.isComplaint ? .cavnarAmber : .cavnarInk2)
                    }
                }
                .padding(.vertical, 10)
            }
        }
    }

    /// A role they can work beyond their shifts so far — trained on bar, or
    /// a promotion. Opened from the "…" menu's "Add a role…".
    @ViewBuilder
    private func addRoleForm(_ person: PersonRecord) -> some View {
        if person.canEdit != false {
            AccountSection(kicker: "Add a role") {
                VStack(alignment: .leading, spacing: 10) {
                    Text("A role they can work beyond their shifts so far \u{2014} trained on bar, or a promotion.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    TextField("Role", text: $viewModel.newRole)
                        .cavnarTextFieldStyle()
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                    Button {
                        Haptic.selection()
                        viewModel.newRolePrimary.toggle()
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: viewModel.newRolePrimary ? "checkmark.square.fill" : "square")
                                .foregroundStyle(viewModel.newRolePrimary ? Color.cavnarEmber2 : Color.cavnarInk3)
                            Text("A promotion \u{2014} make it their role on the roster")
                                .cavnarText(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                    // The day they started in it — "trained on bar from
                    // 9/1": a candidate for that role's gaps from then.
                    HStack(spacing: 10) {
                        Text("From")
                            .cavnarText(.secondary)
                        CavnarDateChip(iso: $viewModel.newRoleSince, accessibilityName: "The day they started in this role")
                        if !viewModel.newRoleSince.isEmpty {
                            Button {
                                Haptic.light()
                                viewModel.newRoleSince = ""
                            } label: {
                                Text("Clear")
                                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                                    .foregroundStyle(Color.cavnarInk2)
                                    .cavnarHitTarget()
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("Clear the start date")
                        } else {
                            Text("optional")
                                .cavnarText(.caption)
                        }
                        Spacer(minLength: 0)
                    }
                    Button {
                        Haptic.light()
                        Task {
                            await viewModel.changeRole(viewModel.newRole, primary: viewModel.newRolePrimary,
                                                       since: viewModel.newRoleSince)
                        }
                    } label: {
                        Group {
                            if viewModel.roleBusy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Add role") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.roleBusy
                                                            || viewModel.newRole.trimmingCharacters(in: .whitespaces).isEmpty))
                    .disabled(viewModel.roleBusy || viewModel.newRole.trimmingCharacters(in: .whitespaces).isEmpty)
                    if let message = viewModel.roleMessage {
                        Text(message).cavnarText(.secondary, color: .cavnarGreen)
                    }
                    if let error = viewModel.roleError {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// The digits (and a leading +) a tel:/sms: link takes; nil when there
    /// aren't enough to dial.
    static func dialable(_ phone: String) -> String? {
        let trimmed = phone.trimmingCharacters(in: .whitespaces)
        let digits = trimmed.filter(\.isNumber)
        guard digits.count >= 7 else { return nil }
        return (trimmed.hasPrefix("+") ? "+" : "") + digits
    }

    private func kv(_ label: String, last: Bool = false, _ value: String) -> some View {
        AccountKVRow(label: label, showsDivider: !last) {
            AccountValue(text: value, isNumber: value.first?.isNumber == true)
        }
    }

    private func field(_ label: String, text: Binding<String>, keyboard: UIKeyboardType) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .cavnarText(.caption, color: .cavnarInk2)
            TextField(label, text: text)
                .cavnarTextFieldStyle()
                .keyboardType(keyboard)
                .textInputAutocapitalization(keyboard == .emailAddress ? .never : .words)
                .autocorrectionDisabled()
        }
    }
}
