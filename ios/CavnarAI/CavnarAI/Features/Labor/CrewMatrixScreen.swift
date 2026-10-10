import SwiftUI
import Observation

// The Studio's Crew stage on the phone (schedule audit 10/10/26 TRUST-12):
// the draft against the restaurant's usual crew, shift by shift, in one
// unit — people on at once through the heart of the shift (contract #2) —
// from the same payload the web reads (GET/POST /mobile/api/labor/crew-matrix,
// crew_matrix.api_body). The phone shows the differences only, in the
// restaurant's own week order (`days`), each with its answer: Keep the usual
// crew every week (it becomes that shift's minimum), Leaner is fine (keeps
// the saving), Add 1 this week (one legal shift, added to the week on
// screen and saved like a hand edit), or Undo.

// MARK: - Payload

struct CrewMatrix: Decodable, Equatable {
    struct Trend: Decodable, Equatable {
        var was: Int? = nil
        var since: String? = nil
        enum CodingKeys: String, CodingKey { case was, since }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            was = c.sfInt(.was)
            since = c.sfText(.since)
        }
    }

    struct Cell: Decodable, Equatable, Identifiable {
        var day: String
        var part: String
        var usual = 0
        var usualPeak = 0
        var draft = 0
        var draftPeak = 0
        var floor = 0
        var status = "match"
        var peakNote: String? = nil
        var demand: String? = nil
        var trend: Trend? = nil
        var answer: String? = nil
        var answerState: String? = nil
        var since: String? = nil
        var review: String? = nil
        var kept: Int? = nil
        var accepted: Int? = nil
        var canAdd = false
        var date: String? = nil

        var id: String { "\(day)|\(part)" }

        enum CodingKeys: String, CodingKey {
            case day, part, usual, draft, floor, status, demand, trend, answer, since, review, kept, accepted, date
            case usualPeak = "usual_peak"
            case draftPeak = "draft_peak"
            case peakNote = "peak_note"
            case answerState = "answer_state"
            case canAdd = "can_add"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let d = c.sfText(.day), let p = c.sfText(.part) else {
                throw DecodingError.dataCorruptedError(forKey: .day, in: c, debugDescription: "A cell with no shift")
            }
            day = d
            part = p
            usual = c.sfInt(.usual) ?? 0
            usualPeak = c.sfInt(.usualPeak) ?? 0
            draft = c.sfInt(.draft) ?? 0
            draftPeak = c.sfInt(.draftPeak) ?? 0
            floor = c.sfInt(.floor) ?? 0
            status = c.sfText(.status) ?? "match"
            peakNote = c.sfText(.peakNote)
            demand = c.sfText(.demand)
            trend = (try? c.decodeIfPresent(Trend.self, forKey: .trend)) ?? nil
            answer = c.sfText(.answer)
            answerState = c.sfText(.answerState)
            since = c.sfText(.since)
            review = c.sfText(.review)
            kept = c.sfInt(.kept)
            accepted = c.sfInt(.accepted)
            canAdd = c.sfBool(.canAdd) ?? false
            date = c.sfText(.date)
        }

        /// "Lunch" / "Dinner" — the owner's words for the two halves.
        var partWord: String { part == "morning" ? "lunch" : "dinner" }
        /// "Fri"
        var dayShort: String { String(day.prefix(3)) }

        /// A difference the owner may want to see: not the usual, and not a
        /// week with nothing built.
        var isDifference: Bool { status != "match" && status != "usual" }

        /// Waiting on the owner: leaner with no answer, or an answer due
        /// for its review, or a "leaner is fine" the draft is now under.
        var isOpen: Bool {
            (status == "under" && answer == nil) || answerState == "review" || answerState == "short_of_accepted"
        }
    }

    struct Role: Decodable, Equatable, Identifiable {
        var role: String
        var usualWeek = 0
        var draftWeek = 0
        var cells: [Cell] = []
        var id: String { role }

        enum CodingKeys: String, CodingKey {
            case role, cells
            case usualWeek = "usual_week"
            case draftWeek = "draft_week"
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let r = c.sfText(.role) else {
                throw DecodingError.dataCorruptedError(forKey: .role, in: c, debugDescription: "A role with no name")
            }
            role = r
            usualWeek = c.sfInt(.usualWeek) ?? 0
            draftWeek = c.sfInt(.draftWeek) ?? 0
            cells = c.setupList(Cell.self, .cells)
        }
    }

    var ok = false
    var weekStart: String? = nil
    var historyId: Int? = nil
    var hasSales = false
    var hasUsual = false
    var differences = 0
    var open = 0
    var roles: [Role] = []
    var days: [String] = []
    var error: String? = nil
    var message: String? = nil

    enum CodingKeys: String, CodingKey {
        case ok, differences, open, roles, days, error, message
        case weekStart = "week_start"
        case historyId = "history_id"
        case hasSales = "has_sales"
        case hasUsual = "has_usual"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        weekStart = c.sfText(.weekStart)
        historyId = c.sfInt(.historyId)
        hasSales = c.sfBool(.hasSales) ?? false
        hasUsual = c.sfBool(.hasUsual) ?? false
        differences = c.sfInt(.differences) ?? 0
        open = c.sfInt(.open) ?? 0
        roles = c.setupList(Role.self, .roles)
        days = c.setupTexts(.days)
        error = c.sfText(.error)
        message = c.sfText(.message)
    }

    /// The weekday order the restaurant's week runs in (its own week start —
    /// Wednesday first at Simple EJ's); Monday-first only when the server
    /// sent none.
    var dayOrder: [String] {
        days.isEmpty ? ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] : days
    }

    /// One shift of one role, flattened for a list.
    struct Item: Identifiable, Equatable {
        let role: String
        let cell: Cell
        var id: String { "\(role)|\(cell.id)" }
    }

    /// Every cell, in the week's order (day, then lunch before dinner, then
    /// the role), the differences only unless `all`.
    func items(all: Bool = false) -> [Item] {
        let order = Dictionary(uniqueKeysWithValues: dayOrder.enumerated().map { ($1, $0) })
        return roles.flatMap { r in r.cells.map { Item(role: r.role, cell: $0) } }
            .filter { all || $0.cell.isDifference }
            .sorted {
                let a = order[$0.cell.day] ?? 9, b = order[$1.cell.day] ?? 9
                if a != b { return a < b }
                if $0.cell.part != $1.cell.part { return $0.cell.part == "morning" }
                return $0.role.lowercased() < $1.role.lowercased()
            }
    }

    /// The shifts the draft runs leaner than usual (or under a minimum).
    var leaner: [Item] { items().filter { $0.cell.status == "under" || $0.cell.status == "below_rule" } }

    /// "3 shifts leaner than usual · 2 to answer" — the line the Labor
    /// screen and the Send sheet carry.
    var headline: String? {
        guard historyId != nil else { return nil }
        let n = leaner.count
        guard n > 0 || open > 0 else { return differences == 0 ? "Matches your usual crew" : nil }
        var out = n == 1 ? "1 shift leaner than usual" : "\(n) shifts leaner than usual"
        if open > 0 { out += " \u{00B7} \(open) to answer" }
        return out
    }
}

