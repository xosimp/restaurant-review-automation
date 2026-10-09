import SwiftUI
import Observation

/// What Cavnar AI remembers about the team, with the owner's say over it
/// (memory round, 9/29/26 — the iOS twin of the web's Labor additions):
///
/// - **Scheduling notes**, each constraint dated ("noted 9/2/26 · ends
///   10/1/26"), ended ones greyed, and "Still true?" on one over
///   `stale_after_days` (GET/POST /mobile/api/labor/staff-notes). Each
///   answer saves on its own — never a whole-note overwrite.
/// - **Same person?** — two records that may be one person; only the owner
///   answers, and nothing is merged on a guess (GET /people/identity,
///   POST /people/identity/<id> {same}).
/// - **Guests naming your team** — a review naming someone on staff counts
///   on their record only once confirmed (GET /people/mentions,
///   POST /people/mentions/<id> {confirm}).
@Observable
@MainActor
final class TeamMemoryViewModel {
    private(set) var notes: [StaffNote] = []
    private(set) var staleCount = 0
    private(set) var staleAfterDays: Int?
    private(set) var canEditNotes = false
    private(set) var questions: [IdentityQuestion] = []
    private(set) var canAnswer = false
    private(set) var mentions: [GuestMention] = []
    private(set) var canConfirm = false
    private(set) var isLoading = false
    private(set) var hasLoaded = false
    /// The row being saved ("note:12:0", "q:4", "m:9").
    private(set) var busyKey: String?
    private(set) var errorMessage: String?
    /// What the last answer did, in the server's terms.
    private(set) var message: String?
    var isExpanded = false

    // The add-a-note form.
    var newName = ""
    var newConstraint = ""
    /// M/D/YY or blank; the server checks it and says so in words.
    var newEnds = ""

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    private typealias OKResponse = APIClient.OKResponse

    /// Everything the owner is asked to answer here — the nudge on the
    /// Labor page and the dropdown's badge.
    var attentionCount: Int { staleCount + questions.count + mentions.count }

    /// "2 scheduling notes over 90 days old — still true? · 1 person may be
    /// listed twice · 3 guests named your team" — nil when nothing waits.
    var nudgeLine: String? { Self.nudgeLine(stale: staleCount, days: staleAfterDays, questions: questions.count,
                                            mentions: mentions.count) }

    nonisolated static func nudgeLine(stale: Int, days: Int?, questions: Int, mentions: Int) -> String? {
        var bits: [String] = []
        if stale > 0 {
            bits.append("\(stale) scheduling note\(stale == 1 ? "" : "s") over \(days ?? 90) days old \u{2014} still true?")
        }
        if questions > 0 {
            bits.append(questions == 1 ? "1 person may be listed twice" : "\(questions) people may be listed twice")
        }
        if mentions > 0 {
            bits.append("\(mentions) guest mention\(mentions == 1 ? "" : "s") of your team to confirm")
        }
        return bits.isEmpty ? nil : bits.joined(separator: " \u{00B7} ")
    }

    func load() async {
        isLoading = !hasLoaded
        defer { isLoading = false; hasLoaded = true }
        async let n: StaffNotesPayload? = try? client.send("/mobile/api/labor/staff-notes", hapticOnError: false)
        async let q: IdentityPayload? = try? client.send("/mobile/api/people/identity", hapticOnError: false)
        async let m: MentionsPayload? = try? client.send("/mobile/api/people/mentions", hapticOnError: false)
        let (notesPayload, identity, mentionPayload) = await (n, q, m)
        if let notesPayload, notesPayload.ok {
            notes = notesPayload.notes
            staleCount = notesPayload.stale
            staleAfterDays = notesPayload.staleAfterDays
            canEditNotes = notesPayload.canEdit
        }
        if let identity, identity.ok {
            questions = identity.questions
            canAnswer = identity.canAnswer
        }
        if let mentionPayload, mentionPayload.ok {
            mentions = mentionPayload.mentions
            canConfirm = mentionPayload.canConfirm
        }
    }

    // MARK: Scheduling notes

    private struct NoteAnswerBody: Encodable {
        let action: String
        let part: Int?
        let expiresOn: String?
        enum CodingKeys: String, CodingKey {
            case action, part
            case expiresOn = "expires_on"
        }
    }

