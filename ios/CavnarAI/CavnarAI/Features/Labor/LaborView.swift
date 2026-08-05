import SwiftUI

private enum LaborSubTab: String, CaseIterable, Identifiable {
    case overview = "Overview"
    case analytics = "Analytics"
    var id: String { rawValue }
}

struct LaborView: View {
    @State private var viewModel = LaborViewModel()
    @State private var analyticsViewModel = LaborAnalyticsViewModel()
    @State private var subTab: LaborSubTab = .overview

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $subTab) {
                ForEach(LaborSubTab.allCases) { tab in
                    Text(tab.rawValue).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.top, 8)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    if subTab == .overview {
                        if let stats = viewModel.stats {
                            overviewCard(stats)
                            if !stats.overtimeRisk.isEmpty {
                                overtimeSection(stats.overtimeRisk)
                            }
                            if !stats.roleSummary.isEmpty {
                                roleSection(stats.roleSummary)
                            }
                            scheduleSection
                        } else if viewModel.isLoading {
                            ProgressView().padding(.top, 60)
                        } else if let error = viewModel.errorMessage {
                            VStack(spacing: 8) {
                                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarInk3)
                                Button("Retry") { Task { await viewModel.load() } }
                            }
                            .padding(.top, 60)
                            .frame(maxWidth: .infinity)
                        }
                    } else {
                        LaborAnalyticsSection(viewModel: analyticsViewModel)
                    }
                }
                .padding(20)
            }
        }
        .background(Color.cavnarPaper)
        .refreshable { await viewModel.load() }
        .navigationTitle("Labor")
        .task { await viewModel.load() }
        .task { await analyticsViewModel.load() }
    }

    @ViewBuilder
    private func overviewCard(_ stats: LaborStats) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(String(format: "%.1f%%", stats.overallLaborPct))
                    .font(.cavnarNumber(32, weight: 500))
                    .foregroundStyle(Color.cavnarInk)
                    .cavnarNumberGlow()
                Text("of sales")
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarInk3)
                Spacer()
                Text(stats.onTrack ? "On track" : "Over target")
                    .font(.cavnarBody(11, weight: 700))
                    .foregroundStyle(stats.onTrack ? Color.cavnarGreen : Color.cavnarRed)
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(stats.onTrack ? Color.cavnarGreenBg : Color.cavnarRedBg)
                    .clipShape(Capsule())
            }
            (Text("Target: ") + Text("\(Int(stats.target))%").font(.cavnarNumber(12)))
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
            if stats.potentialSavings > 0 {
                (Text("Est. ") + Text("$\(Int(stats.potentialSavings))").font(.cavnarNumber(12, weight: 600)) + Text(" in optimized-scheduling savings available"))
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarAmber)
            }
        }
        .cavnarCard()
    }

    @ViewBuilder
    private func overtimeSection(_ entries: [LaborOvertimeEntry]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Overtime risk")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            VStack(spacing: 8) {
                ForEach(entries) { entry in
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(entry.employee ?? "Unknown")
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                            Text(entry.week ?? "")
                                .font(.cavnarBody(11))
                                .foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        Text("\(entry.hours.map { String(format: "%.1f", $0) } ?? "—")h")
                            .font(.cavnarNumber(14, weight: 600))
                            .foregroundStyle(entry.status == "overtime" ? Color.cavnarRed : Color.cavnarAmber)
                    }
                    .padding(10)
                    .background((entry.status == "overtime" ? Color.cavnarRed : Color.cavnarAmber).opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
                }
            }
        }
    }

    @ViewBuilder
    private func roleSection(_ roles: [LaborRoleSummary]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("By role")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            VStack(spacing: 8) {
                ForEach(roles) { role in
                    HStack {
                        Text(role.role)
                            .font(.cavnarBody(13, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                        Spacer()
                        (Text("\(role.headcount)").font(.cavnarNumber(11)) + Text(" staff"))
                            .font(.cavnarBody(11))
                            .foregroundStyle(Color.cavnarInk3)
                        Text(String(format: "%.1f%%", role.laborPct))
                            .font(.cavnarNumber(13, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                    }
                    .padding(.vertical, 4)
                }
            }
            .cavnarCard()
        }
    }

    @ViewBuilder
    private var scheduleSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("AI schedule")
                .font(.cavnarBody(13, weight: 700))
                .foregroundStyle(Color.cavnarInk)

            Button {
                Task { await viewModel.generateSchedule() }
            } label: {
                if viewModel.isGeneratingSchedule {
                    HStack(spacing: 8) {
                        ProgressView().tint(.white)
                        Text("Generating…")
                    }
                    .frame(maxWidth: .infinity)
                } else {
                    Text("Generate next week's schedule")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle())
            .disabled(viewModel.isGeneratingSchedule)

            if let error = viewModel.scheduleError {
                Text(error)
                    .font(.cavnarBody(12))
                    .foregroundStyle(Color.cavnarRed)
            }

            if let result = viewModel.scheduleResult, result.ok {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        if let hours = result.hoursScheduled {
                            (Text(String(format: "%.1f", hours)).font(.cavnarNumber(13, weight: 600)) + Text(" hours scheduled"))
                                .font(.cavnarBody(13, weight: 600))
                                .foregroundStyle(Color.cavnarInk)
                        }
                        Spacer()
                        if let csv = result.scheduleCsv {
                            ShareLink(item: csv, preview: SharePreview("Schedule.csv")) {
                                Image(systemName: "square.and.arrow.up")
                            }
                        }
                    }
                    ForEach(result.summary ?? [], id: \.self) { line in
                        Text("• \(line)")
                            .font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                }
                .cavnarCard()

                if let rows = result.previewRows, !rows.isEmpty {
                    fullScheduleTable(rows)
                }
            }
        }
    }

    @ViewBuilder
    private func fullScheduleTable(_ rows: [ScheduleRow]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Full schedule")
                .font(.cavnarBody(12, weight: 700))
                .foregroundStyle(Color.cavnarInk3)
            VStack(spacing: 6) {
                ForEach(rows) { row in
                    HStack {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(row.employee ?? "").font(.cavnarBody(12, weight: 600)).foregroundStyle(Color.cavnarInk)
                            Text("\(row.day ?? "") · \(row.role ?? "")").font(.cavnarBody(10)).foregroundStyle(Color.cavnarInk3)
                        }
                        Spacer()
                        Text("\(row.shiftStart ?? "")–\(row.shiftEnd ?? "")")
                            .font(.cavnarNumber(11))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                    .padding(.vertical, 4)
                }
            }
            .cavnarCard()
        }
    }
}
