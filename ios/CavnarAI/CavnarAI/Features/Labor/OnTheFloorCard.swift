import SwiftUI
import Observation

// On the floor (schedule audit 10/10/26, J2 — the web's "On the floor"
// panel on the phone, where it is decided during service): labor to date
// against sales, the projected all-in % at close against the target, and
// the legal, priced moves worth making now — let someone go, ask someone to
// stay, call someone in — each one tap (`/mobile/api/labor/live`, its moves'
// act / dismiss / reverse). The switches — suggestions during service,
// earned autopilot, and the published week to the POS time clock (preview
// first) — live in FloorSettingsSheet (`labor/floor-settings`,
// `labor/pos-schedule-push`).

// MARK: - Payloads

struct LiveLaborRead: Decodable, Equatable {
    struct Suggestion: Decodable, Equatable, Hashable {
        var kind: String
        var text: String
        enum CodingKeys: String, CodingKey { case kind, text }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            guard let t = c.sfText(.text) else {
                throw DecodingError.dataCorruptedError(forKey: .text, in: c, debugDescription: "A move says nothing")
            }
            text = t
            kind = c.sfText(.kind) ?? ""
        }
    }

    var available = false
    var reason: String? = nil
    var sentence: String? = nil
    var asOf: String? = nil
    var salesSoFar: Double? = nil
    var salesProjected: Double? = nil
    var laborToDate: Double? = nil
    var laborRest: Double? = nil
    var laborFromSchedule = false
    var hourlySoFar: Double? = nil
    var allInProjected: Double? = nil
    var target: Double? = nil
    var targetLabel: String? = nil
    var overBy: Double? = nil
    var moves: [Suggestion] = []

    enum CodingKeys: String, CodingKey { case available, reason, sentence, sales, labor, pct, moves
        case asOf = "as_of" }
    enum SalesKeys: String, CodingKey { case projected
        case soFar = "so_far" }
    enum LaborKeys: String, CodingKey { case rest, basis
        case toDate = "to_date" }
    enum PctKeys: String, CodingKey {
        case target
        case hourlySoFar = "hourly_so_far"
        case allInProjected = "all_in_projected"
        case targetLabel = "target_label"
        case overBy = "over_by"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        available = c.sfBool(.available) ?? false
        reason = c.sfText(.reason)
        sentence = c.sfText(.sentence)
        asOf = c.sfText(.asOf)
        if let s = try? c.nestedContainer(keyedBy: SalesKeys.self, forKey: .sales) {
            salesSoFar = s.sfDouble(.soFar); salesProjected = s.sfDouble(.projected)
        }
        if let l = try? c.nestedContainer(keyedBy: LaborKeys.self, forKey: .labor) {
            laborToDate = l.sfDouble(.toDate); laborRest = l.sfDouble(.rest)
            laborFromSchedule = l.sfText(.basis) == "schedule"
        }
        if let p = try? c.nestedContainer(keyedBy: PctKeys.self, forKey: .pct) {
            hourlySoFar = p.sfDouble(.hourlySoFar); allInProjected = p.sfDouble(.allInProjected)
            target = p.sfDouble(.target); targetLabel = p.sfText(.targetLabel); overBy = p.sfDouble(.overBy)
        }
        moves = c.setupList(Suggestion.self, .moves)
    }
}

/// A move of tonight's, in the ledger (it carries the id the one tap acts on).
struct LiveLaborMove: Decodable, Identifiable, Equatable {
    var id: Int
    var kind: String
    var employee: String
    var text: String
    var status: String
    var how: String?

    enum CodingKeys: String, CodingKey { case id, kind, employee, text, status, how }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let i = c.sfInt(.id) else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "A move with no id")
        }
        id = i
        kind = c.sfText(.kind) ?? ""
        employee = c.sfText(.employee) ?? ""
        text = c.sfText(.text) ?? ""
        status = c.sfText(.status) ?? "suggested"
        how = c.sfText(.how)
    }

    var isOpen: Bool { status == "suggested" || status == "pushed" }

    /// The one tap's words, as the web says them.
    var actLabel: String {
        switch kind {
        case "cut": return "Let them go"
        case "extend": return "Ask them to stay"
        case "call_in": return "Call them in"
        default: return "Do it"
        }
    }
}

struct LiveLaborResponse: Decodable {
    var ok = false
    var error: String? = nil
    var read = LiveLaborRead()
    var movesToday: [LiveLaborMove] = []
    var canAct = false

