import SwiftUI
import Observation
import UniformTypeIdentifiers

// The owner's report settings on the phone (parity audit #64) and the two
// owner tools the report links to — mapping a POS department (#33) and the
// week's budget (#63). Every route is the web's, on its /mobile/api twin:
//
//   GET  /dsr/settings          the switch, notify, the deadline, gross
//                               basis, late night, the category map, what the
//                               latest night left unmapped (owner only — 403
//                               for anyone else)
//   POST /dsr/settings          one field at a time, saved on change
//   POST /dsr/category          {pos_name, category}
//   GET  /dsr/budget/prefill    ?start&from&pct&days — fills, never saves
//   POST /dsr/budget            {days: [{date, gross, net}]}
//
// The fiscal calendar stays on the web: the phone says where to set it.

// MARK: - Models

struct DSRSettingsPayload: Decodable {
    struct Settings: Decodable {
        let dsrEnabled: Bool
        let dsrNotify: Bool
        let dsrGrossBasis: String
        let dsrDeadlineHour: Int
        let dsrLateNightHour: Int?
        let calendarLabel: String?
        let fiscalYearStart: String?

        enum CodingKeys: String, CodingKey {
            case dsrEnabled = "dsr_enabled"
            case dsrNotify = "dsr_notify"
            case dsrGrossBasis = "dsr_gross_basis"
            case dsrDeadlineHour = "dsr_deadline_hour"
            case dsrLateNightHour = "dsr_late_night_hour"
            case calendarLabel = "calendar_label"
            case fiscalYearStart = "fiscal_year_start"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            dsrEnabled = (try? c.decode(Bool.self, forKey: .dsrEnabled)) ?? true
            dsrNotify = (try? c.decode(Bool.self, forKey: .dsrNotify)) ?? false
            dsrGrossBasis = (try? c.decode(String.self, forKey: .dsrGrossBasis)) ?? "items"
            dsrDeadlineHour = (try? c.decode(Int.self, forKey: .dsrDeadlineHour)) ?? 4
            dsrLateNightHour = (try? c.decodeIfPresent(Int.self, forKey: .dsrLateNightHour)) ?? nil
            calendarLabel = (try? c.decodeIfPresent(String.self, forKey: .calendarLabel)) ?? nil
            fiscalYearStart = (try? c.decodeIfPresent(String.self, forKey: .fiscalYearStart)) ?? nil
        }
    }

    struct MapRow: Decodable, Hashable, Identifiable {
        let posName: String
        let category: String
        var id: String { posName }
        enum CodingKeys: String, CodingKey { case category; case posName = "pos_name" }
    }

    struct UnmappedRow: Decodable, Hashable {
        let department: String
        let net: Double?
        let newIn: String?
        let mappedTo: String?
        enum CodingKeys: String, CodingKey {
            case department, net
            case newIn = "new_in"
            case mappedTo = "mapped_to"
        }
        var asUnmapped: DSRBlock.Unmapped {
            DSRBlock.Unmapped(department: department, net: net, newIn: newIn, mappedTo: mappedTo, posCategory: nil)
        }
    }

    let ok: Bool
    let settings: Settings?
    let categories: [String]
    let categoryMap: [MapRow]
    let held: [String: [String]]
    let contents: [String: [String]]
    let unmapped: [UnmappedRow]
    let unmappedAsOf: String?

    enum CodingKeys: String, CodingKey {
        case ok, settings, categories, held, contents, unmapped
        case categoryMap = "category_map"
        case unmappedAsOf = "unmapped_as_of"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        settings = (try? c.decodeIfPresent(Settings.self, forKey: .settings)) ?? nil
        categories = (try? c.decodeIfPresent([String].self, forKey: .categories)) ?? []
        categoryMap = (try? c.decodeIfPresent([MapRow].self, forKey: .categoryMap)) ?? []
        held = (try? c.decodeIfPresent([String: [String]].self, forKey: .held)) ?? [:]
        contents = (try? c.decodeIfPresent([String: [String]].self, forKey: .contents)) ?? [:]
        unmapped = (try? c.decodeIfPresent([UnmappedRow].self, forKey: .unmapped)) ?? []
        unmappedAsOf = (try? c.decodeIfPresent(String.self, forKey: .unmappedAsOf)) ?? nil
    }

