import SwiftUI
import Observation

/// What the next draft is built to, set where the draft is built (iOS
/// parity #46 and the auto-draft row, 10/7/26) — the web Studio's Setup and
/// AI tabs: the labor target (a ceiling, the restaurant's one number — Home,
/// alerts and the next draft read it), the owner's notes on how they like
/// the week built (`sched_notes`, every draft reads them) with each sentence
/// as Cavnar AI reads it and the rules made from them, and the weekly
/// automation (draft day, auto-publish). Each saves on its own; the full
/// Targets & pay rates form stays in Account.
@Observable
@MainActor
final class ScheduleBuildSettings {
    private(set) var targets: BuildTargets?
    private(set) var canEditTargets = false
    private(set) var noteRules: NoteRulesPayload?
    private(set) var autoDraft: ScheduleAutoDraft?
    private(set) var autoPublish: ScheduleAutoPublish?
    /// Which save is in flight ("target", "notes", "rule:3", "auto_draft"…).
    private(set) var saving: String?
    /// What the last save did, in the web's words.
    private(set) var status: String?
    private(set) var error: String?
    /// The target the stepper shows while its save settles.
    private(set) var targetShown: Double?
    private var targetSave: Task<Void, Never>?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let targetRange: ClosedRange<Double> = 5...60
    static let targetStep: Double = 0.5

    /// The labor target on screen: the one being set, else the saved one.
    var laborTarget: Double? { targetShown ?? targets?.laborTargetPct }
    var notes: String { targets?.schedNotes ?? "" }
    var heldRuleCount: Int { noteRules?.rules.count ?? 0 }

