import SwiftUI

struct LaborView: View {
    @State private var viewModel = LaborViewModel()

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
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
            }
            .padding(20)
        }
        .background(Color.cavnarPaper)
        .refreshable { await viewModel.load() }
        .navigationTitle("Labor")
        .task { await viewModel.load() }
    }

    @ViewBuilder
    private func overviewCard(_ stats: LaborStats) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline) {
                Text(String(format: "%.1f%%", stats.overallLaborPct))
                    .font(.cavnarNumber(32, weight: 500))
                    .foregroundStyle(Color.cavnarInk)
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
            Text("Target: \(Int(stats.target))%")
                .font(.cavnarBody(12))
                .foregroundStyle(Color.cavnarInk3)
            if stats.potentialSavings > 0 {
                Text("Est. $\(Int(stats.potentialSavings)) in optimized-scheduling savings available")
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
                    .clipShape(RoundedRectangle(cornerRadius: 8))
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
                        Text("\(role.headcount) staff")
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
                    if let hours = result.hoursScheduled {
                        Text("\(String(format: "%.1f", hours)) hours scheduled")
                            .font(.cavnarBody(13, weight: 600))
                            .foregroundStyle(Color.cavnarInk)
                    }
                    ForEach(result.summary ?? [], id: \.self) { line in
                        Text("• \(line)")
                            .font(.cavnarBody(12))
                            .foregroundStyle(Color.cavnarInk2)
                    }
                }
                .cavnarCard()
            }
        }
    }
}