    enum CodingKeys: String, CodingKey {
        case ok, error, read
        case movesToday = "moves_today"
        case canAct = "can_act"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        error = c.sfText(.error)
        read = ((try? c.decodeIfPresent(LiveLaborRead.self, forKey: .read)) ?? nil) ?? LiveLaborRead()
        movesToday = c.setupList(LiveLaborMove.self, .movesToday)
        canAct = c.sfBool(.canAct) ?? false
    }
}

struct FloorSettingsResponse: Decodable {
    struct Earned: Decodable { var earned = false; var approvals = 0; var needed = 6
        enum CodingKeys: String, CodingKey { case earned, approvals, needed }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            earned = c.sfBool(.earned) ?? false; approvals = c.sfInt(.approvals) ?? 0; needed = c.sfInt(.needed) ?? 6
        }
    }
    var ok = false
    var error: String? = nil
    var posPush = false
    var liveMoves = false
    var autopilot = false
    var canEdit = false
    var posLabel: String? = nil
    var posProvider: String? = nil
    var posCanPush = false
    var publishedScheduleId: Int? = nil
    var extend: Earned? = nil
    var callIn: Earned? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, settings, pos, autopilot
        case canEdit = "can_edit"
        case publishedScheduleId = "published_schedule_id"
    }
    enum SettingsKeys: String, CodingKey {
        case autopilot
        case posPush = "pos_push"
        case liveMoves = "live_moves"
    }
    enum PosKeys: String, CodingKey {
        case provider, label
        case schedulePush = "schedule_push"
        case schedulePreview = "schedule_preview"
    }
    enum AutopilotKeys: String, CodingKey {
        case extend
        case callIn = "call_in"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        error = c.sfText(.error)
        if let s = try? c.nestedContainer(keyedBy: SettingsKeys.self, forKey: .settings) {
            posPush = s.sfBool(.posPush) ?? false; liveMoves = s.sfBool(.liveMoves) ?? false
            autopilot = s.sfBool(.autopilot) ?? false
        }
        canEdit = c.sfBool(.canEdit) ?? false
        if let p = try? c.nestedContainer(keyedBy: PosKeys.self, forKey: .pos) {
            posProvider = p.sfText(.provider); posLabel = p.sfText(.label)
            posCanPush = (p.sfBool(.schedulePush) ?? false) || (p.sfBool(.schedulePreview) ?? false)
        }
        publishedScheduleId = c.sfInt(.publishedScheduleId)
        if let a = try? c.nestedContainer(keyedBy: AutopilotKeys.self, forKey: .autopilot) {
            extend = (try? a.decodeIfPresent(Earned.self, forKey: .extend)) ?? nil
            callIn = (try? a.decodeIfPresent(Earned.self, forKey: .callIn)) ?? nil
        }
    }
}

/// GET labor/pos-schedule-push: every shift and what the POS would do.
struct PosPushPreview: Decodable {
    struct Shift: Decodable, Identifiable, Hashable {
        var employee: String
        var start: String?
        var role: String?
        var status: String
        var reason: String?
        var id: String { "\(employee)|\(start ?? "")|\(role ?? "")" }
        enum CodingKeys: String, CodingKey { case employee, start, role, status, reason }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            employee = c.sfText(.employee) ?? ""
            start = c.sfText(.start); role = c.sfText(.role); reason = c.sfText(.reason)
            status = c.sfText(.status) ?? ""
        }

        /// "Sat 10/17/26 4pm · Server · will go"
        var line: String {
            var bits: [String] = []
            if let s = start, s.count >= 16 {
                let day = String(s.prefix(10)), t = String(s.dropFirst(11).prefix(5))
                let h = Int(t.prefix(2)) ?? 0, m = String(t.suffix(2))
                bits.append("\(CavnarDate.mdy(day)) \(h % 12 == 0 ? 12 : h % 12)\(m == "00" ? "" : ":\(m)")\(h < 12 ? "am" : "pm")")
            }
            if let role { bits.append(role) }
            bits.append(PosPushPreview.statusWords[status] ?? status)
            return bits.joined(separator: " \u{00B7} ") + (reason.map { ": \($0)" } ?? "")
        }
    }
    struct LastPush: Decodable { var status: String?; var sentOn: String?; var error: String?
        enum CodingKeys: String, CodingKey { case status, error
            case sentOn = "sent_on" }
    }

    static let statusWords: [String: String] = [
        "would_send": "will go", "sent": "sent", "skipped": "skipped", "problem": "the POS would refuse it",
        "started": "already started", "salaried": "salaried, doesn\u{2019}t clock in", "unchanged": "already there",
        "failed": "didn\u{2019}t go through", "removed_unconfirmed": "taken off: check the POS",
    ]

    var ok = false
    var error: String? = nil
    var external: String? = nil
    var shifts: [Shift] = []
    var lastPush: LastPush? = nil
    var supported = false
    var historyId: Int? = nil

    enum CodingKeys: String, CodingKey {
        case ok, error, external, shifts, supported
        case lastPush = "last_push"
        case historyId = "history_id"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = c.sfBool(.ok) ?? false
        error = c.sfText(.error)
        external = c.sfText(.external)
        shifts = c.setupList(Shift.self, .shifts)
        lastPush = (try? c.decodeIfPresent(LastPush.self, forKey: .lastPush)) ?? nil
        supported = c.sfBool(.supported) ?? false
        historyId = c.sfInt(.historyId)
    }
}

