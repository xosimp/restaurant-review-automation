import SwiftUI

// The web Labor tab's two decision surfaces, drawn natively from the SAME
// objects (web-vs-iOS parity audit, 9/25/26):
//
//   labor.money_went      "Where the money went" — every priced item, ranked
//   labor.staffing_board  "Every day and person" — an executive strip, then
//                         the overstaffed / strong-days-run-lean / overtime
//                         lanes of decision cards
//
// The phone used to rebuild its own lists from the raw overstaffed and
// understaffed days and overtime_risk, with "near" overtime rows the web
// never shows and labels of its own ("Under target on a strong day",
// "Overtime risk"). Every sentence, figure and severity here is the
// server's; nothing is recomputed on the phone.

// MARK: - Payload

/// Lenient reads: a figure may arrive as a number or a string, and an odd
/// shape decodes as empty rather than failing the whole Labor screen.
private extension KeyedDecodingContainer {
    func text(_ key: Key) -> String? {
        if let s = try? decodeIfPresent(String.self, forKey: key) { return s }
        if let d = try? decodeIfPresent(Double.self, forKey: key) {
            return d == d.rounded() ? String(Int(d)) : String(d)
        }
        return nil
    }
    func number(_ key: Key) -> Double? {
        if let d = try? decodeIfPresent(Double.self, forKey: key) { return d }
        if let s = try? decodeIfPresent(String.self, forKey: key) { return Double(s) }
        return nil
    }
}

/// How often the same pattern showed on that weekday in the window. `pct`
/// is null under two such days — the ring then reads "first seen".
struct StaffingConsistency: Codable, Equatable {
    var hits: Int? = nil
    var of: Int? = nil
    var pct: Int? = nil

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        hits = c.number(.hits).map { Int($0) }
        of = c.number(.of).map { Int($0) }
        pct = c.number(.pct).map { Int($0.rounded()) }
    }
}

/// A same-role teammate who had room under 40 that week.
struct StaffingMate: Codable, Equatable {
    var name: String = ""
    var hoursText: String? = nil

    enum CodingKeys: String, CodingKey {
        case name
        case hoursText = "hours_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.text(.name) ?? ""
        hoursText = c.text(.hoursText)
    }
}

/// One decision card (labor.staffing_board). `kind` is overstaffed | lean |
/// overtime; the fields a kind does not use are absent.
struct StaffingCard: Codable, Equatable, Identifiable {
    var kind: String = ""
    var title: String = ""
    var date: String? = nil
    var role: String? = nil
    var week: String? = nil
    var dollars: Double = 0
    var dollarsText: String = ""
    var label: String = ""
    var pctText: String? = nil
    var salesText: String? = nil
    var trimText: String? = nil
    var ptsText: String? = nil
    var covers: String? = nil
    var spcText: String? = nil
    var hoursText: String? = nil
    var extraText: String? = nil
    var severity: String = "medium"
    var consistency: StaffingConsistency? = nil
    var mate: StaffingMate? = nil
    var say: String = ""
    var ask: String = ""

    var id: String { "\(kind)|\(title)|\(date ?? week ?? "")" }

    enum CodingKeys: String, CodingKey {
        case kind, title, date, role, week, dollars, label, covers, severity, consistency, mate, say, ask
        case dollarsText = "dollars_text"
        case pctText = "pct_text"
        case salesText = "sales_text"
        case trimText = "trim_text"
        case ptsText = "pts_text"
        case spcText = "spc_text"
        case hoursText = "hours_text"
        case extraText = "extra_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.text(.kind) ?? ""
        title = c.text(.title) ?? ""
        date = c.text(.date)
        role = c.text(.role)
        week = c.text(.week)
        dollars = c.number(.dollars) ?? 0
        dollarsText = c.text(.dollarsText) ?? ""
        label = c.text(.label) ?? ""
        pctText = c.text(.pctText)
        salesText = c.text(.salesText)
        trimText = c.text(.trimText)
        ptsText = c.text(.ptsText)
        covers = c.text(.covers)
        spcText = c.text(.spcText)
        hoursText = c.text(.hoursText)
        extraText = c.text(.extraText)
        severity = c.text(.severity) ?? "medium"
        consistency = try? c.decodeIfPresent(StaffingConsistency.self, forKey: .consistency)
        mate = try? c.decodeIfPresent(StaffingMate.self, forKey: .mate)
        say = c.text(.say) ?? ""
        ask = c.text(.ask) ?? ""
    }
}

