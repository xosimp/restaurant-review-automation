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
    @State private var viewModel = HomeLastNightViewModel()

    var body: some View {
        Group {
            switch viewModel.state {
            // Nothing while loading: most locations have no report, and a
            // placeholder that then vanishes would jump the page for them.
            case .hidden, .loading:
                EmptyView()
            case .ready(let night, let report):
                ready(night, report)
                    .padding(.horizontal, 20)
                    .padding(.top, 30)
            }
        }
        .task(id: homeLoadedAt) {
            guard homeLoadedAt != nil else { return }
            await viewModel.load()
        }
    }

    private func ready(_ night: DSRSummary, _ report: DSRReport?) -> some View {
        let phase = report?.phase ?? night.phase
        let summary = report?.narrative?.executiveSummary?.text
        let sales = report?.facts.blocks["sales"]
        return VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(kicker: "Last night", title: "Daily report", trailing: night.displayDate)
            Button {
                Haptic.light()
                open(.report(date: night.businessDate))
            } label: {
                VStack(alignment: .leading, spacing: 12) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        if let sales, sales.isReady, let net = sales.metric("net") {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(DSRFormat.money(net))
                                    .font(.cavnarNumber(30, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                    .cavnarSensitive()
                                if let vs = sales.metric("vs_yesterday_pct") {
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
                    if let summary {
                        HomeMixedText.make(summary, size: 14.5, color: .cavnarInk2)
                            .lineLimit(4)
                            .multilineTextAlignment(.leading)
                    } else if phase == .running {
                        Text(report?.checklist?.statusLabel ?? "The report is being built.")
                            .font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
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
        /// The latest night, and its report when it could be read.
        case ready(DSRSummary, DSRReport?)
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
            let report: DSRReport? = try? await client.send("/mobile/api/dsr/\(latest.businessDate)",
                                                            hapticOnError: false)
            state = .ready(latest, report)
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