// MARK: - Store

@Observable
@MainActor
final class FloorStore {
    var live: LiveLaborResponse?
    var settings: FloorSettingsResponse?
    var preview: PosPushPreview?
    var busy: String?
    var note: String?
    var error: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        live = try? await client.send("/mobile/api/labor/live", hapticOnError: false)
        settings = try? await client.send("/mobile/api/labor/floor-settings", hapticOnError: false)
    }

    private struct Answer: Decodable {
        let ok: Bool
        let error: String?
        var status: String? = nil
        var note: String? = nil
        var undone: Bool? = nil
    }

    private func moveCall(_ id: Int, path: String, done: (Answer) -> String) async {
        busy = "m\(id)"
        error = nil
        defer { busy = nil }
        do {
            let r: Answer = try await client.send(path, method: .post, hapticOnError: false, retryTransient: false)
            if r.ok { note = done(r); Haptic.success() } else { error = r.error ?? "That didn\u{2019}t go through." }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That didn\u{2019}t go through."
        }
        await load()
    }

    func act(_ m: LiveLaborMove) async {
        await moveCall(m.id, path: "/mobile/api/labor/live/moves/\(m.id)/act") { r in
            r.status == "offered" ? "Offered to \(m.employee) in the Cavnar AI app" : (r.note ?? "Done, and they\u{2019}ve been told")
        }
    }

    func dismiss(_ m: LiveLaborMove) async {
        await moveCall(m.id, path: "/mobile/api/labor/live/moves/\(m.id)/dismiss") { _ in "Okay" }
    }

    func reverse(_ m: LiveLaborMove) async {
        await moveCall(m.id, path: "/mobile/api/labor/live/moves/\(m.id)/reverse") { r in
            r.undone == true ? "Withdrawn. Autopilot comes back to you" : (r.note ?? "Autopilot comes back to you")
        }
    }

    struct SettingsBody: Encodable {
        var liveMoves: Bool? = nil
        var autopilot: Bool? = nil
        var posPush: Bool? = nil
        var previewed: Bool? = nil
        enum CodingKeys: String, CodingKey {
            case autopilot, previewed
            case liveMoves = "live_moves"
            case posPush = "pos_push"
        }
    }

    func set(_ body: SettingsBody) async {
        busy = "settings"
        error = nil
        defer { busy = nil }
        do {
            let r: Answer = try await client.send("/mobile/api/labor/floor-settings", method: .post, body: body,
                                                  hapticOnError: false, retryTransient: false)
            if r.ok { note = "Saved"; Haptic.success() } else { error = r.error ?? "That didn\u{2019}t save." }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "That didn\u{2019}t save."
        }
        await load()
    }

    func loadPreview(_ scheduleId: Int) async {
        busy = "preview"
        defer { busy = nil }
        do {
            preview = try await client.send("/mobile/api/labor/pos-schedule-push",
                                            query: ["schedule_id": String(scheduleId)], hapticOnError: false)
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "The preview couldn\u{2019}t be read."
        }
    }

    private struct PushBody: Encodable {
        let scheduleId: Int
        enum CodingKeys: String, CodingKey { case scheduleId = "schedule_id" }
    }

    func pushNow(_ scheduleId: Int) async {
        busy = "push"
        error = nil
        defer { busy = nil }
        do {
            let r: Answer = try await client.send("/mobile/api/labor/pos-schedule-push", method: .post,
                                                  body: PushBody(scheduleId: scheduleId),
                                                  hapticOnError: false, retryTransient: false)
            if r.ok {
                note = r.status == "unchanged" ? "Already on the POS: nothing changed" : "Sent to the POS"
                Haptic.success()
            } else {
                error = r.error ?? "It didn\u{2019}t all go through: each shift says why."
            }
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "It didn\u{2019}t go through."
        }
        await loadPreview(scheduleId)
    }
}

