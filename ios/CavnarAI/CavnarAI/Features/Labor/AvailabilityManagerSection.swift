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
    @Bindable var viewModel: LaborViewModel
    var onExpand: (() -> Void)? = nil

    @State private var name = ""
    @State private var selectedDays: Set<String> = []
    @State private var notes = ""
    @FocusState private var focusedField: Field?

    private enum Field: Hashable, CaseIterable {
        case name, notes
    }

    var body: some View {
        CavnarDropdown(
            title: "Employee availability",
            subtitle: viewModel.availability.isEmpty ? "Set who's available which days" : "\(viewModel.availability.count) on file",
            isExpanded: $viewModel.availabilityExpanded,
            onExpand: onExpand
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("The AI scheduler will never schedule someone on a day marked unavailable here.")
                    .font(.cavnarBody(14))
                    .foregroundStyle(Color.cavnarInk3)

                addForm

                if viewModel.isLoadingAvailability && viewModel.availability.isEmpty {
                    ProgressView().frame(maxWidth: .infinity).padding(.vertical, 12)
                } else if viewModel.availability.isEmpty {
                    Text("No availability set yet — add someone above.")
                        .font(.cavnarBody(14))
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
            // Tapping anywhere in the expanded section outside an actual
            // text field dismisses the keyboard — Enter was previously the
            // only way to close it. A Color.clear background is needed
            // for the tap gesture to actually hit an empty area of the
            // VStack (a VStack with no background is transparent to hit
            // testing, same as any SwiftUI container with no fill).
            .contentShape(Rectangle())
            .onTapGesture { focusedField = nil }
        }
        // Auto-focuses the name field once the section's own expand
        // animation has settled — 0.3s, CavnarDropdown's real expand
        // duration (see scrollToReveal's comment for why this must match
        // exactly, not the animation's own now-corrected 0.22s-vs-0.3s
        // mismatch): focusing mid-expand made the keyboard fight the
        // still-growing dropdown instead of the two reading as one
        // motion, same "ready to type immediately" feel as Ask Cavnar's
        // input.
        //
        // Collapsing while a field is still focused previously left nothing
        // dismissing the keyboard — it either stayed up over the now-
        // shrinking content or got yanked away out of sync with the
        // collapse animation. Clearing focus in lockstep with the collapse
        // lets both run as one animation instead of two fighting ones.
        //
        // The FIRST expand specifically (not later ones) had another,
        // separate problem on top of the dropdown-timing one above: the
        // loading spinner for the availability list lives INSIDE this
        // dropdown's own expanded content (see the ProgressView branch
        // above), so if loadAvailability() (fired from LaborView's .task
        // when the tab first appears) hasn't resolved yet by the time
        // someone taps to expand, the content is short (spinner) and then
        // resizes — spinner to real rows or the empty-state text — right
        // in the middle of this focus/scroll choreography, fighting both
        // animations at once. Every later expand is already smooth
        // because the data's already cached from the first load, so
        // there's nothing left to resize. Fixed by not focusing until
        // BOTH the dropdown has settled AND the load has actually
        // finished, whichever comes last.
        .onChange(of: viewModel.availabilityExpanded) { _, expanded in
            if expanded {
                scheduleAutoFocus()
            } else {
                focusedField = nil
            }
        }
        .onChange(of: viewModel.isLoadingAvailability) { _, isLoading in
            guard !isLoading, viewModel.availabilityExpanded else { return }
            scheduleAutoFocus()
        }
        // Follows ANY focus change, not just the auto-focus above — a
        // manual tap into a field later (e.g. re-opening notes after
        // already dismissing the keyboard once) gets the same "scroll to
        // keep it above the keyboard" treatment auto-focus gets, instead
        // of only working the one time this view happens to drive focus
        // itself. Fires immediately (no extra delay) — the keyboard's own
        // rise animation and this scroll then move together rather than
        // the scroll waiting for the keyboard to finish first.
        .onChange(of: focusedField) { _, newValue in
            guard newValue != nil else { return }
            onExpand?()
        }
        .keyboardNavToolbar($focusedField)
    }

    /// Schedules focus 0.3s out (CavnarDropdown's real expand duration —
    /// see the .onChange(of: viewModel.availabilityExpanded) comment
    /// above), then re-checks both conditions are STILL true before
    /// actually focusing. Called from two different triggers (expand, and
    /// load finishing) — harmless if both fire close together, since
    /// setting focusedField to a value it's already set to is a no-op.
    private func scheduleAutoFocus() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            guard viewModel.availabilityExpanded, !viewModel.isLoadingAvailability else { return }
            focusedField = .name
        }
    }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("Employee name", text: $name)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .name)

            VStack(alignment: .leading, spacing: 6) {
                Text("Available days")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk3)
                HStack(spacing: 6) {
                    ForEach(Array(zip(LaborDayOfWeek.allNames, LaborDayOfWeek.shortLabels)), id: \.0) { full, short in
                        dayChip(full: full, short: short)
                    }
                }
            }

            TextField("Notes (optional) — e.g. student, no mornings", text: $notes)
                .cavnarTextFieldStyle()
                .focused($focusedField, equals: .notes)

            Button {
                Task {
                    await viewModel.saveAvailability(
                        employeeName: name, availableDays: Array(selectedDays),
                        notes: notes.isEmpty ? nil : notes
                    )
                    name = ""
                    notes = ""
                    selectedDays = []
                    focusedField = nil
                }
            } label: {
                if viewModel.isSavingAvailability {
                    PulsingText("Saving…")
                } else {
                    Text("Save")
                }
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: name.trimmingCharacters(in: .whitespaces).isEmpty))
            .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || viewModel.isSavingAvailability)

            if let error = viewModel.availabilityError {
                Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
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
                .font(.cavnarBody(14, weight: 600))
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
                    .font(.cavnarBody(14, weight: 600))
                    .foregroundStyle(Color.cavnarInk)
                if !entry.availableDays.isEmpty {
                    Text("✓ Available: \(entry.availableDays.map { String($0.prefix(3)) }.joined(separator: ", "))")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarGreen)
                }
                if !entry.unavailableDays.isEmpty {
                    Text("✗ Not available: \(entry.unavailableDays.map { String($0.prefix(3)) }.joined(separator: ", "))")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarRed)
                }
                if let notes = entry.notes, !notes.isEmpty {
                    Text(notes)
                        .font(.cavnarBody(14))
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
