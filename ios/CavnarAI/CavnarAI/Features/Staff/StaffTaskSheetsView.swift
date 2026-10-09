import PhotosUI
import SwiftUI
import UIKit

// MARK: - Your sheets today

/// The staff app's Tasks tab (task_sheets.py): each sheet the published
/// schedule puts this person on, in order, with due times; a line that
/// needs a reading, a note or a photo asks for it before it ticks. A manager
/// also sees the floor, last night's closing note on the opening sheet, and
/// signs a shift off.
///
/// The whole row is the tick (UX-05), at once on screen and put back if the
/// server refuses; the server's answer replaces the sheet in place
/// (PERF-08). Done lines fold per section behind "12 done · Show" and a
/// "Next: <line> · due 4pm" line under the progress bar scrolls to the
/// first open one (iOS readability round, 10/8/26), so the next thing to
/// do never drifts off the screen. With no connection the phone's copy stays up "as of 4:05pm"
/// and ticks and readings wait to send (PERF-09); a photo is taken with the
/// camera first and shrunk on the phone before it goes (UX-24, PERF-06).
///
/// The section keeps its own state (StaffTasksStore) and reads /tasks
/// itself — the app's one /tasks read path (the portal's Tasks tab mounts
/// `StaffTaskSheetsSection()`, and its pull calls the same store). The
/// optional `response` seeds the section from a read a caller already
/// holds (taken as fresh; a newer one replaces what is shown); `reload` is
/// the caller's hook and the section doesn't need it.
struct StaffTaskSheetsSection: View {
    @Environment(StaffSessionStore.self) private var staff
    private let seed: StaffTasksResponse?
    private let store: StaffTasksStore
    let reload: () async -> Void
    /// Scrolls the tab's scroll view to a line (`"line-<key>"`) — a sheet's
    /// "Next:" line takes the person to the first one still open.
    private let scrollTo: ((String) -> Void)?

    init(response: StaffTasksResponse? = nil, store: StaffTasksStore? = nil,
         reload: @escaping () async -> Void = {}, scrollTo: ((String) -> Void)? = nil) {
        self.seed = response
        self.store = store ?? StaffTasksStore.shared
        self.reload = reload
        self.scrollTo = scrollTo
    }

    /// Sections whose done lines are shown ("<sheet>|<group>"); folded by
    /// default so the next open line never drifts off the screen.
    @State private var showingDone: Set<String> = []
    @State private var values: [String: String] = [:]
    @FocusState private var focused: String?
    @State private var untick: LineRef?
    @State private var camera: LineRef?
    @State private var library: LineRef?
    @State private var libraryItem: PhotosPickerItem?
    @State private var signingOff: String?
    @State private var signoffNote = ""
    /// The manager thread about a flagged reading (I3's thread view).
    @State private var messageAbout: StaffLineMessage?
    /// The tick's disc grows with the text (Dynamic Type), from 28pt.
    @ScaledMetric(relativeTo: .body) private var discSize: CGFloat = 28
    private let network = NetworkMonitor.shared

    /// Where a line's reading, photo and note start: past the disc.
    private var inset: CGFloat { discSize + 12 }