struct StaffingSummary: Codable, Equatable {
    struct Biggest: Codable, Equatable {
        var title: String = ""
        var dollarsText: String = ""
        var label: String = ""
        enum CodingKeys: String, CodingKey {
            case title, label
            case dollarsText = "dollars_text"
        }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            title = c.text(.title) ?? ""
            dollarsText = c.text(.dollarsText) ?? ""
            label = c.text(.label) ?? ""
        }
    }
    struct Quick: Codable, Equatable {
        var title: String = ""
        var kind: String = ""
        var why: String = ""
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            title = c.text(.title) ?? ""
            kind = c.text(.kind) ?? ""
            why = c.text(.why) ?? ""
        }
        enum CodingKeys: String, CodingKey { case title, kind, why }
    }
    var atStakeText: String = "$0"
    var overText: String = "$0"
    var otText: String = "$0"
    var biggest: Biggest? = nil
    var quick: Quick? = nil

    enum CodingKeys: String, CodingKey {
        case biggest, quick
        case atStakeText = "at_stake_text"
        case overText = "over_text"
        case otText = "ot_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        atStakeText = c.text(.atStakeText) ?? "$0"
        overText = c.text(.overText) ?? "$0"
        otText = c.text(.otText) ?? "$0"
        biggest = try? c.decodeIfPresent(Biggest.self, forKey: .biggest)
        quick = try? c.decodeIfPresent(Quick.self, forKey: .quick)
    }
}

/// `staffing_board` on /mobile/api/labor — null on sample data, as the web
/// draws no cards on the sample week.
struct StaffingBoard: Codable, Equatable {
    var overstaffed: [StaffingCard] = []
    var lean: [StaffingCard] = []
    var overtime: [StaffingCard] = []
    var summary: StaffingSummary? = nil

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        overstaffed = (try? c.decodeIfPresent([StaffingCard].self, forKey: .overstaffed)) ?? []
        lean = (try? c.decodeIfPresent([StaffingCard].self, forKey: .lean)) ?? []
        overtime = (try? c.decodeIfPresent([StaffingCard].self, forKey: .overtime)) ?? []
        summary = try? c.decodeIfPresent(StaffingSummary.self, forKey: .summary)
    }

    enum CodingKeys: String, CodingKey { case overstaffed, lean, overtime, summary }

    var isEmpty: Bool { overstaffed.isEmpty && lean.isEmpty && overtime.isEmpty }
}

/// One `money_went` item: overstaffed | overtime | past_schedule, with its
/// dollars (an opportunity or an estimate, never "saved").
struct MoneyWentItem: Codable, Equatable, Identifiable {
    var kind: String = ""
    var dollars: Double = 0
    var label: String = ""
    var day: String? = nil
    var date: String? = nil
    var pctText: String? = nil
    var sales: Double? = nil
    var employee: String? = nil
    var week: String? = nil
    var hoursText: String? = nil
    var hoursOverText: String? = nil
    var scheduledText: String? = nil
    var actualText: String? = nil

    var id: String { "\(kind)|\(day ?? employee ?? "")|\(date ?? week ?? "")" }