/// One crew answer: `answer` hold | lean | clear | add, for the week on
/// screen (`history_id`, and its rows when they are at hand).
struct CrewAnswerRequest: Encodable {
    let role: String
    let day: String
    let part: String
    let answer: String
    let historyId: Int?
    var rows: [ScheduleRow]? = nil

    enum CodingKeys: String, CodingKey {
        case role, day, part, answer, rows
        case historyId = "history_id"
    }
}

// MARK: - Store

@Observable
@MainActor
final class CrewMatrixStore {
    var matrix: CrewMatrix?
    var isLoading = false
    var error: String?
    /// The cell being answered now, and what each answer said back.
    var busy: String?
    var notes: [String: String] = [:]
    /// Told after an answer or an added shift saved — the Send sheet reads
    /// its review again, so the verdict counts the answer.
    var onAnswered: (() async -> Void)?

    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    func load(historyId: Int?) async {
        isLoading = matrix == nil
        defer { isLoading = false }
        do {
            let query = historyId.map { ["history_id": String($0)] } ?? [:]
            let m: CrewMatrix = try await client.send("/mobile/api/labor/crew-matrix", query: query,
                                                      hapticOnError: false)
            if m.ok { matrix = m; error = nil } else { error = m.error ?? "Crew couldn\u{2019}t load." }
        } catch is CancellationError {
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Crew couldn\u{2019}t load."
        }
    }

