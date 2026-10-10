import SwiftUI
import Observation

// The owner's labor laws on the phone (schedule audit 10/10/26 RM-15, J1's
// engine — the web rules screen's compliance block): which laws hold the
// restaurant and why, the facts that decide it (confirmed here), the week a
// draft targets under the notice rule, where the owner's own rules are
// laxer than a law, and — for whoever sends schedules — predictability pay
// owed, the schedule estimates a law asks for, and the requests owed a
// written answer, each answered here. GET /mobile/api/labor/compliance and
// its POST twins. "Web explains. iPhone decides.": the library, the school
// calendar and the break tables stay on the web.

// MARK: - Payload

struct CompliancePayload: Decodable {
    struct Jurisdiction: Decodable, Identifiable, Hashable {
        var code: String
        var label: String
        var status: String
        var why: String?
        var id: String { code }
        enum CodingKeys: String, CodingKey { case code, label, status, why }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            code = c.sfText(.code) ?? UUID().uuidString
            label = c.sfText(.label) ?? code
            status = c.sfText(.status) ?? "unknown"
            why = c.sfText(.why)
        }
        var statusWord: String {
            switch status {
            case "applies": return "Applies"
            case "unknown": return "Needs an answer"
            default: return "Doesn\u{2019}t apply"
            }
        }
    }
    struct Question: Decodable, Hashable { var key: String?; var text: String? }
    struct Profile: Decodable {
        var state: String?
        var city: String?
        var employees: Int?
        var locations: Int?
        var employeesHere: Int?
        var kind: String?
        var confirmed: Bool?
        var franchise: Bool?
        enum CodingKeys: String, CodingKey {
            case state, city, employees, locations, kind, confirmed, franchise
            case employeesHere = "employees_here"
        }
    }
    struct Conflict: Decodable, Hashable { var text: String? }
    struct Horizon: Decodable {
        var weekStart: String?
        var weeksAhead: Int?
        var noticeDays: Int?
        enum CodingKeys: String, CodingKey {
            case weekStart = "week_start"
            case weeksAhead = "weeks_ahead"
            case noticeDays = "notice_days"
        }
    }
    struct Ledger: Decodable {
        struct Person: Decodable, Hashable { var employee: String?; var amount: Double? }
        var owed: Int?
        var total: Double?
        var byPerson: [Person]?
        enum CodingKeys: String, CodingKey {
            case owed, total
            case byPerson = "by_person"
        }
    }
    struct EstimatePerson: Decodable, Identifiable, Hashable {
        var employee: String
        var hasEstimate: Bool
        var isNew: Bool
        var id: String { employee }
        enum CodingKeys: String, CodingKey { case employee, estimate, new }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            employee = c.sfText(.employee) ?? ""
            hasEstimate = ((try? c.decodeNil(forKey: .estimate)) == false) && c.contains(.estimate)
            isNew = c.sfBool(.new) ?? false
        }
    }
    struct Estimates: Decodable {
        var law: String?
        var required: Bool?
        var people: [EstimatePerson]?
    }
    struct Request: Decodable, Identifiable, Hashable {
        var id: Int
        var employee: String
        var body: String
        var status: String
        var dueAt: String?
        var overdue: Bool
        enum CodingKeys: String, CodingKey {
            case id, body, status, overdue
            case employee = "employee_name"
            case dueAt = "due_at"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let i = c.sfInt(.id) else {
                throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "no id")
            }
            id = i
            employee = c.sfText(.employee) ?? ""
            body = c.sfText(.body) ?? ""
            status = c.sfText(.status) ?? "open"
            dueAt = c.sfText(.dueAt)
            overdue = c.sfBool(.overdue) ?? false
        }
    }
    struct StateChoice: Decodable, Hashable { var code: String; var label: String }

    var ok = false
    var error: String?
    var jurisdictions: [Jurisdiction] = []
    var questions: [Question] = []
    var profile: Profile?
    var horizon: Horizon?
    var conflicts: [Conflict] = []
    var restMode: String?
    var restHours: Double?
    var ledger: Ledger?
    var estimates: Estimates?
    var requests: [Request] = []
    var canEdit = false
    var canSend = false
    var states: [StateChoice] = []
    var kinds: [String] = []

    enum CodingKeys: String, CodingKey {
        case ok, error, evaluation, horizon, conflicts, rest, ledger, estimates, requests, states, kinds
        case canEdit = "can_edit"
        case canSend = "can_send"
    }
    enum EvalKeys: String, CodingKey { case jurisdictions, questions, profile }
    enum RestKeys: String, CodingKey { case mode, hours }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        error = c.sfText(.error)
        if let e = try? c.nestedContainer(keyedBy: EvalKeys.self, forKey: .evaluation) {
            jurisdictions = e.setupList(Jurisdiction.self, .jurisdictions)
            questions = e.setupList(Question.self, .questions)
            profile = (try? e.decodeIfPresent(Profile.self, forKey: .profile)) ?? nil
        }
        horizon = (try? c.decodeIfPresent(Horizon.self, forKey: .horizon)) ?? nil
        conflicts = c.setupList(Conflict.self, .conflicts)
        if let r = try? c.nestedContainer(keyedBy: RestKeys.self, forKey: .rest) {
            restMode = r.sfText(.mode); restHours = r.sfDouble(.hours)
        }
        ledger = (try? c.decodeIfPresent(Ledger.self, forKey: .ledger)) ?? nil
        estimates = (try? c.decodeIfPresent(Estimates.self, forKey: .estimates)) ?? nil
        requests = c.setupList(Request.self, .requests)
        canEdit = c.sfBool(.canEdit) ?? false
        canSend = c.sfBool(.canSend) ?? false
        states = c.setupList(StateChoice.self, .states)
        kinds = c.setupTexts(.kinds)
    }

    /// "Illinois and Chicago apply." / "Nothing beyond federal law applies."
    var headline: String {
        let on = jurisdictions.filter { $0.status == "applies" }.map(\.label)
        if on.isEmpty { return questions.isEmpty ? "No state or city scheduling law applies here." : "Answer the questions below to see which laws apply." }
        let names = on.count <= 2 ? on.joined(separator: " and ") : on.dropLast().joined(separator: ", ") + " and " + (on.last ?? "")
        return "\(names) \(on.count == 1 ? "applies" : "apply") here."
    }

    /// The open requests owed a written answer.
    var openRequests: [Request] { requests.filter { $0.status == "open" } }

    /// The people a law wants an estimate for who have none.
    var needEstimate: [EstimatePerson] {
        guard estimates?.required == true else { return [] }
        return (estimates?.people ?? []).filter { !$0.hasEstimate }
    }
}

