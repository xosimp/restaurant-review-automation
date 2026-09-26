import SwiftUI
import Observation

/// THE DAY — the morning brief and the open issues, in the slot right under
/// the value graph, the same place the web Home puts them. The brief is
/// read once, top to bottom, before service; each line can be asked about.
/// After 8pm local the close-out card takes this slot instead (HomeView
/// decides), because that is what the evening owner opens the app for.
struct HomeDayCard: View {
    @State private var viewModel = HomeDayViewModel()
    let dateLabel: String?
    /// What the page above already says — the one thing and every Needs
    /// attention item, by job (HomeBriefFilter.same) — so a brief line
    /// about the same job is left out, as the web's `hbShownKeys` does.
    var shownKeys: Set<String> = []
    /// Where a line's own action lands (its `action.nav`).
    var onOpenNav: (String) -> Void = { _ in }
    var onOpenIssues: () -> Void = {}

    /// The lines this card draws: the web's rule, applied to the brief.
    private var lines: [HomeDayViewModel.BriefLine] {
        var shown = shownKeys
        if !viewModel.issues.isEmpty { shown.insert("issues") }
        return HomeBriefFilter.visible(viewModel.lines, shown: shown)
    }

    var body: some View {
        let lines = self.lines
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Today", title: Date.now.formatted(.dateTime.weekday(.wide)), trailing: dateLabel)
            VStack(alignment: .leading, spacing: 0) {
                if viewModel.isLoading && viewModel.lines.isEmpty {
                    CavnarWorkingLine().padding(.vertical, 10)
                } else if lines.isEmpty {
                    Text("Your brief fills in as your numbers come in.")
                        .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        .padding(.vertical, 8)
                } else {
                    ForEach(Array(lines.enumerated()), id: \.element.id) { index, line in
                        VStack(spacing: 0) {
                            HStack(alignment: .top, spacing: 12) {
                                Circle().fill(line.toneColor).frame(width: 8, height: 8).padding(.top, 6)
                                VStack(alignment: .leading, spacing: 4) {
                                    HomeMixedText.make(line.text, size: 14.5, weight: 500, color: .cavnarInk2)
                                    ClaimKindTag(kind: line.claimKind)
                                    // How today's kind of forecast has held
                                    // up here (K8), under the forecast line.
                                    if line.key == "today", let record = viewModel.demandAccuracy?.sentence {
                                        HomeMixedText.make(record + ".", size: 12.5, weight: 500, color: .cavnarInk3)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    // The line's direct action first — send,
                                    // open the order, answer the replies —
                                    // and Ask as the secondary (web #46).
                                    HStack(spacing: 16) {
                                        if let act = line.action {
                                            Button {
                                                Haptic.light()
                                                onOpenNav(act.nav)
                                            } label: {
                                                HStack(spacing: 3) {
                                                    Text(act.label).font(.cavnarBody(13, weight: 700))
                                                    Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
                                                }
                                                .foregroundStyle(Color.cavnarEmber2)
                                                .frame(minHeight: 44)
                                                .contentShape(Rectangle())
                                            }
                                            .buttonStyle(.plain)
                                        }
                                        if let ask = line.ask, !ask.isEmpty {
                                            HomeAskLink(question: ask, label: "Ask")
                                        }
                                    }
                                    // A brief line that stands for a
                                    // recommendation is answered where it
                                    // is read: Done / Not for us, never
                                    // Track (the brief names no metric).
                                    if let key = line.answerKey {
                                        RecAnswerRow(key: key, surface: "home", module: line.answerModule,
                                                     answers: [.completed, .notForUs],
                                                     alsoKeys: line.recKeys ?? [])
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 9)
                            if index < lines.count - 1 { AccountRowDivider() }
                        }
                    }
                }
            }
            .cavnarCard(.ai)

            // Open issues: the day's obligations, beside the day's read.
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("OPEN ISSUES").font(.cavnarBody(12, weight: 700)).tracking(1.1).foregroundStyle(Color.cavnarInk3)
                    Spacer()
                    if !viewModel.issues.isEmpty {
                        Text("\(viewModel.issues.count)").font(.cavnarNumber(12, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                }
                .padding(.bottom, 6)
                if viewModel.issues.isEmpty {
                    HStack(spacing: 8) {
                        Image(systemName: "checkmark").font(.system(size: 12, weight: .bold)).foregroundStyle(Color.cavnarGreen)
                        Text("Nothing open.").font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    }
                    .padding(.vertical, 6)
                } else {
                    ForEach(Array(viewModel.issues.prefix(4).enumerated()), id: \.element.id) { index, issue in
                        VStack(spacing: 0) {
                            HStack(alignment: .top, spacing: 12) {
                                Circle().fill(issue.tone).frame(width: 8, height: 8).padding(.top, 6)
                                VStack(alignment: .leading, spacing: 2) {
                                    HomeMixedText.make(issue.title, size: 14.5, weight: 600, color: .cavnarInk)
                                    Text((issue.assigneeName ?? "unassigned") + " · " + (issue.status == "acknowledged" ? "on it" : (issue.status ?? "open")))
                                        .font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                                    // A coverage issue's suggested covers: one
                                    // tap asks that person (text or email).
                                    // The schedule moves only when the manager
                                    // decides who is on.
                                    let covers = issue.coversToAsk
                                    if !covers.isEmpty {
                                        HStack(spacing: 14) {
                                            ForEach(covers, id: \.self) { name in
                                                Button {
                                                    Haptic.light()
                                                    Task { await viewModel.askToCover(issue, name: name) }
                                                } label: {
                                                    Text("Ask \(name.split(separator: " ").first.map(String.init) ?? name) to cover")
                                                        .font(.cavnarBody(12.5, weight: 700))
                                                        .foregroundStyle(Color.cavnarEmber2)
                                                }
                                                .buttonStyle(.plain)
                                            }
                                        }
                                        .padding(.top, 4)
                                    }
                                    if let note = viewModel.coverNote[issue.id] {
                                        Text(note).font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarGreen)
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 8)
                            if index < min(viewModel.issues.count, 4) - 1 { AccountRowDivider() }
                        }
                    }
                }
            }
            .cavnarCard()
        }
        .task { await viewModel.load() }
    }
}

@Observable
@MainActor
final class HomeDayViewModel {
    struct BriefLine: Decodable, Identifiable {
        let key: String?
        let text: String
        let tone: String?
        let ask: String?
        /// The recommendation the line stands for, and whether Done / Not
        /// for us apply to it (false for the money ranking and owed
        /// replies, which nothing can answer). Both optional: an older
        /// server sends neither, and the line reads as before.
        let recKey: String?
        /// Every key the line stands for (running low: one per named item).
        let recKeys: [String]?
        let answerable: Bool?
        /// K4: "forecast" on today's line (demand.forecast_day), and the
        /// like — a small tag beside it. Absent on an older server.
        var claimKind: String? = nil
        /// The job the line stands for (`rec`) and where its figures came
        /// from (`source`: "dsr" on the report's "yesterday" line) — what
        /// the web filters on (parity audit #3).
        var rec: String? = nil
        var source: String? = nil
        /// The line's direct action ({label, nav}: "Reply now" →
        /// reviews?filter=urgent), drawn before Ask.
        var action: LineAction? = nil
        var id: String { (key ?? "") + text }

        struct LineAction: Decodable, Hashable {
            let label: String
            let nav: String
        }

        enum CodingKeys: String, CodingKey {
            case key, text, tone, ask, answerable, rec, source, action
            case recKey = "rec_key"
            case recKeys = "rec_keys"
            case claimKind = "claim_kind"
        }

        init(key: String?, text: String, rec: String? = nil, source: String? = nil) {
            self.key = key; self.text = text; self.rec = rec; self.source = source
            tone = nil; ask = nil; recKey = nil; recKeys = nil; answerable = nil
        }

        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            claimKind = try? c.decodeIfPresent(String.self, forKey: .claimKind)
            rec = (try? c.decodeIfPresent(String.self, forKey: .rec)) ?? nil
            source = (try? c.decodeIfPresent(String.self, forKey: .source)) ?? nil
            action = (try? c.decodeIfPresent(LineAction.self, forKey: .action)) ?? nil
            key = try? c.decodeIfPresent(String.self, forKey: .key)
            text = try c.decode(String.self, forKey: .text)
            tone = try? c.decodeIfPresent(String.self, forKey: .tone)
            ask = try? c.decodeIfPresent(String.self, forKey: .ask)
            recKey = try? c.decodeIfPresent(String.self, forKey: .recKey)
            recKeys = try? c.decodeIfPresent([String].self, forKey: .recKeys)
            answerable = try? c.decodeIfPresent(Bool.self, forKey: .answerable)
        }

        /// The key Done / Not for us answer — only on a keyed line the
        /// server marked answerable.
        var answerKey: String? {
            guard answerable == true, let k = recKey?.trimmingCharacters(in: .whitespaces), !k.isEmpty else {
                return nil
            }
            return k
        }

        /// The module the answer is recorded for, from the line's own key.
        var answerModule: String { Self.module(forLineKey: key) }

        static func module(forLineKey key: String?) -> String {
            switch key {
            case "reviews": return "reviews"
            case "stock": return "food"
            case "schedule": return "labor"
            case "slow_day": return "marketing"
            case "loss": return "ops"
            default: return "home"
            }
        }
        var toneColor: Color {
            switch tone {
            case "good": return .cavnarGreen
            case "bad", "critical": return .cavnarRed
            // A line that needs doing is a warning tone; ember is not a
            // status (B4 L7).
            case "action", "important": return .cavnarAmber
            case "warn", "watch": return .cavnarAmber
            default: return .cavnarInk3
            }
        }
    }
    struct Issue: Decodable, Identifiable {
        struct Person: Decodable { let name: String? }
        /// A coverage issue's structured detail: who could cover, and who
        /// has already been asked (issues._public parses meta_json).
        struct Meta: Decodable { let covers: [Person]?; let asked: [Person]? }
        let id: Int
        let title: String
        let severity: String?
        let status: String?
        let assigneeName: String?
        let meta: Meta?
        enum CodingKeys: String, CodingKey { case id, title, severity, status, meta; case assigneeName = "assignee_name" }
        /// Up to two suggested covers nobody has asked yet.
        var coversToAsk: [String] {
            guard status != "resolved" else { return [] }
            let asked = Set((meta?.asked ?? []).compactMap { $0.name?.lowercased() })
            return Array((meta?.covers ?? []).compactMap(\.name)
                .filter { !$0.isEmpty && !asked.contains($0.lowercased()) }.prefix(2))
        }
        var tone: Color {
            if status != "open" { return .cavnarInk3 }
            return severity == "high" ? .cavnarRed : .cavnarAmber
        }
    }
    private struct BriefResponse: Decodable {
        struct Brief: Decodable {
            let lines: [BriefLine]?
            /// K8: how far today's forecast has been off here, nightly and
            /// out of sample — beside the forecast line, never in its text.
            var demandAccuracy: DemandAccuracy? = nil
            enum CodingKeys: String, CodingKey {
                case lines
                case demandAccuracy = "demand_accuracy"
            }
        }
        let ok: Bool
        let brief: Brief?
    }
    private struct IssuesResponse: Decodable { let ok: Bool; let issues: [Issue] }

    var lines: [BriefLine] = []
    /// The brief's K8 demand record, shown under today's forecast line.
    var demandAccuracy: DemandAccuracy?
    var issues: [Issue] = []
    /// "Ana has been asked", per issue, after a cover request.
    var coverNote: [Int: String] = [:]
    var isLoading = false
    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        async let b: BriefResponse? = try? client.send("/mobile/api/morning-brief", query: ["view": "home"], hapticOnError: false)
        async let i: IssuesResponse? = try? client.send("/mobile/api/issues", query: ["status": "unresolved"], hapticOnError: false)
        let (brief, iss) = await (b, i)
        // The focus and money lines have their own cards on Home; an
        // all-clear alone is not a brief.
        demandAccuracy = brief?.brief?.demandAccuracy
        // Every line; HomeDayCard filters what the page already says
        // (HomeBriefFilter) against what Home knows at draw time.
        lines = brief?.brief?.lines ?? []
        issues = iss?.issues ?? []
    }

    private struct CoverBody: Encodable { let name: String }

    /// POST /mobile/api/issues/<id>/ask-cover — text (or email) the chosen
    /// cover; the server refuses anyone not suggested or already asked.
    func askToCover(_ issue: Issue, name: String) async {
        let r: APIClient.OKResponse? = try? await client.send(
            "/mobile/api/issues/\(issue.id)/ask-cover", method: .post,
            body: CoverBody(name: name), retryTransient: false)
        if r?.ok == true {
            await Haptic.success()
            coverNote[issue.id] = "\(name) has been asked"
            await load()
        } else {
            coverNote[issue.id] = r?.error ?? "Couldn\u{2019}t ask \(name)"
        }
    }
}

/// The close-out card, on its own so HomeView can put it in the day's slot
/// after 8pm and HomeFollowThrough can keep it at the bottom before then.
struct HomeCloseOutCard: View {
    let viewModel: HomeFollowThroughViewModel
    @State private var showingCloseOut = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "End of the night", title: "Close-out",
                              trailing: viewModel.closeOut == nil ? nil : "filed")
            VStack(alignment: .leading, spacing: 10) {
                Text(viewModel.closeOut == nil
                     ? "Four lines from whoever closes. They lead tomorrow morning's brief."
                     : filedSummary)
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
                Button {
                    Haptic.light()
                    showingCloseOut = true
                } label: {
                    Text(viewModel.closeOut == nil ? "Hand off the night" : "Update the handoff")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
            }
            .cavnarCard()
        }
        .sheet(isPresented: $showingCloseOut) {
            CloseOutSheet(viewModel: viewModel)
        }
    }