    /// "holds Darts, Pool and 2 more" — what a mapped department holds, so
    /// "Other" reads as what it is; nil when it only holds itself.
    func holds(_ posName: String) -> String? {
        let h = held[posName] ?? contents[posName] ?? []
        guard !h.isEmpty, !(h.count == 1 && h[0].lowercased() == posName.lowercased()) else { return nil }
        return "holds " + h.prefix(3).joined(separator: ", ") + (h.count > 3 ? " and \(h.count - 3) more" : "")
    }
}

/// One field of POST /dsr/settings. A field the server reads as "clear"
/// (late night back to the POS's meal periods) is sent as an explicit null.
struct DSRSettingBody: Encodable {
    let field: String
    let value: Value

    enum Value: Equatable {
        case bool(Bool)
        case int(Int)
        case string(String)
        case null
    }

    private struct Key: CodingKey {
        var stringValue: String
        var intValue: Int? { nil }
        init(stringValue: String) { self.stringValue = stringValue }
        init?(intValue: Int) { nil }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: Key.self)
        let k = Key(stringValue: field)
        switch value {
        case .bool(let b): try c.encode(b, forKey: k)
        case .int(let i): try c.encode(i, forKey: k)
        case .string(let s): try c.encode(s, forKey: k)
        case .null: try c.encodeNil(forKey: k)
        }
    }
}

/// POST /dsr/category — the department as the POS names it and the
/// category it counts toward.
struct DSRCategoryBody: Encodable, Equatable {
    let posName: String
    let category: String
    enum CodingKeys: String, CodingKey { case category; case posName = "pos_name" }
}

// MARK: - Settings view model

@Observable
@MainActor
final class DSRSettingsViewModel {
    private(set) var payload: DSRSettingsPayload?
    private(set) var isLoading = false
    var errorMessage: String?
    var savedLine: String?
    private(set) var saving: Set<String> = []

    // The editable copies, reconciled with the server on each save.
    var enabled = true
    var notify = false
    var grossBasis = "items"
    var deadlineHour = 4
    var lateNightHour: Int? = nil

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private struct SaveResponse: Decodable { let ok: Bool; let error: String?; let settings: DSRSettingsPayload.Settings? }
    private struct CategoryResponse: Decodable {
        let ok: Bool
        let error: String?
        let note: String?
        let posName: String?
        let category: String?
        enum CodingKeys: String, CodingKey { case ok, error, note, category; case posName = "pos_name" }
    }

    func load() async {
        isLoading = payload == nil
        defer { isLoading = false }
        do {
            let r: DSRSettingsPayload = try await client.send("/mobile/api/dsr/settings", hapticOnError: false)
            payload = r
            if let s = r.settings { adopt(s) }
            errorMessage = nil
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t load the report\u{2019}s settings."
        }
    }

    private func adopt(_ s: DSRSettingsPayload.Settings) {
        enabled = s.dsrEnabled
        notify = s.dsrNotify
        grossBasis = s.dsrGrossBasis
        deadlineHour = s.dsrDeadlineHour
        lateNightHour = s.dsrLateNightHour
        DSRAvailability.record(s.dsrEnabled)
    }

