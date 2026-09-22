import SwiftUI

/// Events and reservations — dated reasons to expect more covers than
/// the history alone would say. The generator reads them as a lift on
/// that day; the owner feeds them here, one at a time or as a pasted
/// `date,covers` list from the reservations system.
struct DemandSignalsSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil

    @State private var showingAdd = false
    @State private var showingPaste = false

    var body: some View {
        CavnarDropdown(
            title: "Events & reservations",
            subtitle: subtitle,
            badge: viewModel.signals.isEmpty ? nil : viewModel.signals.count,
            tone: .neutral,
            isExpanded: $viewModel.demandExpanded,
            onExpand: {
                onExpand?()
                Task {
                    await viewModel.loadSignals()
                    // The feed's status line comes with the rules.
                    if viewModel.reservationFeed == nil { await viewModel.loadRules() }
                }
            }
        ) {
            VStack(alignment: .leading, spacing: 14) {
                Text("The next 60 days. A party of forty on a Tuesday is the difference between a quiet night and a slammed one, and the history alone cannot see it coming.")
                    .font(.cavnarBody(13.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)

                // The reservation system's own sentence — connected, keyed
                // but not live, or nothing at all. Set under Schedule rules.
                if let feed = viewModel.reservationFeed, let message = feed.message, !message.isEmpty {
                    HStack(alignment: .top, spacing: 7) {
                        Image(systemName: feed.live == true ? "antenna.radiowaves.left.and.right" : "antenna.radiowaves.left.and.right.slash")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(feed.live == true ? Color.cavnarGreen : Color.cavnarInk3)
                            .padding(.top, 2)
                        HomeMixedText.make(message, size: 12.5, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }

                HStack(spacing: 10) {
                    Button {
                        Haptic.light()
                        showingAdd = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "plus").font(.system(size: 11, weight: .bold))
                            Text("Add one")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    Button {
                        Haptic.light()
                        showingPaste = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "doc.on.clipboard").font(.system(size: 11, weight: .bold))
                            Text("Paste a list")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                }

                if let outcome = viewModel.signalOutcome {
                    HomeMixedText.make(outcome, size: 13.5, weight: 600, color: .cavnarGreen)
                }
                if let error = viewModel.signalError {
                    Text(error)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if viewModel.isLoadingSignals && viewModel.signals.isEmpty {
                    CavnarSkeletonLines(widths: [1.0, 0.8, 0.6])
                } else if viewModel.signals.isEmpty {
                    Text("Nothing on the books for the next 60 days. Add a party or paste your reservations and the next draft will staff for them.")
                        .font(.cavnarBody(14))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    List {
                        ForEach(viewModel.signals) { signal in
                            signalRow(signal)
                                .listRowBackground(Color.clear)
                                .listRowInsets(EdgeInsets(top: 6, leading: 0, bottom: 6, trailing: 0))
                                .listRowSeparatorTint(Color.cavnarPaper3.opacity(0.5))
                        }
                        .onDelete { offsets in
                            let ids = offsets.map { viewModel.signals[$0].id }
                            Task { for id in ids { await viewModel.deleteSignal(id: id) } }
                        }
                    }
                    .listStyle(.plain)
                    .scrollContentBackground(.hidden)
                    .scrollDisabled(true)
                    .frame(height: CGFloat(viewModel.signals.count) * 58)
                }
            }
        }
        .sheet(isPresented: $showingAdd) {
            DemandSignalEditor(viewModel: viewModel)
        }
        .sheet(isPresented: $showingPaste) {
            DemandSignalPasteSheet(viewModel: viewModel)
        }
    }

    private var subtitle: String {
        if viewModel.signals.isEmpty { return "What the history can't see coming" }
        let covers = viewModel.signals.compactMap(\.covers).reduce(0, +)
        return covers > 0 ? "\(covers) covers on the books" : "Next 60 days"
    }

    private func signalRow(_ signal: DemandSignal) -> some View {
        HStack(spacing: 10) {
            Image(systemName: signal.kind == "reservations" ? "book.closed.fill" : "party.popper.fill")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Color.cavnarEmber)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(CavnarDate.mdy(signal.date))
                        .font(.cavnarNumber(14.5, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    Text(signal.label?.isEmpty == false ? signal.label! : signal.kindLabel)
                        .font(.cavnarBody(14.5, weight: 600))
                        .foregroundStyle(Color.cavnarInk)
                        .lineLimit(1)
                }
                HomeMixedText.make(detail(signal), size: 13, color: .cavnarInk3)
            }
            Spacer()
        }
    }

    private func detail(_ signal: DemandSignal) -> String {
        var parts: [String] = [signal.kindLabel.lowercased()]
        if let covers = signal.covers, covers > 0 { parts.append("\(covers) covers") }
        if let lift = signal.liftPct, lift != 0 {
            parts.append("\(lift > 0 ? "+" : "")\(Int(lift.rounded()))% lift")
        }
        if let source = signal.source, !source.isEmpty, source != "manual" { parts.append("from \(source)") }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Add one

private struct DemandSignalEditor: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var date = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var kind = "event"
    @State private var label = ""
    @State private var covers = ""
    @State private var lift = ""
    @FocusState private var focused: Field?
    private enum Field: Hashable, CaseIterable { case label, covers, lift }

    private var canSave: Bool {
        !viewModel.isSavingSignal && (!covers.isEmpty || !lift.isEmpty || !label.isEmpty)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    VStack(alignment: .leading, spacing: 8) {
                        kicker("Date")
                        DatePicker("", selection: $date, in: Date()..., displayedComponents: .date)
                            .datePickerStyle(.compact)
                            .labelsHidden()
                            .tint(Color.cavnarEmber)
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        kicker("Kind")
                        HStack(spacing: 8) {
                            kindChip("Event", value: "event")
                            kindChip("Reservations", value: "reservations")
                        }
                    }
                    CavnarFloatingField(icon: "tag", placeholder: "Label — e.g. Rehearsal dinner, 40",
                                        text: $label, focus: $focused, field: .label)
                    CavnarFloatingField(icon: "person.3", placeholder: "Covers", text: $covers,
                                        keyboardType: .numberPad, focus: $focused, field: .covers)
                    CavnarFloatingField(icon: "arrow.up.right", placeholder: "Lift % (optional, e.g. 25)",
                                        text: $lift, keyboardType: .numbersAndPunctuation,
                                        focus: $focused, field: .lift)
                    Text("Give covers when you know them; a lift is for a day you expect to be busier without a number to hang on it.")
                        .font(.cavnarBody(13))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                    if let error = viewModel.signalError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(spacing: 10) {
                        Button {
                            focused = nil
                            Task {
                                let ok = await viewModel.addSignal(
                                    date: date, kind: kind, label: label,
                                    covers: Int(covers.trimmingCharacters(in: .whitespaces)),
                                    liftPct: Double(lift.trimmingCharacters(in: .whitespaces)))
                                if ok { dismiss() }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingSignal {
                                    CavnarShimmerText(text: "Saving…")
                                } else {
                                    Text("Add")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !canSave))
                        .disabled(!canSave)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Add a date")
            .keyboardNavToolbar($focused)
        }
        .onAppear { viewModel.signalError = nil }
    }

    private func kicker(_ text: String) -> some View {
        Text(text.uppercased())
            .font(.cavnarBody(11.5, weight: 700))
            .tracking(0.8)
            .foregroundStyle(Color.cavnarInk3)
    }

    private func kindChip(_ label: String, value: String) -> some View {
        let on = kind == value
        return Button {
            Haptic.selection()
            kind = value
        } label: {
            Text(label)
                .font(.cavnarBody(14, weight: on ? 700 : 500))
                .foregroundStyle(on ? Color.cavnarPaper : Color.cavnarInk2)
                .frame(maxWidth: .infinity, minHeight: 36)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(on ? Color.cavnarEmber : Color.cavnarPaper2))
                .overlay(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .strokeBorder(Color.cavnarPaper3, lineWidth: on ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Paste a list

/// `date,covers` lines straight from the reservations export. The server
/// says what it wrote, what it skipped and what it could not read.
private struct DemandSignalPasteSheet: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    @Environment(\.dismiss) private var dismiss

    @State private var csv = ""
    @FocusState private var focused: Bool

    private var lineCount: Int {
        csv.split(whereSeparator: \.isNewline).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }.count
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("One reservation day per line as date,covers — the date as 9/21/26 or 2026-09-21. A header line is fine.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    TextEditor(text: $csv)
                        .font(.cavnarNumber(15))
                        .foregroundStyle(Color.cavnarInk)
                        .scrollContentBackground(.hidden)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .focused($focused)
                        .frame(minHeight: 180)
                        .padding(10)
                        .background(
                            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                                .fill(Color.cavnarPaper2))
                        .overlay(
                            RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
                                .strokeBorder(focused ? Color.cavnarEmber : Color.cavnarPaper3, lineWidth: 1))
                        .overlay(alignment: .topLeading) {
                            if csv.isEmpty {
                                Text("9/26/26,64\n9/27/26,88")
                                    .font(.cavnarNumber(15))
                                    .foregroundStyle(Color.cavnarInk3.opacity(0.6))
                                    .padding(.horizontal, 15)
                                    .padding(.top, 18)
                                    .allowsHitTesting(false)
                            }
                        }

                    if lineCount > 0 {
                        HomeMixedText.make("\(lineCount) \(lineCount == 1 ? "line" : "lines")", size: 13.5, color: .cavnarInk3)
                    }
                    if let outcome = viewModel.signalOutcome {
                        HomeMixedText.make(outcome, size: 14, weight: 600, color: .cavnarGreen)
                    }
                    if let error = viewModel.signalError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    VStack(spacing: 10) {
                        Button {
                            focused = false
                            Task {
                                if await viewModel.pasteSignals(csv: csv), viewModel.signalError == nil {
                                    dismiss()
                                }
                            }
                        } label: {
                            Group {
                                if viewModel.isSavingSignal {
                                    CavnarShimmerText(text: "Reading…")
                                } else {
                                    Text("Add these")
                                }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: lineCount == 0 || viewModel.isSavingSignal))
                        .disabled(lineCount == 0 || viewModel.isSavingSignal)
                        Button { dismiss() } label: { Text("Cancel").frame(maxWidth: .infinity) }
                            .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Paste reservations")
        }
        .onAppear {
            viewModel.signalError = nil
            viewModel.signalOutcome = nil
            focused = true
        }
    }
}
