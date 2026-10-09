import SwiftUI
import UniformTypeIdentifiers

/// Events and reservations — dated reasons to expect more covers than
/// the history alone would say. The generator reads them as a lift on
/// that day; the owner feeds them here, one at a time or as a pasted
/// `date,covers` list from the reservations system.
struct DemandSignalsSection: View {
    @Bindable var viewModel: ScheduleSetupViewModel
    var onExpand: (() -> Void)? = nil

    @State private var showingAdd = false
    @State private var showingPaste = false
    /// Each signal row's measured height — see CavnarFittedList.
    @State private var signalRowHeights: [DemandSignal.ID: CGFloat] = [:]

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
                    await viewModel.loadRemovedGames()
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

                Button {
                    Haptic.light()
                    showingAdd = true
                } label: {
                    HStack(spacing: CavnarSpace.xs) {
                        Image(systemName: "plus").font(.cavnar(.secondary))
                        Text("Add a party")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarSecondaryButtonStyle())
                // A whole list of reservations — a CSV or a pasted table —
                // is imported on the web (iOS readability round, 10/8/26).
                CavnarWebLinkRow(title: "Import a list of events", path: "labor/team")

                if let outcome = viewModel.signalOutcome {
                    HomeMixedText.make(outcome, size: 13.5, weight: 600, color: .cavnarGreen)
                }
                if let error = viewModel.signalError {
                    Text(error)
                        .font(.cavnarBody(13.5))
                        .foregroundStyle(Color.cavnarRed)
                        .fixedSize(horizontal: false, vertical: true)
                }

                if !viewModel.nightLessons.isEmpty { nightsTaughtCard }

                if !viewModel.eventFollows.isEmpty { followsCard }

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
                                .cavnarReportsRowHeight(signal.id, into: $signalRowHeights)
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
                    .frame(height: CavnarFittedList.height(ids: viewModel.signals.map(\.id),
                                                           measured: signalRowHeights, verticalInsets: 12))
                }

                if !viewModel.removedGames.isEmpty { removedGamesCard }
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
            Image(systemName: signal.kind == "reservations" ? "book.closed.fill" : signal.kind == "post" ? "megaphone.fill" : "party.popper.fill")
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
                // Its measured record here, when the nights have one.
                if let measured = signal.measured {
                    HomeMixedText.make(measured.line, size: 12.5, weight: 500,
                                       color: measured.applies ? .cavnarEmber2 : .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
        }
    }