    /// "Keep two cooks on Friday lunch. · 2 held as rules" — the row's line.
    var notesLine: String {
        let text = notes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return "Nothing written yet \u{2014} every draft reads what you write here" }
        let first = text.split(whereSeparator: { $0 == "\n" }).first.map(String.init) ?? text
        let clipped = first.count > 70 ? String(first.prefix(68)) + "\u{2026}" : first
        let n = heldRuleCount
        return n > 0 ? "\(clipped) \u{00B7} \(n) held as \(n == 1 ? "a rule" : "rules")" : clipped
    }

    private struct EnabledBody: Encodable { let enabled: Bool }
    private struct WeekdayBody: Encodable { let weekday: Int }

    func load(weekStart: String? = nil) async {
        async let t: BuildTargetsResponse? = try? client.send("/mobile/api/account/targets", hapticOnError: false)
        async let d: ScheduleAutoDraft? = try? client.send("/mobile/api/labor/auto-draft", hapticOnError: false)
        async let p: ScheduleAutoPublish? = try? client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        let (tr, dr, pr) = await (t, d, p)
        if let tr, tr.ok {
            targets = tr.targets
            canEditTargets = tr.canEdit
        }
        if let dr { autoDraft = dr }
        if let pr { autoPublish = pr }
        await loadNoteRules(weekStart: weekStart)
    }

    func loadNoteRules(weekStart: String? = nil) async {
        let q = (weekStart ?? "").isEmpty ? [:] : ["week_start": weekStart ?? ""]
        if let r: NoteRulesPayload = try? await client.send("/mobile/api/labor/note-rules", query: q,
                                                             hapticOnError: false), r.ok {
            noteRules = r
        }
    }

    // MARK: Labor target

    /// One step of the stepper. The save waits for the value to settle —
    /// a run of taps is one save, as the web's cavSettled (Simple EJ's 35%
    /// became 31.5% in seven saves in two seconds, 10/2/26).
    func stepTarget(by delta: Double) {
        guard canEditTargets, let current = laborTarget else { return }
        let next = min(Self.targetRange.upperBound, max(Self.targetRange.lowerBound, current + delta))
        guard next != current else { return }
        Haptic.selection()
        targetShown = next
        status = nil
        targetSave?.cancel()
        targetSave = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(800))
            guard !Task.isCancelled else { return }
            await self?.saveTarget(next)
        }
    }

    func saveTarget(_ value: Double) async {
        saving = "target"
        error = nil
        defer { saving = nil }
        do {
            let r: BuildTargetsResponse = try await client.send(
                "/mobile/api/account/targets", method: .post, body: LaborTargetBody(laborTargetPct: value))
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t save the target."; targetShown = nil; return }
            if let t = r.targets { targets = t } else { targets?.laborTargetPct = value }
            targetShown = nil
            status = "Saved \u{00B7} your labor target is now \(Self.pct(value))% everywhere: Home, alerts and the next draft"
            Haptic.success()
        } catch let e as APIClient.APIError {
            error = e.message
            targetShown = nil
        } catch {
            self.error = "Couldn\u{2019}t save the target."
            targetShown = nil
        }
    }

    nonisolated static func pct(_ v: Double) -> String { v == v.rounded() ? String(Int(v)) : String(format: "%.1f", v) }

    // MARK: How you like the week built

    @discardableResult
    func saveNotes(_ text: String) async -> Bool {
        saving = "notes"
        error = nil
        defer { saving = nil }
        do {
            let r: BuildTargetsResponse = try await client.send(
                "/mobile/api/account/targets", method: .post, body: SchedNotesBody(schedNotes: text))
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t save the notes."; return false }
            if let t = r.targets { targets = t } else { targets?.schedNotes = text }
            status = "Saved \u{00B7} every draft reads these"
            Haptic.success()
            await loadNoteRules(weekStart: noteRules?.weekOf)
            return true
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t save the notes."
        }
        return false
    }

    private struct RuleResponse: Decodable { let ok: Bool; let error: String? }

    /// Make a sentence a rule the draft is built to and checked against —
    /// every week, or the week of `week_of` only. Nil when made, else why not.
    func addRule(source: String, role: String, min: Int, dayparts: [String], days: [String], everyWeek: Bool) async -> String? {
        guard !role.isEmpty else { return "Pick a role." }
        saving = "rule:add"
        defer { saving = nil }
        let body = NoteRuleAddBody(role: role, min: min, dayparts: dayparts, days: days,
                                   scope: everyWeek ? "every" : "week", weekStart: noteRules?.weekOf ?? "",
                                   sourceText: source)
        do {
            let r: RuleResponse = try await client.send("/mobile/api/labor/note-rules", method: .post, body: body)
            guard r.ok else { return r.error ?? "Couldn\u{2019}t make that a rule." }
            status = "Rule made \u{2014} every draft is checked against it"
            Haptic.success()
            await loadNoteRules(weekStart: noteRules?.weekOf)
            return nil
        } catch let e as APIClient.APIError {
            return e.message
        } catch {
            return "Couldn\u{2019}t make that a rule."
        }
    }

    func removeRule(_ id: Int) async {
        saving = "rule:\(id)"
        error = nil
        defer { saving = nil }
        do {
            let r: RuleResponse = try await client.send("/mobile/api/labor/note-rules/\(id)/remove", method: .post,
                                                        body: [String: String]())
            guard r.ok else { error = r.error ?? "Couldn\u{2019}t remove that rule."; return }
            status = "Rule removed \u{2014} the note is guidance again"
            Haptic.success()
            await loadNoteRules(weekStart: noteRules?.weekOf)
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t remove that rule."
        }
    }

    // MARK: The automation

    func setAutoDraft(_ on: Bool) async {
        saving = "auto_draft"
        error = nil
        defer { saving = nil }
        do {
            autoDraft = try await client.send("/mobile/api/labor/auto-draft", method: .post, body: EnabledBody(enabled: on))
            autoPublish = try? await client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t change that."
        }
    }

    func setAutoDraftDay(_ weekday: Int) async {
        saving = "auto_draft_day"
        error = nil
        defer { saving = nil }
        do {
            autoDraft = try await client.send("/mobile/api/labor/auto-draft", method: .post, body: WeekdayBody(weekday: weekday))
            autoPublish = try? await client.send("/mobile/api/labor/auto-publish", hapticOnError: false)
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t change the day."
        }
    }

    func setAutoPublish(_ on: Bool) async {
        saving = "auto_publish"
        error = nil
        defer { saving = nil }
        do {
            autoPublish = try await client.send("/mobile/api/labor/auto-publish", method: .post, body: EnabledBody(enabled: on))
        } catch let e as APIClient.APIError {
            error = e.message
        } catch {
            self.error = "Couldn\u{2019}t change that."
        }
    }

    static let draftDays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
}

// MARK: - In the Generate card

