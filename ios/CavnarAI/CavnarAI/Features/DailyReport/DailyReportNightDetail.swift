import SwiftUI
import Charts

// The night in detail (parity audit #14) — the phone half of the web's
// detailHtml (dashboard.html "The night in detail", owner 9/30/26 and
// 10/5/26): the hour-by-hour story, where the money came from (meal
// periods and rooms), where the labor went (departments, salaries
// included), the servers and bartenders, what was given away (comps,
// discounts and refunds by reason and approver, voids apart), the punches
// a manager edited, and cash and cards.
//
// Every figure is read from the stored facts as this login was sent them
// (dsr/access.py): a manager without the comps-and-voids grant has no
// `loss` in the payload, and nobody but the owner has `timeclock_edits` or
// `salaried_cost` — so a part the payload leaves out is simply not drawn.
// Nothing here re-derives a permission.

// MARK: - Reading the service block

extension DSRBlock {
    struct Split: Hashable, Identifiable {
        let name: String
        let net: Double?
        let guests: Double?
        let checks: Double?
        let perGuest: Double?
        let sharePct: Double?
        var id: String { name }
    }

    struct Server: Hashable, Identifiable {
        let name: String
        let checks: Double?
        let guests: Double?
        let net: Double?
        let perGuest: Double?
        let drinksPerGuest: Double?
        let tipPct: Double?
        let hours: Double?
        let netPerHour: Double?
        /// Spend per guest against the floor's, in dollars.
        let vsFloor: Double?
        var id: String { name }
    }

    struct LossLine: Hashable, Identifiable {
        /// "comp" / "discount" / "refund" for money given away; nil for a void.
        let kind: String?
        let label: String
        let lines: Double?
        let amount: Double?
        var id: String { (kind ?? "") + "|" + label }
    }

    struct LossGroup: Hashable {
        let total: Double?
        let lines: Double?
        let pctOfGross: Double?
        let byReason: [LossLine]
        let byApprover: [LossLine]
    }

    struct PunchEdit: Hashable, Identifiable {
        let employee: String
        let role: String?
        let clockIn: String?
        let clockOut: String?
        let hours: Double?
        let editedBy: String
        let editedAt: String?
        let code: String?
        var id: String { employee + (clockIn ?? "") + (editedAt ?? "") }
    }

    struct Tender: Hashable, Identifiable {
        let method: String
        let payments: Double?
        let amount: Double?
        let tips: Double?
        var id: String { method }
    }

    struct Payout: Hashable, Identifiable {
        let category: String
        let isPayIn: Bool
        let amount: Double?
        let approvedBy: String
        let at: String?
        let reference: String?
        var id: String { category + (at ?? "") + (reference ?? "") + String(amount ?? 0) }
    }

    struct Register: Hashable {
        let tenders: [Tender]
        let all: Double?
        let card: Double?
        let cash: Double?
        let cardTips: Double?
        let cardTipFees: Double?
        let paidOut: Double?
        let paidIn: Double?
        let payouts: [Payout]
        let payoutsNote: String?
    }

    struct Department: Hashable, Identifiable {
        let name: String
        let hours: Double?
        let cost: Double?
        let pctOfSales: Double?
        let roles: [String]
        var salaried = false
        var id: String { name }
    }

    struct Unmapped: Hashable, Identifiable {
        let department: String
        let net: Double?
        /// The mapped department this one is new inside ("Other › Pool").
        let newIn: String?
        let mappedTo: String?
        let posCategory: String?
        var id: String { department }
    }

    private static func splits(_ v: JSONValue?) -> [Split] {
        (v?.array ?? []).compactMap { x in
            guard let name = x["name"]?.string else { return nil }
            return Split(name: name, net: x["net"]?.double, guests: x["guests"]?.double, checks: x["checks"]?.double,
                         perGuest: x["per_guest"]?.double, sharePct: x["share_pct"]?.double)
        }
    }

    /// Meal periods (the POS's own tags, late night apart when set).
    var dayparts: [Split] { Self.splits(detail["dayparts"]) }
    /// Rooms — the POS's profit centres.
    var rooms: [Split] { Self.splits(detail["rooms"]) }
    /// The meal-period split is worth drawing: more than one period, or one
    /// that isn't the POS's "Other" (the web's rule).
    var showsDayparts: Bool {
        let d = dayparts
        return d.count > 1 || (d.count == 1 && d[0].name != "Other")
    }

    var servers: [Server] {
        (detail["servers"]?.array ?? []).compactMap { x in
            guard let name = x["name"]?.string else { return nil }
            return Server(name: name, checks: x["checks"]?.double, guests: x["guests"]?.double, net: x["net"]?.double,
                          perGuest: x["per_guest"]?.double, drinksPerGuest: x["drinks_per_guest"]?.double,
                          tipPct: x["tip_pct"]?.double, hours: x["hours"]?.double,
                          netPerHour: x["net_per_hour"]?.double, vsFloor: x["vs_floor"]?.double)
        }
    }
    var serversBelowFloor: Int? { detail["servers_below_floor"]?.int }
    var serversMinChecks: Int? { detail["servers_min_checks"]?.int }
    var serversBasis: String? { detail["servers_basis"]?.string }

    private static func lossGroup(_ v: JSONValue?, given: Bool) -> LossGroup? {
        guard let v, v.object != nil else { return nil }
        let reasons = (v["by_reason"]?.array ?? []).compactMap { r -> LossLine? in
            guard let reason = r["reason"]?.string else { return nil }
            let kind = r["kind"]?.string
            let label = given ? "\(DSRNightDetail.cap(kind ?? "")) \u{00B7} \(reason)" : reason
            return LossLine(kind: given ? kind : nil, label: label, lines: r["lines"]?.double, amount: r["amount"]?.double)
        }
        let approvers = (v["by_approver"]?.array ?? []).compactMap { r -> LossLine? in
            guard let who = r["approver"]?.string else { return nil }
            return LossLine(kind: nil, label: who, lines: r["lines"]?.double, amount: r["amount"]?.double)
        }
        return LossGroup(total: v["total"]?.double, lines: v["lines"]?.double, pctOfGross: v["pct_of_gross"]?.double,
                         byReason: reasons, byApprover: approvers)
    }

    /// Comps, discounts and refunds — nil when this login wasn't sent them
    /// (the comps-and-voids permission) or the night had no loss read.
    var lossGiven: LossGroup? { Self.lossGroup(detail["loss"]?["given"], given: true) }
    /// Voided lines — never reached the bill, so listed apart.
    var lossVoids: LossGroup? { Self.lossGroup(detail["loss"]?["voids"], given: false) }

    /// The punches a manager edited — nil when the payload has no list (a
    /// non-owner view), an empty list when nobody edited one.
    var punchEdits: [PunchEdit]? {
        guard case .array(let list)? = detail["timeclock_edits"] else { return nil }
        return list.map { x in
            PunchEdit(employee: x["employee"]?.string ?? "Unknown", role: x["role"]?.string,
                      clockIn: x["clock_in"]?.string, clockOut: x["clock_out"]?.string, hours: x["hours"]?.double,
                      editedBy: x["edited_by"]?.string ?? "Not recorded", editedAt: x["edited_at"]?.string,
                      code: x["code"]?.string)
        }
    }

