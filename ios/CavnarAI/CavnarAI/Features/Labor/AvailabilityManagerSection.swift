import SwiftUI

/// Employee availability manager — add staff, mark which days they can't
/// work, and leave a note; the AI scheduler already respects this data
/// (client_api.py's schedule builder reads the same staff_availability
/// rows and is told never to schedule someone on an unavailable day). Ports
/// the web Labor tab's "Employee Availability" panel, previously entirely
/// absent on iOS. Collapsed by default via CavnarDropdown — this is a setup
/// tool touched occasionally, not a daily-glance metric like the sections
/// above it.
struct AvailabilityManagerSection: View {
    let viewModel: LaborViewModel
    var onExpand: (() -> Void)? = nil

    @State private var name = ""
    @State private var selectedDays: Set<String> = []
    @State private var notes = ""

    var body: some View {
        CavnarDropdown(
            title: "Employee availability",
            subtitle: viewModel.availability.isEmpty ? "Set who's available which days" : "\(viewModel.availability.count) on file",
            onExpand: onExpand
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("The AI scheduler will never schedule someone on a day marked unavailable here.")
                    .font(.cavnarBody(11))
                    .foregroundStyle(Color.cavnarInk3)

                addForm

                if viewModel.isLoadingAvailability && viewModel.availability.isEmpty {
                    ProgressView().frame(maxWidth: .infinity).padding(.vertical, 12)
                } else if viewModel.availability.isEmpty {
                    Text("No availability set yet — add someone above.")
                        .font(.cavnarBody(11))
                        .foregroundStyle(Color.cavnarInk3)
                        .italic()
                } else {
                    VStack(spacing: 8) {
                        ForEach(viewModel.availability) { entry in
                            availabilityRow(entry)
                        }
                    }
                }
            }
        }
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("Employee name", text: $name)
                .cavnarTextFieldStyle()

            VStack(alignment: .leading, spacing: 6) {
                Text("Available days")
                    .font(.cavnarBody(10, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                HStack(spacing: 6) {
                    ForEach(Array(zip(LaborDayOfWeek.allNames, LaborDayOfWeek.shortLabels)), id: \.0) { full, short in
                        dayChip(full: full, short: short)
                    }
                }
            }

            TextField("Notes (optional) — e.g. student, no mornings", text: $notes)
                .cavnarTextFieldStyle()

            Button {
                Task {
                    await viewModel.saveAvailability(
                        employeeName: name, availableDays: Array(selectedDays),
                        notes: notes.isEmpty ? nil : notes
                    )
                    name = ""
                    notes = ""
                    selectedDays = []
                }
            } label: {
                if viewModel.isSavingAvailability {
                    ProgressView().tint(.white).frame(maxWidth: .infinity)
                } else {
                    Text("Save")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: name.trimmingCharacters(in: .whitespaces).isEmpty))
            .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || viewModel.isSavingAvailability)

            if let error = viewModel.availabilityError {
                Text(error).font(.cavnarBody(11)).foregroundStyle(Color.cavnarRed)
            }
        }
        .padding(12)
        .background(Color.cavnarPaper2.opacity(0.4))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }

    private func dayChip(full: String, short: String) -> some View {
        let isOn = selectedDays.contains(full)
        return Button {
            Haptic.selection()
            if isOn { selectedDays.remove(full) } else { selectedDays.insert(full) }
        } label: {
            Text(short)
                .font(.cavnarBody(11, weight: 600))
                .foregroundStyle(isOn ? Color.cavnarInk : Color.cavnarInk3)
                .frame(width: 38, height: 30)
                .background(isOn ? Color.cavnarEmber.opacity(0.55) : Color.cavnarPaper3.opacity(0.5))
                .clipShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
    }

    private func availabilityRow(_ entry: StaffAvailabilityEntry) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(entry.employeeName)
                    .font(.cavnarBody(12, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if !entry.availableDays.isEmpty {
                    Text("✓ Available: \(entry.availableDays.map { String($0.prefix(3)) }.joined(separator: ", "))")
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarGreen)
                }
                if !entry.unavailableDays.isEmpty {
                    Text("✗ Not available: \(entry.unavailableDays.map { String($0.prefix(3)) }.joined(separator: ", "))")
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarRed)
                }
                if let notes = entry.notes, !notes.isEmpty {
                    Text(notes)
                        .font(.cavnarBody(10))
                        .foregroundStyle(Color.cavnarInk3)
                }
            }
            Spacer()
            Button {
                Task { await viewModel.deleteAvailability(employeeName: entry.employeeName) }
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .buttonStyle(.plain)
        }
        .padding(10)
        .background(Color.cavnarPaper2.opacity(0.5))
        .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control))
    }
}