    private var filedSummary: String {
        guard let c = viewModel.closeOut else { return "" }
        let who = c.submittedBy ?? "A manager"
        // The same line the morning brief leads with (closeout.summarise).
        let bits = [c.wentWrong, c.eightySixed.map { "86'd: \($0)" }, c.callouts.map { "callouts: \($0)" },
                    c.equipment.map { "equipment: \($0)" }, c.maintenance.map { "maintenance: \($0)" }]
            .compactMap { $0 }
        return bits.isEmpty ? "\(who) filed tonight's handoff." : "\(who): " + bits.joined(separator: " · ")
    }
}

/// One item, once (web `hbSame` / `hbShownKeys`, friction #11): the same job
/// reached Home several ways — the one thing, a Needs-attention row, a brief
/// line — each with its own key. `same` maps each surface's key onto one
/// job, and `visible` drops a brief line whose job the page already shows,
/// with the server's `morning_brief.shown_on_home` rule (parity audit #3).
/// Pure, so the rule is pinned by tests.
enum HomeBriefFilter {
    private static let jobs: [String: String] = [
        "urgent_reviews": "replies", "stale_low_reviews": "replies", "awaiting_approval": "replies",
        "reviews_awaiting_approval": "replies", "low_response_rate": "replies", "no_response": "replies",
        "reviews": "replies", "critical_low": "stock", "stock": "stock", "issues": "issues",
        "labor_overtime": "overtime",
    ]