// MARK: - Store

@Observable
@MainActor
final class ComplianceStore {
    var payload: CompliancePayload?
    var busy: String?
    var notes: [String: String] = [:]
    var error: String?
    /// Drafted estimates waiting for "Issue it", by person.
    var drafts: [String: [String: LooseValue]] = [:]

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        do {
            let p: CompliancePayload = try await client.send("/mobile/api/labor/compliance", hapticOnError: false)
            if p.ok { payload = p; error = nil } else { error = p.error ?? "The rules couldn\u{2019}t load." }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "The rules couldn\u{2019}t load."
        }
    }

    struct ProfileBody: Encodable {
        var state: String?
        var city: String?
        var employees: Int?
        var locations: Int?
        var employeesHere: Int?
        var kind: String?
        var franchise: Bool?
        enum CodingKeys: String, CodingKey {
            case state, city, employees, locations, kind, franchise
            case employeesHere = "employees_here"
        }
    }

    private struct Answer: Decodable {
        let ok: Bool
        let error: String?
        var estimate: [String: LooseValue]? = nil
    }

    func saveProfile(_ body: ProfileBody) async -> Bool {
        busy = "profile"
        defer { busy = nil }
        do {
            let r: Answer = try await client.send("/mobile/api/labor/compliance/profile", method: .post, body: body,
                                                  hapticOnError: false, retryTransient: false)
            if r.ok { Haptic.success(); await load(); return true }
            notes["profile"] = r.error ?? "That didn\u{2019}t save."
        } catch let e as APIClient.APIError {
            notes["profile"] = e.message
        } catch {
            notes["profile"] = "That didn\u{2019}t save."
        }
        return false
    }

    private struct RequestAnswerBody: Encodable { let decision: String; let answer: String }

    func answer(_ r: CompliancePayload.Request, decision: String, words: String) async {
        let key = "r\(r.id)"
        busy = key
        defer { busy = nil }
        do {
            let a: Answer = try await client.send("/mobile/api/labor/compliance/requests/\(r.id)/answer", method: .post,
                                                  body: RequestAnswerBody(decision: decision, answer: words),
                                                  hapticOnError: false, retryTransient: false)
            if a.ok { Haptic.success(); await load() } else { notes[key] = a.error ?? "That didn\u{2019}t save." }
        } catch let e as APIClient.APIError {
            notes[key] = e.message
        } catch {
            notes[key] = "That didn\u{2019}t save."
        }
    }

    private struct DraftBody: Encodable { let employee: String }
    private struct IssueBody: Encodable { let estimate: [String: LooseValue] }

    /// The estimate as the server drafts it from the person's weeks —
    /// nothing stored until "Issue it".
    func draft(_ name: String) async {
        let key = "e\(name)"
        busy = key
        defer { busy = nil }
        do {
            let a: Answer = try await client.send("/mobile/api/labor/compliance/estimates/draft", method: .post,
                                                  body: DraftBody(employee: name), hapticOnError: false,
                                                  retryTransient: false)
            if a.ok, let e = a.estimate { drafts[name] = e } else { notes[key] = a.error ?? "It couldn\u{2019}t be drafted." }
        } catch let e as APIClient.APIError {
            notes[key] = e.message
        } catch {
            notes[key] = "It couldn\u{2019}t be drafted."
        }
    }

    func issue(_ name: String) async {
        guard let est = drafts[name] else { return }
        let key = "e\(name)"
        busy = key
        defer { busy = nil }
        do {
            let a: Answer = try await client.send("/mobile/api/labor/compliance/estimates", method: .post,
                                                  body: IssueBody(estimate: est), hapticOnError: false,
                                                  retryTransient: false)
            if a.ok {
                drafts[name] = nil
                notes[key] = "Issued \u{2014} \(name) signs for it in the app"
                Haptic.success()
                await load()
            } else {
                notes[key] = a.error ?? "It couldn\u{2019}t be issued."
            }
        } catch let e as APIClient.APIError {
            notes[key] = e.message
        } catch {
            notes[key] = "It couldn\u{2019}t be issued."
        }
    }
}