    enum CodingKeys: String, CodingKey {
        case kind, dollars, label, day, date, sales, employee, week
        case pctText = "pct_text"
        case hoursText = "hours_text"
        case hoursOverText = "hours_over_text"
        case scheduledText = "scheduled_text"
        case actualText = "actual_text"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        kind = c.text(.kind) ?? ""
        dollars = c.number(.dollars) ?? 0
        label = c.text(.label) ?? ""
        day = c.text(.day)
        date = c.text(.date)
        pctText = c.text(.pctText)
        sales = c.number(.sales)
        employee = c.text(.employee)
        week = c.text(.week)
        hoursText = c.text(.hoursText)
        hoursOverText = c.text(.hoursOverText)
        scheduledText = c.text(.scheduledText)
        actualText = c.text(.actualText)
    }

    /// The row's sentence, as the web's "Where the money went" writes it.
    var line: String {
        switch kind {
        case "overstaffed":
            return "\(day ?? "") \(date ?? "") · overstaffed · \(pctText ?? "")% labor on \(Self.money(sales)) sales"
        case "overtime":
            return "\(employee ?? "") · overtime · \(hoursText ?? "")h week of \(week ?? "")"
        default:
            return "\(employee ?? "") · past schedule · \(hoursOverText ?? "")h over (\(actualText ?? "")h worked, \(scheduledText ?? "")h scheduled)"
        }
    }

    var lead: String { kind == "overstaffed" ? (day ?? "") : (employee ?? "") }

    static func money(_ v: Double?) -> String {
        let f = NumberFormatter()
        f.numberStyle = .decimal
        f.maximumFractionDigits = 0
        return "$" + (f.string(from: NSNumber(value: (v ?? 0).rounded())) ?? "0")
    }
}

// MARK: - Tones

/// The web board's lane tones (`--tc`): overstaffed ember, strong days run
/// lean amber, overtime red. Past schedule rows in "Where the money went"
/// are amber, as the web's `.lb2-tint.under`.
enum StaffingLane: String, CaseIterable {
    case over, lean, ot

    var tone: Color {
        switch self {
        case .over: return .cavnarEmber
        case .lean: return .cavnarAmber
        case .ot: return .cavnarRed
        }
    }

    var title: String {
        switch self {
        case .over: return "Overstaffed days"
        case .lean: return "Strong days run lean"
        case .ot: return "Overtime"
        }
    }

    func subtitle(targetLabel: String) -> String {
        switch self {
        case .over: return "Labor above \(targetLabel) on the day\u{2019}s own sales"
        case .lean: return "Big sales on a thin crew \u{2014} check service first"
        case .ot: return "Hours past 40 in a week, at the premium"
        }
    }

    var empty: String {
        switch self {
        case .over: return "No day ran over your target in this window."
        case .lean: return "No strong day ran lean in this window."
        case .ot: return "No one went past 40 hours in this window."
        }
    }

    var icon: String {
        switch self {
        case .over: return "chart.line.uptrend.xyaxis"
        case .lean: return "chart.line.downtrend.xyaxis"
        case .ot: return "clock"
        }
    }
}

// MARK: - Where the money went