    var register: Register? {
        guard let r = detail["register"], r.object != nil else { return nil }
        let t = r["totals"]
        let tenders = (r["tenders"]?.array ?? []).compactMap { x -> Tender? in
            guard let m = x["method"]?.string else { return nil }
            return Tender(method: m, payments: x["payments"]?.double, amount: x["amount"]?.double, tips: x["tips"]?.double)
        }
        let payouts = (r["payouts"]?.array ?? []).map { x in
            Payout(category: x["category"]?.string ?? "Not recorded", isPayIn: x["type"]?.string == "pay-in",
                   amount: x["amount"]?.double, approvedBy: x["approved_by"]?.string ?? "Not recorded",
                   at: x["at"]?.string, reference: x["reference"]?.string)
        }
        return Register(tenders: tenders, all: t?["all"]?.double, card: t?["card"]?.double, cash: t?["cash"]?.double,
                        cardTips: t?["card_tips"]?.double, cardTipFees: t?["card_tip_fees"]?.double,
                        paidOut: t?["payouts"]?.double, paidIn: t?["payins"]?.double, payouts: payouts,
                        payoutsNote: r["payouts_note"]?.string)
    }

    // Labor

    /// Where the labor went, by department — and, for the owner, the
    /// salaries as their own slice (`salaried_cost` is owner-only).
    var departments: [Department] {
        var out = (detail["departments"]?.array ?? []).compactMap { x -> Department? in
            guard let name = x["department"]?.string else { return nil }
            return Department(name: name, hours: x["hours"]?.double, cost: x["cost"]?.double,
                              pctOfSales: x["pct_of_sales"]?.double,
                              roles: x["roles"]?.array.compactMap(\.string) ?? [])
        }
        let withCost = out.contains { $0.cost != nil }
        if withCost, !out.isEmpty, let sal = metric("salaried_cost") {
            out.append(Department(name: "Salaried", hours: nil, cost: sal, pctOfSales: nil, roles: [], salaried: true))
        }
        return out
    }
    var departmentsBasis: String? { detail["departments_basis"]?.string }

    // Sales

    /// The night's slowest sellers (block_sales bottom_items) — the web's
    /// "Slowest" table under Top items.
    var slowestItems: [Item] {
        (detail["bottom_items"]?.array ?? []).compactMap { x in
            guard let name = x["name"]?.string else { return nil }
            return Item(name: name, qty: x["qty"]?.double, net: x["net"]?.double)
        }
    }

    /// POS departments the report couldn't place — never guessed into a
    /// category — and departments new inside one the owner mapped.
    var unmappedDepartments: [Unmapped] {
        (detail["unmapped"]?.array ?? []).compactMap { x in
            guard let d = x["department"]?.string else { return nil }
            return Unmapped(department: d, net: x["net"]?.double, newIn: x["new_in"]?.string,
                            mappedTo: x["mapped_to"]?.string, posCategory: x["pos_category"]?.string)
        }
    }
    /// Net the POS put in no department.
    var unallocated: Double? { detail["unallocated"]?.double }
}

// MARK: - The hour-by-hour story

/// One night, hour by hour (the web's hourData / hxCallouts / hxStory):
/// net sales, a usual same weekday when two finished reports exist for it,
/// and the people on the clock. Every callout is arithmetic over those
/// three series — nothing here is new data.
struct DSRHourStory: Equatable {
    struct Row: Equatable, Identifiable {
        let hour: Int
        let net: Double
        let usual: Double?
        let hours: Double?
        var splh: Double? { (hours ?? 0) >= 1 ? net / hours! : nil }
        var id: Int { hour }
    }

    struct Callout: Equatable, Identifiable {
        enum Tone: Equatable { case info, good, warn, bad }
        let index: Int
        let kind: String
        let tone: Tone
        let title: String
        let text: String
        var id: String { kind + String(index) }
    }

    let rows: [Row]
    let typicalNights: Int?
    let hasLabor: Bool

    /// Nil unless the Sales block is ready with at least three hours.
    init?(sales: DSRBlock?, labor: DSRBlock?) {
        guard let sales, sales.isReady else { return nil }
        let hourly = sales.detail["hourly"]?.array ?? []
        guard hourly.count >= 3 else { return nil }
        let typical = sales.detail["hourly_typical"]?["hours"]?.object
        let laborHours = (labor?.isReady == true) ? labor?.detail["hourly_hours"]?.object : nil
        rows = hourly.compactMap { h in
            guard let hr = h["hour"]?.int else { return nil }
            let key = String(hr)
            return Row(hour: hr, net: h["net"]?.double ?? 0, usual: typical?[key]?.double, hours: laborHours?[key]?.double)
        }
        guard rows.count >= 3 else { return nil }
        typicalNights = typical == nil ? nil : sales.detail["hourly_typical"]?["nights"]?.int
        hasLabor = laborHours != nil
    }

    var hasTypical: Bool { rows.contains { $0.usual != nil } }

    var peak: Row { rows.max { $0.net < $1.net } ?? rows[0] }

    /// "6pm", "12pm", "11am" — the web's hourLabel(h, true).
    static func hourLong(_ hour: Int) -> String {
        let h = ((hour % 24) + 24) % 24
        return "\(h % 12 == 0 ? 12 : h % 12)\(h < 12 ? "am" : "pm")"
    }

    /// Sales per labor hour across the night (hours ≥ 0 only where staffed).
    var nightSPLH: Double? {
        let staffed = rows.filter { ($0.hours ?? 0) > 0 }
        let hrs = staffed.reduce(0) { $0 + ($1.hours ?? 0) }
        guard hrs > 0 else { return nil }
        return staffed.reduce(0) { $0 + $1.net } / hrs
    }

