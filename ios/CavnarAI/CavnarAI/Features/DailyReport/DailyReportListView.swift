import SwiftUI

/// Every night's report, newest first — Home's "Last night" card → "All
/// nights". Close day for tonight sits at the top (the one primary action
/// here); each row opens that night.
struct DailyReportListView: View {
    /// Appends to Home's NavigationPath (HomeView owns it).
    var open: (DailyReportRoute) -> Void
    @State private var viewModel = DailyReportListViewModel()
    @State private var clock = CavnarEntranceClock()
    @State private var didLoad = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if viewModel.isLoading && viewModel.reports.isEmpty {
                    CavnarSkeletonLines(widths: [1, 0.9, 0.95, 0.8]).cavnarCard()
                } else if viewModel.noAccess {
                    Text(viewModel.errorMessage ?? "The daily report isn\u{2019}t part of your access.")
                        .cavnarText(.body)
                        .cavnarCard()
                } else {
                    CachedDataNotice(text: viewModel.stalenessNotice)
                    // Switched off here (parity audit #64): past nights
                    // still open; nothing new is built, so no Close day.
                    if viewModel.enabled {
                        tonightCard
                    } else {
                        CavnarCaveat(title: "Switched off", detail: DSRAvailability.offLine)
                    }
                    if let error = viewModel.errorMessage, viewModel.reports.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text(error).cavnarText(.body, color: .cavnarRedText)
                            Button("Try again") { Task { await viewModel.load() } }
                                .buttonStyle(CavnarSecondaryButtonStyle())
                        }
                        .cavnarCard()
                    } else if viewModel.reports.isEmpty {
                        CavnarEmptyHearth(title: "No nights yet",
                                          message: "Each night's report lands here after close.")
                    } else {
                        Button {
                            Haptic.light()
                            open(.week(date: viewModel.reports.first?.businessDate))
                        } label: {
                            HStack {
                                Image(systemName: "tablecells").font(.cavnar(.secondary))
                                Text("The week, day by day").font(.cavnarBody(CavnarType.body, weight: 700))
                                Spacer()
                                Image(systemName: "chevron.right").font(.cavnar(.caption))
                            }
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 4)

                        VStack(spacing: 0) {
                            ForEach(Array(viewModel.reports.enumerated()), id: \.element.id) { index, night in
                                Button {
                                    Haptic.light()
                                    open(.report(date: night.businessDate))
                                } label: {
                                    row(night)
                                }
                                .buttonStyle(.plain)
                                .cavnarRowEntrance(index: index, clock: clock)
                                .onAppear {
                                    if night.id == viewModel.reports.last?.id { Task { await viewModel.loadMore() } }
                                }
                                if index < viewModel.reports.count - 1 { AccountRowDivider() }
                            }
                            if viewModel.isLoadingMore {
                                CavnarWorkingLine().padding(.vertical, 12)
                            }
                        }
                        .accountCard()
                    }
                }
            }
            .padding(.horizontal, 20)
            .padding(.top, 8)
            .padding(.bottom, 80)
        }
        .cavnarEmberRefreshable { await viewModel.load() }
        .cavnarModuleBackground()
        .navigationTitle("Daily reports")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { cavnarTitleToolbar("Daily reports") }
        .cavnarEmberBackButton()
        .task {
            guard !didLoad else { return }
            didLoad = true
            await viewModel.load()
        }
        // A night that finished while the app was away shows up on return
        // (audit 4.2).
        .refreshOnForeground(lastLoaded: viewModel.lastLoadedAt) { await viewModel.load() }
    }

    private var tonightCard: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            CavnarKicker("Tonight")
            Text("The report builds itself after close. Close the day to build it now.")
                .cavnarText(.body)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                Haptic.medium()
                Task {
                    if let route = await viewModel.closeTonight() { open(route) }
                }
            } label: {
                Group {
                    if viewModel.isClosing { CavnarShimmerText(text: "Closing the day") } else { Text("Close day") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: viewModel.isClosing))
            .disabled(viewModel.isClosing)
            .confirmationDialog(DSRCloseGate.question,
                                isPresented: Binding(get: { viewModel.confirmingEarlyClose },
                                                     set: { viewModel.confirmingEarlyClose = $0 }),
                                titleVisibility: .visible) {
                Button("Close it anyway") {
                    Task { if let route = await viewModel.closeTonight(early: true) { open(route) } }
                }
                Button("Cancel", role: .cancel) {}
            }
            if let error = viewModel.closeError {
                Text(error).cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard()
    }

    /// "Good day · $6,975 · +8.1% vs yesterday" — the night's verdict, net
    /// and its change against the night before, each only when the row
    /// carries it (10/8/26, readability item 99). Nil when none is there.
    static func summaryLine(_ night: DSRSummary) -> String? {
        let parts = [
            night.verdict,
            night.net.map { DSRFormat.money($0) },
            night.vsYesterdayPct.map { "\(DSRFormat.signedPct($0)) vs yesterday" },
        ].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " \u{00B7} ")
    }

    private func row(_ night: DSRSummary) -> some View {
        HStack(spacing: CavnarSpace.s) {
            VStack(alignment: .leading, spacing: CavnarSpace.xxs) {
                (Text(DSRFormat.weekday(night.businessDate).map { "\($0) " } ?? "")
                    .font(.cavnar(.label))
                 + Text(night.displayDate).font(.cavnarNumber(CavnarType.body, weight: 600)))
                    .foregroundStyle(Color.cavnarInk)
                if let line = Self.summaryLine(night) {
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        if night.verdict != nil {
                            Circle()
                                .fill(DSRScorecard.color(night.tone) ?? Color.cavnarInk3)
                                .frame(width: 8, height: 8)
                                .accessibilityHidden(true)
                        }
                        HomeMixedText.make(line, role: .secondary)
                            .lineLimit(2)
                            .cavnarSensitive()
                    }
                }
                if !night.missing.isEmpty {
                    HomeMixedText.make(night.missing.count == 1 ? night.missing[0]
                                       : "\(night.missing.count) things still missing",
                                       role: .caption, color: .cavnarAmber)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: CavnarSpace.xs)
            // A finished night needs no badge; anything else says so.
            if night.phase != .final {
                DSRStatusPill(phase: night.phase)
            }
            Image(systemName: "chevron.right")
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .accessibilityHidden(true)
        }
        .padding(.vertical, 11)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}