    /// Saves one field; on a refusal the stored settings come back.
    func save(_ field: String, _ value: DSRSettingBody.Value) async {
        saving.insert(field)
        defer { saving.remove(field) }
        savedLine = nil
        do {
            let r: SaveResponse = try await client.send("/mobile/api/dsr/settings", method: .post,
                                                        body: DSRSettingBody(field: field, value: value),
                                                        retryTransient: false)
            if r.ok {
                if let s = r.settings { adopt(s) }
                savedLine = "Saved"
                Haptic.success()
            } else {
                errorMessage = r.error ?? "Couldn\u{2019}t save that."
                await load()
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
            await load()
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
            await load()
        }
    }

    /// Maps a POS department; returns the server's note on success.
    func map(_ department: String, to category: String) async -> String? {
        let cat = category.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cat.isEmpty else { errorMessage = "Pick a category."; return nil }
        saving.insert("category:" + department)
        defer { saving.remove("category:" + department) }
        do {
            let r: CategoryResponse = try await client.send("/mobile/api/dsr/category", method: .post,
                                                            body: DSRCategoryBody(posName: department, category: cat),
                                                            retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that category."; return nil }
            Haptic.success()
            await load()
            return r.note ?? "\(department) counts as \(r.category ?? cat) from the next report on."
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t save that category."
        }
        return nil
    }
}

// MARK: - The settings sheet

struct DSRSettingsSheet: View {
    @State private var viewModel = DSRSettingsViewModel()
    @State private var otherFor: String?
    @State private var otherText = ""
    @State private var mapNote: String?

    /// The web's deadline hours: 1am to 10am.
    static let deadlineHours = Array(1...10)
    static let lateNightHours = Array(18...23)

    static func hourWords(_ h: Int) -> String {
        let h12 = h % 12 == 0 ? 12 : h % 12
        return "\(h12)\(h < 12 ? "am" : "pm")"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if viewModel.isLoading {
                        CavnarSkeletonLines(widths: [1, 0.8, 0.9, 0.6]).cavnarCard()
                    } else if let p = viewModel.payload {
                        reportSection
                        calendarSection(p)
                        departmentsSection(p)
                    } else if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.body)
                            .fixedSize(horizontal: false, vertical: true)
                            .cavnarCard()
                    }
                    if viewModel.payload != nil, let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let note = mapNote {
                        HomeMixedText.make(note, role: .label, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Daily report settings")
            .task { await viewModel.load() }
            .alert("Another category", isPresented: Binding(get: { otherFor != nil }, set: { if !$0 { otherFor = nil } })) {
                TextField("Your category", text: $otherText)
                Button("Save") {
                    guard let dept = otherFor else { return }
                    let cat = otherText
                    Task { mapNote = await viewModel.map(dept, to: cat) }
                    otherFor = nil
                }
                Button("Cancel", role: .cancel) { otherFor = nil }
            } message: {
                Text(otherFor.map { "What should \($0) count toward?" } ?? "")
            }
        }
    }

    private var reportSection: some View {
        AccountSection(kicker: "The report") {
            AccountSwitchRow(label: "Build the report every night",
                             detail: viewModel.enabled ? nil : DSRAvailability.offLine,
                             isOn: Binding(get: { viewModel.enabled }, set: { v in
                                 viewModel.enabled = v
                                 Task { await viewModel.save("dsr_enabled", .bool(v)) }
                             }),
                             busy: viewModel.saving.contains("dsr_enabled"))
            AccountSwitchRow(label: "Send it when it\u{2019}s ready",
                             detail: "Email and a push to the owner as soon as the night is final.",
                             isOn: Binding(get: { viewModel.notify }, set: { v in
                                 viewModel.notify = v
                                 Task { await viewModel.save("dsr_notify", .bool(v)) }
                             }),
                             busy: viewModel.saving.contains("dsr_notify"))
            AccountKVRow(label: "Finish by") {
                Picker("Finish by", selection: Binding(get: { viewModel.deadlineHour }, set: { h in
                    viewModel.deadlineHour = h
                    Task { await viewModel.save("dsr_deadline_hour", .int(h)) }
                })) {
                    // A stored hour outside the web's list stays choosable.
                    ForEach(Array(Set(Self.deadlineHours + [viewModel.deadlineHour])).sorted(), id: \.self) { h in
                        Text(Self.hourWords(h)).tag(h)
                    }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
            }
            AccountKVRow(label: "Gross is") {
                Picker("Gross is", selection: Binding(get: { viewModel.grossBasis }, set: { b in
                    viewModel.grossBasis = b
                    Task { await viewModel.save("dsr_gross_basis", .string(b)) }
                })) {
                    Text("Items only").tag("items")
                    Text("Everything rung").tag("all")
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
            }
            AccountKVRow(label: "Late night starts", showsDivider: false) {
                Picker("Late night starts", selection: Binding(get: { viewModel.lateNightHour ?? 0 }, set: { h in
                    viewModel.lateNightHour = h == 0 ? nil : h
                    Task { await viewModel.save("dsr_late_night_hour", h == 0 ? .null : .int(h)) }
                })) {
                    Text("The POS\u{2019}s meal periods").tag(0)
                    ForEach(Self.lateNightHours, id: \.self) { h in Text(Self.hourWords(h)).tag(h) }
                }
                .pickerStyle(.menu)
                .tint(Color.cavnarEmber2)
            }
        }
        .overlay(alignment: .topTrailing) {
            if let saved = viewModel.savedLine {
                Text(saved).font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarGreen)
            }
        }
    }

    private func calendarSection(_ p: DSRSettingsPayload) -> some View {
        AccountSection(kicker: "Your calendar") {
            VStack(alignment: .leading, spacing: 6) {
                if let label = p.settings?.calendarLabel, p.settings?.fiscalYearStart != nil {
                    HomeMixedText.make("Today falls in \(label).", role: .label)
                } else {
                    Text("No fiscal calendar yet \u{2014} the report has weeks but no period numbers.")
                        .cavnarText(.body)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text("Set your calendar on the web: Account \u{2192} Daily report.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.vertical, 10)
        }
    }

    private func departmentsSection(_ p: DSRSettingsPayload) -> some View {
        AccountSection(kicker: "POS departments") {
            if p.unmapped.isEmpty && p.categoryMap.isEmpty {
                Text("No POS departments yet. They appear here after the first night the POS reports its departments.")
                    .cavnarText(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.vertical, 10)
            }
            ForEach(Array(p.unmapped.enumerated()), id: \.element.department) { i, u in
                departmentRow(name: u.department,
                              detail: DSRCategoryMapSheet.why(u.asUnmapped)
                                + (p.unmappedAsOf.map { " on \($0)" } ?? ""),
                              current: nil, categories: p.categories,
                              showsDivider: i < p.unmapped.count - 1 || !p.categoryMap.isEmpty)
            }
            ForEach(Array(p.categoryMap.enumerated()), id: \.element.id) { i, m in
                departmentRow(name: m.posName,
                              detail: "Counts toward \(m.category)" + (p.holds(m.posName).map { " \u{00B7} \($0)" } ?? ""),
                              current: m.category, categories: p.categories,
                              showsDivider: i < p.categoryMap.count - 1)
            }
        }
    }

    private func departmentRow(name: String, detail: String, current: String?, categories: [String],
                               showsDivider: Bool) -> some View {
        VStack(spacing: 0) {
            HStack(alignment: .center, spacing: 12) {
                VStack(alignment: .leading, spacing: 3) {
                    Text(name).cavnarText(.label)
                    HomeMixedText.make(detail, role: .caption, color: current == nil ? .cavnarAmber : .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                Menu {
                    ForEach(Array(Set(categories + (current.map { [$0] } ?? []))).sorted(), id: \.self) { c in
                        Button {
                            guard c != current else { return }
                            Task { mapNote = await viewModel.map(name, to: c) }
                        } label: {
                            if c == current { Label(c, systemImage: "checkmark") } else { Text(c) }
                        }
                    }
                    Button("Another category\u{2026}") {
                        otherText = ""
                        otherFor = name
                    }
                } label: {
                    HStack(spacing: 4) {
                        Text(current ?? "Pick").font(.cavnarBody(CavnarType.secondary, weight: 700))
                        Image(systemName: "chevron.up.chevron.down").font(.system(size: 10, weight: .bold))
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(minHeight: 44)
                }
                .disabled(viewModel.saving.contains("category:" + name))
            }
            .padding(.vertical, 6)
            if showsDivider { AccountRowDivider() }
        }
    }
}

// MARK: - Map one department

/// "Map it" from the report: one unmapped department, the owner's
/// categories, and "Another category…". Saved on the pick; the report says
/// it counts from the next night on (the nights already reported keep
/// their split).
struct DSRCategoryMapSheet: View {
    let department: DSRBlock.Unmapped
    @State private var viewModel = DSRSettingsViewModel()
    @State private var other = ""
    @State private var note: String?
    @Environment(\.dismiss) private var dismiss

    /// Why a department is unplaced, in the web's words.
    static func why(_ u: DSRBlock.Unmapped) -> String {
        if let inside = u.newIn {
            return "New inside \(inside), which counts toward \(u.mappedTo ?? "one of your categories") \u{2014} not counted there until you pick"
        }
        return "Not mapped to a category yet, so it shows on its own \u{2014} never guessed"
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 6) {
                        DSRKicker(text: "POS department")
                        Text(department.department).cavnarText(.headline)
                        HomeMixedText.make(Self.why(department) + (department.net.map { " \u{00B7} \(DSRFormat.money($0)) that night" } ?? "") + ".",
                                           role: .secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let note {
                        HomeMixedText.make(note, role: .label, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                        Button("Done") { dismiss() }.buttonStyle(CavnarPrimaryButtonStyle())
                    } else if viewModel.isLoading {
                        CavnarSkeletonLines(widths: [1, 0.8, 0.6])
                    } else if let p = viewModel.payload {
                        AccountSection(kicker: "Counts toward") {
                            ForEach(Array(p.categories.enumerated()), id: \.element) { i, c in
                                Button {
                                    Haptic.light()
                                    Task { note = await viewModel.map(department.department, to: c) }
                                } label: {
                                    AccountKVRow(label: c, showsDivider: i < p.categories.count - 1) {
                                        if department.mappedTo == c {
                                            Text("where it counts now").cavnarText(.caption, color: .cavnarInk2)
                                        }
                                        AccountDisclosureChip()
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .disabled(!viewModel.saving.isEmpty)
                            }
                        }
                        VStack(alignment: .leading, spacing: 8) {
                            AccountKicker(text: "Or your own")
                            HStack(spacing: 10) {
                                TextField("Another category", text: $other)
                                    .cavnarTextFieldStyle()
                                    .submitLabel(.done)
                                Button {
                                    Task { note = await viewModel.map(department.department, to: other) }
                                } label: { Text("Save") }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                                .disabled(other.trimmingCharacters(in: .whitespaces).isEmpty || !viewModel.saving.isEmpty)
                            }
                        }
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Map a department")
            .task { await viewModel.load() }
        }
    }
}

// MARK: - The week's budget (parity audit #63)

struct DSRBudgetPrefill: Decodable {
    struct Day: Decodable {
        let date: String
        let gross: Double?
        let net: Double?
        let from: String?
    }
    let ok: Bool
    let days: [Day]
    let missing: [String]
    let basis: String?
    let error: String?

    enum CodingKeys: String, CodingKey { case ok, days, missing, basis, error }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
        days = (try? c.decodeIfPresent([Day].self, forKey: .days)) ?? []
        missing = (try? c.decodeIfPresent([String].self, forKey: .missing)) ?? []
        basis = (try? c.decodeIfPresent(String.self, forKey: .basis)) ?? nil
        error = (try? c.decodeIfPresent(String.self, forKey: .error)) ?? nil
    }
}

/// POST /dsr/budget — every night with both figures; a blank is null,
/// which clears that night's budget (nothing carries over on its own).
struct DSRBudgetBody: Encodable {
    struct Day: Encodable, Equatable {
        let date: String
        let gross: Double?
        let net: Double?
        func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(date, forKey: .date)
            try c.encode(gross, forKey: .gross)
            try c.encode(net, forKey: .net)
        }
        enum CodingKeys: String, CodingKey { case date, gross, net }
    }
    let days: [Day]
}

@Observable
@MainActor
final class DSRBudgetViewModel {
    struct Row: Identifiable, Equatable {
        let date: String
        let weekday: String?
        var gross: String
        var net: String
        var id: String { date }
    }

    enum Source: String, CaseIterable, Identifiable {
        case lastWeek = "last_week"
        case lastYear = "last_year"
        case forecast = "forecast"
        var id: String { rawValue }
        var label: String {
            switch self {
            case .lastWeek: return "Same as last week"
            case .lastYear: return "Last year"
            case .forecast: return "Cavnar AI\u{2019}s forecast"
            }
        }
    }

    var rows: [Row]
    var lastYearPct = 0
    var isFilling = false
    var isSaving = false
    var fillLine: String?
    var errorMessage: String?
    var saved = false

    private let client: APIClient

    init(days: [DSRGridDay], client: APIClient = .shared) {
        self.client = client
        rows = days.map { d in
            Row(date: d.date, weekday: d.weekday ?? DSRFormat.weekday(d.date),
                gross: Self.text(d.budgetGross), net: Self.text(d.budgetNet))
        }
    }

    static func text(_ v: Double?) -> String {
        guard let v else { return "" }
        return v == v.rounded() ? String(Int(v)) : String(format: "%.2f", v)
    }

    /// A typed figure: blank is nil (no budget); anything else must be a
    /// dollar amount, $0 or more. `bad` when it isn't.
    static func parse(_ s: String) -> (value: Double?, bad: Bool) {
        let t = s.replacingOccurrences(of: "$", with: "").replacingOccurrences(of: ",", with: "")
            .trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return (nil, false) }
        guard let v = Double(t), v.isFinite, v >= 0 else { return (nil, true) }
        return (v, false)
    }

    /// The body Save posts, or nil with `errorMessage` set.
    func body() -> DSRBudgetBody? {
        var days: [DSRBudgetBody.Day] = []
        for r in rows {
            let g = Self.parse(r.gross), n = Self.parse(r.net)
            if g.bad || n.bad {
                errorMessage = "Budgets are dollar amounts, $0 or more."
                return nil
            }
            days.append(.init(date: r.date, gross: g.value, net: n.value))
        }
        return DSRBudgetBody(days: days)
    }

    /// Fills the boxes from a source; nothing is saved. A filled night takes
    /// this source alone — a figure it has none for is cleared, never kept
    /// from another source (F2-19).
    func prefill(_ source: Source) async {
        guard let start = rows.first?.date else { return }
        isFilling = true
        fillLine = nil
        errorMessage = nil
        defer { isFilling = false }
        var query = ["start": start, "from": source.rawValue, "days": String(rows.count)]
        if source == .lastYear { query["pct"] = String(lastYearPct) }
        do {
            let r: DSRBudgetPrefill = try await client.send("/mobile/api/dsr/budget/prefill", query: query,
                                                            hapticOnError: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t fill the budget."; return }
            var filled = 0
            for d in r.days where d.from != nil {
                guard let i = rows.firstIndex(where: { $0.date == d.date }) else { continue }
                filled += 1
                rows[i].gross = d.gross.map { String(Int($0.rounded())) } ?? ""
                rows[i].net = d.net.map { String(Int($0.rounded())) } ?? ""
            }
            if filled == 0 {
                fillLine = "Nothing to go on for these nights yet."
            } else {
                var s = r.basis ?? "Filled."
                if !r.missing.isEmpty {
                    s += " \(r.missing.count) night\(r.missing.count == 1 ? "" : "s") had nothing to go on."
                }
                fillLine = s + " Not saved yet."
                Haptic.selection()
            }
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "Couldn\u{2019}t reach Cavnar AI."
        }
    }

    func save() async -> Bool {
        guard let body = body() else { return false }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let r: APIClient.OKResponse = try await client.send("/mobile/api/dsr/budget", method: .post, body: body,
                                                                retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save the budget."; return false }
            saved = true
            Haptic.success()
            return true
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch is CancellationError {
        } catch {
            errorMessage = "The answer didn\u{2019}t arrive \u{2014} reload the week to see what saved."
        }
        return false
    }
}

/// The week's budget, night by night: gross and net the way the owner's
/// sheet has them, a "Start from" that fills the boxes, and nothing saved
/// until Save.
struct DSRBudgetSheet: View {
    @State private var viewModel: DSRBudgetViewModel
    var onSaved: () -> Void = {}
    @Environment(\.dismiss) private var dismiss
    @FocusState private var focus: String?

    init(days: [DSRGridDay], onSaved: @escaping () -> Void = {}) {
        _viewModel = State(initialValue: DSRBudgetViewModel(days: days))
        self.onSaved = onSaved
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("Gross and net for each night. Leave a night blank for no budget \u{2014} nothing carries over on its own.")
                        .cavnarText(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    startFrom
                    VStack(spacing: 0) {
                        HStack {
                            Text("NIGHT").frame(maxWidth: .infinity, alignment: .leading)
                            Text("GROSS").frame(width: 104, alignment: .leading)
                            Text("NET").frame(width: 104, alignment: .leading)
                        }
                        .font(.cavnarBody(CavnarType.kicker, weight: 700)).tracking(1).foregroundStyle(Color.cavnarInk2)
                        .padding(.bottom, 6)
                        ForEach($viewModel.rows) { $row in
                            HStack(spacing: 8) {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(row.weekday ?? "").cavnarText(.label)
                                    Text(CavnarDate.mdy(row.date)).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                field("Gross", $row.gross, id: row.date + "g")
                                field("Net", $row.net, id: row.date + "n")
                            }
                            .padding(.vertical, 6)
                            AccountRowDivider()
                        }
                    }
                    .cavnarCard()
                    if let line = viewModel.fillLine {
                        HomeMixedText.make(line, role: .caption, color: .cavnarInk2).fixedSize(horizontal: false, vertical: true)
                    }
                    if let error = viewModel.errorMessage {
                        Text(error).cavnarText(.secondary, color: .cavnarRedText)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Button {
                        focus = nil
                        Task {
                            if await viewModel.save() {
                                onSaved()
                                dismiss()
                            }
                        }
                    } label: {
                        Group {
                            if viewModel.isSaving { CavnarShimmerText(text: "Saving") } else { Text("Save budget") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isSaving))
                    .disabled(viewModel.isSaving)
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .accountSheetChrome("Budget for the week")
        }
    }

    private var startFrom: some View {
        VStack(alignment: .leading, spacing: 10) {
            AccountKicker(text: "Start from")
            AccountFlowLayout(spacing: 8) {
                ForEach(DSRBudgetViewModel.Source.allCases) { s in
                    Button {
                        Haptic.light()
                        Task { await viewModel.prefill(s) }
                    } label: {
                        Text(s == .lastYear && viewModel.lastYearPct != 0
                             ? "Last year \(viewModel.lastYearPct > 0 ? "+" : "\u{2212}")\(abs(viewModel.lastYearPct))%" : s.label)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(viewModel.isFilling)
                }
            }
            Stepper(value: $viewModel.lastYearPct, in: -50...100, step: 1) {
                HomeMixedText.make("Last year plus \(viewModel.lastYearPct)%", role: .secondary)
            }
            .tint(Color.cavnarEmber2)
            if viewModel.isFilling { CavnarShimmerText(text: "Filling", color: .cavnarInk) }
        }
    }

    private func field(_ label: String, _ text: Binding<String>, id: String) -> some View {
        TextField(label, text: text)
            .keyboardType(.decimalPad)
            .font(.cavnarNumber(CavnarType.body, weight: 600))
            .foregroundStyle(Color.cavnarInk)
            .padding(.vertical, 8)
            .padding(.horizontal, 10)
            .background(Color.cavnarPaper3.opacity(0.35), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .frame(width: 104)
            .focused($focus, equals: id)
            .accessibilityLabel("\(label) budget")
    }
}

// MARK: - The week as a workbook (parity audit #79)

/// The week's grid as the server's .xlsx in Erik's layout (/dsr/week.xlsx —
/// the manager's file has no budget columns), downloaded when it is shared.
struct DSRWeekWorkbook: Transferable {
    let date: String?
    let filename: String

    /// "Daily sales - week of 9-16-26.xlsx": the week's start in M-D-YY (a
    /// filename can't carry the slashes).
    static func filename(start: String) -> String {
        "Daily sales - week of \(CavnarDate.mdy(start).replacingOccurrences(of: "/", with: "-")).xlsx"
    }

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(exportedContentType: UTType(filenameExtension: "xlsx") ?? .data) { book in
            let data = try await APIClient.shared.fetchFile("/mobile/api/dsr/week.xlsx",
                                                            query: book.date.map { ["date": $0] } ?? [:])
            let url = FileManager.default.temporaryDirectory.appendingPathComponent(book.filename)
            try data.write(to: url, options: .atomic)
            return SentTransferredFile(url)
        }
    }
}
