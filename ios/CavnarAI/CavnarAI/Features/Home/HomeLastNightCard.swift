import SwiftUI
import Observation

/// LAST NIGHT — the latest Daily Sales Report, in the day's slot on Home
/// just above the morning brief: the night's net sales against yesterday,
/// its status, and the first lines of the morning read. Tap for the whole
/// report. Shows nothing for a login the report isn't part of, or for a
/// restaurant that has never had one — Home doesn't carry an empty card
/// for a feature a location doesn't run.
struct HomeLastNightCard: View {
    var open: (DailyReportRoute) -> Void
    /// When /mobile/api/home last answered — this reads again with every
    /// Home refresh (pull, foreground, a location switch), and not before
    /// the first one, the same rhythm as HomeFollowThrough.
    var homeLoadedAt: Date?
    /// The restaurant's own clock from Home (`local_now`, ISO) — the
    /// fallback for "tonight" when the report list doesn't carry it.
    var localNow: String? = nil
    @State private var viewModel = HomeLastNightViewModel()

    var body: some View {
        Group {
            switch viewModel.state {
            // Nothing while loading: most locations have no report, and a
            // placeholder that then vanishes would jump the page for them.
            case .hidden, .loading:
                EmptyView()
            case .ready(let night, let report, let tonight):
                ready(night, report, gap: LastNightGap.days(from: night.businessDate,
                                                            to: tonight ?? localNow))
                    .padding(.horizontal, 20)
                    .padding(.top, 30)
            }
        }
        .task(id: homeLoadedAt) {
            guard homeLoadedAt != nil else { return }
            await viewModel.load()
        }
    }

    /// The night's verdict: label, tone and score. The list row's fields
    /// (the shared `access.summary` contract) lead; the peeked report's own
    /// scorecard stands in on an older server. Both are the stored
    /// scorecard, never recomputed here. Nil when neither has a label.
    struct Verdict: Equatable {
        let label: String
        let tone: String?
        let overall: Int?
    }

    static func verdict(_ night: DSRSummary, _ report: DSRReport?) -> Verdict? {
        if let label = night.verdict, !label.isEmpty {
            return Verdict(label: label, tone: night.tone, overall: night.overall)
        }
        if let card = report?.scorecard, let v = card.verdict, !v.label.isEmpty {
            return Verdict(label: v.label, tone: v.tone, overall: card.overall)
        }
        return nil
    }