    /// One line on one sheet — what a dialog or a picker is about.
    struct LineRef: Identifiable {
        let sheet: StaffSheet
        let line: StaffSheetLine
        var id: String { StaffSheetMerge.key(sheet.id, line.lineID) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            statusLine
            content
        }
        .task {
            store.attach(staff)
            if let seed { store.seed(seed) }
            await store.refreshIfNeeded()
        }
        // A caller's newer read (its pull to refresh) replaces what is shown.
        .onChange(of: seed?.sheets) { _, _ in
            if let seed { store.seed(seed) }
        }
        .cavnarPostedOverlay(store.posted) { store.posted = nil }
        .sheet(item: $messageAbout) { about in
            StaffMessageThreadView(context: about.context, draft: about.draft)
                .environment(staff)
        }
        .confirmationDialog(untick.map { "Mark \u{201C}\($0.line.label)\u{201D} not done?" } ?? "",
                            isPresented: Binding(get: { untick != nil }, set: { if !$0 { untick = nil } }),
                            titleVisibility: .visible, presenting: untick) { ref in
            Button("Mark not done", role: .destructive) {
                Haptic.light()
                Task { await store.tick(ref.sheet, ref.line, done: false) }
            }
            Button("Keep it done", role: .cancel) {}
        } message: { _ in
            Text("Your manager sees it as not done until it's ticked again.")
        }
        .fullScreenCover(item: $camera) { ref in
            StaffCameraPicker { image in
                camera = nil
                if let image { Task { await store.sendPhoto(image, sheet: ref.sheet, line: ref.line) } }
            }
            .ignoresSafeArea()
        }
        .photosPicker(isPresented: Binding(get: { library != nil }, set: { if !$0 && libraryItem == nil { library = nil } }),
                      selection: $libraryItem, matching: .images)
        .onChange(of: libraryItem) { _, item in
            guard let item, let ref = library else { return }
            libraryItem = nil
            library = nil
            Task {
                guard let data = try? await item.loadTransferable(type: Data.self), let image = UIImage(data: data) else {
                    // Said, never a silent nothing (re-audit L15).
                    store.banner = "That photo couldn\u{2019}t be read. Try another, or take one."
                    return
                }
                await store.sendPhoto(image, sheet: ref.sheet, line: ref.line)
            }
        }
        .alert("Sign off the \(StaffSheetFormat.kind(signingOff ?? "").lowercased()) shift?",
               isPresented: Binding(get: { signingOff != nil }, set: { if !$0 { signingOff = nil } })) {
            TextField("Anything to note (optional)", text: $signoffNote)
            Button("Sign off") {
                let kind = signingOff ?? ""
                signingOff = nil
                Task { await store.signOff(kind, note: signoffNote) }
            }
            Button("Cancel", role: .cancel) { signingOff = nil }
        } message: {
            Text("It records every sheet on the shift as it stands now. A closing note is shown to tomorrow's opener.")
        }
    }

    // MARK: What state the screen is in

