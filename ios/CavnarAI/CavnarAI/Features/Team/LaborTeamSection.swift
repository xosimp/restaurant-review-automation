import SwiftUI
import Observation
import Charts

/// What the team says and reads, on Labor's page (parity audit 10/7/26):
/// the Team inbox's way in with its unread count (#10) and tonight's lineup
/// brief waiting for approval (#26). Each part shows only for a login the
/// server answers for — a 403 hides it rather than showing an empty card.
///
/// Under Needs you only what waits on the owner shows (re-audit 10/8/26
/// M14): the brief while its draft waits for approval, the inbox while
/// something in it is unread or somebody is running late. How shifts felt
/// (#67) is a reading, not a decision — it sits under Why
/// (`StaffPulseTile(summary: model.pulse)`), fed by the same model, which
/// the Labor screen owns so both places read one load.
struct LaborTeamSection: View {
    let model: LaborTeamModel
    /// Opens the Team inbox, on a thread when one is named.
    var openInbox: (Int?) -> Void
    /// Whether the inbox sheet is up — its counts re-read when it closes.
    var inboxOpen: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            // Always on screen, so the load runs even before any part has
            // something to show.
            Color.clear.frame(height: 0)
                .task { await model.load() }
            VStack(alignment: .leading, spacing: 14) {
                if model.briefWaits { LineupBriefCard(model: model) }
                if model.inboxWaits { inboxRow }
            }
        }
        .onChange(of: inboxOpen) { _, open in
            if !open { Task { await model.loadInbox() } }
        }
    }

    private var inboxRow: some View {
        Button {
            Haptic.light()
            openInbox(nil)
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "tray.full")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Color.cavnarEmber2)
                    .frame(width: 28)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Team inbox")
                        .font(.cavnarBody(CavnarType.body, weight: 700))
                        .foregroundStyle(Color.cavnarInk)
                    HomeMixedText.make(model.inboxLine, size: CavnarType.caption, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if model.unread > 0 {
                    Text("\(model.unread)")
                        .font(.cavnarNumber(CavnarType.caption, weight: 700))
                        .foregroundStyle(Color.white)
                        .padding(.horizontal, 8)
                        .frame(minHeight: 22)
                        .background(Capsule().fill(Color.cavnarEmber))
                }
                Image(systemName: "chevron.right")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundStyle(Color.cavnarInk3)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .cavnarCard()
        .accessibilityLabel("Team inbox" + (model.unread > 0 ? ", \(model.unread) unread" : ""))
        .accessibilityHint("Messages from staff, who\u{2019}s running late, announcements")
    }
}

@Observable
@MainActor
final class LaborTeamModel {
    private(set) var inboxAvailable = false
    private(set) var unread = 0
    private(set) var lateCount = 0
    private(set) var pulse: StaffPulseSummary?
    private(set) var brief: LineupBrief?
    private(set) var canEditBrief = false
    private(set) var briefBusy = false
    var briefMessage: String?
    var briefError: String?

    private let client: APIClient
    init(client: APIClient = .shared) { self.client = client }

    static let briefPath = "/mobile/api/staff-brief"
    static let pulsePath = "/mobile/api/labor/staff-pulse"

    /// Something in the inbox waits on the owner: an unread message or a
    /// teammate running late today (M14).
    var inboxWaits: Bool { inboxAvailable && (unread > 0 || lateCount > 0) }
    /// Tonight's brief has a draft waiting for approval (M14).
    var briefWaits: Bool { brief?.isWaiting == true }
    /// Anything of the team's under Needs you.
    var needsYou: Bool { inboxWaits || briefWaits }

    /// "2 unread messages · 1 running late today" — the web's summary line.
    var inboxLine: String {
        var parts: [String] = []
        if unread > 0 { parts.append("\(unread) unread message" + (unread == 1 ? "" : "s")) }
        if lateCount > 0 { parts.append("\(lateCount) running late today") }
        return parts.isEmpty ? "Messages from staff, who\u{2019}s running late, announcements"
                             : parts.joined(separator: " \u{00B7} ")
    }

    func load() async {
        async let i: Void = loadInbox()
        async let b: Void = loadBrief()
        async let p: Void = loadPulse()
        _ = await (i, b, p)
    }

    func loadInbox() async {
        guard let r: TeamInboxResponse = try? await client.send(TeamInboxViewModel.inboxPath, hapticOnError: false),
              r.ok else { return }
        inboxAvailable = true
        unread = r.unread
        lateCount = r.lateToday.count
    }

