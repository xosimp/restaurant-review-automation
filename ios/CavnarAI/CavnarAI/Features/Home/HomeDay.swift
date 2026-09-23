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
    var onOpenIssues: () -> Void = {}

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "The day", title: "This morning's brief", trailing: dateLabel)
            VStack(alignment: .leading, spacing: 0) {
                if viewModel.isLoading && viewModel.lines.isEmpty {
                    CavnarWorkingLine().padding(.vertical, 10)
                } else if viewModel.lines.isEmpty {
                    Text("Your brief fills in as your numbers come in.")
                        .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                        .padding(.vertical, 8)
                } else {
                    ForEach(Array(viewModel.lines.enumerated()), id: \.element.id) { index, line in
                        VStack(spacing: 0) {
                            HStack(alignment: .top, spacing: 12) {
                                Circle().fill(line.toneColor).frame(width: 8, height: 8).padding(.top, 6)
                                VStack(alignment: .leading, spacing: 4) {
                                    HomeMixedText.make(line.text, size: 14.5, weight: 500, color: .cavnarInk2)
                                    if let ask = line.ask, !ask.isEmpty {
                                        HomeAskLink(question: ask, label: "Ask")
                                    }
                                }
                                Spacer(minLength: 0)
                            }
                            .padding(.vertical, 9)
                            if index < viewModel.lines.count - 1 { AccountRowDivider() }
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
        var id: String { (key ?? "") + text }
        var toneColor: Color {
            switch tone {
            case "good": return .cavnarGreen
            case "bad", "critical": return .cavnarRed
            case "action", "important": return .cavnarEmber2
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
        struct Brief: Decodable { let lines: [BriefLine]? }
        let ok: Bool
        let brief: Brief?
    }
    private struct IssuesResponse: Decodable { let ok: Bool; let issues: [Issue] }

    var lines: [BriefLine] = []
    var issues: [Issue] = []
    /// "Ana has been asked", per issue, after a cover request.
    var coverNote: [Int: String] = [:]
    var isLoading = false
    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        isLoading = true
        defer { isLoading = false }
        async let b: BriefResponse? = try? client.send("/mobile/api/morning-brief", hapticOnError: false)
        async let i: IssuesResponse? = try? client.send("/mobile/api/issues", query: ["status": "unresolved"], hapticOnError: false)
        let (brief, iss) = await (b, i)
        // The focus and money lines have their own cards on Home; an
        // all-clear alone is not a brief.
        lines = (brief?.brief?.lines ?? []).filter { $0.key != "fix_first" && $0.key != "money" }
        if lines.count == 1, lines[0].key == "all_clear" { lines = [] }
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
        let bits = [c.wentWrong, c.eightySixed.map { "86'd: \($0)" }, c.callouts.map { "callouts: \($0)" }]
            .compactMap { $0 }
        return bits.isEmpty ? "\(who) filed tonight's handoff." : "\(who): " + bits.joined(separator: " · ")
    }
}