    /// "Showing your sheets as of 4:05pm", ticks waiting to send, a failed
    /// refresh with Try again — said once, above the sheets.
    @ViewBuilder
    private var statusLine: some View {
        if store.queuedCount > 0 {
            let n = store.queuedCount
            Label {
                Text(network.isOnline
                     ? "\(n) tick\(n == 1 ? "" : "s") waiting to send"
                     : "Offline. \(n) tick\(n == 1 ? "" : "s") will send when you're back online.")
            } icon: {
                Image(systemName: "icloud.slash")
            }
            .font(.cavnar(.label))
            .foregroundStyle(Color.cavnarAmber)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color.cavnarAmberBg, in: Capsule())
            .accessibilityElement(children: .combine)
        }
        if store.payload != nil, store.showingCached, let asOf = store.asOf {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("Your sheets as of \(StaffSheetFormat.asOf(asOf))"
                     + (store.loadError.map { ". \($0)" } ?? ""))
                    .cavnarText(.caption, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                tryAgain
            }
        }
        if let banner = store.banner {
            Text(banner)
                .cavnarText(.secondary, color: .cavnarRedText)
                .onTapGesture { store.banner = nil }
        }
    }

    private var tryAgain: some View {
        Button {
            Task { await store.load() }
        } label: {
            // While it reads: the same words with the house shimmer, never
            // a loading word (re-audit L17).
            Group {
                if store.isLoading {
                    StaffShimmerLabel(text: "Try again", color: .cavnarEmber2)
                } else {
                    Text("Try again")
                }
            }
            .cavnarText(.label, color: .cavnarEmber2)
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(store.isLoading)
    }

    @ViewBuilder
    private var content: some View {
        if let payload = store.payload {
            if let note = store.lastNightNote { lastNightCard(note) }
            let sheets = store.sheets
            if sheets.isEmpty {
                Text("No tasks for you today.")
                    .cavnarText(.body)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .cavnarCard()
            }
            ForEach(sheets) { sheet in sheetCard(sheet) }
            if payload.manager { floorSection(payload) }
        } else if let error = store.loadError, !store.isLoading {
            VStack(alignment: .leading, spacing: 6) {
                Text(error)
                    .cavnarText(.body, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
                tryAgain
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .cavnarCard()
        } else {
            // The house loading state (DESIGN_SYSTEM §10).
            VStack(alignment: .leading, spacing: 8) {
                CavnarSkeletonBar(height: 3).frame(width: 180)
                Text("Loading your sheets").cavnarText(.body)
            }
            .accessibilityElement(children: .combine)
        }
    }

    // MARK: Last night's note (COM-16)

    private func lastNightCard(_ note: StaffLastNightNote) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            CavnarKicker("Last night\u{2019}s note")
            Text(note.note)
                .cavnarText(.body, color: .cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            let by = [note.signedBy, note.dateLabel].compactMap { $0 }.filter { !$0.isEmpty }
            if !by.isEmpty {
                CavnarMixedText("\(note.shiftKind == "any" ? "Signed off" : "Closing signed off") by " + by.joined(separator: " · "),
                                role: .caption)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .accessibilityElement(children: .combine)
    }

    // MARK: One sheet

    /// The title, the count and the progress bar, then "Next: <line> · due
    /// 4pm" (a tap scrolls to it); then each section with its done lines
    /// folded behind "12 done · Show" — done/total stays in the header.
    private func sheetCard(_ s: StaffSheet) -> some View {
        let open = s.status == "open"
        return VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline) {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(s.title)
                            .cavnarText(.label)
                        let meta = meta(s, open: open)
                        if !meta.isEmpty {
                            CavnarMixedText(meta, role: .secondary)
                        }
                    }
                    Spacer(minLength: 8)
                    Text("\(s.done)/\(s.total)")
                        .cavnarText(.figureS, color: s.total > 0 && s.done == s.total ? Color.cavnarGreen : Color.cavnarInk2)
                }
                StaffEmberProgressBar(done: s.done, total: s.total)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("\(s.title). \(s.done) of \(s.total) done"
                                + ((s.overdue ?? 0) > 0 ? ", \(s.overdue ?? 0) overdue" : ""))
            .accessibilityAddTraits(.isHeader)
            .padding(.bottom, 6)
            if open, let next = StaffSheetProgress.next(in: s) {
                nextLine(s, next)
            }
            ForEach(StaffSheetProgress.groups(s.lines)) { group in
                sectionGroup(s, group, open: open)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// "Next: Restock the bar · due 4pm" — a tap scrolls to that line.
    private func nextLine(_ s: StaffSheet, _ l: StaffSheetLine) -> some View {
        let key = StaffSheetMerge.key(s.id, l.lineID)
        let due = l.dueAt.map { StaffSheetFormat.clock($0) }.flatMap { $0.isEmpty ? nil : $0 }
        let late = l.overdue == true
        return Button {
            Haptic.light()
            scrollTo?("line-" + key)
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                (Text("Next: ").font(.cavnar(.label)).foregroundStyle(Color.cavnarInk)
                    + Text(l.label).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarInk)
                    + (due.map {
                        HomeMixedText.make(late ? " \u{00B7} overdue since \($0)" : " \u{00B7} due \($0)",
                                           role: .secondary, color: late ? .cavnarRedText : .cavnarInk2)
                    } ?? Text("")))
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)
                Spacer(minLength: 0)
                Image(systemName: "arrow.down")
                    .font(.cavnar(.caption).weight(.semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint("Scrolls to it")
    }

    /// One section: its heading, its open lines, and its done lines folded
    /// behind "12 done · Show". A done line that still has something to say
    /// (an out-of-range alert, a refusal, a tick waiting to send) stays out.
    @ViewBuilder
    private func sectionGroup(_ s: StaffSheet, _ group: StaffSheetProgress.Group, open: Bool) -> some View {
        let foldKey = "\(s.id)|\(group.index)"
        let showAll = showingDone.contains(foldKey)
        let visible = group.lines.filter { l in
            let key = StaffSheetMerge.key(s.id, l.lineID)
            return showAll || !l.done || store.note(key) != nil || store.overlay(key) != nil
        }
        let folded = group.lines.count - visible.count
        if let sec = group.section {
            Text(sec)
                .cavnarText(.kicker, color: .cavnarInk2)
                .padding(.top, 10).padding(.bottom, 2)
                .accessibilityAddTraits(.isHeader)
        }
        ForEach(visible) { line in
            lineRow(s, line, open: open)
        }
        if folded > 0 || (showAll && group.doneCount > 0) {
            Button {
                Haptic.light()
                withAnimation(.easeOut(duration: 0.22)) {
                    if showAll { showingDone.remove(foldKey) } else { showingDone.insert(foldKey) }
                }
            } label: {
                HStack(spacing: CavnarSpace.xs) {
                    HomeMixedText.make(showAll ? "Hide done" : "\(folded) done \u{00B7} Show",
                                       role: .label, color: .cavnarEmber2)
                    Image(systemName: "chevron.down")
                        .font(.cavnar(.caption))
                        .foregroundStyle(Color.cavnarEmber2)
                        .rotationEffect(.degrees(showAll ? 180 : 0))
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                }
                .frame(minHeight: 44)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
            .accessibilityLabel(showAll ? "Hide done lines" : "\(folded) done. Show them")
        }
    }

    private func meta(_ s: StaffSheet, open: Bool) -> String {
        var bits: [String] = []
        if s.shiftStart != nil {
            bits.append("\(StaffSheetFormat.clock(s.shiftStart))–\(StaffSheetFormat.clock(s.shiftEnd))")
        }
        if !open { bits.append("Closed") }
        if s.unassigned { bits.append("Shared sheet") }
        if s.assignees.count > 1 { bits.append("with " + s.assignees.joined(separator: ", ")) }
        return bits.joined(separator: " · ")
    }

    // MARK: One line

    @ViewBuilder
    private func lineRow(_ s: StaffSheet, _ l: StaffSheetLine, open: Bool) -> some View {
        let key = StaffSheetMerge.key(s.id, l.lineID)
        let overlay = store.overlay(key)
        let busy = store.isBusy(key)
        VStack(alignment: .leading, spacing: 8) {
            Button {
                tap(s, l, open: open)
            } label: {
                HStack(alignment: .top, spacing: 12) {
                    StaffCheckDisc(done: l.done, overdue: l.overdue == true, pending: overlay?.state, size: discSize)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(l.label)
                            .cavnarText(.body, color: l.done ? Color.cavnarInk2 : Color.cavnarInk)
                            .fixedSize(horizontal: false, vertical: true)
                        tags(l)
                        if let sub = subline(l, overlay: overlay) {
                            CavnarMixedText(sub, role: .caption, color: .cavnarInk2)
                        }
                    }
                    .padding(.top, 3)
                    Spacer(minLength: 0)
                }
                .frame(maxWidth: .infinity, minHeight: 44, alignment: .topLeading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!open || busy)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(l.label)
            .accessibilityValue(accessibilityValue(l, overlay: overlay))
            .accessibilityHint(open ? accessibilityHint(l) : "")
            .accessibilityAddTraits(l.done ? .isSelected : [])

            if open && !l.done && (l.proofKind == "number" || l.proofKind == "note") {
                readingField(s, l, key: key, busy: busy)
            }
            if open && !l.done && l.proofKind == "photo" {
                photoControls(s, l, key: key, busy: busy)
            }
            if let token = l.photo, l.done {
                StaffProofThumbnail(token: token, store: store)
                    .padding(.leading, inset)
            }
            if let note = store.note(key) {
                StaffLineNoteView(note: note) {
                    messageAbout = StaffLineMessage.about(sheet: s, line: l,
                                                          reading: l.proofValue ?? overlay?.value)
                }
                .padding(.leading, inset)
            }
        }
        .padding(.vertical, 8)
        .overlay(alignment: .top) { Rectangle().fill(Color.cavnarPaper3).frame(height: 1) }
        .id("line-" + key)
    }

    /// The row's one tap: tick a plain line; ask before un-ticking; go to
    /// the reading, or the camera, for a line that needs proof first.
    private func tap(_ s: StaffSheet, _ l: StaffSheetLine, open: Bool) {
        guard open else { return }
        let key = StaffSheetMerge.key(s.id, l.lineID)
        if l.done {
            untick = LineRef(sheet: s, line: l)
            return
        }
        switch l.proofKind {
        case "number", "note":
            focused = key
        case "photo":
            if store.heldPhotos[key] != nil {
                Task { await store.retryHeldPhoto(key) }
            } else if StaffCameraPicker.isAvailable {
                camera = LineRef(sheet: s, line: l)
            } else {
                library = LineRef(sheet: s, line: l)
            }
        default:
            Haptic.light()
            Task { await store.tick(s, l, done: true) }
        }
    }

    /// A reading or a note, saved with a compact button or Return (UX-17).
    private func readingField(_ s: StaffSheet, _ l: StaffSheetLine, key: String, busy: Bool) -> some View {
        let text = values[key] ?? ""
        let empty = text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let save = {
            // No haptic here: the Save chip gives its own, and Return
            // needs none (one haptic per action, M2).
            guard !empty, !busy else { return }
            let value = text
            Task {
                await store.tick(s, l, done: true, value: value)
                if store.note(key).map(Self.isRefusal) != true { values[key] = nil }
            }
        }
        return HStack(spacing: 8) {
            TextField(l.proofLabel ?? (l.proofKind == "number" ? "Reading" : "Your note"),
                      text: Binding(get: { values[key] ?? "" }, set: { values[key] = $0 }))
                // Numbers and punctuation, not the decimal pad: the pad has
                // no Return key, and Return saves.
                .keyboardType(l.proofKind == "number" ? .numbersAndPunctuation : .default)
                .submitLabel(.done)
                .focused($focused, equals: key)
                .onSubmit(save)
                .font(.cavnar(.body))
                .padding(.horizontal, 12)
                .frame(minHeight: 44)
                .background(Color.cavnarPaper, in: RoundedRectangle(cornerRadius: CavnarRadius.control))
                .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control).strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .accessibilityLabel("\(l.proofLabel ?? (l.proofKind == "number" ? "Reading" : "Note")) for \(l.label)")
            Button(action: save) {
                Text("Save").frame(minWidth: 44, minHeight: 32)
            }
            .buttonStyle(CavnarChipButtonStyle(tone: Color.cavnarPaper3))
            .disabled(empty || busy)
            .opacity(empty || busy ? 0.5 : 1)
            .accessibilityLabel("Save \(l.label)")
        }
        .padding(.leading, inset)
    }

    private static func isRefusal(_ note: StaffTasksStore.LineNote) -> Bool {
        if case .error = note { return true }
        return false
    }

    /// Camera first ("Take photo"), the library second (UX-24).
    @ViewBuilder
    private func photoControls(_ s: StaffSheet, _ l: StaffSheetLine, key: String, busy: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if busy {
                HStack(spacing: 10) {
                    CavnarSkeletonBar(height: 3).frame(width: 80)
                    Text("Sending the photo").cavnarText(.secondary)
                }
                .frame(minHeight: 44)
                .accessibilityElement(children: .combine)
            } else if store.heldPhotos[key] != nil {
                Button {
                    Task { await store.retryHeldPhoto(key) }
                } label: {
                    Label("Send photo", systemImage: "arrow.up.circle")
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: !network.isOnline))
                .disabled(!network.isOnline)
            } else {
                HStack(spacing: 14) {
                    if StaffCameraPicker.isAvailable {
                        Button {
                            camera = LineRef(sheet: s, line: l)
                        } label: {
                            Label("Take photo", systemImage: "camera")
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle())
                    }
                    Button {
                        library = LineRef(sheet: s, line: l)
                    } label: {
                        Text(StaffCameraPicker.isAvailable ? "Choose from library" : "Choose a photo")
                            .cavnarText(.label, color: .cavnarEmber2)
                            .frame(minHeight: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.leading, inset)
    }

    @ViewBuilder
    private func tags(_ l: StaffSheetLine) -> some View {
        let items: [(String, Color)] = [
            l.critical == true ? ("Critical", Color.cavnarEmber2) : nil,
            l.overdue == true ? ("Overdue", Color.cavnarRedText) : nil,
            l.late == true ? ("Late", Color.cavnarAmber) : nil,
            l.flagged == true ? ("Out of range", Color.cavnarRedText) : nil,
        ].compactMap { $0 }
        if !items.isEmpty {
            HStack(spacing: 6) {
                ForEach(items, id: \.0) { t in
                    Text(t.0)
                        .cavnarText(.tag, color: t.1)
                        .padding(.horizontal, 7).padding(.vertical, 3)
                        .overlay(Capsule().stroke(t.1, lineWidth: 1))
                }
            }
        }
    }

    private func subline(_ l: StaffSheetLine, overlay: StaffLineOverlay?) -> String? {
        if overlay?.state == .queued { return l.done ? "Ticked here · not sent yet" : "Un-ticked here · not sent yet" }
        if overlay?.state == .sending { return l.done ? "Saving" : nil }
        if l.done {
            var s = [l.completedBy, StaffSheetFormat.clock(l.completedAt)].compactMap { $0 }.filter { !$0.isEmpty }
                .joined(separator: " · ")
            if let v = l.proofValue { s += (s.isEmpty ? "" : " · ") + "\(l.proofLabel.map { $0 + ": " } ?? "")\(v)" }
            return s.isEmpty ? nil : s
        }
        if let due = l.dueAt { return "Due \(StaffSheetFormat.clock(due))" }
        return nil
    }

    // MARK: VoiceOver (UX-18)

    private func accessibilityValue(_ l: StaffSheetLine, overlay: StaffLineOverlay?) -> String {
        var parts: [String] = [l.done ? "Done" : "Not done"]
        if overlay?.state == .queued { parts.append("waiting to send") }
        if l.critical == true { parts.append("critical") }
        if l.overdue == true { parts.append("overdue") }
        if l.flagged == true { parts.append("out of range") }
        if l.late == true { parts.append("late") }
        if let sub = subline(l, overlay: overlay), overlay == nil { parts.append(sub) }
        return parts.joined(separator: ", ")
    }

    private func accessibilityHint(_ l: StaffSheetLine) -> String {
        if l.done { return "Double-tap to mark it not done." }
        switch l.proofKind {
        case "number": return "Needs a reading. Double-tap to enter it."
        case "note": return "Needs a note. Double-tap to write it."
        case "photo": return "Needs a photo. Double-tap to take one."
        default: return "Double-tap to mark it done."
        }
    }

    // MARK: The floor (managers)

    @ViewBuilder
    private func floorSection(_ payload: StaffTasksPayload) -> some View {
        CavnarKicker("The floor \u{00B7} today")
            .padding(.top, 10)
        if payload.floor.isEmpty {
            Text("No other sheets went out today.").cavnarText(.body)
        }
        ForEach(payload.floor) { s in
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(s.title).cavnarText(.label)
                    Text(s.unassigned ? "Unassigned" : s.assignees.joined(separator: ", "))
                        .cavnarText(.caption, color: .cavnarInk2)
                }
                Spacer()
                if let o = s.overdue, o > 0 {
                    CavnarMixedText("\(o) overdue", role: .caption, color: .cavnarRedText)
                }
                Text("\(s.done)/\(s.total)").cavnarText(.figureS, color: .cavnarInk2)
            }
            .cavnarCard()
            .accessibilityElement(children: .combine)
        }
        let signed = Dictionary(payload.signoffs.map { ($0.shiftKind, $0) }, uniquingKeysWith: { a, _ in a })
        ForEach(payload.canSignOff, id: \.self) { kind in
            if let s = signed[kind] {
                Text("\(StaffSheetFormat.kind(kind)) signed off by \(s.signedBy ?? "a manager").")
                    .cavnarText(.body)
            } else {
                // Secondary: the sheets' ticks are the work here, and one
                // screen spends one primary (DESIGN_SYSTEM §5, UX-17).
                Button("Sign off the \(StaffSheetFormat.kind(kind).lowercased()) shift") {
                    signoffNote = ""
                    signingOff = kind
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: store.isBusy("signoff-" + kind)))
                .disabled(store.isBusy("signoff-" + kind))
            }
        }
    }
}