    private func verdictRow(_ v: Verdict, vsBudget: Double?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Circle()
                .fill(DSRScorecard.color(v.tone) ?? Color.cavnarInk3)
                .frame(width: 9, height: 9)
                .alignmentGuide(.firstTextBaseline) { d in d[.bottom] - 1 }
            Text(v.label)
                .font(.cavnarBody(CavnarType.body, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            if let overall = v.overall {
                (Text("\(overall)").font(.cavnarNumber(CavnarType.body, weight: 700)).foregroundColor(.cavnarInk)
                 + Text("/100").font(.cavnarNumber(CavnarType.caption)).foregroundColor(.cavnarInk3))
            }
            if let vs = vsBudget {
                Text("\(vs >= 0 ? "+" : "\u{2212}")\(DSRFormat.money(abs(vs))) vs budget")
                    .font(.cavnarNumber(CavnarType.caption, weight: 600))
                    .foregroundStyle(vs >= 0 ? Color.cavnarGreen : Color.cavnarAmber)
                    .cavnarSensitive()
                    .lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func ready(_ night: DSRSummary, _ report: DSRReport?, gap: Int?) -> some View {
        let phase = report?.phase ?? night.phase
        // The list's lead is this login's own (the operations summary for a
        // manager); the report's executive summary is the older server's.
        let summary = night.lead ?? report?.narrative?.executiveSummary?.text
        let sales = report?.facts.blocks["sales"]
        return VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: LastNightGap.kicker(gap: gap), title: "Daily report", trailing: night.displayDate)
            // The latest report is older than last night: say the night
            // that's missing, so an old report never reads as last night's.
            if LastNightGap.lastNightMissing(gap: gap) {
                Text(LastNightGap.missingLine)
                    .font(.cavnarBody(13.5, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
                    .padding(.leading, 4)
            }
            Button {
                Haptic.light()
                open(.report(date: night.businessDate))
            } label: {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        if let net = (sales?.isReady == true ? sales?.metric("net") : nil) ?? night.net {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(DSRFormat.money(net))
                                    .font(.cavnarNumber(30, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                    .cavnarSensitive()
                                if let vs = sales?.metric("vs_yesterday_pct") ?? night.vsYesterdayPct {
                                    (Text(DSRFormat.signedPct(vs)).font(.cavnarNumber(13, weight: 700))
                                        .foregroundStyle(DSRFormat.tone(vs))
                                     + Text(" net vs yesterday").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3))
                                } else {
                                    Text("net sales").font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                                }
                            }
                        }
                        Spacer(minLength: 8)
                        DSRStatusPill(phase: phase)
                    }
                    // The verdict beside the number (density #1): "did we
                    // win last night?" is a dot, a label and a score here,
                    // not a paragraph to read. Only what the server sent —
                    // the list row's verdict (dsr.access.summary), else the
                    // peeked report's own scorecard; nothing for neither.
                    if let v = Self.verdict(night, report) {
                        verdictRow(v, vsBudget: night.vsBudget)
                    }
                    if let summary {
                        HomeMixedText.make(summary, size: CavnarType.secondary, color: .cavnarInk2)
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                    } else if phase == .running {
                        Text(report?.checklist?.statusLabel ?? "The report is being built.")
                            .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                    }
                    // "Watch:" the night's first risk, as the web card has it.
                    if phase != .running, let risk = night.firstRisk, !risk.isEmpty {
                        (Text("Watch: ").font(.cavnarBody(13, weight: 700)).foregroundColor(.cavnarInk2)
                         + HomeMixedText.make(risk, size: 13, color: .cavnarInk2))
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                    }
                    if !night.missing.isEmpty {
                        HomeMixedText.make(night.missing.count == 1 ? night.missing[0]
                                           : "\(night.missing.count) things still missing",
                                           size: 12.5, color: .cavnarAmber)
                            .lineLimit(2)
                    }
                    HStack(spacing: 6) {
                        Text("Read the report").font(.cavnarBody(13.5, weight: 700))
                        Image(systemName: "chevron.right").font(.system(size: 11, weight: .bold))
                    }
                    .foregroundStyle(Color.cavnarEmber2)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .cavnarCard(summary == nil ? .card : .ai)

            Button {
                Haptic.light()
                open(.list)
            } label: {
                Text("All nights")
                    .font(.cavnarBody(13, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .buttonStyle(.plain)
            .padding(.leading, 4)
        }
    }
}

@Observable
@MainActor
final class HomeLastNightViewModel {
    enum State {
        case loading
        case hidden
        /// The latest night, its report when it could be read, and
        /// tonight's business date when the server sent it.
        case ready(DSRSummary, DSRReport?, String?)
    }

    private(set) var state: State = .loading
    private let client: APIClient

    init(client: APIClient = .shared) { self.client = client }

    func load() async {
        do {
            let list: DSRListResponse = try await client.send("/mobile/api/dsr", query: ["limit": "1"],
                                                              hapticOnError: false)
            guard let latest = list.reports.first else {
                state = .hidden
                return
            }
            // One call, as the web's card (parity audit #9): the list row
            // (dsr.access.summary) carries the net, its change against the
            // night before, the verdict and the lead — the second read of
            // the whole report (peeked) is gone.
            state = .ready(latest, nil, list.tonight?.value)
        } catch is CancellationError {
        } catch let error as APIClient.APIError where error.status == 403 {
            state = .hidden
        } catch {
            // Home must not grow an error card for a secondary read: keep
            // what's on screen, or show nothing if nothing loaded yet.
            if case .loading = state { state = .hidden }
        }
    }
}

/// The web's gap rule for Home's report card (`hbDayGap` / renderLastNight):
/// the latest report's business date against tonight's, on the
/// restaurant's clock. The same night is "Tonight", the night before is
/// "Last night", anything older is only the "Latest report" — and then last
/// night's report is missing, which the card says in amber.
enum LastNightGap {
    static let missingLine = "No report for last night"

    /// Whole days from `businessDate` to `tonight` (both ISO, only the date
    /// part is read); nil when either can't be read.
    static func days(from businessDate: String, to tonight: String?) -> Int? {
        guard let tonight, let a = day(businessDate), let b = day(tonight) else { return nil }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(secondsFromGMT: 0)!
        return cal.dateComponents([.day], from: a, to: b).day
    }

    static func kicker(gap: Int?) -> String {
        switch gap {
        case 0: return "Tonight"
        case 1: return "Last night"
        default: return "Latest report"
        }
    }

    /// True when the latest report is older than last night.
    static func lastNightMissing(gap: Int?) -> Bool { (gap ?? 0) > 1 }

    private static func day(_ iso: String) -> Date? {
        let p = iso.prefix(10).split(separator: "-")
        guard p.count == 3, let y = Int(p[0]), let m = Int(p[1]), let d = Int(p[2]) else { return nil }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(secondsFromGMT: 0)!
        return cal.date(from: DateComponents(year: y, month: m, day: d))
    }
}