// MARK: - Screen

struct ComplianceScreen: View {
    @State private var store = ComplianceStore()
    @State private var editingProfile = false
    @State private var answering: Int?
    @State private var answerWords = ""
    @State private var decision = "granted"

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if let p = store.payload {
                        content(p)
                    } else if let error = store.error {
                        ScheduleNotice(text: error, tone: .cavnarRed) {
                            Button { Task { await store.load() } } label: { Text("Try again").frame(maxWidth: .infinity) }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    } else {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6])
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Labor laws")
        }
        .task { await store.load() }
        .sheet(isPresented: $editingProfile) {
            if let p = store.payload { ComplianceProfileSheet(store: store, payload: p) }
        }
    }

    @ViewBuilder
    private func content(_ p: CompliancePayload) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Where you are")
            CavnarMixedText(p.headline, role: .lead, color: .cavnarInk)
            if let h = p.horizon, let w = h.weekStart {
                CavnarMixedText("Drafts target the week of \(CavnarDate.mdy(w))"
                                + (h.noticeDays.map { ": your \($0)-day notice rule makes the nearer weeks live, and changes to them are priced." } ?? "."),
                                role: .secondary)
            }
        }
        ForEach(p.questions, id: \.self) { q in
            if let t = q.text { ScheduleNotice(text: t, tone: .cavnarAmber, symbol: "questionmark.circle.fill") }
        }
        if !p.jurisdictions.isEmpty {
            VStack(spacing: 0) {
                ForEach(p.jurisdictions) { j in
                    VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(j.label).cavnarText(.label)
                            Spacer(minLength: CavnarSpace.xs)
                            AccountChip(text: j.statusWord, muted: j.status != "applies",
                                        tint: j.status == "unknown" ? .cavnarAmber : nil)
                        }
                        if let why = j.why { CavnarMixedText(why, role: .secondary) }
                    }
                    .padding(.vertical, CavnarSpace.xs)
                    if j.id != p.jurisdictions.last?.id {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            .cavnarCard()
        }
        if p.canEdit {
            if p.profile?.confirmed == true {
                Button { Haptic.light(); editingProfile = true } label: {
                    Text("Change where you are and your size").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            } else {
                Button { Haptic.light(); editingProfile = true } label: {
                    Text("Confirm where you are and your size").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
            }
        }
        ForEach(p.conflicts, id: \.self) { c in
            if let t = c.text { ScheduleNotice(text: t, tone: .cavnarAmber) }
        }
        if p.restMode == "consent_premium", let h = p.restHours {
            CavnarMixedText("Rest between shifts: \(String(format: "%g", h)) hours, or less with the person\u{2019}s written consent, paid the premium.",
                            role: .secondary)
        }
        if p.canSend { sendersPart(p) }
        CavnarWebLinkRow(title: "Every rule in force, breaks and the school calendar", path: "labor/schedule",
                         actionLabel: "Open on the web")
    }

    @ViewBuilder
    private func sendersPart(_ p: CompliancePayload) -> some View {
        if let l = p.ledger, let owed = l.owed, owed > 0 {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Predictability pay owed")
                CavnarMixedText("$\((l.total ?? 0).commaFormatted) across \(owed) change\(owed == 1 ? "" : "s") in the last 90 days.",
                                role: .lead, color: .cavnarInk)
                ForEach((l.byPerson ?? []).prefix(3), id: \.self) { person in
                    CavnarMixedText("\(person.employee ?? "") \u{00B7} $\((person.amount ?? 0).commaFormatted)", role: .secondary)
                }
                Text("The payroll export is on the web.").cavnarText(.caption)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
        if !p.openRequests.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("Owed a written answer", tint: .cavnarAmber)
                ForEach(p.openRequests) { r in requestRow(r) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
        let need = p.needEstimate
        if !need.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                CavnarKicker("Schedule estimates")
                CavnarMixedText("\(p.estimates?.law ?? "The law") asks for a good-faith estimate for each person. \(need.count) \(need.count == 1 ? "has" : "have") none.",
                                role: .body)
                ForEach(need.prefix(8)) { person in estimateRow(person.employee) }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    private func requestRow(_ r: CompliancePayload.Request) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarMixedText("\(r.employee): \u{201C}\(r.body)\u{201D}", role: .body, color: .cavnarInk)
            if let due = r.dueAt {
                CavnarMixedText("Due \(CavnarDate.mdy(due))" + (r.overdue ? " \u{00B7} overdue" : ""),
                                role: .secondary, color: r.overdue ? .cavnarRedText : .cavnarInk2)
            }
            if answering == r.id {
                CavnarSegmentedControl(selection: $decision, options: ["granted", "modified", "declined"],
                                       accessibilityTitle: "Your answer") { $0.capitalized }
                TextField("Your written answer", text: $answerWords, axis: .vertical)
                    .font(.cavnar(.body))
                    .lineLimit(2...5)
                    .padding(CavnarSpace.s)
                    .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                        .fill(Color.cavnarPaper2))
                Button {
                    let words = answerWords.trimmingCharacters(in: .whitespacesAndNewlines)
                    Task {
                        await store.answer(r, decision: decision, words: words)
                        answering = nil
                        answerWords = ""
                    }
                } label: { Text("Send the answer").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: answerWords.trimmingCharacters(in: .whitespaces).isEmpty))
                    .disabled(store.busy != nil || answerWords.trimmingCharacters(in: .whitespaces).isEmpty)
            } else {
                Button {
                    answering = r.id
                    answerWords = ""
                    decision = "granted"
                } label: { Text("Answer in writing").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarSecondaryButtonStyle())
            }
            if let n = store.notes["r\(r.id)"] { CavnarMixedText(n, role: .secondary, color: .cavnarRedText) }
        }
    }

    private func estimateRow(_ name: String) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            Text(name).cavnarText(.label)
            if let draft = store.drafts[name] {
                if case .string(let text)? = draft["text"] { CavnarMixedText(text, role: .secondary) }
                Button { Task { await store.issue(name) } } label: { Text("Issue it").frame(maxWidth: .infinity) }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.busy != nil))
                    .disabled(store.busy != nil)
            } else {
                Button { Task { await store.draft(name) } } label: {
                    Group {
                        if store.busy == "e\(name)" { CavnarShimmerText(text: "Drafting\u{2026}") } else { Text("Draft from their weeks") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(store.busy != nil)
            }
            if let n = store.notes["e\(name)"] { CavnarMixedText(n, role: .secondary) }
        }
    }
}