/// Three rows under the week picker: the labor target with a stepper, the
/// notes the draft reads, and the weekly automation — so what shapes the
/// draft sits where the draft is made, not only under Account.
struct GenerateBuildRows: View {
    @Bindable var settings: ScheduleBuildSettings
    let onOpenNotes: () -> Void
    @State private var confirmingAutoPublish = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            targetRow
            hairline
            Button {
                Haptic.light()
                onOpenNotes()
            } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("How you like the week built")
                            .font(.cavnarBody(14, weight: 700))
                            .foregroundStyle(Color.cavnarInk)
                        HomeMixedText.make(settings.notesLine, size: 12.5, color: .cavnarInk3)
                            .lineLimit(2)
                            .multilineTextAlignment(.leading)
                    }
                    Spacer(minLength: 6)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Color.cavnarEmber2)
                }
                .frame(minHeight: 48)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityHint("Opens your scheduling notes and the rules made from them")
            if let draft = settings.autoDraft, draft.externalTool.isEmpty {
                hairline
                autoDraftRow(draft)
                if let publish = settings.autoPublish {
                    hairline
                    autoPublishRow(publish)
                }
            }
            if let error = settings.error {
                Text(error).font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarRed)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 6)
            } else if let status = settings.status {
                HomeMixedText.make(status, size: 12.5, weight: 600, color: .cavnarGreen)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.bottom, 6)
            }
        }
        .padding(.horizontal, 12)
        .background(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .fill(Color.cavnarPaper.opacity(0.35)))
        .overlay(RoundedRectangle(cornerRadius: CavnarRadius.control, style: .continuous)
            .strokeBorder(Color.cavnarPaper3.opacity(0.6), lineWidth: 1))
        .confirmationDialog("Publish the schedule on its own?", isPresented: $confirmingAutoPublish,
                            titleVisibility: .visible) {
            Button("Turn on auto-publish") { Task { await settings.setAutoPublish(true) } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Once your record arms it, the drafted week goes to every employee \(settings.autoPublish?.day ?? "the day after the draft") without you pressing Send. You're told first and can undo it.")
        }
    }

    private var hairline: some View {
        Rectangle().fill(Color.cavnarPaper3.opacity(0.5)).frame(height: 1)
    }

    private var targetRow: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Labor target")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                Text("A ceiling, not a quota \u{2014} the draft keeps labor at or under it")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if let target = settings.laborTarget {
                HStack(spacing: 4) {
                    if settings.canEditTargets {
                        stepButton("minus", delta: -ScheduleBuildSettings.targetStep,
                                   disabled: target <= ScheduleBuildSettings.targetRange.lowerBound)
                    }
                    (Text(ScheduleBuildSettings.pct(target)).font(.cavnarNumber(16, weight: 700))
                     + Text("%").font(.cavnarNumber(12, weight: 600)))
                        .foregroundStyle(Color.cavnarInk)
                        .frame(minWidth: 46)
                        .opacity(settings.saving == "target" ? 0.6 : 1)
                        .accessibilityLabel("Labor target \(ScheduleBuildSettings.pct(target)) percent")
                    if settings.canEditTargets {
                        stepButton("plus", delta: ScheduleBuildSettings.targetStep,
                                   disabled: target >= ScheduleBuildSettings.targetRange.upperBound)
                    }
                }
            } else {
                Text("\u{2014}").font(.cavnarNumber(16, weight: 700)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .frame(minHeight: 52)
    }

    private func stepButton(_ symbol: String, delta: Double, disabled: Bool) -> some View {
        Button {
            settings.stepTarget(by: delta)
        } label: {
            Image(systemName: symbol)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(Color.cavnarEmber2)
                .frame(width: 34, height: 34)
                .background(Circle().fill(Color.cavnarPaper2))
                .overlay(Circle().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.4 : 1)
        .accessibilityLabel(delta > 0 ? "Raise the labor target" : "Lower the labor target")
    }

    private func autoDraftRow(_ draft: ScheduleAutoDraft) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    Text("Cavnar AI drafts every")
                        .font(.cavnarBody(14, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    if draft.canEdit {
                        Menu {
                            ForEach(Array(ScheduleBuildSettings.draftDays.enumerated()), id: \.offset) { i, day in
                                Button(day) { Task { await settings.setAutoDraftDay(i) } }
                            }
                        } label: {
                            HStack(spacing: 3) {
                                Text(draft.day).font(.cavnarBody(14, weight: 700))
                                Image(systemName: "chevron.down").font(.system(size: 9, weight: .bold))
                            }
                            .foregroundStyle(Color.cavnarEmber2)
                        }
                        .disabled(settings.saving == "auto_draft_day")
                    } else {
                        Text(draft.day).font(.cavnarBody(14, weight: 700)).foregroundStyle(Color.cavnarInk)
                    }
                }
                Text("A draft only \u{2014} nothing goes to staff until you send it")
                    .font(.cavnarBody(12.5))
                    .foregroundStyle(Color.cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            // Only a login the save allows gets the switch (re-audit
            // 10/8/26 #12); any other reads the state.
            if draft.canEdit {
                AccountStateSwitch(isOn: Binding(get: { draft.enabled },
                                                 set: { on in Task { await settings.setAutoDraft(on) } }),
                                   busy: settings.saving == "auto_draft")
            } else {
                stateText(draft.enabled)
            }
        }
        .frame(minHeight: 56)
    }

    private func autoPublishRow(_ p: ScheduleAutoPublish) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Publish it \(p.day ?? "the day after")")
                    .font(.cavnarBody(14, weight: 700))
                    .foregroundStyle(Color.cavnarInk)
                HomeMixedText.make(p.armed ? "Armed \u{2014} \(p.trust) schedules went out unedited in a row"
                                           : "Your record: \(p.trust) of \(p.needed) unedited schedules in a row",
                                   size: 12.5, color: .cavnarInk3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 6)
            if p.canEdit {
                AccountStateSwitch(isOn: Binding(get: { p.enabled }, set: { on in
                    if on { confirmingAutoPublish = true } else { Task { await settings.setAutoPublish(false) } }
                }), busy: settings.saving == "auto_publish", optimistic: false)
            } else {
                stateText(p.enabled)
            }
        }
        .frame(minHeight: 56)
    }

    /// "On" / "Off" for a login that may read the automation but not switch it.
    private func stateText(_ on: Bool) -> some View {
        Text(on ? "On" : "Off")
            .font(.cavnarBody(14, weight: 700))
            .foregroundStyle(on ? Color.cavnarGreen : Color.cavnarInk3)
            .accessibilityLabel(on ? "On \u{2014} your login can\u{2019}t change it" : "Off \u{2014} your login can\u{2019}t change it")
    }
}