    private struct AddResponse: Decodable {
        let ok: Bool
        let error: String?
        var suggestion: [String: LooseValue]? = nil
        var text: String? = nil
    }

    /// Keep usual / Leaner is fine / Undo — saved, and the crew read back
    /// for the same week (the payload is that week's).
    func answer(_ item: CrewMatrix.Item, _ choice: String, rows: [ScheduleRow]?) async {
        guard busy == nil else { return }
        busy = item.id
        defer { busy = nil }
        notes[item.id] = nil
        do {
            let m: CrewMatrix = try await client.send(
                "/mobile/api/labor/crew-matrix", method: .post,
                body: CrewAnswerRequest(role: item.role, day: item.cell.day, part: item.cell.part, answer: choice,
                                        historyId: matrix?.historyId, rows: rows),
                hapticOnError: false, retryTransient: false)
            if m.ok {
                matrix = m
                Haptic.success()
                await onAnswered?()
            } else {
                notes[item.id] = m.error ?? "That couldn\u{2019}t be saved."
                Haptic.error()
            }
        } catch let e as APIClient.APIError {
            notes[item.id] = e.message
            Haptic.error()
        } catch {
            notes[item.id] = "That couldn\u{2019}t be saved."
            Haptic.error()
        }
    }

    /// Add 1 this week: the server picks one legal person for that shift
    /// (nothing is saved, nothing standing is set); the shift goes on the
    /// week on screen through `addRow` — the same edit, re-score and save as
    /// a hand-added shift. Nil when added; otherwise why not.
    func addOne(_ item: CrewMatrix.Item, rows: [ScheduleRow]?,
                addRow: ([String: LooseValue]) async -> String?) async -> String? {
        guard busy == nil else { return nil }
        busy = item.id
        defer { busy = nil }
        do {
            let r: AddResponse = try await client.send(
                "/mobile/api/labor/crew-matrix", method: .post,
                body: CrewAnswerRequest(role: item.role, day: item.cell.day, part: item.cell.part, answer: "add",
                                        historyId: matrix?.historyId, rows: rows),
                hapticOnError: false, retryTransient: false)
            guard r.ok, let row = r.suggestion else {
                let why = r.error ?? "Nobody can legally take another one this week."
                notes[item.id] = why
                return why
            }
            if let refusal = await addRow(row) {
                notes[item.id] = refusal
                return refusal
            }
            notes[item.id] = r.text.map { $0 + " \u{00B7} saved" } ?? "Added and saved"
            await load(historyId: matrix?.historyId)
            await onAnswered?()
            return nil
        } catch let e as APIClient.APIError {
            notes[item.id] = e.message
            return e.message
        } catch {
            notes[item.id] = "That couldn\u{2019}t be added."
            return notes[item.id]
        }
    }
}

// MARK: - Views

/// One shift of the crew: usual → draft, in words an owner reads at a
/// glance, and its answer.
struct CrewCellRow: View {
    let item: CrewMatrix.Item
    let store: CrewMatrixStore
    /// Rows on screen, for Add 1 and the answer's read of the week.
    let rows: [ScheduleRow]?
    /// Puts a suggested row on the week (Labor's addShift). Nil: no Add 1.
    let addRow: (([String: LooseValue]) async -> String?)?
    let canAnswer: Bool

    private var cell: CrewMatrix.Cell { item.cell }