/// Where the restaurant is and how big the business is — the facts every
/// law's coverage turns on; the account holder confirms them.
struct ComplianceProfileSheet: View {
    let store: ComplianceStore
    let payload: CompliancePayload
    @Environment(\.dismiss) private var dismiss
    @State private var state = ""
    @State private var city = ""
    @State private var employees = ""
    @State private var locations = ""
    @State private var employeesHere = ""
    @State private var kind = ""
    @State private var franchise = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    AccountSection(kicker: "Where you are") {
                        AccountKVRow(label: "State") {
                            Menu {
                                ForEach(payload.states, id: \.self) { s in
                                    Button(s.label) { state = s.code }
                                }
                            } label: {
                                Text(payload.states.first { $0.code == state }?.label ?? (state.isEmpty ? "Pick" : state))
                                    .cavnarText(.label, color: .cavnarEmber2)
                            }
                        }
                        field("City", text: $city, number: false)
                    }
                    AccountSection(kicker: "How big the business is") {
                        field("Employees, all locations", text: $employees, number: true)
                        field("Locations", text: $locations, number: true)
                        field("Employees at this one", text: $employeesHere, number: true)
                        AccountSwitchRow(label: "A franchise", isOn: $franchise)
                        AccountKVRow(label: "Kind", showsDivider: false) {
                            Menu {
                                ForEach(payload.kinds, id: \.self) { k in
                                    Button(k.replacingOccurrences(of: "_", with: " ").capitalized) { kind = k }
                                }
                            } label: {
                                Text(kind.isEmpty ? "Pick" : kind.replacingOccurrences(of: "_", with: " ").capitalized)
                                    .cavnarText(.label, color: .cavnarEmber2)
                            }
                        }
                    }
                    if let n = store.notes["profile"] { CavnarMixedText(n, role: .secondary, color: .cavnarRedText) }
                    Button {
                        Task {
                            let ok = await store.saveProfile(.init(state: state.isEmpty ? nil : state,
                                                                   city: city.isEmpty ? nil : city,
                                                                   employees: Int(employees), locations: Int(locations),
                                                                   employeesHere: Int(employeesHere),
                                                                   kind: kind.isEmpty ? nil : kind,
                                                                   franchise: franchise))
                            if ok { dismiss() }
                        }
                    } label: {
                        Group {
                            if store.busy == "profile" { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Confirm") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.busy != nil || state.isEmpty))
                    .disabled(store.busy != nil || state.isEmpty)
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("Where you are")
        }
        .onAppear {
            let p = payload.profile
            state = p?.state ?? ""
            city = p?.city ?? ""
            employees = p?.employees.map(String.init) ?? ""
            locations = p?.locations.map(String.init) ?? ""
            employeesHere = p?.employeesHere.map(String.init) ?? ""
            kind = p?.kind ?? ""
            franchise = p?.franchise ?? false
        }
    }

    private func field(_ label: String, text: Binding<String>, number: Bool) -> some View {
        AccountKVRow(label: label) {
            TextField("\u{2014}", text: text)
                .font(number ? .cavnar(.figureS) : .cavnar(.body))
                .multilineTextAlignment(.trailing)
                .keyboardType(number ? .numberPad : .default)
                .frame(maxWidth: 160)
        }
    }
}