// MARK: - The notes sheet

/// "How you like the week built" and each sentence as Cavnar AI reads it —
/// a minimum it can hold becomes a rule the draft is checked against once
/// the owner confirms it (every week, or the picked week only).
struct ScheduleNotesSheet: View {
    @Bindable var settings: ScheduleBuildSettings
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var seeded = false
    @State private var ruleFor: RuleTarget?
    @State private var removing: NoteRule?

    struct RuleTarget: Identifiable {
        let id: Int
        let source: String
        let draft: NoteRuleDraft
    }

    private var editable: Bool { settings.canEditTargets }
    private var changed: Bool {
        text.trimmingCharacters(in: .whitespacesAndNewlines) != settings.notes.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    AccountSection(kicker: "How you like the week built") {
                        VStack(alignment: .leading, spacing: 10) {
                            TextField("e.g. Keep two cooks on Friday lunch. Open with a bartender on game days.",
                                      text: $text, axis: .vertical)
                                .cavnarTextFieldStyle()
                                .lineLimit(3...10)
                                .disabled(!editable)
                            Text("These are about the whole restaurant, and every draft reads them. A rule about one person \u{2014} \u{201C}no Tuesdays\u{201D}, \u{201C}out 12/20\u{2013}12/28\u{201D} \u{2014} belongs in that person\u{2019}s scheduling notes in Scheduling setup.")
                                .font(.cavnarBody(13))
                                .foregroundStyle(Color.cavnarInk3)
                                .fixedSize(horizontal: false, vertical: true)
                            if editable {
                                Button {
                                    Task { await settings.saveNotes(text) }
                                } label: {
                                    Group {
                                        if settings.saving == "notes" { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Save notes") }
                                    }
                                    .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: !changed || settings.saving != nil))
                                .disabled(!changed || settings.saving != nil)
                            } else {
                                Text("Only the account owner can change these.")
                                    .font(.cavnarBody(13)).foregroundStyle(Color.cavnarAmber)
                            }
                        }
                        .padding(.vertical, 12)
                    }
                    readings
                    if let error = settings.error {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    } else if let status = settings.status {
                        HomeMixedText.make(status, size: 13.5, weight: 600, color: .cavnarGreen)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .padding(20)
            }
            .scrollDismissesKeyboard(.interactively)
            .accountSheetChrome("Scheduling notes")
        }
        .onAppear {
            guard !seeded else { return }
            seeded = true
            text = settings.notes
        }
        .task { await settings.loadNoteRules(weekStart: settings.noteRules?.weekOf) }
        .sheet(item: $ruleFor) { target in
            NoteRuleForm(settings: settings, target: target)
        }
        .confirmationDialog(removing.map { "Stop holding \u{201C}\($0.words)\u{201D}?" } ?? "",
                            isPresented: Binding(get: { removing != nil }, set: { if !$0 { removing = nil } }),
                            titleVisibility: .visible) {
            Button("Remove the rule", role: .destructive) {
                if let r = removing { Task { await settings.removeRule(r.id) } }
                removing = nil
            }
            Button("Cancel", role: .cancel) { removing = nil }
        } message: {
            Text("The note stays; the draft only aims for it again.")
        }
    }

    @ViewBuilder
    private var readings: some View {
        let payload = settings.noteRules
        let sentences = payload?.sentences ?? []
        let earlier = payload?.earlierRules ?? []
        if sentences.isEmpty && earlier.isEmpty {
            Text("Write a note above and Cavnar AI shows here how it reads each one \u{2014} a minimum it can hold, like \u{201C}keep two cooks on Friday lunch\u{201D}, becomes a rule the draft is checked against.")
                .font(.cavnarBody(13.5))
                .foregroundStyle(Color.cavnarInk3)
                .fixedSize(horizontal: false, vertical: true)
        } else {
            VStack(alignment: .leading, spacing: 10) {
                AccountKicker(text: "How Cavnar AI reads your notes")
                ForEach(sentences) { s in sentenceRow(s, canEdit: payload?.canEdit == true) }
                if !earlier.isEmpty {
                    AccountKicker(text: "Rules from earlier notes").padding(.top, 6)
                    ForEach(earlier) { r in ruleRow(source: r.sourceText, words: r.words, stale: r.stale, rule: r,
                                                     canEdit: payload?.canEdit == true) }
                }
                if payload?.canEdit != true {
                    Text("Only the account owner can turn a note into a rule.")
                        .font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3)
                }
            }
        }
    }

    @ViewBuilder
    private func sentenceRow(_ s: NoteSentence, canEdit: Bool) -> some View {
        if let id = s.ruleId {
            let rule = settings.noteRules?.rules.first { $0.id == id }
            ruleRow(source: s.text, words: s.enforced ?? rule?.words ?? "", stale: s.stale ?? rule?.stale,
                    rule: rule ?? NoteRule(id: id, words: s.enforced ?? ""), canEdit: canEdit)
        } else {
            let (tone, head, body): (Color, String, String) = {
                switch s.kind {
                case "rule": return (.cavnarEmber2, "Cavnar AI can hold this as a minimum.",
                                     (s.why.map { $0 + " " } ?? "") + "Until you make it a rule, the draft only aims for it.")
                case "unchecked": return (.cavnarAmber, "Not checked.",
                                          (s.why ?? "") + " The draft aims for it; check the draft against it yourself.")
                case "person": return (.cavnarAmber, "About one person.", s.why ?? "")
                default: return (.cavnarInk3, "Guidance.", "The draft aims for it; nothing checks it.")
                }
            }()
            VStack(alignment: .leading, spacing: 6) {
                HomeMixedText.make("\u{201C}\(s.text)\u{201D}", size: 14, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
                (Text(head + " ").font(.cavnarBody(13, weight: 700)).foregroundStyle(tone)
                 + Text(body).font(.cavnarBody(13)).foregroundStyle(Color.cavnarInk3))
                    .fixedSize(horizontal: false, vertical: true)
                if canEdit, s.kind == "rule" || s.kind == "unchecked" {
                    Button {
                        Haptic.light()
                        ruleFor = RuleTarget(id: s.id, source: s.text,
                                             draft: s.rule ?? NoteRuleDraft(min: 1, dayparts: ["morning", "night"]))
                    } label: {
                        Text(s.kind == "rule" ? "Make it a rule" : "Make a minimum from it")
                            .font(.cavnarBody(13.5, weight: 700))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(minHeight: 36)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.white.opacity(0.03)))
        }
    }

    private func ruleRow(source: String?, words: String, stale: String?, rule: NoteRule, canEdit: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let source, !source.isEmpty {
                HomeMixedText.make("\u{201C}\(source)\u{201D}", size: 14, weight: 600, color: .cavnarInk)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                Image(systemName: stale == nil ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(stale == nil ? Color.cavnarGreen : Color.cavnarAmber)
                HomeMixedText.make((stale.map { $0 + " " } ?? "A rule the draft is checked against: ") + words,
                                   size: 13, color: .cavnarInk2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if canEdit {
                Button(role: .destructive) {
                    removing = rule
                } label: {
                    Text(settings.saving == "rule:\(rule.id)" ? "Removing\u{2026}" : "Remove the rule")
                        .font(.cavnarBody(13, weight: 600))
                        .foregroundStyle(Color.cavnarInk3)
                        .frame(minHeight: 36)
                }
                .buttonStyle(.plain)
                .disabled(settings.saving != nil)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .fill((stale == nil ? Color.cavnarGreen : Color.cavnarAmber).opacity(0.07)))
    }
}

/// Confirm a minimum: at least N of a role at lunch, dinner or both, on the
/// days picked (none is every day) — every week, or one week only.
private struct NoteRuleForm: View {
    let settings: ScheduleBuildSettings
    let target: ScheduleNotesSheet.RuleTarget
    @Environment(\.dismiss) private var dismiss
    @State private var role = ""
    @State private var min = 1
    @State private var part = "both"
    @State private var days: Set<String> = []
    @State private var error: String?
    @State private var seeded = false

    private static let week = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    private var roles: [String] {
        let all = settings.noteRules?.roles ?? []
        let choices = target.draft.roleChoices ?? []
        return choices.isEmpty ? all : choices + all.filter { !choices.contains($0) }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    HomeMixedText.make("\u{201C}\(target.source)\u{201D}", size: 15, weight: 600, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                    AccountSection(kicker: "The rule") {
                        AccountKVRow(label: "At least") {
                            Stepper(value: $min, in: 1...10) {
                                Text("\(min)").font(.cavnarNumber(16, weight: 700)).foregroundStyle(Color.cavnarInk)
                            }
                            .fixedSize()
                        }
                        AccountKVRow(label: "Role") {
                            Picker("Role", selection: $role) {
                                Text("Pick a role").tag("")
                                ForEach(roles, id: \.self) { Text($0).tag($0) }
                            }
                            .tint(Color.cavnarEmber)
                        }
                        AccountKVRow(label: "At", showsDivider: false) {
                            Picker("At", selection: $part) {
                                Text("Lunch").tag("morning")
                                Text("Dinner").tag("night")
                                Text("Both").tag("both")
                            }
                            .pickerStyle(.segmented)
                            .frame(maxWidth: 220)
                        }
                    }
                    VStack(alignment: .leading, spacing: 8) {
                        AccountKicker(text: "On")
                        AccountFlowLayout(spacing: 6) {
                            ForEach(Self.week, id: \.self) { d in
                                Button {
                                    Haptic.selection()
                                    if days.contains(d) { days.remove(d) } else { days.insert(d) }
                                } label: { AccountChip(text: String(d.prefix(3)), muted: !days.contains(d)) }
                                .buttonStyle(.plain)
                                .accessibilityAddTraits(days.contains(d) ? .isSelected : [])
                            }
                        }
                        Text("None picked is every day.").font(.cavnarBody(12.5)).foregroundStyle(Color.cavnarInk3)
                    }
                    if let error {
                        Text(error).font(.cavnarBody(13.5)).foregroundStyle(Color.cavnarRed)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    VStack(spacing: 10) {
                        Button { Task { await make(everyWeek: true) } } label: {
                            Text("Make it a rule \u{00B7} every week").frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: role.isEmpty || settings.saving != nil))
                        .disabled(role.isEmpty || settings.saving != nil)
                        if let week = settings.noteRules?.weekOf {
                            Button { Task { await make(everyWeek: false) } } label: {
                                Text("Week of \(CavnarDate.mdy(week)) only").frame(maxWidth: .infinity)
                            }
                            .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: role.isEmpty || settings.saving != nil))
                            .disabled(role.isEmpty || settings.saving != nil)
                        }
                    }
                }
                .padding(20)
            }
            .accountSheetChrome("Make it a rule")
        }
        .presentationDetents([.large])
        .onAppear {
            guard !seeded else { return }
            seeded = true
            role = target.draft.role ?? ""
            min = Swift.max(1, Swift.min(10, target.draft.min ?? 1))
            let parts = target.draft.dayparts ?? ["morning", "night"]
            part = parts.count == 1 ? parts[0] : "both"
            days = Set(target.draft.days ?? [])
        }
    }

    private func make(everyWeek: Bool) async {
        error = nil
        let parts = part == "both" ? ["morning", "night"] : [part]
        let ordered = Self.week.filter { days.contains($0) }
        if let why = await settings.addRule(source: target.source, role: role, min: min, dayparts: parts,
                                            days: ordered, everyWeek: everyWeek) {
            error = why
        } else {
            dismiss()
        }
    }
}