    /// At most four, left to right — the web's hxCallouts, rule for rule.
    var callouts: [Callout] {
        let n = rows.count
        let pk = rows.firstIndex(of: peak) ?? 0
        let peakRow = rows[pk]
        var out: [Callout] = []
        let money = DSRFormat.money
        // The rush is the climb INTO the peak: walk back while sales rose,
        // then take that climb's biggest jump.
        var st = pk
        while st > 0 && rows[st - 1].net < rows[st].net { st -= 1 }
        var rush: Int?
        if st + 1 < pk {
            for i in (st + 1)..<pk {
                let r = rows[i], pv = rows[i - 1]
                guard pv.net > 0, r.net >= 1.5 * pv.net, r.net >= 0.45 * peakRow.net else { continue }
                if let k = rush, r.net / pv.net <= rows[k].net / rows[k - 1].net { continue }
                rush = i
            }
        }
        if let k = rush {
            let r = rows[k]
            out.append(Callout(index: k, kind: "rush", tone: .info,
                               title: r.hour >= 15 ? "Dinner rush begins" : "The rush begins",
                               text: "\(money(r.net)) at \(Self.hourLong(r.hour)), up from \(money(rows[k - 1].net)) the hour before."))
        }
        var spike: Int?
        for i in 0..<n where i != pk {
            let r = rows[i]
            guard let u = r.usual, u >= 0.1 * peakRow.net, r.net >= 1.25 * u else { continue }
            if let k = spike, r.net / u <= rows[k].net / (rows[k].usual ?? 1) { continue }
            spike = i
        }
        if let k = spike, let u = rows[k].usual {
            out.append(Callout(index: k, kind: "spike", tone: .good,
                               title: "\(Self.hourLong(rows[k].hour)) ran \(Int(((rows[k].net / u - 1) * 100).rounded()))% above usual",
                               text: "\(money(rows[k].net)) against a usual night\u{2019}s \(money(u))."))
        }
        let staffedHours = rows.compactMap(\.hours).filter { $0 > 0 }.sorted()
        let median = staffedHours.isEmpty ? nil : staffedHours[staffedHours.count / 2]
        var dip: Int?
        for i in 0..<n {
            let r = rows[i]
            guard let u = r.usual, u >= 0.15 * peakRow.net, r.net <= 0.8 * u else { continue }
            if let k = dip, u - r.net <= (rows[k].usual ?? 0) - rows[k].net { continue }
            dip = i
        }
        if let k = dip, let u = rows[k].usual {
            let r = rows[k]
            let staffed = r.hours != nil && median != nil && r.hours! >= median!
            out.append(Callout(index: k, kind: staffed ? "dip" : "missed", tone: .bad,
                               title: staffed ? "Sales dipped despite full staffing" : "Missed revenue at \(Self.hourLong(r.hour))",
                               text: "\(Self.hourLong(r.hour)) came in \(money(u - r.net)) below a usual night"
                                   + (staffed ? ", with \(String(format: "%.1f", r.hours!)) hours on the clock." : ".")))
        }
        if let night = nightSPLH {
            var lo: Int?
            for i in 0..<n {
                guard let s = rows[i].splh, (rows[i].hours ?? 0) >= 2, s < 0.6 * night else { continue }
                if let k = lo, s >= (rows[k].splh ?? .infinity) { continue }
                lo = i
            }
            let top = rows.enumerated().sorted { $0.element.net > $1.element.net }.prefix(3)
            let ok = top.first { e in
                guard let s = e.element.splh, (e.element.hours ?? 0) >= 2 else { return false }
                return abs(s / night - 1) <= 0.15
            }?.offset
            if let k = lo, k != dip {
                out.append(Callout(index: k, kind: "labor", tone: .warn, title: "Labor ran ahead of demand",
                                   text: "\(Self.hourLong(rows[k].hour)): \(money(rows[k].splh)) per labor hour against \(money(night)) for the night."))
            }
            if let k = ok {
                out.append(Callout(index: k, kind: "matched", tone: .good, title: "Staffing matched demand",
                                   text: "\(Self.hourLong(rows[k].hour)): \(money(rows[k].splh)) per labor hour, right on the night\u{2019}s \(money(night))."))
            }
        }
        return Array(out.sorted { $0.index < $1.index }.prefix(4))
    }

    /// One sentence: where the rush began, the peak against a usual night,
    /// and the first hour that ran ahead of demand or under it.
    func story(weekday: String?) -> String {
        let wd = weekday ?? "night"
        let cs = callouts
        let p = peak
        var s = ""
        if let rush = cs.first(where: { $0.kind == "rush" }) {
            s = "The rush began at \(Self.hourLong(rows[rush.index].hour)) and peaked"
        } else {
            s = "Sales peaked"
        }
        s += " at \(Self.hourLong(p.hour)) with \(DSRFormat.money(p.net))"
        if let u = p.usual, u > 0 {
            let d = Int(((p.net / u - 1) * 100).rounded())
            s += d == 0 ? ", right on a usual \(wd)." : ", \(abs(d))% \(d > 0 ? "above" : "below") a usual \(wd)."
        } else {
            s += "."
        }
        for c in cs {
            if c.kind == "labor" { s += " Staffing ran ahead of demand at \(Self.hourLong(rows[c.index].hour))."; break }
            if c.kind == "dip" || c.kind == "missed" {
                s += " \(Self.hourLong(rows[c.index].hour)) came in below a usual \(wd)."
                break
            }
        }
        return s
    }
}

// MARK: - The card's body

/// Whether the hour chart is being scrubbed — read by the report's swipe
/// between nights, so dragging a finger along the chart never changes the
/// night (10/8/26). `lastScrubAt` covers the two gestures ending in either
/// order.
@Observable
final class DSRScrubState {
    var active = false
    var lastScrubAt: Date = .distantPast

    /// True while scrubbing and for half a second after.
    var recentlyScrubbed: Bool { active || Date().timeIntervalSince(lastScrubAt) < 0.5 }
}

/// What the "The night in detail" card shows when opened (10/8/26, "Web
/// explains. iPhone decides."): the hour story and its chart, the top three
/// servers, what was given away, voided and how many punches were edited.
/// The rest — the money split, where the labor went, every server, given
/// away by reason and approver, each edited punch, cash and cards — is the
/// web's, one link to this night (re-audit D12). Each part only when the
/// payload carries it.
struct DSRNightDetail: View {
    let service: DSRBlock
    let blocks: [String: DSRBlock]
    var businessDate: String?
    @State private var openServer: DSRBlock.Server?

    /// Servers shown on the phone; every one is on the web.
    static let serversShown = 3

    /// What the web link holds, in an owner's words — only what this
    /// night's payload carries.
    static func webSubtitle(service: DSRBlock, blocks: [String: DSRBlock]) -> String? {
        var parts: [String] = []
        if !(service.showsDayparts ? service.dayparts : []).isEmpty || !service.rooms.isEmpty {
            parts.append("sales by meal period and room")
        }
        if let labor = blocks["labor"], labor.isReady, !labor.departments.isEmpty { parts.append("where the labor went") }
        if service.servers.count > serversShown { parts.append("all \(service.servers.count) servers") }
        if service.lossGiven != nil { parts.append("given away by reason and approver") }
        if !(service.punchEdits ?? []).isEmpty { parts.append("each edited punch") }
        if service.register != nil { parts.append("cash and cards") }
        guard !parts.isEmpty else { return nil }
        return cap(DSRText.list(parts))
    }

    static func cap(_ s: String) -> String { s.prefix(1).uppercased() + s.dropFirst() }

    /// "3 punches edited" / "No punch was edited" — nil when the payload
    /// carries no list (a non-owner view).
    static func punchSummary(_ edits: [DSRBlock.PunchEdit]?) -> String? {
        guard let edits else { return nil }
        if edits.isEmpty { return "No punch was edited for this night." }
        return "\(edits.count) punch\(edits.count == 1 ? "" : "es") edited by a manager"
    }

