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
                        .font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarInk3)
                        .cavnarCard()
                } else {
                    tonightCard
                    if let error = viewModel.errorMessage, viewModel.reports.isEmpty {
                        VStack(alignment: .leading, spacing: 10) {
                            Text(error).font(.cavnarBody(14.5)).foregroundStyle(Color.cavnarRed)
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
                                Image(systemName: "tablecells").font(.system(size: 14, weight: .semibold))
                                Text("The week, day by day").font(.cavnarBody(14.5, weight: 700))
                                Spacer()
                                Image(systemName: "chevron.right").font(.system(size: 12, weight: .semibold))
                            }
                            .foregroundStyle(Color.cavnarEmber2)
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
        VStack(alignment: .leading, spacing: 10) {
            DSRKicker(text: "Tonight")
            Text("The report builds itself after close. Close the day to build it now.")
                .font(.cavnarBody(14.5))
                .foregroundStyle(Color.cavnarInk3)
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
                Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .cavnarCard()
    }

    private func row(_ night: DSRSummary) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                (Text(DSRFormat.weekday(night.businessDate).map { "\($0) " } ?? "")
                    .font(.cavnarBody(15.5, weight: 700))
                 + Text(night.displayDate).font(.cavnarNumber(15.5, weight: 600)))
                    .foregroundStyle(Color.cavnarInk)
                if !night.missing.isEmpty {
                    HomeMixedText.make(night.missing.count == 1 ? night.missing[0]
                                       : "\(night.missing.count) things still missing",
                                       size: 12.5, color: .cavnarAmber)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 8)
            DSRStatusPill(phase: night.phase)
            Image(systemName: "chevron.right")
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(Color.cavnarInk3)
        }
        .padding(.vertical, 11)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
    }
}