    func loadPulse() async {
        guard let r: StaffPulseResponse = try? await client.send(Self.pulsePath, query: ["days": "14"],
                                                                 hapticOnError: false),
              r.ok else { return }
        pulse = r.summary
    }

    func loadBrief() async {
        guard let r: LineupBriefResponse = try? await client.send(Self.briefPath, hapticOnError: false),
              r.ok, let b = r.brief else { return }
        brief = b
        canEditBrief = r.canEdit
    }

    /// One write, answered with the saved brief (every staff-brief write is).
    private func write(_ path: String, body: some Encodable & Sendable, ok: String, timeout: TimeInterval? = nil) async {
        briefBusy = true
        briefError = nil
        briefMessage = nil
        defer { briefBusy = false }
        do {
            let r: LineupBriefResponse = try await client.send(path, method: .post, body: body, timeout: timeout,
                                                               retryTransient: false)
            guard r.ok, let b = r.brief else { briefError = r.error ?? "Couldn\u{2019}t save that."; return }
            brief = b
            briefMessage = ok
            Haptic.success()
        } catch let e as APIClient.APIError {
            briefError = e.message
        } catch {
            briefError = "Couldn\u{2019}t reach Cavnar AI."
        }
    }

    func approve(text: String) async {
        guard let day = brief?.day else { return }
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else {
            briefError = "Write the brief first, or leave it and the team reads the lines."
            return
        }
        await write(Self.briefPath + "/approve", body: LineupApproveBody(day: day, text: t),
                    ok: "Approved \u{2014} the team sees it now.")
    }

    func withdraw() async {
        guard let day = brief?.day else { return }
        await write(Self.briefPath + "/withdraw", body: LineupDayBody(day: day), ok: "The team sees the lines again.")
    }

    func draft() async {
        guard let day = brief?.day else { return }
        await write(Self.briefPath + "/draft", body: LineupDayBody(day: day),
                    ok: "Drafted \u{2014} read it before you approve it.", timeout: 60)
        if brief?.hasDraft != true, briefError == nil { briefMessage = nil }
    }

    func setFocus(item: String, line: String) async {
        guard let day = brief?.day else { return }
        let i = item.trimmingCharacters(in: .whitespacesAndNewlines)
        await write(Self.briefPath + "/focus",
                    body: LineupFocusBody(day: day, item: i, line: i.isEmpty ? "" : line.trimmingCharacters(in: .whitespacesAndNewlines)),
                    ok: i.isEmpty ? "Focus cleared." : "Focus saved.")
    }
}

// MARK: - Lineup brief

/// Tonight's lineup brief before service (#26) — the web's card in
/// Account → Notifications: the lines every working teammate sees, the
/// AI draft waiting for approval (nothing the AI wrote reaches staff until
/// someone who publishes the schedule approves it), and tonight's focus.
///
/// At rest (iOS readability round, 10/8/26) it is the brief's words, its
/// status and Approve; writing, drafting, withdrawing and tonight's focus
/// are in a sheet. Under Needs you it is a card only while it waits on an
/// approval — once approved it folds to one row that opens the same sheet.
struct LineupBriefCard: View {
    let model: LaborTeamModel

    @State private var text = ""
    @State private var focusItem = ""
    @State private var focusLine = ""
    @State private var seededFor = ""
    @State private var showingAllLines = false
    @State private var confirmApprove = false
    @State private var confirmWithdraw = false
    @State private var editing = false

    var body: some View {
        if let b = model.brief {
            Group {
                if b.isApproved {
                    approvedRow(b)
                } else {
                    restingCard(b)
                }
            }
            .onAppear { seed(b) }
            .onChange(of: b) { _, nb in seed(nb) }
            .sheet(isPresented: $editing) { editSheet }
            .confirmationDialog("Approve this brief for the team?", isPresented: $confirmApprove,
                                titleVisibility: .visible) {
                Button(b.isApproved ? "Save the brief" : "Approve for the team") {
                    Task { await model.approve(text: text) }
                }
                Button("Not yet", role: .cancel) {}
            } message: {
                Text("Everyone working \(b.weekday) reads it in the app right away.")
            }
            .confirmationDialog("Show the team the plain lines instead?", isPresented: $confirmWithdraw,
                                titleVisibility: .visible) {
                Button("Show the lines instead", role: .destructive) { Task { await model.withdraw() } }
                Button("Keep the brief", role: .cancel) {}
            }
        }
    }