// MARK: - The card

/// Labor's live card during service: one sentence, the projected all-in %
/// against the target, and tonight's moves with their one tap. Nothing
/// when no published schedule covers today and no move was made.
struct OnTheFloorCard: View {
    let store: FloorStore
    var onSettings: () -> Void

    private static func money(_ v: Double?) -> String { v.map { "$" + $0.commaFormatted } ?? "\u{2014}" }

    var body: some View {
        if let live = store.live, live.ok, live.read.available || !live.movesToday.isEmpty {
            let r = live.read
            VStack(alignment: .leading, spacing: CavnarSpace.s) {
                HStack(alignment: .firstTextBaseline) {
                    CavnarKicker("On the floor", icon: "dot.radiowaves.left.and.right")
                    Spacer()
                    if let at = r.asOf { Text(at).cavnarText(.caption) }
                }
                if r.available {
                    if let pct = r.allInProjected {
                        HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                            Text(String(format: "%g%%", pct))
                                .cavnarText(.figureL, color: (r.overBy ?? 0) > 0 ? .cavnarRedText : .cavnarInk)
                            Text(r.target.map { "projected all-in \u{00B7} \(r.targetLabel ?? "target") \(String(format: "%g", $0))%" }
                                 ?? "projected all-in at close")
                                .cavnarText(.secondary)
                        }
                    }
                    if let s = r.sentence { CavnarMixedText(s, role: .body) }
                    CavnarMixedText("Sales \(Self.money(r.salesSoFar)) so far \u{00B7} labor \(Self.money(r.laborToDate))"
                                    + (r.laborFromSchedule ? " (from the schedule: no live clock feed)" : ""),
                                    role: .caption)
                } else if let reason = r.reason {
                    CavnarMixedText(reason.prefix(1).uppercased() + reason.dropFirst() + ".", role: .secondary)
                }
                ForEach(live.movesToday) { m in moveRow(m, canAct: live.canAct) }
                if live.movesToday.isEmpty {
                    ForEach(r.moves, id: \.self) { s in
                        CavnarMixedText(s.text, role: .secondary, color: .cavnarInk)
                    }
                }
                if let note = store.note { CavnarMixedText(note, role: .secondary, color: .cavnarInk2) }
                if let error = store.error { CavnarMixedText(error, role: .secondary, color: .cavnarRedText) }
                Button {
                    Haptic.light()
                    onSettings()
                } label: {
                    HStack(spacing: CavnarSpace.xxs) {
                        Text("Suggestions, autopilot and the POS").cavnarText(.label, color: .cavnarEmber2)
                        Image(systemName: "chevron.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarEmber2)
                            .accessibilityHidden(true)
                    }
                    .cavnarHitTarget()
                }
                .buttonStyle(.plain)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    @ViewBuilder
    private func moveRow(_ m: LiveLaborMove, canAct: Bool) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarMixedText(m.text, role: .body, color: .cavnarInk)
            if m.isOpen && canAct {
                HStack(spacing: CavnarSpace.s) {
                    Button { Task { await store.act(m) } } label: {
                        Group {
                            if store.busy == "m\(m.id)" { CavnarShimmerText(text: "Working\u{2026}") } else { Text(m.actLabel) }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: store.busy != nil))
                    .disabled(store.busy != nil)
                    Button { Task { await store.dismiss(m) } } label: { Text("Not now").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(store.busy != nil)
                }
            } else if !m.isOpen {
                Text(m.status.replacingOccurrences(of: "_", with: " ").capitalized + (m.how == "auto" ? " automatically" : ""))
                    .cavnarText(.caption)
                if m.how == "auto" && m.status == "offered" && canAct {
                    Button { Task { await store.reverse(m) } } label: { Text("Reverse").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                        .disabled(store.busy != nil)
                }
            }
        }
    }
}

// MARK: - The switches

/// Suggestions during service, earned autopilot, and the published week to
/// the POS time clock — switched on only from its preview.
struct FloorSettingsSheet: View {
    let store: FloorStore
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: CavnarSpace.l) {
                    if let s = store.settings, s.ok {
                        content(s)
                    } else {
                        CavnarSkeletonLines(widths: [1.0, 0.7, 0.8])
                    }
                    if let note = store.note { CavnarMixedText(note, role: .secondary) }
                    if let error = store.error { CavnarMixedText(error, role: .secondary, color: .cavnarRedText) }
                }
                .padding(CavnarSpace.gutter)
            }
            .accountSheetChrome("On the floor")
        }
        .task { if store.settings == nil { await store.load() } }
    }

    @ViewBuilder
    private func content(_ s: FloorSettingsResponse) -> some View {
        let busy = store.busy != nil
        AccountSection(kicker: "During service") {
            AccountSwitchRow(label: "Suggest moves during service",
                             detail: "Let someone go on a slow night, ask someone to stay or call someone in on a busy one, each priced and legal.",
                             isOn: Binding(get: { s.liveMoves }, set: { on in Task { await store.set(.init(liveMoves: on)) } }),
                             busy: store.busy == "settings", disabled: !s.canEdit || busy)
            AccountSwitchRow(label: "Autopilot",
                             detail: "An extension or call-in you\u{2019}ve approved \(s.extend?.needed ?? 6) times yourself is offered without your tap. A cut always waits for you; reverse one and it comes back to you."
                                + " Extensions: " + ((s.extend?.earned ?? false) ? "earned" : "\(s.extend?.approvals ?? 0) so far")
                                + ". Call-ins: " + ((s.callIn?.earned ?? false) ? "earned" : "\(s.callIn?.approvals ?? 0) so far") + ".",
                             isOn: Binding(get: { s.autopilot }, set: { on in Task { await store.set(.init(autopilot: on)) } }),
                             busy: store.busy == "settings", disabled: !s.canEdit || busy, showsDivider: false)
        }
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("The week on the POS time clock")
            let pos = s.posLabel ?? "your POS"
            if !s.posCanPush {
                CavnarMixedText(s.posProvider == nil ? "No POS is connected."
                                : "\(pos) has no schedule write Cavnar AI can use: the week stays in Cavnar AI and the staff app.",
                                role: .body)
            } else {
                CavnarMixedText(s.posPush
                                ? "On: every week you send to staff also goes to \(pos)\u{2019}s time clock."
                                : "Off: nothing is written to \(pos). Look at the preview first: it shows every shift and what the POS would do with it.",
                                role: .body)
                if let id = s.publishedScheduleId {
                    Button { Task { await store.loadPreview(id) } } label: {
                        Group {
                            if store.busy == "preview" { CavnarShimmerText(text: "Reading the POS\u{2026}") } else { Text("Preview this week") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .disabled(busy)
                } else {
                    Text("Send a week to staff and its preview shows here.").cavnarText(.secondary)
                }
                if let pv = store.preview { previewBlock(pv, s: s, pos: pos) }
            }
        }
    }

    @ViewBuilder
    private func previewBlock(_ pv: PosPushPreview, s: FloorSettingsResponse, pos: String) -> some View {
        if !pv.ok {
            CavnarMixedText(pv.error ?? "The preview couldn\u{2019}t be read.", role: .secondary, color: .cavnarRedText)
        } else {
            if let ext = pv.external { CavnarMixedText(ext, role: .secondary) }
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                ForEach(pv.shifts) { sh in
                    CavnarMixedText("\(sh.employee) \u{00B7} \(sh.line)", role: .secondary,
                                    color: sh.status == "problem" || sh.status == "failed" ? .cavnarRedText : .cavnarInk2)
                }
            }
            .cavnarCard()
            if let lp = pv.lastPush, let st = lp.status {
                CavnarMixedText("Last push: \(st)" + (lp.sentOn.map { " on \($0)" } ?? "") + (lp.error.map { " \u{00B7} \($0)" } ?? ""),
                                role: .caption)
            }
            let busy = store.busy != nil
            if s.canEdit && pv.supported && !s.posPush && pv.external == nil {
                Button { Task { await store.set(.init(posPush: true, previewed: true)) } } label: {
                    Text("Send published weeks to \(pos)").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy))
                .disabled(busy)
            }
            if s.canEdit && s.posPush, let hid = pv.historyId {
                Button { Task { await store.pushNow(hid) } } label: {
                    Group {
                        if store.busy == "push" { CavnarShimmerText(text: "Sending\u{2026}") } else { Text("Send this week now") }
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
                Button { Task { await store.set(.init(posPush: false)) } } label: {
                    Text("Stop sending to the POS").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .disabled(busy)
            }
        }
    }
}