    /// CALENDARS FOLLOWED FOR YOU — the web's `.ev-follows` list: one row a
    /// calendar (name, miles away, next game or "Not followed", the season
    /// so far) with its switch on the right, "Stop following" / "Follow".
    /// A button, not a native Toggle: it saves over the network
    /// (DESIGN_SYSTEM → forms, iOS).
    private var followsCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("CALENDARS FOLLOWED FOR YOU")
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            ForEach(viewModel.eventFollows) { follow in
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(follow.name)
                            .font(.cavnarBody(14, weight: 600))
                            .foregroundStyle(follow.following ? Color.cavnarInk : Color.cavnarInk3)
                        if !follow.line.isEmpty {
                            HomeMixedText.make(follow.line, size: 12.5, color: .cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if follow.following, let season = follow.seasonText, !season.isEmpty {
                            HomeMixedText.make(season, size: 12.5, weight: 500, color: .cavnarInk2)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    Spacer(minLength: 8)
                    Button {
                        Haptic.light()
                        Task { await viewModel.setFollow(seriesId: follow.seriesId, active: !follow.following) }
                    } label: {
                        Text(viewModel.followBusyId == follow.seriesId
                             ? (follow.following ? "Stopping…" : "Following…")
                             : (follow.following ? "Stop following" : "Follow"))
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .fixedSize()
                    .disabled(viewModel.followBusyId != nil)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarPaper3.opacity(0.35)))
    }

    /// GAMES YOU REMOVED — catalog games swiped off this list stay off
    /// every forecast, brief and report until put back here.
    private var removedGamesCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("GAMES YOU REMOVED")
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            ForEach(viewModel.removedGames) { game in
                HStack(alignment: .center, spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        HomeMixedText.make(game.text, size: 14, weight: 600, color: .cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        HomeMixedText.make(game.removedLine, size: 12.5, color: .cavnarInk3)
                    }
                    Spacer(minLength: 8)
                    Button {
                        Haptic.light()
                        Task { await viewModel.restoreGame(eventId: game.eventId) }
                    } label: {
                        Text(viewModel.restoringGameId == game.eventId ? "Putting back…" : "Put back")
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    .fixedSize()
                    .disabled(viewModel.restoringGameId != nil)
                }
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarPaper3.opacity(0.35)))
    }

    /// WHAT YOUR NIGHTS HAVE TAUGHT — each recurring effect's sentence
    /// (event_memory.summaries: M/D/YY, "before and after, not proof").
    /// The ones past the sample floor lead, lit; the rest are dimmer.
    private var nightsTaughtCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("WHAT YOUR NIGHTS HAVE TAUGHT")
                .font(.cavnarBody(CavnarType.kicker, weight: 700))
                .tracking(1.4)
                .foregroundStyle(Color.cavnarEmber2)
            ForEach(viewModel.nightLessons.prefix(6)) { lesson in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Circle()
                        .fill(lesson.applies ? Color.cavnarEmber : Color.cavnarInk3.opacity(0.6))
                        .frame(width: 6, height: 6)
                    HomeMixedText.make(lesson.text, size: 13.5, color: lesson.applies ? .cavnarInk2 : .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Text("Measured on your own nights against a typical same weekday. The forecast uses one only once it has enough nights.")
                .font(.cavnarBody(12.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarEmber.opacity(0.06)))
    }

    private func detail(_ signal: DemandSignal) -> String {
        var parts: [String] = [signal.kindLabel.lowercased()]
        if let covers = signal.covers, covers > 0 { parts.append("\(covers) covers") }
        if let lift = signal.liftPct, lift != 0 {
            parts.append("\(lift > 0 ? "+" : "")\(Int(lift.rounded()))% lift")
        }
        // A catalog game is worded as the web words it, never the raw
        // source key "events" (re-audit 2 RX-10); a feed says which.
        if let source = signal.source, !source.isEmpty, source != "manual" {
            parts.append(source == "events" ? "from a calendar you follow" : "via \(source)")
        }
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
            .font(.cavnarBody(CavnarType.kicker, weight: 700))
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
    @Environment(\.colorSchemeContrast) private var contrast

    @State private var csv = ""
    @FocusState private var focused: Bool
    @State private var importing = false
    @State private var fileError: String?

    private var lineCount: Int {
        csv.split(whereSeparator: \.isNewline).filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }.count
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text("One reservation day per line as date,covers — the date as 9/21/26 or 2026-09-21. A header line is fine. Or bring your reservation system's own booking export: one row per booking, cancellations and no-shows left out.")
                        .font(.cavnarBody(14.5))
                        .foregroundStyle(Color.cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)

                    // The booking export as a file (schedule audit 10/3/26
                    // D-31) — read into the box, sent with Add these.
                    Button {
                        Haptic.light()
                        fileError = nil
                        importing = true
                    } label: {
                        HStack(spacing: 6) {
                            Image(systemName: "doc.badge.plus").font(.system(size: 12, weight: .semibold))
                            Text("Import your reservation system\u{2019}s booking export")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle())
                    if let fileError {
                        Text(fileError).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }

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
                                    .foregroundStyle(Color.cavnarInk3Muted(contrast))
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
                    if let report = viewModel.importReport {
                        HomeMixedText.make(report.sentence, size: 13.5, color: .cavnarInk2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let error = viewModel.signalError {
                        Text(error).font(.cavnarBody(14)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                    VStack(spacing: 10) {
                        Button {
                            focused = false
                            Task {
                                // A booking export's report stays on screen
                                // to be read; a plain paste closes.
                                if await viewModel.pasteSignals(csv: csv), viewModel.signalError == nil,
                                   viewModel.importReport == nil {
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
            .fileImporter(isPresented: $importing, allowedContentTypes: [.commaSeparatedText, .plainText, .text]) { result in
                switch result {
                case .success(let url):
                    let scoped = url.startAccessingSecurityScopedResource()
                    defer { if scoped { url.stopAccessingSecurityScopedResource() } }
                    if let data = try? Data(contentsOf: url),
                       let text = String(data: data, encoding: .utf8) ?? String(data: data, encoding: .isoLatin1) {
                        csv = text
                    } else {
                        fileError = "That file couldn\u{2019}t be read \u{2014} export it as a CSV and try again."
                    }
                case .failure:
                    fileError = "That file couldn\u{2019}t be opened."
                }
            }
        }
        .onAppear {
            viewModel.signalError = nil
            viewModel.signalOutcome = nil
            focused = true
        }
    }
}