/// The three costliest items of any kind, most first, each in its lane's
/// tone and the costliest a shade darker — directly under Waiting on you,
/// as on the web. "Show all" opens the board below.
struct LaborMoneyWentCard: View {
    let items: [MoneyWentItem]
    let days: Int?
    /// Everything behind "Show all" — the board's day and person count.
    let moreCount: Int
    var onShowAll: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HomeMixedText.make("WHERE THE MONEY WENT" + (days.map { " · \($0)-DAY WINDOW" } ?? ""),
                               size: 13, weight: 700, color: .cavnarEmber2)
                .tracking(1.2)
            let top = Array(items.prefix(3))
            ForEach(Array(top.enumerated()), id: \.element.id) { index, item in
                row(item, worst: index == 0 && items.count > 1)
            }
            if moreCount > top.count {
                Button {
                    Haptic.light()
                    onShowAll()
                } label: {
                    HomeMixedText.make("Show all \(moreCount) \u{2192}", size: 13.5, weight: 700, color: .cavnarEmber2)
                        .frame(minHeight: 44, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    private func tone(_ kind: String) -> Color {
        switch kind {
        case "overstaffed": return StaffingLane.over.tone
        case "overtime": return StaffingLane.ot.tone
        default: return .cavnarAmber
        }
    }

    private func row(_ item: MoneyWentItem, worst: Bool) -> some View {
        let tc = tone(item.kind)
        return HStack(alignment: .top, spacing: 10) {
            Circle().fill(tc).frame(width: 7, height: 7)
                .shadow(color: tc.opacity(0.7), radius: 4)
                .padding(.top, 6)
            HomeMixedText.make(item.line, size: 14, weight: 500, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
            VStack(alignment: .trailing, spacing: 1) {
                Text(MoneyWentItem.money(item.dollars))
                    .font(.cavnarNumber(15, weight: 700))
                    .foregroundStyle(tc)
                Text(item.label)
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(tc.opacity(0.85))
                    .multilineTextAlignment(.trailing)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tc.opacity(worst ? 0.2 : 0.09)))
        .overlay(alignment: .leading) {
            if worst {
                UnevenRoundedRectangle(topLeadingRadius: 10, bottomLeadingRadius: 10, style: .continuous)
                    .fill(tc).frame(width: 3)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

// MARK: - The board

/// "Every day and person" — collapsed like the web's `<details>`; open, the
/// executive strip and one lane per kind. Six cards show per lane, the rest
/// behind Show all.
struct StaffingBoardSection: View {
    /// Nil on sample data (and if the board could not be built).
    let board: StaffingBoard?
    let isLive: Bool
    /// "your target" or "Cavnar AI's starting target" (savings_breakdown).
    let targetLabel: String
    let blendedRate: Double?
    @Binding var isExpanded: Bool
    var onExpand: (() -> Void)? = nil

    @State private var showAll: Set<StaffingLane> = []

    private var subtitle: String {
        guard let b = board else { return "Overstaffed days, strong days run lean, overtime" }
        let l = b.lean.count
        return "\(b.overstaffed.count) overstaffed · \(l) strong day\(l == 1 ? "" : "s") run lean · \(b.overtime.count) overtime"
    }

    var body: some View {
        CavnarDropdown(title: "Every day and person", subtitle: subtitle,
                       badge: board.map { $0.overstaffed.count + $0.lean.count + $0.overtime.count },
                       tone: .neutral, isExpanded: $isExpanded, onExpand: onExpand) {
            if let board, isLive {
                VStack(alignment: .leading, spacing: 24) {
                    summaryStrip(board.summary)
                    lane(.over, board.overstaffed)
                    lane(.lean, board.lean)
                    lane(.ot, board.overtime)
                }
            } else {
                Text("Staffing cards show once your own shifts are in \u{2014} sample data is never scored here.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // The executive strip: at stake, costliest, fastest win.
    @ViewBuilder
    private func summaryStrip(_ s: StaffingSummary?) -> some View {
        VStack(spacing: 10) {
            tile(kicker: "At stake in this window", hero: true) {
                Text(s?.atStakeText ?? "$0")
                    .font(.cavnarNumber(34, weight: 600))
                    .foregroundStyle(Color.cavnarEmber)
                    .cavnarNumberGlow()
                HomeMixedText.make("\(s?.overText ?? "$0") above target · \(s?.otText ?? "$0") overtime premium",
                                   size: 13.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .top, spacing: 10) {
                tile(kicker: "Costliest") {
                    if let b = s?.biggest {
                        Text(b.title).font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
                            .lineLimit(2).minimumScaleFactor(0.8)
                        HomeMixedText.make("\(b.dollarsText) \(b.label)", size: 13.5, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    } else {
                        Text("—").font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
                        Text("Nothing priced in this window").font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
                tile(kicker: "Fastest win") {
                    if let q = s?.quick {
                        HomeMixedText.make(q.why.prefix(1).uppercased() + q.why.dropFirst(), size: 16, weight: 600,
                                           color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        Text((q.kind == "overtime" ? "from " : "") + q.title)
                            .font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    } else {
                        Text("—").font(.cavnarHeadline(19)).foregroundStyle(Color.cavnarInk)
                        Text("Nothing to fix here").font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
                    }
                }
            }
        }
    }

    private func tile<Content: View>(kicker: String, hero: Bool = false,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(kicker.uppercased())
                .font(.cavnarBody(11.5, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarInk3)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .fill(hero
                      ? AnyShapeStyle(RadialGradient(colors: [Color.cavnarEmber.opacity(0.16), Color.cavnarPaper2],
                                                     center: .topLeading, startRadius: 0, endRadius: 260))
                      : AnyShapeStyle(Color.cavnarPaper2)))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(hero ? Color.cavnarEmber.opacity(0.3) : Color.cavnarPaper3.opacity(0.6), lineWidth: 1))
    }

    @ViewBuilder
    private func lane(_ lane: StaffingLane, _ items: [StaffingCard]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 12) {
                Image(systemName: lane.icon)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(lane.tone)
                    .frame(width: 34, height: 34)
                    .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(lane.tone.opacity(0.14)))
                    .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous)
                        .strokeBorder(lane.tone.opacity(0.28), lineWidth: 1))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text(lane.title).font(.cavnarBody(17, weight: 600)).foregroundStyle(Color.cavnarInk)
                    Text(lane.subtitle(targetLabel: targetLabel))
                        .font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 4)
                Text("\(items.count)")
                    .font(.cavnarNumber(13, weight: 700))
                    .foregroundStyle(lane.tone)
                    .padding(.horizontal, 10).padding(.vertical, 3)
                    .background(Capsule().fill(lane.tone.opacity(0.12)))
            }
            if items.isEmpty {
                HStack(spacing: 10) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 12, weight: .bold))
                        .foregroundStyle(Color.cavnarGreen)
                        .frame(width: 26, height: 26)
                        .background(Circle().fill(Color.cavnarGreen.opacity(0.14)))
                    Text(lane.empty).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Color.cavnarPaper3, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
            } else {
                let shown = showAll.contains(lane) ? items : Array(items.prefix(6))
                ForEach(Array(shown.enumerated()), id: \.element.id) { index, card in
                    StaffingDecisionCard(card: card, lane: lane, worst: index == 0 && items.count > 1,
                                         targetLabel: targetLabel, blendedRate: blendedRate)
                }
                if items.count > 6 && !showAll.contains(lane) {
                    Button {
                        Haptic.light()
                        withAnimation(.easeOut(duration: 0.25)) { _ = showAll.insert(lane) }
                    } label: {
                        HomeMixedText.make("Show all \(items.count) \u{2192}", size: 13.5, weight: 700, color: .cavnarEmber2)
                            .frame(minHeight: 44, alignment: .leading)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

/// One card: severity, the consistency ring, who/when, the dollars in the
/// lane's tone, the one sentence, fact chips, then Explain why (the
/// arithmetic, opened in place) and Ask Cavnar AI under a hairline.
struct StaffingDecisionCard: View {
    let card: StaffingCard
    let lane: StaffingLane
    let worst: Bool
    let targetLabel: String
    let blendedRate: Double?
    @State private var explaining = false

    private var severityText: String {
        let lead = worst ? "Biggest · " : ""
        switch lane {
        case .ot: return lead + "\(card.extraText ?? "")h past 40"
        case .over: return lead + "\(card.ptsText ?? "") pts over"
        case .lean: return lead + "\(card.ptsText ?? "") pts under"
        }
    }

    private var when: String {
        if lane == .ot {
            return [card.role ?? "", "week of \(card.week ?? "")"].filter { !$0.isEmpty }.joined(separator: " · ")
        }
        return card.date ?? ""
    }

    var body: some View {
        let tc = lane.tone
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                HomeMixedText.make(severityText, size: 12, weight: 700, color: tc)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(Capsule().fill(tc.opacity(card.severity == "high" ? 0.2 : 0.12)))
                    .overlay(Capsule().strokeBorder(tc.opacity(0.22), lineWidth: 1))
                Spacer(minLength: 4)
                if lane != .ot { StaffingConsistencyRing(consistency: card.consistency, tone: tc) }
            }
            .frame(minHeight: 38)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.title).font(.cavnarBody(17, weight: 600)).foregroundStyle(Color.cavnarInk)
                HomeMixedText.make(when, size: 13, weight: 500, color: .cavnarInk3)
            }
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(card.dollarsText)
                    .font(.cavnarNumber(30, weight: 600))
                    .foregroundStyle(tc)
                    .cavnarNumberGlow(tc)
                Text(card.label).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarInk3)
            }
            HomeMixedText.make(card.say, size: 14.5, color: .cavnarInk2)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
            AccountFlowLayout(spacing: 6, lineSpacing: 6) {
                ForEach(Array(chips.enumerated()), id: \.offset) { _, chip in
                    chipView(chip.text, ok: chip.ok)
                }
            }
            if explaining {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(explainLines.enumerated()), id: \.offset) { _, line in
                        (Text(line.head + " ").font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk)
                         + HomeMixedText.make(line.body, size: 14, color: .cavnarInk2))
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.cavnarPaper3.opacity(0.35)))
                .transition(.opacity.combined(with: .scale(scale: 0.98, anchor: .top)))
            }
            Rectangle().fill(Color.cavnarPaper3.opacity(0.6)).frame(height: 1)
            HStack(spacing: 16) {
                Button {
                    Haptic.light()
                    withAnimation(.easeOut(duration: 0.25)) { explaining.toggle() }
                } label: {
                    Text(explaining ? "Hide the working" : "Explain why")
                        .font(.cavnarBody(13, weight: 700))
                        .foregroundStyle(Color.cavnarInk2)
                        .frame(minHeight: 44)
                }
                .buttonStyle(.plain)
                .accessibilityHint("Shows how this was worked out")
                if !card.ask.isEmpty {
                    HomeAskLink(question: card.ask, label: "Ask Cavnar AI",
                                screen: AskScreen(panel: "labor"))
                }
                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
        .padding(.bottom, 6)
        .background(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .fill(LinearGradient(colors: [tc.opacity(worst ? 0.16 : 0.07), Color.cavnarPaper2],
                                     startPoint: .top, endPoint: .center)))
        .overlay(
            RoundedRectangle(cornerRadius: CavnarRadius.card, style: .continuous)
                .strokeBorder(tc.opacity(worst ? 0.55 : 0.2), lineWidth: 1))
        .shadow(color: worst ? tc.opacity(0.25) : .clear, radius: 14, y: 8)
    }

    private struct Chip { let text: String; var ok = false }

    private var chips: [Chip] {
        switch lane {
        case .over:
            var out = [Chip(text: "\(card.pctText ?? "")% labor"), Chip(text: "\(card.salesText ?? "") sales")]
            if let t = card.trimText, !t.isEmpty { out.append(Chip(text: "trim ~\(t)h")) }
            return out
        case .lean:
            var out = [Chip(text: "\(card.pctText ?? "")% labor")]
            if let c = card.covers, !c.isEmpty, c != "0" { out.append(Chip(text: "\(c) covers")) }
            if let s = card.spcText, !s.isEmpty { out.append(Chip(text: "\(s) a cover")) }
            return out
        case .ot:
            var out = [Chip(text: "\(card.hoursText ?? "")h that week")]
            if let m = card.mate { out.append(Chip(text: "give \(card.extraText ?? "")h to \(m.name)", ok: true)) }
            return out
        }
    }

    private func chipView(_ text: String, ok: Bool) -> some View {
        HomeMixedText.make(text, size: 12.5, weight: 500, color: ok ? .cavnarGreen : .cavnarInk3,
                           numberColor: ok ? .cavnarGreen : .cavnarInk)
            .padding(.horizontal, 10).padding(.vertical, 4)
            .background(Capsule().fill(ok ? Color.cavnarGreen.opacity(0.1) : Color.white.opacity(0.04)))
            .overlay(Capsule().strokeBorder(ok ? Color.cavnarGreen.opacity(0.35) : Color.cavnarPaper3.opacity(0.7),
                                            lineWidth: 1))
    }

    /// The web's "Explain why" body, line for line.
    private var explainLines: [(head: String, body: String)] {
        let c = card.consistency
        switch lane {
        case .over:
            var out: [(String, String)] = [
                ("What happened:", "\(card.pctText ?? "")% labor on \(card.salesText ?? "") in sales, against \(targetLabel)."),
                ("The figure:", "labor spent above \(targetLabel) of that day\u{2019}s sales = \(card.dollarsText)."),
            ]
            if let t = card.trimText, !t.isEmpty {
                out.append(("Hours to trim:", "\(card.dollarsText) ÷ your blended rate of $\(String(format: "%.2f", blendedRate ?? 0))/h ≈ \(t)h."))
            }
            if let c, let of = c.of, of > 0 {
                out.append(("Consistency:", "\(c.hits ?? 0) of \(of) \(card.title)s in this window ran over target."))
            }
            return out
        case .lean:
            var out: [(String, String)] = [
                ("What happened:", "\(card.dollarsText) in sales on \(card.pctText ?? "")% labor \u{2014} well under \(targetLabel) on one of your strongest days."),
                ("Why not add staff straight away:", "a lean crew on a big day can be efficient, not short. Slow tickets or complaints from that day are the evidence that it was short."),
            ]
            if let c, let of = c.of, of > 0 {
                out.append(("Consistency:", "\(c.hits ?? 0) of \(of) \(card.title)s in this window ran lean."))
            }
            return out
        case .ot:
            var out: [(String, String)] = [
                ("What happened:", "\(card.hoursText ?? "")h the week of \(card.week ?? "") \u{2014} \(card.extraText ?? "")h past 40."),
                ("The premium:", "hours past 40 × their blended rate × 0.5 (time and a half) = \(card.dollarsText)."),
            ]
            if let m = card.mate {
                out.append(("Who had room:", "\(m.name) worked \(m.hoursText ?? "")h that week in the same role \u{2014} those hours at straight time would not have cost the premium."))
            } else {
                out.append(("Who had room:", "no one in the same role had \(card.extraText ?? "")h free under 40 that week."))
            }
            return out
        }
    }
}

/// The measured consistency ring: the share of that weekday's days in the
/// window with the same pattern, drawn in; "—" and "first seen" under two.
struct StaffingConsistencyRing: View {
    let consistency: StaffingConsistency?
    let tone: Color
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drawn = false

    var body: some View {
        let pct = consistency?.pct
        HStack(spacing: 8) {
            ZStack {
                Circle().stroke(Color.cavnarInk3.opacity(0.22), lineWidth: 3.2)
                if let pct {
                    Circle()
                        .trim(from: 0, to: drawn || reduceMotion ? CGFloat(min(max(pct, 0), 100)) / 100 : 0)
                        .stroke(tone, style: StrokeStyle(lineWidth: 3.2, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                        .shadow(color: tone.opacity(0.4), radius: 2)
                }
            }
            .frame(width: 34, height: 34)
            VStack(alignment: .leading, spacing: 0) {
                Text(pct.map { "\($0)%" } ?? "—")
                    .font(.cavnarNumber(14, weight: 700))
                    .foregroundStyle(pct == nil ? Color.cavnarInk3 : Color.cavnarInk)
                Text(pct == nil ? "first seen" : "consistent")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .onAppear {
            guard !reduceMotion else { return }
            withAnimation(.timingCurve(0.2, 0.9, 0.3, 1, duration: 0.9)) { drawn = true }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(pct.map { "Consistency \($0)%, the same pattern on \(consistency?.hits ?? 0) of \(consistency?.of ?? 0) of these weekdays" }
                            ?? "First time in this window")
    }
}