    /// The brief's words (the draft, else the first lines), its status and
    /// one Approve — secondary, as every approve on Labor is.
    private func restingCard(_ b: LineupBrief) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            HStack(alignment: .firstTextBaseline) {
                CavnarKicker("Lineup brief \u{00B7} \(b.weekday) \(CavnarDate.mdy(b.day))")
                Spacer(minLength: CavnarSpace.xs)
                AccountChip(text: b.isWaiting ? "Draft waiting" : "Lines only", muted: !b.isWaiting)
            }
            if let draft = b.draftText?.trimmingCharacters(in: .whitespacesAndNewlines), !draft.isEmpty {
                CavnarMixedText(draft, role: .body, color: .cavnarInk)
                    .lineLimit(4)
            } else {
                lines(b, cap: 2)
            }
            if let m = model.briefMessage {
                Text(m).cavnarText(.secondary, color: .cavnarGreen)
            }
            if let e = model.briefError {
                Text(e).cavnarText(.secondary, color: .cavnarRedText)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if model.canEditBrief {
                HStack(spacing: CavnarSpace.s) {
                    if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        Button {
                            Haptic.light()
                            confirmApprove = true
                        } label: {
                            Group {
                                if model.briefBusy { CavnarShimmerText(text: "Saving\u{2026}") } else { Text("Approve") }
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.briefBusy))
                        .disabled(model.briefBusy)
                    }
                    Button {
                        Haptic.light()
                        editing = true
                    } label: {
                        Text(b.isWaiting ? "Edit" : "Write it").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.briefBusy))
                    .disabled(model.briefBusy)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// Approved: nothing waits on the owner, so one row — the sheet keeps
    /// Save, Withdraw and tonight's focus a tap away.
    private func approvedRow(_ b: LineupBrief) -> some View {
        Button {
            Haptic.light()
            editing = true
        } label: {
            HStack(spacing: CavnarSpace.s) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.cavnar(.body))
                    .foregroundStyle(Color.cavnarGreen)
                    .accessibilityHidden(true)
                CavnarMixedText("Lineup brief approved for \(b.weekday) \(CavnarDate.mdy(b.day))", role: .label)
                Spacer(minLength: 0)
                Image(systemName: "chevron.right")
                    .font(.cavnar(.caption))
                    .foregroundStyle(Color.cavnarInk3)
                    .accessibilityHidden(true)
            }
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, CavnarSpace.xxs)
        .accessibilityHint("Opens the brief and tonight's focus")
    }

    /// Everything that writes the brief: the lines, the words, Approve /
    /// Withdraw / Draft it for me, and tonight's focus.
    private var editSheet: some View {
        NavigationStack {
            ScrollView {
                if let b = model.brief {
                    VStack(alignment: .leading, spacing: CavnarSpace.l) {
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            HStack {
                                CavnarMixedText("\(b.weekday) \(CavnarDate.mdy(b.day))", role: .label)
                                Spacer()
                                AccountChip(text: b.isApproved ? "Approved" : (b.isWaiting ? "Draft waiting" : "Lines only"),
                                            muted: !b.isApproved && !b.isWaiting,
                                            tint: b.isApproved ? .cavnarGreen : nil)
                            }
                            Text("What your team reads before service. Nothing Cavnar AI writes reaches them until you approve it.")
                                .cavnarText(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                            CavnarKicker("The lines")
                            lines(b, cap: 3)
                        }
                        if model.canEditBrief {
                            editor(b)
                            focusEditor(b)
                        } else {
                            if let approved = b.approvedText, !approved.isEmpty {
                                Text(approved)
                                    .cavnarText(.body)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                            CavnarMixedText(b.focus.map { "Tonight\u{2019}s focus: \($0.item)" + (($0.line ?? "").isEmpty ? "" : " \u{2014} \($0.line!)") }
                                            ?? "No focus item tonight.", role: .secondary)
                        }
                        if let m = model.briefMessage {
                            Text(m).cavnarText(.secondary, color: .cavnarGreen)
                        }
                        if let e = model.briefError {
                            Text(e).cavnarText(.secondary, color: .cavnarRedText)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .padding(CavnarSpace.gutter)
                }
            }
            .scrollDismissesKeyboard(.immediately)
            .accountSheetChrome("Lineup brief")
        }
        .presentationDetents([.large])
        .presentationDragIndicator(.visible)
    }

    /// The text box starts on the approved words, else the draft; refilled
    /// whenever the server answers with a new brief.
    private func seed(_ b: LineupBrief) {
        let key = b.day + "|" + (b.approvedText ?? "") + "|" + (b.draftText ?? "") + "|" + (b.focus?.item ?? "")
        guard seededFor != key else { return }
        seededFor = key
        text = b.approvedText ?? b.draftText ?? ""
        focusItem = b.focus?.item ?? ""
        focusLine = b.focus?.line ?? ""
    }

    @ViewBuilder
    private func lines(_ b: LineupBrief, cap: Int) -> some View {
        if b.items.isEmpty {
            Text("Nothing to tell the team yet today.")
                .cavnarText(.body)
        } else {
            let shown = showingAllLines ? b.items : Array(b.items.prefix(cap))
            VStack(alignment: .leading, spacing: CavnarSpace.xs) {
                ForEach(Array(shown.enumerated()), id: \.offset) { _, item in
                    HStack(alignment: .firstTextBaseline, spacing: CavnarSpace.xs) {
                        Circle().fill(Color.cavnarEmber2).frame(width: 5, height: 5).offset(y: -2)
                            .accessibilityHidden(true)
                        CavnarMixedText(item.text, role: .body)
                    }
                }
                if b.items.count > cap {
                    CavnarMoreToggle(hiddenCount: b.items.count - cap, total: b.items.count,
                                     isExpanded: $showingAllLines)
                }
            }
        }
    }

    @ViewBuilder
    private func editor(_ b: LineupBrief) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Brief for the team")
            TextField("Write a short brief, or leave it and the team reads the lines above.", text: $text, axis: .vertical)
                .lineLimit(3...8)
                .cavnarTextFieldStyle()
                .onChange(of: text) { _, v in if v.count > 600 { text = String(v.prefix(600)) } }
            Text(b.draftSaid + (b.draftItemsChanged ? " The lines changed since it was drafted." : ""))
                .cavnarText(.caption, color: .cavnarInk2)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: CavnarSpace.s) {
                Button {
                    Haptic.light()
                    if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        model.briefError = "Write the brief first, or leave it and the team reads the lines."
                    } else {
                        confirmApprove = true
                    }
                } label: {
                    Group {
                        if model.briefBusy { CavnarShimmerText(text: "Saving\u{2026}") }
                        else { Text(b.isApproved ? "Save the brief" : "Approve") }
                    }
                }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.briefBusy))
                .disabled(model.briefBusy)
                if b.isApproved {
                    Button {
                        Haptic.light()
                        confirmWithdraw = true
                    } label: { Text("Withdraw") }
                    .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.briefBusy))
                    .disabled(model.briefBusy)
                }
                if b.canAskForDraft {
                    Button {
                        Haptic.light()
                        Task { await model.draft() }
                    } label: { Text("Draft it for me") }
                    .buttonStyle(CavnarSoftButtonStyle(isDisabled: model.briefBusy))
                    .disabled(model.briefBusy)
                }
            }
        }
    }

    @ViewBuilder
    private func focusEditor(_ b: LineupBrief) -> some View {
        VStack(alignment: .leading, spacing: CavnarSpace.xs) {
            CavnarKicker("Tonight\u{2019}s focus")
            Text("The team sees the item and your line, never why it was suggested.")
                .cavnarText(.secondary)
            HStack(spacing: CavnarSpace.xs) {
                TextField("Item (Fall old fashioned)", text: $focusItem)
                    .cavnarTextFieldStyle()
                    .onChange(of: focusItem) { _, v in if v.count > 80 { focusItem = String(v.prefix(80)) } }
                if !b.suggestions.isEmpty {
                    Menu {
                        ForEach(b.suggestions, id: \.self) { s in
                            Button {
                                focusItem = s.item
                            } label: {
                                if let why = s.why, !why.isEmpty {
                                    Text(s.item); Text(why)
                                } else {
                                    Text(s.item)
                                }
                            }
                        }
                    } label: {
                        Image(systemName: "sparkles")
                            .font(.cavnar(.body))
                            .foregroundStyle(Color.cavnarEmber2)
                            .frame(width: 44, height: 44)
                            .background(Color.cavnarEmber.opacity(0.14), in: Circle())
                    }
                    .accessibilityLabel("Ideas for tonight\u{2019}s focus")
                }
            }
            TextField("Your line for the team", text: $focusLine, axis: .vertical)
                .lineLimit(1...3)
                .cavnarTextFieldStyle()
                .onChange(of: focusLine) { _, v in if v.count > 160 { focusLine = String(v.prefix(160)) } }
            HStack(spacing: CavnarSpace.s) {
                Button {
                    Haptic.light()
                    Task { await model.setFocus(item: focusItem, line: focusLine) }
                } label: { Text("Save focus") }
                .buttonStyle(CavnarSecondaryButtonStyle(isDisabled: model.briefBusy))
                .disabled(model.briefBusy)
                if b.focus != nil {
                    Button {
                        Haptic.light()
                        Task { await model.setFocus(item: "", line: "") }
                    } label: {
                        Text("Clear")
                            .cavnarText(.label, color: .cavnarInk2)
                            .cavnarHitTarget()
                    }
                    .buttonStyle(.plain)
                    .disabled(model.briefBusy)
                }
            }
        }
    }
}