    static func same(_ key: String?) -> String? {
        guard let k = key?.trimmingCharacters(in: .whitespaces), !k.isEmpty else { return nil }
        if let job = jobs[k] { return job }
        if k.hasPrefix("stock_low:") { return "stock" }
        return k
    }

    /// The jobs the page above the brief already says: every Needs
    /// attention item (its type and the key it is answered under) and the
    /// one thing's key.
    static func shownKeys(attention: [NeedsAttentionItem], focusKey: String?) -> Set<String> {
        var s = Set<String>()
        for a in attention {
            if let j = same(a.type) { s.insert(j) }
            if let j = same(a.recKey) { s.insert(j) }
        }
        if let j = same(focusKey) { s.insert(j) }
        return s
    }

    static func visible(_ lines: [HomeDayViewModel.BriefLine], shown: Set<String>) -> [HomeDayViewModel.BriefLine] {
        // An all-clear alone is not a brief.
        if lines.count == 1, lines[0].key == "all_clear" { return [] }
        return lines.filter { l in
            // The one thing and the money line have their own card.
            if l.key == "fix_first" || l.key == "money" { return false }
            // The report's "Last night" line: the Last night card says it.
            if l.key == "yesterday" && l.source == "dsr" { return false }
            if let j = same(l.rec), shown.contains(j) { return false }
            if let j = same(l.key), shown.contains(j) { return false }
            return true
        }
    }
}
