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
    /// are per ROLE (people._pay_rate), set on the web in Account → Targets & pay rates
    /// (/api/account/targets; the phone has no editor for them). The server
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

    enum CodingKeys: String, CodingKey {
        case key, name, role, active, phone, email, hours, availability, rating, certifications, editable
        case pinSet = "pin_set"
        case posId = "pos_id"
        case payRate = "pay_rate"
        case payRateAmount = "pay_rate_amount"
        case canEdit = "can_edit"
    }

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
    }

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
            let parts = o.keys.sorted().compactMap { k -> String? in
                guard let v = describe(o[k]) else { return nil }
                return "\(k.replacingOccurrences(of: "_", with: " ")) \(v)"
            }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
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

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

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

struct PersonSheet: View {
    let target: PersonSheetTarget
    @State private var viewModel = PersonSheetViewModel()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    switch viewModel.state {
                    case .loading:
                        CavnarSkeletonLines(widths: [0.6, 1.0, 0.8, 0.9])
                    case .unavailable(let why):
                        Text(target.name)
                            .font(.cavnarHeadline(24))
                            .foregroundStyle(Color.cavnarInk)
                        Text(why)
                            .font(.cavnarBody(14.5))
                            .foregroundStyle(Color.cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    case .loaded(let person):
                        loaded(person)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Person")
        }
        .task { await viewModel.load(target) }
    }

    @ViewBuilder
    private func loaded(_ person: PersonRecord) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(person.name)
                .font(.cavnarHeadline(26))
                .foregroundStyle(Color.cavnarInk)
            HStack(spacing: 6) {
                if let role = person.role, !role.isEmpty { AccountChip(text: role) }
                AccountChip(text: person.active == false ? "Not active" : "Active", muted: person.active == false)
            }
        }

        AccountSection(kicker: "Scheduling") {
            kv("Hours", PersonRecord.describe(person.hours) ?? "Not set")
            kv("Availability", PersonRecord.describe(person.availability) ?? "Not set")
            kv("Rating", PersonRecord.describe(person.rating) ?? "Not rated")
            kv("Certifications", last: true,
                (person.certifications ?? []).isEmpty ? "None on file"
                                : (person.certifications ?? []).joined(separator: ", "))
        }

        AccountSection(kicker: "Login") {
            kv("PIN", last: true, person.pinSet == true ? "Set" : "Not set")
        }

        if !person.mayEdit("pay_rate", fallback: false) {
            AccountSection(kicker: "Pay") {
                kv("Pay rate", last: true, person.payRateLine ?? "Not set")
                Text("Rates are per role \u{2014} set them on the web, in Account \u{2192} Targets & pay rates.")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .padding(.top, 6)
            }
        }

        if person.canEdit == false {
            AccountSection(kicker: "Contact") {
                kv("Role", person.role?.isEmpty == false ? person.role! : "Not set")
                kv("Phone", person.phone?.isEmpty == false ? person.phone! : "Not set")
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
                    field("Email", text: $viewModel.email, keyboard: .emailAddress)
                    field("POS id", text: $viewModel.posId, keyboard: .asciiCapable)
                    if person.mayEdit("pay_rate", fallback: false) {
                        field("Pay rate ($/h)", text: $viewModel.payRate, keyboard: .decimalPad)
                    }
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
                        Text(message).font(.cavnarBody(14)).foregroundStyle(Color.cavnarGreen)
                    }
                    if let error = viewModel.saveError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func kv(_ label: String, last: Bool = false, _ value: String) -> some View {
        AccountKVRow(label: label, showsDivider: !last) {
            AccountValue(text: value, isNumber: value.first?.isNumber == true)
        }
    }

    private func field(_ label: String, text: Binding<String>, keyboard: UIKeyboardType) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.cavnarBody(13))
                .foregroundStyle(Color.cavnarInk3)
            TextField(label, text: text)
                .cavnarTextFieldStyle()
                .keyboardType(keyboard)
                .textInputAutocapitalization(keyboard == .emailAddress ? .never : .words)
                .autocorrectionDisabled()
        }
    }
}