    private var tone: Color {
        switch cell.status {
        case "below_rule": return .cavnarRed
        case "under": return .cavnarAmber
        case "match": return .cavnarGreen
        default: return .cavnarInk3
        }
    }

    /// "Fri dinner · Server"
    private var title: String { "\(cell.dayShort) \(cell.partWord) \u{00B7} \(item.role)" }

    /// "2 on at once; you usually run 3."
    private var line: String {
        if cell.status == "usual" { return "Usually \(cell.usual) on at once." }
        var out = "\(cell.draft) on at once; you usually run \(cell.usual)"
        if cell.status == "below_rule" { out += ", and your minimum is \(cell.floor)" }
        return out + "."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                Circle().fill(tone).frame(width: 8, height: 8).accessibilityHidden(true)
                Text(title).cavnarText(.label)
                Spacer(minLength: 0)
                if let demand = cell.demand {
                    ScheduleRowTag(text: demand, tone: demand == "busy" ? .cavnarAmber : .cavnarInk2)
                }
            }
            CavnarMixedText(line, role: .body)
            if let note = cell.peakNote { CavnarMixedText(note, role: .secondary) }
            if let t = cell.trend, let was = t.was {
                CavnarMixedText("Was \(was) before \(t.since ?? "")".trimmingCharacters(in: .whitespaces),
                                role: .caption)
            }
            answerBlock
            if let note = store.notes[item.id] {
                CavnarMixedText(note, role: .secondary, color: note.hasSuffix("saved") || note.hasPrefix("Added")
                                ? .cavnarInk2 : .cavnarRedText)
            }
        }
        .padding(.vertical, CavnarSpace.xs)
        .opacity(store.busy == item.id ? 0.6 : 1)
    }

    @ViewBuilder
    private var answerBlock: some View {
        let busy = store.busy != nil
        if let answer = cell.answer, cell.answerState == "in_force" {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                CavnarMixedText(answer == "hold"
                                ? "Kept \(cell.kept ?? cell.usual) every \(cell.dayShort)" + (cell.review.map { " \u{00B7} review \($0)" } ?? "")
                                : "Leaner is fine (\(cell.accepted ?? cell.draft))" + (cell.review.map { " \u{00B7} review \($0)" } ?? ""),
                                role: .secondary, color: .cavnarInk)
                Spacer(minLength: 0)
                if canAnswer {
                    Button { Task { await store.answer(item, "clear", rows: rows) } } label: {
                        Text("Undo").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                }
            }
            if answer == "hold", cell.status == "below_rule", cell.canAdd, let addRow, canAnswer {
                addButton(addRow, busy: busy)
            }
        } else if canAnswer, cell.status == "under" || cell.status == "below_rule" || cell.answerState != nil {
            if cell.answerState == "review" {
                CavnarMixedText(cell.answer == "hold"
                                ? "You kept \(cell.kept ?? cell.usual) since \(cell.since ?? "") \u{00B7} time to review"
                                : "Leaner was fine since \(cell.since ?? "") \u{00B7} time to review",
                                role: .secondary, color: .cavnarAmber)
            }
            if cell.status == "under" || cell.answerState == "review" || cell.answerState == "short_of_accepted" {
                Button { Task { await store.answer(item, "hold", rows: rows) } } label: {
                    Text(cell.answerState == "review" && cell.answer == "hold"
                         ? "Still keep \(cell.kept ?? cell.usual)" : "Keep \(cell.usual) every \(cell.dayShort)")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
                .accessibilityHint("Every week: it becomes that shift's minimum, reviewed in a season.")
            }
            if cell.canAdd, let addRow { addButton(addRow, busy: busy) }
            if cell.status == "under" || cell.answerState == "short_of_accepted" {
                Button { Task { await store.answer(item, "lean", rows: rows) } } label: {
                    Text("Leaner is fine on \(cell.dayShort)").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
            }
            if cell.answerState == "review" {
                Button { Task { await store.answer(item, "clear", rows: rows) } } label: {
                    Text("Undo").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
            }
        }
    }

    private func addButton(_ addRow: @escaping ([String: LooseValue]) async -> String?, busy: Bool) -> some View {
        Button {
            Task { _ = await store.addOne(item, rows: rows, addRow: addRow) }
        } label: {
            Text("Add 1 this week").frame(maxWidth: .infinity)
        }
        .buttonStyle(CavnarSecondaryButtonStyle())
        .disabled(busy)
    }
}

/// The Crew screen: the line, then each shift that differs from the usual
/// crew (or, with no week built, the usual crew itself), in the week's
/// order. "All shifts" shows the matches too.
struct CrewMatrixScreen: View {
    let historyId: Int?
    let rows: [ScheduleRow]?
    let addRow: (([String: LooseValue]) async -> String?)?
    var canAnswer = true

    @State private var store = CrewMatrixStore()
    @State private var showAll = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if store.isLoading && store.matrix == nil {
                        CavnarSkeletonLines(widths: [1.0, 0.8, 0.6])
                    } else if let error = store.error, store.matrix == nil {
                        ScheduleNotice(text: error, tone: .cavnarRed) {
                            Button { Task { await store.load(historyId: historyId) } } label: {
                                Text("Try again").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                    } else if let m = store.matrix {
                        content(m)
                    }
                }
                .padding(CavnarSpace.gutter)
            }
            .cavnarModuleBackground()
            .navigationTitle("Crew")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                cavnarTitleToolbar("Crew")
                cavnarToolbarItem(placement: .topBarTrailing) {
                    Button { Haptic.light(); dismiss() } label: {
                        Text("Done").cavnarText(.label, color: .cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .task { await store.load(historyId: historyId) }
    }

    @ViewBuilder
    private func content(_ m: CrewMatrix) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Your usual crew")
            CavnarMixedText(m.headline ?? (m.historyId == nil ? "No week built yet: your usual crew, shift by shift."
                                                              : "\(m.differences) shifts differ from your usual crew."),
                            role: .lead, color: .cavnarInk)
            Text("Every number is people on at once through the heart of the shift.")
                .cavnarText(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        if !m.hasUsual {
            ScheduleNotice(text: "Cavnar AI learns your usual crew from the weeks you run. It fills in after a few weeks of shifts.",
                           tone: .cavnarInk2, symbol: "clock")
        } else {
            if m.historyId != nil {
                CavnarSegmentedControl(selection: $showAll, options: [false, true],
                                       accessibilityTitle: "Shifts shown") { $0 ? "All shifts" : "Differences" }
            }
            let items = m.items(all: showAll || m.historyId == nil)
            if items.isEmpty {
                ScheduleNotice(text: "Every shift matches your usual crew.", tone: .cavnarGreen,
                               symbol: "checkmark.circle.fill")
            } else {
                VStack(spacing: 0) {
                    ForEach(items) { item in
                        CrewCellRow(item: item, store: store, rows: rows,
                                    addRow: m.historyId == nil ? nil : addRow,
                                    canAnswer: canAnswer && m.historyId != nil)
                        if item.id != items.last?.id {
                            Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                        }
                    }
                }
                .cavnarCard()
            }
        }
    }
}

/// The Send sheet's crew line (TRUST-12): "2 shifts leaner than usual", the
/// cells, and their answers — the same rows as the Crew screen.
struct PublishCrewCard: View {
    let store: CrewMatrixStore
    let rows: [ScheduleRow]?
    let addRow: (([String: LooseValue]) async -> String?)?

    var body: some View {
        if let m = store.matrix, m.historyId != nil, let headline = m.headline, !m.leaner.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                CavnarKicker("Against your usual crew", icon: "person.3")
                CavnarMixedText(headline, role: .lead, color: .cavnarInk)
                ForEach(m.leaner) { item in
                    CrewCellRow(item: item, store: store, rows: rows, addRow: addRow, canAnswer: true)
                    if item.id != m.leaner.last?.id {
                        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }
}