    private var hasFullDetail: Bool {
        let dp = service.showsDayparts ? service.dayparts : []
        let labor = blocks["labor"]
        return !dp.isEmpty || !service.rooms.isEmpty
            || (labor?.isReady == true && !(labor?.departments.isEmpty ?? true))
            || service.servers.count > Self.serversShown
            || service.lossGiven != nil
            || !(service.punchEdits ?? []).isEmpty
            || service.register != nil
    }

    /// The web's address for THIS night (re-audit D2): "dsr" alone opens the
    /// latest night, so a past night's link names its date (nav.py
    /// "dsr/night/<date>"; dashboard.html's 'dsr' handler reads it).
    static func webPath(_ businessDate: String?) -> String {
        guard let d = businessDate, DSRFormat.isISODate(d) else { return "dsr" }
        return "dsr/night/" + d
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.l) {
            if let story = DSRHourStory(sales: blocks["sales"], labor: blocks["labor"]) {
                DSRHourStoryView(story: story, weekday: businessDate.flatMap { DSRFormat.weekday($0) })
            }
            servers(Array(service.servers.prefix(Self.serversShown)), header: true)
            glance
            // The audit view — the money split, where the labor went, every
            // server, given away by reason and approver, each edited punch,
            // cash and cards — is the web's (re-audit D12, "Web explains.
            // iPhone decides."): one link to this night there.
            if hasFullDetail {
                CavnarWebLinkRow(title: "Every detail of the night", subtitle: Self.webSubtitle(service: service, blocks: blocks),
                                 path: Self.webPath(businessDate), actionLabel: "Open on the web")
            }
        }
        .sheet(item: $openServer) { s in
            DSRServerSheet(server: s, floor: service.metric("server_floor_per_guest"))
                .presentationDetents([.medium])
                .presentationDragIndicator(.visible)
        }
    }

    /// "4 lines voided · $86" — voids are apart from what was given away,
    /// and said whether or not anything was (re-audit D27). Nil when none.
    static func voidLine(_ voids: DSRBlock.LossGroup?) -> String? {
        guard let v = voids, let n = v.lines, n > 0 else { return nil }
        let count = DSRFormat.count(n)
        return "\(count) line\(n == 1 ? "" : "s") voided" + (v.total.map { " \u{00B7} \(DSRFormat.money($0))" } ?? "")
    }

    /// The given-away total, the voided lines and the punch-edit count, a
    /// line each.
    @ViewBuilder
    private var glance: some View {
        let given = service.lossGiven
        let voided = Self.voidLine(service.lossVoids)
        let punches = Self.punchSummary(service.punchEdits)
        if given != nil || voided != nil || punches != nil {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                if let given {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        Text(DSRFormat.money(given.total)).cavnarText(.figureS)
                        HomeMixedText.make("given away in comps, discounts and refunds"
                                           + (given.pctOfGross.map { " \u{00B7} \(DSRFormat.pct($0)) of gross" } ?? ""),
                                           role: .secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                if let voided {
                    HomeMixedText.make(voided, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let punches {
                    HomeMixedText.make(punches, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    @ViewBuilder
    private func servers(_ list: [DSRBlock.Server], header: Bool) -> some View {
        if !list.isEmpty {
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                if header {
                    CavnarKicker(service.servers.count > Self.serversShown ? "Top servers and bartenders"
                                                                          : "Servers and bartenders")
                }
                VStack(spacing: 0) {
                    ForEach(Array(list.enumerated()), id: \.element.id) { i, s in
                        Button {
                            Haptic.light()
                            openServer = s
                        } label: { serverRow(s) }
                        .buttonStyle(.plain)
                        .accessibilityHint("Opens \(s.name)'s night")
                        if i < list.count - 1 { AccountRowDivider() }
                    }
                }
                if !header {
                    if let below = service.serversBelowFloor, below > 0, let min = service.serversMinChecks {
                        HomeMixedText.make("\(below) with fewer than \(min) checks not shown", role: .caption, color: .cavnarInk2)
                    }
                    if let floor = service.metric("server_floor_per_guest") {
                        HomeMixedText.make("Per guest is set against the floor\u{2019}s \(DSRFormat.money(floor)).",
                                           role: .caption, color: .cavnarInk2)
                    }
                    if let basis = service.serversBasis {
                        Text(Self.cap(basis)).cavnarText(.caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    private func serverRow(_ s: DSRBlock.Server) -> some View {
        HStack(spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text(s.name).cavnarText(.label)
                HomeMixedText.make("\(DSRFormat.count(s.checks)) checks \u{00B7} \(DSRFormat.count(s.guests)) guests",
                                   role: .caption, color: .cavnarInk2)
            }
            Spacer(minLength: CavnarSpace.xs)
            VStack(alignment: .trailing, spacing: 2) {
                Text(DSRFormat.money(s.net)).cavnarText(.figureS)
                Text("\(DSRFormat.money(s.perGuest)) a guest")
                    .font(.cavnarNumber(CavnarType.caption, weight: 600))
                    .foregroundStyle(DSRServerSheet.floorTone(s.vsFloor))
            }
            Image(systemName: "chevron.right").font(.cavnar(.caption)).foregroundStyle(Color.cavnarInk3)
                .accessibilityHidden(true)
        }
        .padding(.vertical, 9)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Parts

/// The hour-by-hour chart: ember bars of net sales, the usual same weekday
/// dashed, the people on the clock on their own right-hand scale, the peak
/// hour lit and its figure over it, then "What stood out".
struct DSRHourStoryView: View {
    let story: DSRHourStory
    let weekday: String?
    /// A callout's number badge grows with Dynamic Type (W11).
    @ScaledMetric(relativeTo: .caption) private var badgeSize: CGFloat = 24
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(DSRScrubState.self) private var scrub: DSRScrubState?
    @State private var grown = false
    @State private var selected: Int?

    /// Press and hold, then drag (10/8/26): a plain drag from a finger
    /// down on the chart trapped the report's scroll, so the scrub waits
    /// for a hold and a quick swipe still scrolls the page.
    private func scrubGesture(_ proxy: ChartProxy, _ geo: GeometryProxy) -> some Gesture {
        LongPressGesture(minimumDuration: 0.25)
            .sequenced(before: DragGesture(minimumDistance: 0))
            .onChanged { value in
                switch value {
                case .first(true):
                    scrub?.active = true
                case .second(true, let drag?):
                    scrub?.active = true
                    guard let plot = proxy.plotFrame else { return }
                    let x = drag.location.x - geo[plot].origin.x
                    guard let label: String = proxy.value(atX: x),
                          let r = story.rows.first(where: { DSRHourStory.hourLong($0.hour) == label }) else { return }
                    if selected != r.hour { Haptic.selection() }
                    selected = r.hour
                default:
                    break
                }
            }
            .onEnded { _ in
                selected = nil
                scrub?.active = false
                scrub?.lastScrubAt = Date()
            }
    }

    private var maxNet: Double { max(1, story.rows.map { max($0.net, $0.usual ?? 0) }.max() ?? 1) }
    private var maxHours: Double { max(1, story.rows.compactMap(\.hours).max() ?? 1) }
    /// People-on-the-clock drawn on the dollar axis, scaled so the busiest
    /// hour's headcount sits at 90% of the chart (the web's yl()).
    private var hoursScale: Double { maxNet * 1.05 * 0.9 / (maxHours * 1.15) }

    var body: some View {
        let peak = story.peak
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Hour by hour")
            CavnarMixedText(story.story(weekday: weekday), role: .lead)
            Chart {
                ForEach(story.rows) { r in
                    BarMark(x: .value("Hour", DSRHourStory.hourLong(r.hour)), y: .value("Net", grown || reduceMotion ? r.net : 0))
                        .foregroundStyle(LinearGradient(colors: [r == peak ? Color.cavnarEmber2 : Color.cavnarEmber,
                                                                 Color.cavnarEmber.opacity(0.3)],
                                                        startPoint: .top, endPoint: .bottom))
                        .cornerRadius(4)
                        .annotation(position: .top) {
                            if r == peak {
                                Text(DSRFormat.money(r.net)).font(.cavnarNumber(CavnarType.caption, weight: 700))
                                    .foregroundStyle(Color.cavnarEmber2)
                            }
                        }
                    if let u = r.usual {
                        LineMark(x: .value("Hour", DSRHourStory.hourLong(r.hour)), y: .value("Usual", u),
                                 series: .value("Series", "usual"))
                            .foregroundStyle(Color.cavnarInk3)
                            .lineStyle(StrokeStyle(lineWidth: 1.5, dash: [4, 3]))
                            .interpolationMethod(.catmullRom)
                    }
                    if let h = r.hours {
                        LineMark(x: .value("Hour", DSRHourStory.hourLong(r.hour)), y: .value("People", h * hoursScale),
                                 series: .value("Series", "people"))
                            .foregroundStyle(Color.cavnarAmber)
                            .lineStyle(StrokeStyle(lineWidth: 2))
                            .interpolationMethod(.catmullRom)
                            .symbol(.circle)
                            .symbolSize(18)
                    }
                }
                if let s = selected, let r = story.rows.first(where: { $0.hour == s }) {
                    RuleMark(x: .value("Hour", DSRHourStory.hourLong(r.hour)))
                        .foregroundStyle(Color.cavnarInk3.opacity(0.4))
                        .annotation(position: .top, overflowResolution: .init(x: .fit, y: .disabled)) {
                            tooltip(r)
                        }
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading) { v in
                    AxisGridLine().foregroundStyle(Color.cavnarPaper3.opacity(0.4))
                    // Chart axes: 11pt, the floor's one exception (§2).
                    AxisValueLabel {
                        if let d = v.as(Double.self) { Text(Self.short(d)).font(.cavnarNumber(CavnarType.tag)).foregroundStyle(Color.cavnarInk2) }
                    }
                }
                if story.hasLabor {
                    AxisMarks(position: .trailing, values: [0, maxHours * hoursScale]) { v in
                        AxisValueLabel {
                            if let d = v.as(Double.self) {
                                Text(String(Int((d / hoursScale).rounded()))).font(.cavnarNumber(CavnarType.tag)).foregroundStyle(Color.cavnarAmber)
                            }
                        }
                    }
                }
            }
            .chartXAxis {
                AxisMarks { _ in AxisValueLabel().font(.cavnarNumber(CavnarType.tag)).foregroundStyle(Color.cavnarInk2) }
            }
            .chartOverlay { proxy in
                GeometryReader { geo in
                    Rectangle().fill(Color.clear).contentShape(Rectangle())
                        .gesture(scrubGesture(proxy, geo))
                }
            }
            .frame(height: 210)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Net sales by hour")
            .accessibilityValue("Peak at \(DSRHourStory.hourLong(peak.hour)), \(DSRFormat.money(peak.net))")
            .accessibilityHint("Press and hold, then drag, to read each hour")
            legend
            let cs = story.callouts
            if !cs.isEmpty {
                VStack(alignment: .leading, spacing: CavnarSpace.s) {
                    CavnarKicker("What stood out")
                    ForEach(Array(cs.enumerated()), id: \.element.id) { i, c in
                        HStack(alignment: .top, spacing: CavnarSpace.s) {
                            Text(story.rows[c.index] == peak ? "\u{2605}" : "\(i + 1)")
                                .font(.cavnarNumber(CavnarType.caption, weight: 700))
                                .foregroundStyle(Self.tone(c.tone))
                                .frame(width: badgeSize, height: badgeSize)
                                .background(Self.tone(c.tone).opacity(0.14), in: Circle())
                            VStack(alignment: .leading, spacing: 2) {
                                Text(c.title).cavnarText(.label)
                                CavnarMixedText(c.text, role: .secondary)
                            }
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            if !story.hasTypical {
                Text("A usual night shows here once two finished reports exist for this weekday.")
                    .cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onAppear {
            if reduceMotion { grown = true } else { withAnimation(.easeOut(duration: 0.6)) { grown = true } }
        }
    }

    private var legend: some View {
        AccountFlowLayout(spacing: 12) {
            legendItem(Color.cavnarEmber, "Net sales", dashed: false, bar: true)
            if story.hasLabor { legendItem(Color.cavnarAmber, "People on the clock", dashed: false, bar: false) }
            if story.hasTypical {
                legendItem(Color.cavnarInk3, "A usual \(weekday ?? "night")"
                           + (story.typicalNights.map { " \u{00B7} median of \($0)" } ?? ""), dashed: true, bar: false)
            }
        }
    }

    private func legendItem(_ color: Color, _ text: String, dashed: Bool, bar: Bool) -> some View {
        HStack(spacing: 5) {
            if bar {
                RoundedRectangle(cornerRadius: 2).fill(color).frame(width: 9, height: 9)
            } else {
                Rectangle().fill(color).frame(width: 14, height: 2).opacity(dashed ? 0.7 : 1)
            }
            HomeMixedText.make(text, role: .caption, color: .cavnarInk2)
        }
    }

    private func tooltip(_ r: DSRHourStory.Row) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(DSRHourStory.hourLong(r.hour)).font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarInk2)
            Text(DSRFormat.money(r.net)).cavnarText(.figureS)
            if let u = r.usual { HomeMixedText.make("Usual \(DSRFormat.money(u))", role: .caption, color: .cavnarInk2) }
            if let h = r.hours { HomeMixedText.make("\(String(format: "%.1f", h)) labor hours", role: .caption, color: .cavnarInk2) }
            if let s = r.splh { HomeMixedText.make("\(DSRFormat.money(s)) per labor hour", role: .caption, color: .cavnarInk2) }
        }
        .padding(8)
        .background(Color.cavnarPaper2.opacity(0.96), in: RoundedRectangle(cornerRadius: CavnarRadius.control))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1))
    }

    static func tone(_ t: DSRHourStory.Callout.Tone) -> Color {
        switch t {
        case .info: return .cavnarEmber2
        case .good: return .cavnarGreen
        case .warn: return .cavnarAmber
        case .bad: return .cavnarRed
        }
    }

    /// "$1.2k" / "$800" on the axis.
    static func short(_ v: Double) -> String {
        let a = abs(v)
        if a >= 1000 {
            let k = (a / 100).rounded() / 10
            return "$" + (k == k.rounded() ? String(Int(k)) : String(k)) + "k"
        }
        return "$\(Int(a.rounded()))"
    }
}

/// One split — meal periods or rooms — as bars against the biggest, each
/// with its share, guests, checks and spend per guest.
struct DSRSplitBars: View {
    let title: String
    let rows: [DSRBlock.Split]

    var body: some View {
        let top = max(1, rows.compactMap(\.net).max() ?? 1)
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk2)
            ForEach(rows) { r in
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text(r.name).cavnarText(.body, color: .cavnarInk)
                        Spacer(minLength: 8)
                        Text(DSRFormat.money(r.net)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                        Text(DSRFormat.pct(r.sharePct)).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                            .fixedSize().frame(minWidth: 52, alignment: .trailing).layoutPriority(1)
                    }
                    GeometryReader { geo in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Color.cavnarPaper3.opacity(0.6))
                            Capsule()
                                .fill(LinearGradient(colors: [Color.cavnarEmber, Color.cavnarEmber2], startPoint: .leading, endPoint: .trailing))
                                .frame(width: max(4, geo.size.width * CGFloat((r.net ?? 0) / top)))
                        }
                    }
                    .frame(height: 6)
                    HomeMixedText.make("\(DSRFormat.count(r.guests)) guest\(r.guests == 1 ? "" : "s") \u{00B7} \(DSRFormat.count(r.checks)) checks"
                                       + (r.perGuest.map { " \u{00B7} \(DSRFormat.money($0)) a guest" } ?? ""),
                                       role: .caption, color: .cavnarInk2)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

/// Where the labor went: one stacked bar by department (cost when the
/// payroll is known, else hours), then each department's hours, cost and
/// share of sales — the salaries their own slice for the owner.
struct DSRLaborDepartments: View {
    let labor: DSRBlock
    let salesNet: Double?

    private static let tones: [Color] = [.cavnarEmber, .cavnarEmber2, .cavnarAmber, .cavnarInk2]

    var body: some View {
        let ds = labor.departments
        let withCost = ds.contains { $0.cost != nil && !$0.salaried }
        let weights = ds.map { withCost ? ($0.cost ?? 0) : ($0.hours ?? 0) }
        let total = max(weights.reduce(0, +), 1)
        VStack(alignment: .leading, spacing: 12) {
            CavnarKicker("Where the labor went")
            GeometryReader { geo in
                HStack(spacing: 2) {
                    ForEach(Array(ds.enumerated()), id: \.element.id) { i, d in
                        Rectangle().fill(color(i, d))
                            .frame(width: max(2, geo.size.width * CGFloat(weights[i] / total) - 2))
                    }
                }
            }
            .frame(height: 10)
            .clipShape(Capsule())
            .accessibilityHidden(true)
            VStack(spacing: 0) {
                ForEach(Array(ds.enumerated()), id: \.element.id) { i, d in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Circle().fill(color(i, d)).frame(width: 8, height: 8)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(d.name).cavnarText(.body, color: .cavnarInk)
                            if !d.roles.isEmpty {
                                Text(d.roles.joined(separator: ", ")).cavnarText(.caption, color: .cavnarInk2)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        Spacer(minLength: 6)
                        if let h = d.hours {
                            Text("\(String(format: "%.1f", h))h").font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                        }
                        Text(DSRFormat.money(d.cost)).font(.cavnarNumber(CavnarType.secondary, weight: 600))
                            .foregroundStyle(d.cost == nil ? Color.cavnarInk3 : Color.cavnarInk)
                            .fixedSize().frame(minWidth: 70, alignment: .trailing).layoutPriority(1)
                        Text(pctOfSales(d).map { DSRFormat.pct($0) } ?? "")
                            .font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                            .fixedSize().frame(minWidth: 48, alignment: .trailing).layoutPriority(1)
                    }
                    .padding(.vertical, 7)
                    .accessibilityElement(children: .combine)
                }
            }
            if withCost, let sal = labor.metric("salaried_cost") {
                let all = ds.filter { !$0.salaried }.compactMap(\.cost).reduce(0, +) + sal
                HomeMixedText.make("\(DSRFormat.money(all)) all in, salaries included.", role: .secondary)
            }
            if let basis = labor.departmentsBasis {
                Text(DSRNightDetail.cap(basis)).cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func color(_ i: Int, _ d: DSRBlock.Department) -> Color {
        d.salaried ? Color.cavnarInk3.opacity(0.6) : Self.tones[i % Self.tones.count]
    }

    /// A salaried slice's share is its cost over the night's net (the web's rule).
    private func pctOfSales(_ d: DSRBlock.Department) -> Double? {
        if let p = d.pctOfSales { return p }
        guard d.salaried, let c = d.cost, let net = salesNet, net > 0 else { return nil }
        return c / net * 100
    }
}

/// Comps, discounts and refunds by reason and by who approved them, and
/// voided lines apart — only in a payload that carries `loss`. The total
/// sits above "Full detail" (DSRNightDetail's glance); this is the split.
struct DSRLossSection: View {
    let given: DSRBlock.LossGroup?
    let voids: DSRBlock.LossGroup?

    /// Voids render on their own, whether or not anything was given away
    /// (re-audit D27: they sat inside `if let given`).
    private var hasVoids: Bool { (voids?.lines ?? 0) > 0 }

    var body: some View {
        if given != nil || hasVoids {
            VStack(alignment: .leading, spacing: 12) {
                if let given {
                    CavnarKicker("Given away")
                    if !given.byReason.isEmpty { DSRLossLines(title: "By reason", lines: given.byReason) }
                    if !given.byApprover.isEmpty { DSRLossLines(title: "Approved by", lines: given.byApprover) }
                }
                if let voids, hasVoids {
                    if given == nil { CavnarKicker("Voided") }
                    DSRLossLines(title: given == nil ? "By reason" : "Voided", lines: voids.byReason)
                }
            }
        }
    }
}

struct DSRLossLines: View {
    let title: String
    let lines: [DSRBlock.LossLine]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk2)
            ForEach(lines) { l in
                HStack(alignment: .firstTextBaseline) {
                    HomeMixedText.make(l.label, role: .secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Text(DSRFormat.count(l.lines)).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                    Text(DSRFormat.money(l.amount)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                        .fixedSize().frame(minWidth: 76, alignment: .trailing).layoutPriority(1)
                }
                .padding(.vertical, 3)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

/// Punches a manager edited — the owner's check on who edits them. The
/// count sits above "Full detail"; each punch is here.
struct DSRPunchEdits: View {
    let edits: [DSRBlock.PunchEdit]
    static let shown = 8

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            CavnarKicker("Punch edits")
            if edits.isEmpty {
                Text("No punch was edited for this night.").cavnarText(.secondary)
            } else {
                ForEach(edits.prefix(Self.shown)) { e in
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            Text(e.employee).cavnarText(.label)
                            if let role = e.role { Text(role).cavnarText(.caption, color: .cavnarInk2) }
                        }
                        HomeMixedText.make(Self.line(e), role: .caption, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                if edits.count > Self.shown {
                    HomeMixedText.make("\(edits.count - Self.shown) more in the POS.", role: .caption, color: .cavnarInk2)
                }
            }
        }
    }

    /// "4:02pm–11:15pm · 7.22h · edited by Dana at 11:40pm" — the POS's own
    /// edit code is left out (10/8/26: a code number means nothing to an owner).
    static func line(_ e: DSRBlock.PunchEdit) -> String {
        var parts: [String] = []
        if let i = DSRFormat.localTime(e.clockIn) {
            parts.append("\(i)\u{2013}\(DSRFormat.localTime(e.clockOut) ?? "open")")
        }
        if let h = e.hours { parts.append(String(format: "%.2fh", h)) }
        parts.append("edited by \(e.editedBy)" + (DSRFormat.localTime(e.editedAt).map { " at \($0)" } ?? ""))
        return parts.joined(separator: " \u{00B7} ")
    }
}

/// Cash and cards: the tenders, card tips, and every payout and pay-in rung
/// on the POS with who approved it.
struct DSRRegisterSection: View {
    let register: DSRBlock.Register

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            CavnarKicker("Cash and cards")
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(DSRFormat.money(register.all)).cavnarText(.figureS)
                HomeMixedText.make("taken \u{00B7} cards \(DSRFormat.money(register.card)) \u{00B7} cash \(DSRFormat.money(register.cash))",
                                   role: .secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !register.tenders.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("By tender").font(.cavnarBody(CavnarType.secondary, weight: 700)).foregroundStyle(Color.cavnarInk2)
                    ForEach(register.tenders) { t in
                        HStack {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(t.method).cavnarText(.secondary)
                                if let tips = t.tips, tips > 0 {
                                    HomeMixedText.make("tips \(DSRFormat.money(tips))", role: .caption, color: .cavnarInk2)
                                }
                            }
                            Spacer(minLength: 8)
                            Text(DSRFormat.count(t.payments)).font(.cavnarNumber(CavnarType.caption)).foregroundStyle(Color.cavnarInk2)
                            Text(DSRFormat.money(t.amount)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                                .fixedSize().frame(minWidth: 80, alignment: .trailing).layoutPriority(1)
                        }
                        .accessibilityElement(children: .combine)
                    }
                }
            }
            if let tips = register.cardTips, tips > 0 {
                HomeMixedText.make("Card tips \(DSRFormat.money(tips))"
                                   + ((register.cardTipFees ?? 0) > 0 ? " \u{00B7} tip fees \(DSRFormat.money(register.cardTipFees))" : "")
                                   + ".", role: .caption, color: .cavnarInk2)
            }
            VStack(alignment: .leading, spacing: 4) {
                Text("Paid out and in").font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarInk2)
                if register.payouts.isEmpty {
                    Text(register.payoutsNote ?? "Nothing was paid out or in.").cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    ForEach(register.payouts) { p in
                        HStack(alignment: .firstTextBaseline) {
                            VStack(alignment: .leading, spacing: 1) {
                                Text(p.category + (p.isPayIn ? " \u{00B7} pay-in" : "")).cavnarText(.secondary)
                                HomeMixedText.make([p.approvedBy, DSRFormat.localTime(p.at), p.reference].compactMap { $0 }
                                                    .joined(separator: " \u{00B7} "), role: .caption, color: .cavnarInk2)
                            }
                            Spacer(minLength: 8)
                            Text(DSRFormat.money(p.amount)).font(.cavnarNumber(CavnarType.secondary, weight: 600)).foregroundStyle(Color.cavnarInk)
                        }
                        .accessibilityElement(children: .combine)
                    }
                    HomeMixedText.make("Paid out \(DSRFormat.money(register.paidOut))"
                                       + ((register.paidIn ?? 0) > 0 ? " \u{00B7} paid in \(DSRFormat.money(register.paidIn))" : "") + ".",
                                       role: .caption, color: .cavnarInk2)
                }
            }
        }
    }
}

/// One server's night, opened from the list.
struct DSRServerSheet: View {
    let server: DSRBlock.Server
    let floor: Double?

    /// Green a dollar or more over the floor's spend per guest, red a
    /// dollar or more under it.
    static func floorTone(_ vs: Double?) -> Color {
        guard let vs else { return .cavnarInk3 }
        if vs >= 1 { return .cavnarGreen }
        if vs <= -1 { return .cavnarRed }
        return .cavnarInk2
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    VStack(alignment: .leading, spacing: 4) {
                        CavnarKicker("Their night")
                        Text(DSRFormat.money(server.net)).cavnarText(.figureL)
                            .cavnarNumberGlow()
                        HomeMixedText.make("net across \(DSRFormat.count(server.checks)) checks", role: .secondary)
                    }
                    DSRTileRow(tiles: [
                        DSRStatTile(label: "Guests", value: DSRFormat.count(server.guests)),
                        DSRStatTile(label: "Per guest", value: DSRFormat.money(server.perGuest), tone: Self.floorTone(server.vsFloor),
                                    detail: server.vsFloor.flatMap { abs($0) >= 1 ? "\($0 > 0 ? "+" : "\u{2212}")\(DSRFormat.money(abs($0))) vs the floor" : nil }),
                        DSRStatTile(label: "Drinks a guest", value: server.drinksPerGuest.map { String(format: "%.2f", $0) } ?? DSRFormat.dash),
                        DSRStatTile(label: "Card tip", value: DSRFormat.pct(server.tipPct)),
                        DSRStatTile(label: "Net an hour", value: DSRFormat.money(server.netPerHour)),
                        DSRStatTile(label: "Hours", value: server.hours.map { String(format: "%.1f", $0) } ?? DSRFormat.dash),
                    ])
                    if let floor {
                        HomeMixedText.make("The floor\u{2019}s spend per guest was \(DSRFormat.money(floor)).",
                                           role: .caption, color: .cavnarInk2)
                    }
                }
                .padding(20)
            }
            .accountSheetChrome(server.name)
        }
    }
}

// MARK: - The day after: labor and overtime

/// Tomorrow's labor % (the schedule priced against the forecast, salaries
/// in for the owner) and the week's overtime — dsr.tomorrow labor_plan and
/// overtime_outlook, the web's tmrLaborHtml.
struct DSRTomorrowLaborCard: View {
    let tomorrow: DSRTomorrow
    let isOwner: Bool

    var body: some View {
        if tomorrow.labor != nil || tomorrow.overtime != nil {
            VStack(alignment: .leading, spacing: 14) {
                if let l = tomorrow.labor { laborPart(l) }
                if let o = tomorrow.overtime { overtimePart(o) }
                if let basis = tomorrow.labor?.basis ?? tomorrow.overtime?.basis {
                    Text(DSRNightDetail.cap(basis)).cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        }
    }

    private func laborPart(_ l: DSRTomorrow.LaborPlan) -> some View {
        let own = isOwner && l.salariedTotalPct != nil
        let pct = own ? l.salariedTotalPct : l.hourlyPct
        let gap = l.gap(pct: pct)
        let soft = l.targetSource == "default"
        let tone: Color = gap.map { $0 > 0 ? (soft ? .cavnarAmber : .cavnarRed) : .cavnarGreen } ?? .cavnarInk
        return VStack(alignment: .leading, spacing: 4) {
            CavnarKicker("\(tomorrow.weekday ?? "The day after")\u{2019}s labor")
            Text(DSRFormat.pct(pct)).cavnarText(.figureM, color: pct == nil ? Color.cavnarInk3 : tone)
            HomeMixedText.make("\(DSRFormat.count(l.hours)) hours \u{00B7} \(DSRFormat.money(own ? l.salariedTotalCost : l.hourlyCost))"
                               + (own ? " with salaries" : "")
                               + (l.forecastNet.map { " over a \(DSRFormat.money($0)) forecast" } ?? ""),
                               role: .secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let line = l.targetLine(pct: pct) {
                HomeMixedText.make(line, role: .label, color: tone == .cavnarRed ? .cavnarRedText : tone)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let ot = l.overtimeHours, ot > 0 {
                HomeMixedText.make("\(DSRFormat.count(ot)) overtime hours on it", role: .label, color: .cavnarRedText)
            }
        }
    }

    private func overtimePart(_ o: DSRTomorrow.Overtime) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            CavnarKicker("Overtime this week")
            if o.people.isEmpty {
                Text("No one goes past 40 hours this week if the schedule holds.")
                    .cavnarText(.secondary, color: .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                HomeMixedText.make("\(o.overCount ?? o.people.count) \((o.overCount ?? o.people.count) == 1 ? "person goes" : "people go") past 40 hours"
                                   + ((o.extraCost ?? 0) > 0 ? " \u{00B7} about \(DSRFormat.money(o.extraCost)) extra" : ""),
                                   role: .label)
                    .fixedSize(horizontal: false, vertical: true)
                // One action, the people behind a tap (re-audit D17): the
                // fix is in the schedule, not in reading the list.
                Button {
                    Haptic.light()
                    if let nav = NavPath("labor/schedule") {
                        NotificationCenter.default.post(name: .cavnarOpenNav, object: nav)
                    }
                } label: {
                    Text("Fix in schedule")
                        .frame(maxWidth: .infinity)
                        .cavnarHitTarget()
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                .accessibilityHint("Opens the schedule in Labor")
                CavnarMoreDisclosure(hiddenCount: o.people.count, total: o.people.count) {
                    overtimePeople(o)
                }
            }
        }
    }

    private func overtimePeople(_ o: DSRTomorrow.Overtime) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(o.people) { p in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(p.employee).cavnarText(.label)
                        if let r = p.role { Text(r).cavnarText(.caption, color: .cavnarInk2) }
                    }
                    HomeMixedText.make("\(DSRFormat.count(p.projectedHours))h scheduled \u{00B7} \(DSRFormat.count(p.overtimeHours))h over"
                                       + (p.extraCost.map { " \u{00B7} \(DSRFormat.money($0)) extra" } ?? ""),
                                       role: .secondary)
                    if !p.room.isEmpty {
                        HomeMixedText.make("Room: " + p.room.joined(separator: ", "), role: .secondary, color: .cavnarGreen)
                    }
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

// MARK: - The game card (intel)

/// Tonight's game against the last one on the same side — both against
/// their own usual weekday (the web's drGameHtml). Nothing until tonight's
/// net is in.
struct DSRGameCard: View {
    let game: JSONValue

    var body: some View {
        if let tonight = game["tonight"], tonight["net"]?.double != nil {
            let side = game["side"]?.string == "home" ? "home" : "road"
            VStack(alignment: .leading, spacing: 10) {
                CavnarKicker("Compared to your last \(side) game")
                HStack(alignment: .top, spacing: 10) {
                    sideCard("Tonight", title: game["describe"]?.string, x: tonight, weekday: game["weekday"]?.string)
                    if let last = game["last"], last.object != nil {
                        sideCard("Last \(side) game", title: last["describe"]?.string, x: last, weekday: last["weekday"]?.string)
                    } else {
                        VStack(alignment: .leading, spacing: 4) {
                            CavnarKicker("Last \(side) game")
                            Text("The first one measured here.").cavnarText(.caption, color: .cavnarInk2)
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color.cavnarPaper3.opacity(0.25), in: RoundedRectangle(cornerRadius: 12))
                    }
                }
                if let also = Self.alsoTonight(game) {
                    HomeMixedText.make("Also tonight: \(also).", role: .caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let basis = game["basis"]?.string {
                    Text(DSRNightDetail.cap(basis)).cavnarText(.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    /// The other headline games that shared the night (`also`, or the tail
    /// of an older report's text).
    static func alsoTonight(_ game: JSONValue) -> String? {
        let list = game["also"]?.array.compactMap(\.string) ?? []
        if !list.isEmpty { return list.joined(separator: "; ") }
        guard let text = game["text"]?.string, let r = text.range(of: "Also tonight: ") else { return nil }
        let tail = String(text[r.upperBound...])
        return tail.hasSuffix(".") ? String(tail.dropLast()) : tail
    }

    private func sideCard(_ kicker: String, title: String?, x: JSONValue, weekday: String?) -> some View {
        let lift = x["lift_pct"]?.double.map { Int($0.rounded()) }
        var bits: [String] = []
        if let g = x["guests"]?.double { bits.append("\(DSRFormat.count(g)) guests") }
        if let h = x["headcount"]?.double { bits.append("\(DSRFormat.count(h)) on the clock") }
        if let l = x["labor_pct"]?.double { bits.append("labor \(DSRFormat.pct(l))") }
        return VStack(alignment: .leading, spacing: 4) {
            CavnarKicker(kicker)
            if let title { HomeMixedText.make(title, role: .caption, color: .cavnarInk2).fixedSize(horizontal: false, vertical: true) }
            Text(DSRFormat.money(x["net"]?.double)).cavnarText(.figureS)
            if let lift {
                HomeMixedText.make("\(lift > 0 ? "+" : (lift < 0 ? "\u{2212}" : ""))\(abs(lift))% vs a usual \(weekday ?? "night")"
                                   + (x["usual"]?.double.map { " \u{00B7} \(DSRFormat.money($0))" } ?? ""),
                                   role: .caption, color: lift < 0 ? .cavnarRedText : .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("No usual night to compare yet").cavnarText(.caption, color: .cavnarInk2)
            }
            if !bits.isEmpty {
                HomeMixedText.make(bits.joined(separator: " \u{00B7} "), role: .caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.cavnarPaper3.opacity(0.25), in: RoundedRectangle(cornerRadius: 12))
        .accessibilityElement(children: .combine)
    }
}