// MARK: - Staff pulse

/// How shifts felt to the staff who answered the post-shift question in the
/// app, last 14 days (#67) — in aggregate only. Below
/// staff_insights.PULSE_MIN_N answers it is the count sentence and nothing
/// else, so nobody's answer is read off a quiet night.
struct StaffPulseTile: View {
    let summary: StaffPulseSummary?
    @State private var showingDays = false

    var body: some View {
        if let s = summary {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    CavnarKicker("How shifts felt")
                    Spacer()
                    HomeMixedText.make(s.window?.label ?? "Last \(s.days) days", size: CavnarType.caption,
                                       weight: 600, color: .cavnarInk2)
                }
                if s.enough {
                    HStack(alignment: .bottom, spacing: 18) {
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(alignment: .firstTextBaseline, spacing: 4) {
                                Text(s.averageText)
                                    .font(.cavnarNumber(CavnarType.cardNumber, weight: 600))
                                    .foregroundStyle(Color.cavnarInk)
                                    .cavnarNumberGlow()
                                Text("of 5")
                                    .font(.cavnarBody(CavnarType.secondary))
                                    .foregroundStyle(Color.cavnarInk3)
                            }
                            HomeMixedText.make("\(s.responses) answers after their shifts",
                                               size: CavnarType.caption, color: .cavnarInk3)
                        }
                        distributionChart(s)
                            .frame(height: 64)
                    }
                    if !s.byDay.isEmpty || !s.notes.isEmpty { details(s) }
                } else {
                    HomeMixedText.make(s.floorLine, size: CavnarType.body, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .cavnarCard()
            .accessibilityElement(children: .contain)
        }
    }