    private struct NoteResponse: Decodable {
        let ok: Bool
        let error: String?
        let note: StaffNote?
        let removed: Bool?
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            error = try? c.decodeIfPresent(String.self, forKey: .error)
            note = try? c.decodeIfPresent(StaffNote.self, forKey: .note)
            removed = try? c.decodeIfPresent(Bool.self, forKey: .removed)
        }
        enum CodingKeys: String, CodingKey { case ok, error, note, removed }
    }

    /// Today as the ISO date the server reads for "it ended today".
    nonisolated static func isoToday(_ now: Date = Date()) -> String {
        let c = Calendar(identifier: .gregorian).dateComponents([.year, .month, .day], from: now)
        return String(format: "%04d-%02d-%02d", c.year ?? 2000, c.month ?? 1, c.day ?? 1)
    }

    /// "Still true" (confirm), "It's over" (ends today) or "Remove" — one
    /// constraint at a time, saved at once.
    // MARK: Remove / "It's over", with Undo (re-audit 10/8/26 M9)

    /// A Remove or "It's over" held for the Undo window: the part leaves
    /// the list at once and the request that makes it final is sent only if
    /// nobody taps Undo in 7 seconds — the server has no inverse
    /// (DESIGN_SYSTEM §10 tier 1, `cavUndoable`). Leaving the screen inside
    /// the window keeps the note (the safe side).
    struct PendingNoteAnswer: Equatable {
        let note: StaffNote
        let part: StaffNotePart
        let action: String
        var key: String { "note:\(note.id):\(part.index)" }
        var line: String {
            action == "expire" ? "Ended \(note.employeeName)\u{2019}s note" : "Removed \(note.employeeName)\u{2019}s note"
        }
    }
    private(set) var pendingNote: PendingNoteAnswer?
    private var pendingTask: Task<Void, Never>?

    /// Hidden while its Remove / It's over waits out the Undo window.
    func isPending(_ note: StaffNote, _ part: StaffNotePart) -> Bool {
        pendingNote?.key == "note:\(note.id):\(part.index)"
    }

    func answerWithUndo(_ note: StaffNote, part: StaffNotePart, action: String) {
        // A second one commits the first at once.
        if let earlier = pendingNote {
            pendingTask?.cancel()
            Task { await answer(earlier.note, part: earlier.part, action: earlier.action) }
        }
        let pending = PendingNoteAnswer(note: note, part: part, action: action)
        pendingNote = pending
        message = nil
        pendingTask = Task { @MainActor [weak self] in
            try? await Task.sleep(for: .seconds(7))
            guard !Task.isCancelled, let self, self.pendingNote == pending else { return }
            self.pendingNote = nil
            await self.answer(pending.note, part: pending.part, action: pending.action)
        }
    }

    func undoPendingNote() {
        pendingTask?.cancel()
        pendingTask = nil
        pendingNote = nil
    }

    func answer(_ note: StaffNote, part: StaffNotePart, action: String) async {
        busyKey = "note:\(note.id):\(part.index)"
        errorMessage = nil
        message = nil
        defer { busyKey = nil }
        let body = NoteAnswerBody(action: action, part: part.index,
                                  expiresOn: action == "expire" ? Self.isoToday() : nil)
        do {
            let r: NoteResponse = try await client.send("/mobile/api/labor/staff-notes/\(note.id)",
                                                        method: .post, body: body, retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            if let i = notes.firstIndex(where: { $0.id == note.id }) {
                if let updated = r.note { notes[i] = updated } else if r.removed == true { notes.remove(at: i) }
            }
            staleCount = notes.reduce(0) { $0 + $1.staleParts.count }
            message = action == "confirm" ? "Kept \u{2014} dated today." :
                (action == "expire" ? "Ended today \u{2014} the schedule stops using it." : "Removed.")
            Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }

    // MARK: Hold a note as a constraint (iOS parity #68)

    private struct HoldResponse: Decodable { let ok: Bool; let error: String? }

    /// Hold one note part the way availability holds: the person can't be
    /// scheduled on the days and at the dayparts confirmed (POST
    /// /labor/staff-note-holds — the reading's own dates ride along).
    func hold(_ note: StaffNote, part: StaffNotePart, days: [String], dayparts: [String]) async {
        let key = "hold:\(note.id):\(part.index)"
        busyKey = key
        errorMessage = nil
        defer { busyKey = nil }
        let reading = part.reading?.hold
        let body = StaffNoteHoldBody(employeeName: note.employeeName, partText: part.text,
                                     days: days, dayparts: dayparts, start: reading?.start, end: reading?.end)
        do {
            let r: HoldResponse = try await client.send("/mobile/api/labor/staff-note-holds", method: .post, body: body)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t hold that note."; return }
            message = "Held \u{2014} the draft can\u{2019}t schedule them then"
            Haptic.success()
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t hold that note."
        }
    }

    func unhold(_ held: StaffNoteHeld) async {
        busyKey = "unhold:\(held.id)"
        errorMessage = nil
        defer { busyKey = nil }
        do {
            let r: HoldResponse = try await client.send("/mobile/api/labor/staff-note-holds/\(held.id)/remove",
                                                        method: .post, body: [String: String]())
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t stop holding it."; return }
            message = "No longer held \u{2014} the draft only aims for it"
            Haptic.success()
            await load()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t stop holding it."
        }
    }

    private struct NoteAddBody: Encodable {
        let employeeName: String
        let notes: String
        let expiresOn: String?
        enum CodingKeys: String, CodingKey {
            case notes
            case employeeName = "employee_name"
            case expiresOn = "expires_on"
        }
    }

    var canAddNote: Bool {
        !newName.trimmingCharacters(in: .whitespaces).isEmpty
            && !newConstraint.trimmingCharacters(in: .whitespaces).isEmpty
    }

    /// Adds one constraint for one person, dated today; the form clears
    /// only once the server has it.
    func addNote() async {
        guard canAddNote else { return }
        busyKey = "note:add"
        errorMessage = nil
        message = nil
        defer { busyKey = nil }
        let ends = newEnds.trimmingCharacters(in: .whitespaces)
        let body = NoteAddBody(employeeName: newName.trimmingCharacters(in: .whitespaces),
                               notes: newConstraint.trimmingCharacters(in: .whitespacesAndNewlines),
                               expiresOn: ends.isEmpty ? nil : ends)
        do {
            let r: NoteResponse = try await client.send("/mobile/api/labor/staff-notes", method: .post, body: body,
                                                        retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t add that."; return }
            if let note = r.note {
                if let i = notes.firstIndex(where: { $0.id == note.id }) { notes[i] = note } else { notes.append(note) }
            } else {
                await load()
            }
            newConstraint = ""
            newEnds = ""
            message = "Added \u{2014} the next draft reads it."
            Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t add that."
        }
    }

    // MARK: Same person?

    private struct IdentityBody: Encodable { let same: Bool }
    private struct IdentityResponse: Decodable {
        let ok: Bool
        let error: String?
        let status: String?
        let into: String?
    }

    func answer(_ question: IdentityQuestion, same: Bool) async {
        busyKey = "q:\(question.id)"
        errorMessage = nil
        message = nil
        defer { busyKey = nil }
        do {
            let r: IdentityResponse = try await client.send("/mobile/api/people/identity/\(question.id)",
                                                            method: .post, body: IdentityBody(same: same),
                                                            retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            questions.removeAll { $0.id == question.id }
            message = same
                ? "Merged \u{2014} every rating, setting, note and shift now sits under \(r.into ?? question.b.name)."
                : "Kept apart \u{2014} Cavnar AI won\u{2019}t ask about these two again."
            Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }

    // MARK: Guests naming the team

    private struct MentionBody: Encodable { let confirm: Bool }

    func answer(_ mention: GuestMention, confirm: Bool) async {
        busyKey = "m:\(mention.id)"
        errorMessage = nil
        message = nil
        defer { busyKey = nil }
        do {
            let r: OKResponse = try await client.send("/mobile/api/people/mentions/\(mention.id)", method: .post,
                                                      body: MentionBody(confirm: confirm), retryTransient: false)
            guard r.ok else { errorMessage = r.error ?? "Couldn\u{2019}t save that."; return }
            mentions.removeAll { $0.id == mention.id }
            message = confirm ? "On \(mention.name)\u{2019}s record." : "Left off \(mention.name)\u{2019}s record."
            Haptic.success()
        } catch let error as APIClient.APIError {
            errorMessage = error.message
        } catch {
            errorMessage = "Couldn\u{2019}t save that."
        }
    }
}

// MARK: - The section (Scheduling setup)

struct TeamMemorySection: View {
    @Bindable var viewModel: TeamMemoryViewModel
    /// The roster's names, offered for a new note.
    var names: [String] = []
    var onExpand: (() -> Void)? = nil
    @State private var person: PersonSheetTarget?
    /// "Same person" merges two records: asked first (M9).
    @State private var confirmingSame: IdentityQuestion?

    var body: some View {
        CavnarDropdown(
            title: "Notes & who\u{2019}s who",
            subtitle: subtitle,
            badge: viewModel.attentionCount > 0 ? viewModel.attentionCount : nil,
            tone: viewModel.attentionCount > 0 ? .warning : .neutral,
            isExpanded: $viewModel.isExpanded,
            onExpand: {
                onExpand?()
                Task { await viewModel.load() }
            }
        ) {
            VStack(alignment: .leading, spacing: 18) {
                if viewModel.isLoading {
                    CavnarSkeletonLines(widths: [1.0, 0.8, 0.6])
                } else {
                    if !viewModel.questions.isEmpty { identityBlock }
                    notesBlock
                    if !viewModel.mentions.isEmpty { mentionsBlock }
                }
                if let pending = viewModel.pendingNote {
                    HStack(spacing: CavnarSpace.s) {
                        Text(pending.line)
                            .cavnarText(.label)
                            .fixedSize(horizontal: false, vertical: true)
                        Spacer(minLength: 0)
                        Button {
                            Haptic.light()
                            viewModel.undoPendingNote()
                        } label: {
                            Text("Undo").cavnarText(.label, color: .cavnarEmber2).cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, CavnarSpace.s)
                    .background(Capsule().fill(Color.cavnarPaper2))
                    .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                } else if let message = viewModel.message {
                    Text(message).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarGreen)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if let error = viewModel.errorMessage {
                    Text(error).font(.cavnar(.secondary)).foregroundStyle(Color.cavnarRedText)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .sheet(item: $person) { target in PersonSheet(target: target) }
        // Leaving inside the Undo window keeps the note (the safe side).
        .onDisappear { viewModel.undoPendingNote() }
        .confirmationDialog(confirmingSame.map { "Merge \($0.a.name) and \($0.b.name)?" } ?? "",
                            isPresented: Binding(get: { confirmingSame != nil },
                                                 set: { if !$0 { confirmingSame = nil } }),
                            titleVisibility: .visible) {
            Button("Same person \u{2014} merge them") {
                guard let q = confirmingSame else { return }
                confirmingSame = nil
                Task { await viewModel.answer(q, same: true) }
            }
            Button("Cancel", role: .cancel) { confirmingSame = nil }
        } message: {
            Text("Every rating, setting, note and shift moves under one name.")
        }
    }

    private var subtitle: String {
        if let line = viewModel.nudgeLine { return line }
        let n = viewModel.notes.count
        return n == 0 ? "Scheduling notes, and who is who" : "\(n) \(n == 1 ? "person has" : "people have") scheduling notes"
    }

    private func blockTitle(_ title: String, _ help: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk)
            Text(help)
                .font(.cavnar(.caption))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// A button's label, or the shimmer while its save is in flight —
    /// never a spinner, never "…" on its own.
    @ViewBuilder
    private func busyLabel(_ busy: Bool, _ text: String, busyText: String = "Saving\u{2026}") -> some View {
        if busy { CavnarShimmerText(text: busyText) } else { Text(text) }
    }

    private func divider() -> some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }

    // MARK: Scheduling notes

    private var notesBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            blockTitle("Scheduling notes",
                       "What the schedule and the labor read obey, each dated. A note over "
                       + "\(viewModel.staleAfterDays ?? 90) days old asks whether it\u{2019}s still true.")
            if viewModel.notes.isEmpty {
                Text("No scheduling notes yet \u{2014} add one below (\u{201C}out until 10/15/26\u{201D}, \u{201C}no doubles\u{201D}).")
                    .font(.cavnar(.secondary))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                VStack(spacing: 0) {
                    ForEach(viewModel.notes) { note in
                        noteRow(note)
                        if note.id != viewModel.notes.last?.id { divider() }
                    }
                }
            }
            if viewModel.canEditNotes { addNoteForm }
        }
    }

    private func noteRow(_ note: StaffNote) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                Haptic.light()
                person = PersonSheetTarget(key: nil, name: note.employeeName)
            } label: {
                Text(note.employeeName)
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens \(note.employeeName)\u{2019}s record")
            ForEach(note.parts.filter { !viewModel.isPending(note, $0) }) { part in partRow(note, part) }
        }
        .padding(.vertical, 10)
    }

    private func partRow(_ note: StaffNote, _ part: StaffNotePart) -> some View {
        let busy = viewModel.busyKey == "note:\(note.id):\(part.index)"
        let asking = part.stale && !part.ended
        return VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Circle()
                    .fill(part.ended ? Color.cavnarInk3 : (asking ? Color.cavnarAmber : Color.cavnarGreen))
                    .frame(width: 6, height: 6)
                HomeMixedText.make(part.text, size: CavnarType.secondary, color: part.ended ? .cavnarInk3 : .cavnarInk2)
                    .strikethrough(part.ended, color: Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let line = part.dateLine {
                HomeMixedText.make(line, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                    .padding(.leading, 14)
            }
            // Held as a constraint, or offered to be (#68).
            StaffNoteHoldRow(viewModel: viewModel, note: note, part: part)
            if asking {
                HStack(spacing: 8) {
                    Text("Still true?")
                        .font(.cavnarBody(CavnarType.caption, weight: 700))
                        .foregroundStyle(Color.cavnarAmber)
                    Spacer(minLength: 4)
                    if viewModel.canEditNotes {
                        Button {
                            Haptic.light()
                            Task { await viewModel.answer(note, part: part, action: "confirm") }
                        } label: { busyLabel(busy, "Still true") }
                            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                            .disabled(busy)
                        Button {
                            Haptic.light()
                            viewModel.answerWithUndo(note, part: part, action: "expire")
                        } label: {
                            Text("It\u{2019}s over")
                                .font(.cavnarBody(CavnarType.secondary, weight: 600))
                                .foregroundStyle(Color.cavnarInk2)
                                .cavnarHitTarget()
                        }
                        .buttonStyle(.plain)
                        .disabled(busy)
                    }
                }
                .padding(.leading, 14)
            } else if viewModel.canEditNotes && !part.ended {
                Button {
                    Haptic.light()
                    viewModel.answerWithUndo(note, part: part, action: "remove")
                } label: {
                    Text("Remove")
                        .font(.cavnarBody(CavnarType.caption, weight: 600))
                        .foregroundStyle(Color.cavnarInk2)
                        .cavnarHitTarget()
                }
                .buttonStyle(.plain)
                .disabled(busy)
                .padding(.leading, 14)
            }
        }
    }

    private var addNoteForm: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Add a note")
                .font(.cavnarBody(CavnarType.secondary, weight: 700))
                .foregroundStyle(Color.cavnarInk2)
            if !names.isEmpty {
                Menu {
                    ForEach(names, id: \.self) { name in
                        Button(name) { viewModel.newName = name }
                    }
                } label: {
                    HStack {
                        Text(viewModel.newName.isEmpty ? "Who" : viewModel.newName)
                            .font(.cavnar(.body))
                            .foregroundStyle(viewModel.newName.isEmpty ? Color.cavnarInk3 : Color.cavnarInk)
                        Spacer()
                        Image(systemName: "chevron.down")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .padding(.horizontal, 12)
                    .frame(height: 44)
                    .background(Color.cavnarPaper)
                    .clipShape(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous))
                }
            } else {
                TextField("Who", text: $viewModel.newName)
                    .cavnarTextFieldStyle()
                    .textInputAutocapitalization(.words)
                    .autocorrectionDisabled()
            }
            TextField("The constraint \u{2014} \u{201C}no closes on school nights\u{201D}", text: $viewModel.newConstraint,
                      axis: .vertical)
                .cavnarTextFieldStyle()
                .lineLimit(1...3)
            TextField("Ends on (M/D/YY, optional)", text: $viewModel.newEnds)
                .cavnarTextFieldStyle()
                .keyboardType(.numbersAndPunctuation)
                .autocorrectionDisabled()
            Button {
                Haptic.light()
                Task { await viewModel.addNote() }
            } label: {
                busyLabel(viewModel.busyKey == "note:add", "Add note", busyText: "Adding\u{2026}")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: !viewModel.canAddNote || viewModel.busyKey != nil))
            .disabled(!viewModel.canAddNote || viewModel.busyKey != nil)
        }
        .padding(.top, 4)
    }

    // MARK: Same person?

    private var identityBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            blockTitle("Same person?",
                       "Two records that may be one person. A rating under one name judges nobody while the "
                       + "roster says the other \u{2014} nothing is merged until you say so.")
            ForEach(viewModel.questions) { q in identityRow(q) }
        }
    }

    private func identityRow(_ q: IdentityQuestion) -> some View {
        let busy = viewModel.busyKey == "q:\(q.id)"
        return VStack(alignment: .leading, spacing: 6) {
            Text(q.question)
                .font(.cavnar(.label))
                .foregroundStyle(Color.cavnarInk)
                .fixedSize(horizontal: false, vertical: true)
            if let reason = q.reason {
                HomeMixedText.make(reason, size: CavnarType.caption, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach([q.a, q.b], id: \.self) { side in
                if let detail = side.detail {
                    HomeMixedText.make("\(side.name): \(detail)", size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            if let asked = q.asked {
                HomeMixedText.make("Asked " + asked, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
            }
            if viewModel.canAnswer {
                HStack(spacing: 8) {
                    Button {
                        Haptic.light()
                        confirmingSame = q
                    } label: { busyLabel(busy, "Same person").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                    Button {
                        Haptic.light()
                        Task { await viewModel.answer(q, same: false) }
                    } label: { Text("Different people").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                }
            } else {
                Text("The account owner answers this one.")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarAmber.opacity(0.06)))
    }

    // MARK: Guests naming the team

    private var mentionsBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            blockTitle("Guests naming your team",
                       "A review that names someone on staff counts on their record only once you confirm it\u{2019}s them.")
            VStack(spacing: 0) {
                ForEach(viewModel.mentions) { m in
                    mentionRow(m)
                    if m.id != viewModel.mentions.last?.id { divider() }
                }
            }
        }
    }

    private func mentionRow(_ m: GuestMention) -> some View {
        let busy = viewModel.busyKey == "m:\(m.id)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text(m.name)
                    .font(.cavnar(.label))
                    .foregroundStyle(Color.cavnarInk)
                if m.isPraise { AccountChip(text: "Praise", tint: .cavnarGreen) }
                if m.isComplaint { AccountChip(text: "Complaint", tint: .cavnarRed) }
                Spacer(minLength: 4)
                if let d = m.dateLabel {
                    Text(d).font(.cavnarNumber(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
                }
            }
            if let snippet = m.snippet {
                HomeMixedText.make("\u{201C}\(snippet)\u{201D}", size: CavnarType.secondary, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if viewModel.canConfirm {
                HStack(spacing: 8) {
                    Button {
                        Haptic.light()
                        Task { await viewModel.answer(m, confirm: true) }
                    } label: { busyLabel(busy, "It\u{2019}s them").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                    Button {
                        Haptic.light()
                        Task { await viewModel.answer(m, confirm: false) }
                    } label: { Text("Not them").frame(maxWidth: .infinity) }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: busy))
                        .disabled(busy)
                }
            }
        }
        .padding(.vertical, 10)
    }
}

// MARK: - The nudge (Labor → Needs you)

/// One line under Waiting on you when the team memory has something for
/// the owner — the action queue's `staff_note:stale` and `people:identity`
/// items land on Labor, and this is where they are answered from. Hidden
/// when nothing waits.
struct TeamMemoryNudge: View {
    let viewModel: TeamMemoryViewModel
    let onOpen: () -> Void

    var body: some View {
        if let line = viewModel.nudgeLine {
            Button {
                Haptic.light()
                viewModel.isExpanded = true
                onOpen()
            } label: {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "person.text.rectangle")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(Color.cavnarAmber)
                        .frame(width: 28)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Your team\u{2019}s notes")
                            .font(.cavnarBody(CavnarType.body, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(line, size: CavnarType.caption, weight: 500, color: .cavnarInk3)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 8)
                    Text("Review")
                        .font(.cavnarBody(CavnarType.secondary, weight: 700))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .cavnarCard()
            .accessibilityHint("Opens your team\u{2019}s notes in the scheduling setup")
        }
    }
}

// MARK: - Rating history (Operational Score)

/// Who changed a rating, a target, a profile or the weighting, and when —
/// the first question after a disputed schedule (GET
/// /mobile/api/labor/capability-changes; memory round, 9/29/26). Loaded
/// when the Operational Score section opens; folded to the newest few.
struct CapabilityHistoryBlock: View {
    @State private var changes: [CapabilityChange] = []
    @State private var loaded = false
    @State private var showingAll = false

    private struct Response: Decodable {
        let ok: Bool
        let changes: [CapabilityChange]
        enum CodingKeys: String, CodingKey { case ok, changes }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            ok = (try? c.decode(Bool.self, forKey: .ok)) ?? false
            // Entry by entry: one odd row never hides the rest.
            var out: [CapabilityChange] = []
            if var list = try? c.nestedUnkeyedContainer(forKey: .changes) {
                while !list.isAtEnd {
                    let before = list.currentIndex
                    if let e = try? list.decode(CapabilityChange.self) { out.append(e) } else { _ = try? list.decode(JSONValue.self) }
                    if list.currentIndex == before { break }
                }
            }
            changes = out
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if loaded && !changes.isEmpty {
                Text("Rating history")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                let shown = showingAll ? changes : Array(changes.prefix(5))
                ForEach(shown) { change in
                    HomeMixedText.make(change.line, size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if changes.count > 5 {
                    Button {
                        Haptic.selection()
                        withAnimation(.easeOut(duration: 0.2)) { showingAll.toggle() }
                    } label: {
                        Text(showingAll ? "Show fewer" : "Show all \(changes.count)")
                            .font(.cavnarBody(CavnarType.caption, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .task {
            guard !loaded else { return }
            let r: Response? = try? await APIClient.shared.send("/mobile/api/labor/capability-changes",
                                                                hapticOnError: false)
            changes = r?.changes ?? []
            loaded = true
        }
    }
}

// MARK: - "Did Zed take it?" (a coverage issue's cover)

/// The manager's word on a cover ask — they took it, or they didn't
/// (POST /mobile/api/issues/<id>/cover-answer {name, accepted}). It stands
/// over what the nightly job infers from the punches, and their record of
/// taking covers ranks the next suggestions. The answer lives on the issue
/// (its `asked[].answer`, decoded by HomeDay's Issue.Person), so once anyone
/// answers — here, on another device or on the web — the row is not shown
/// again and the server refuses a different later answer (memory re-audit
/// 9/29/26, INVENTORY-10). Nothing is kept on
/// the device: the old UserDefaults copy held a staff name unencrypted, and
/// is cleared the first time the row appears.
struct CoverAnswerRow: View {
    let issueId: Int
    let name: String
    @State private var answer: Bool?
    @State private var busy = false
    @State private var error: String?

    private struct AnswerBody: Encodable { let name: String; let accepted: Bool }

    /// The key an earlier build stored the answer under — read only to
    /// remove it.
    static func legacyStorageKey(issue: Int, name: String) -> String {
        "coverAnswer.\(issue).\(name.lowercased())"
    }

    /// The first name, as the question asks it.
    private var first: String { name.split(separator: " ").first.map(String.init) ?? name }

    var body: some View {
        Group {
            if let answer {
                Text(answer ? "\(first) took it \u{2014} it counts on their record."
                            : "\(first) didn\u{2019}t take it \u{2014} noted.")
                    .font(.cavnarBody(CavnarType.caption, weight: 600))
                    .foregroundStyle(answer ? Color.cavnarGreen : Color.cavnarInk3)
            } else {
                HStack(spacing: 12) {
                    Text("Did \(first) take it?")
                        .font(.cavnarBody(CavnarType.caption, weight: 700))
                        .foregroundStyle(Color.cavnarInk2)
                    Button { send(true) } label: {
                        Text("Yes").font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarEmber2)
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                    Button { send(false) } label: {
                        Text("No").font(.cavnarBody(CavnarType.caption, weight: 700)).foregroundStyle(Color.cavnarInk3)
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                }
            }
            if let error {
                Text(error).font(.cavnar(.caption)).foregroundStyle(Color.cavnarRedText)
            }
        }
        .onAppear {
            UserDefaults.standard.removeObject(forKey: Self.legacyStorageKey(issue: issueId, name: name))
        }
    }

    private func send(_ accepted: Bool) {
        Haptic.light()
        busy = true
        error = nil
        Task {
            defer { busy = false }
            do {
                let r: APIClient.OKResponse = try await APIClient.shared.send(
                    "/mobile/api/issues/\(issueId)/cover-answer", method: .post,
                    body: AnswerBody(name: name, accepted: accepted), retryTransient: false)
                if r.ok {
                    answer = accepted
                    Haptic.success()
                } else {
                    error = r.error ?? "Couldn\u{2019}t save that."
                }
            } catch let e as APIClient.APIError {
                error = e.message
            } catch {
                self.error = "Couldn\u{2019}t save that."
            }
        }
    }
}


/// "Cavnar AI can hold this" with day and daypart chips, or "Held — …" with
/// Stop holding (iOS parity #68; the web's mem-hold row). The chips start
/// as the reading has them; the owner can narrow or widen before Hold it.
private struct StaffNoteHoldRow: View {
    let viewModel: TeamMemoryViewModel
    let note: StaffNote
    let part: StaffNotePart
    @State private var days: Set<String> = []
    @State private var parts: Set<String> = []
    @State private var seeded = false
    @State private var stopping = false

    private static let week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

    var body: some View {
        Group {
            if let held = part.held {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "lock.fill").font(.system(size: 10, weight: .bold)).foregroundStyle(Color.cavnarGreen)
                        HomeMixedText.make("Held \u{2014} \(held.words ?? "") The draft can\u{2019}t schedule them then.",
                                           size: CavnarType.caption, weight: 600, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if viewModel.canEditNotes {
                        Button {
                            stopping = true
                        } label: {
                            Text("Stop holding").font(.cavnarBody(CavnarType.caption, weight: 600)).foregroundStyle(Color.cavnarInk3)
                                .frame(minHeight: 32)
                        }
                        .buttonStyle(.plain)
                        .disabled(viewModel.busyKey != nil)
                    }
                }
                .padding(.leading, 14)
                .confirmationDialog("Stop holding this note?", isPresented: $stopping, titleVisibility: .visible) {
                    Button("Stop holding", role: .destructive) { Task { await viewModel.unhold(held) } }
                    Button("Cancel", role: .cancel) {}
                } message: { Text("The note stays; the draft only aims for it again.") }
            } else if !part.ended, part.reading?.kind == "hold", let hold = part.reading?.hold {
                VStack(alignment: .leading, spacing: 7) {
                    HomeMixedText.make("Cavnar AI can hold this \u{2014} \(hold.words ?? ""). Until you do, the draft only aims for it.",
                                       size: CavnarType.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    if viewModel.canEditNotes {
                        AccountFlowLayout(spacing: 5) {
                            ForEach(Self.week, id: \.self) { d in
                                chip(String(d.prefix(3)), on: days.contains(d)) { toggle(&days, d) }
                            }
                            chip("Lunch", on: parts.contains("morning")) { toggle(&parts, "morning") }
                            chip("Dinner", on: parts.contains("night")) { toggle(&parts, "night") }
                        }
                        Button {
                            Haptic.light()
                            Task {
                                await viewModel.hold(note, part: part,
                                                     days: Self.week.filter { days.contains($0) },
                                                     dayparts: ["morning", "night"].filter { parts.contains($0) })
                            }
                        } label: {
                            Group {
                                if viewModel.busyKey == "hold:\(note.id):\(part.index)" { CavnarShimmerText(text: "Saving\u{2026}") }
                                else { Text("Hold it") }
                            }
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: viewModel.busyKey != nil || !canHold(hold)))
                        .disabled(viewModel.busyKey != nil || !canHold(hold))
                    }
                }
                .padding(10)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.cavnarEmber.opacity(0.07)))
                .padding(.leading, 14)
                .onAppear {
                    guard !seeded else { return }
                    seeded = true
                    days = Set(hold.days ?? [])
                    parts = Set(hold.dayparts ?? [])
                }
            } else if !part.ended, part.reading?.kind == "unchecked", let why = part.reading?.why, !why.isEmpty {
                HomeMixedText.make("Not held. \(why)", size: CavnarType.caption, color: .cavnarAmber)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 14)
            }
        }
    }

    /// A hold needs a day, a daypart or the reading's own dates.
    private func canHold(_ hold: StaffNoteReading.Hold) -> Bool {
        !days.isEmpty || !parts.isEmpty || hold.start != nil || hold.end != nil
    }

    private func toggle(_ set: inout Set<String>, _ v: String) {
        Haptic.selection()
        if set.contains(v) { set.remove(v) } else { set.insert(v) }
    }

    private func chip(_ text: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { AccountChip(text: text, muted: !on) }
            .buttonStyle(.plain)
            .accessibilityAddTraits(on ? .isSelected : [])
    }
}