    /// Answers by rating, 1 to 5 — an ember gradient that warms toward 5.
    private func distributionChart(_ s: StaffPulseSummary) -> some View {
        let bars = (1...5).map { (rating: $0, count: s.distribution[String($0)] ?? 0) }
        return Chart(bars, id: \.rating) { bar in
            BarMark(x: .value("Rating", String(bar.rating)), y: .value("Answers", bar.count))
                .foregroundStyle(LinearGradient(colors: [Color.cavnarEmber.opacity(0.45 + 0.11 * Double(bar.rating)),
                                                         Color.cavnarEmber2],
                                                startPoint: .bottom, endPoint: .top))
                .cornerRadius(3)
        }
        .chartYAxis(.hidden)
        .chartXAxis {
            AxisMarks { _ in
                AxisValueLabel().font(.cavnarNumber(CavnarType.tag, weight: 600)).foregroundStyle(Color.cavnarInk3)
            }
        }
        .accessibilityLabel("Answers by rating: " + bars.map { "\($0.rating): \($0.count)" }.joined(separator: ", "))
    }

    @ViewBuilder
    private func details(_ s: StaffPulseSummary) -> some View {
        Button {
            Haptic.selection()
            withAnimation(.easeOut(duration: 0.2)) { showingDays.toggle() }
        } label: {
            HStack(spacing: 6) {
                Text(showingDays ? "Hide the nights" : "By night, and what they said")
                    .font(.cavnarBody(CavnarType.secondary, weight: 700))
                    .foregroundStyle(Color.cavnarEmber2)
                Image(systemName: showingDays ? "chevron.up" : "chevron.down")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(Color.cavnarEmber2)
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        if showingDays {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(s.byDay.prefix(7)) { d in
                    HStack {
                        HomeMixedText.make(CavnarDate.mdy(d.date), size: CavnarType.secondary, weight: 600)
                        Spacer()
                        HomeMixedText.make("\(d.average.map { String(format: "%.1f", $0) } ?? "\u{2014}") of 5 \u{00B7} \(d.responses) answers",
                                           size: CavnarType.secondary, color: .cavnarInk3)
                    }
                }
                if s.otherDaysResponses > 0 {
                    HomeMixedText.make("\(s.otherDaysResponses) more came on nights with too few answers to show on their own.",
                                       size: CavnarType.caption, color: .cavnarInk3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                ForEach(Array(s.notes.prefix(5).enumerated()), id: \.offset) { _, n in
                    HomeMixedText.make("\u{201C}\(n.text)\u{201D}" + (n.dateLabel.map { " \u{00B7} \($0)" } ?? ""),
                                       size: CavnarType.secondary, color: .cavnarInk2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}
