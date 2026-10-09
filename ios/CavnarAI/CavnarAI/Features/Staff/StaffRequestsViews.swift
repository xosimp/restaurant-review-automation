import SwiftUI

// The Requests tab (employee audit wave 2, I3 — UX-06/07/13/14/34, W2–W5,
// H2/H9/M6/M7): what waits on you first — each with its yes (Accept / Take
// it) as the primary and Decline as plain text, both confirmed — then your
// own requests; "Ask for time off" is the tab's one primary, pinned in thumb
// reach by the portal. Availability and preferences moved to Me.
// Same /staff/api routes as the web portal had, one for one; the payloads
// are in StaffRequestModels.swift.

// MARK: - Shared pieces for the staff screens (I3)

@MainActor
enum StaffUI {
    /// A section heading: the Account kicker (ink3, never ember inside the
    /// page — UX-16), and a VoiceOver heading so the rotor can jump (UX-18).
    static func header(_ text: String) -> some View {
        CavnarKicker(text)
            .padding(.top, CavnarSpace.xs)
    }

    /// A helper sentence — Ink2 (readable; Ink3 is for meta only).
    static func note(_ text: String, color: Color = .cavnarInk2) -> some View {
        CavnarMixedText(text, role: .secondary, color: color)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// A failure, said under the control that failed (DS §7, UX-13).
    static func errorLine(_ text: String) -> some View {
        Text(text)
            .cavnarText(.secondary, color: .cavnarRedText)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityLabel("Error: \(text)")
    }

    /// A section that didn't load: the reason in one red sentence and Try
    /// again — never an empty list, never a form (UX-07, C7).
    static func failedCard(_ message: String, retry: @escaping () -> Void) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            errorLine(message)
            StaffTextButton(title: "Try again", action: retry)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
    }

    /// The house loading line with a plain label — never "Loading…".
    static func loading(_ label: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            CavnarSkeletonBar(height: 3).frame(width: 180)
            Text(label).cavnarText(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// A button label while its request runs: the verb with the house shimmer,
/// still under Reduce Motion — never "…" (UX-27).
struct StaffShimmerLabel: View {
    let text: String
    var color: Color = .white
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Group {
            if reduceMotion {
                Text(text).foregroundStyle(color.opacity(0.6))
            } else {
                CavnarShimmerText(text: text, color: color)
            }
        }
        .accessibilityLabel(text)
    }
}

/// A text action in a row: ember2, 44pt tall however short the word.
struct StaffTextButton: View {
    let title: String
    var tone: Color = .cavnarEmber2
    var busy: Bool = false
    var busyTitle: String? = nil
    var disabled: Bool = false
    let action: () -> Void

    var body: some View {
        Button {
            action()
        } label: {
            Group {
                if busy { StaffShimmerLabel(text: busyTitle ?? title, color: tone) } else { Text(title) }
            }
            .font(.cavnar(.label))
            .foregroundStyle(disabled ? Color.cavnarInk3 : tone)
            .frame(minHeight: 44, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(busy || disabled)
    }
}

/// The DS Undo capsule (§12 "Undo toast (iOS)"), shown where the thing was:
/// these screens sit inside the container's scroll view, so the capsule
/// takes the row's place rather than the screen's foot.
struct StaffUndoCapsule: View {
    let text: String
    let undo: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Text(text)
                .cavnarText(.secondary, color: .cavnarInk)
                .lineLimit(2)
            Spacer(minLength: 8)
            Button {
                Haptic.light()
                undo()
            } label: {
                Text("Undo")
                    .cavnarText(.label, color: .cavnarEmber2)
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 16)
        .background(Color.cavnarPaper2, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.cavnarPaper3, lineWidth: 1))
        .accessibilityElement(children: .contain)
    }
}

extension StaffSessionStore {
    /// A staff GET with a query string — `authed(_:query:)`, so it shares
    /// the store's transport, bearer and ended-session rule (an ended
    /// session lands on the PIN pad with the server's sentence; it is not
    /// an explicit sign-out, so this person's cached screens stay).
    func staffGet<Response: Decodable>(_ path: String, query: [String: String]) async throws -> Response {
        try await authed(path, query: query)
    }
}

// MARK: - Bodies (the exact keys the web portal posted)

struct StaffTimeOffBody: Encodable, Equatable {
    let startDate: String
    let endDate: String
    let reason: String
    /// Part of each day off (schedule audit 10/3/26 D-39): off until a time,
    /// from a time, or lunch / dinner — none of them is the whole day.
    var startTime: String? = nil
    var endTime: String? = nil
    var daypart: String? = nil
    enum CodingKeys: String, CodingKey {
        case reason, daypart
        case startDate = "start_date"
        case endDate = "end_date"
        case startTime = "start_time"
        case endTime = "end_time"
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(startDate, forKey: .startDate)
        try c.encode(endDate, forKey: .endDate)
        try c.encode(reason, forKey: .reason)
        try c.encodeIfPresent(startTime, forKey: .startTime)
        try c.encodeIfPresent(endTime, forKey: .endTime)
        try c.encodeIfPresent(daypart, forKey: .daypart)
    }
}

struct StaffShiftChangeBody: Encodable, Equatable {
    var kind: String = "drop"
    let date: String
    let shiftStart: String
    var targetName: String? = nil
    var targetDate: String? = nil
    var targetStart: String? = nil
    let reason: String
    enum CodingKeys: String, CodingKey {
        case kind, date, reason
        case shiftStart = "shift_start"
        case targetName = "target_name"
        case targetDate = "target_date"
        case targetStart = "target_start"
    }
}

// MARK: - The Requests tab

/// `StaffRequestsView()` / `StaffRequestsView(refresh:portal:)` — content for
/// the container's scroll view (it does not scroll itself). `refresh`: bump
/// it (pull-to-refresh) to reload. `portal`: the container's store, so an
/// answer catches the Requests and Inbox badges up (`reloadBadges()`) and
/// a change reloads the week (`reloadShifts()`).
struct StaffRequestsView: View {
    var refresh: Int = 0
    var portal: StaffPortalStore?
    /// Scrolls the tab's scroll view to a row (the container's
    /// ScrollViewReader), for a push that opened a request.
    var scrollTo: ((String) -> Void)?
    /// The container's "Ask for time off" (pinned in its CavnarPinnedBar).
    /// Without one, the view draws the button itself at its foot.
    var askingTimeOff: Binding<Bool>?

    init(refresh: Int = 0, portal: StaffPortalStore? = nil, askingTimeOff: Binding<Bool>? = nil,
         scrollTo: ((String) -> Void)? = nil) {
        self.refresh = refresh
        self.portal = portal
        self.askingTimeOff = askingTimeOff
        self.scrollTo = scrollTo
    }

    private var timeOffPresented: Binding<Bool> { askingTimeOff ?? $showingTimeOff }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.scenePhase) private var scenePhase
    @State private var board: StaffLoad<StaffRequestsBoard> = .loading
    @State private var timeOff: StaffLoad<[StaffTimeOffRequest]> = .loading
    @State private var refreshNote: String?
    @State private var busy: Set<String> = []
    @State private var rowError: [String: String] = [:]
    @State private var postedLabel: String?
    @State private var confirming: Confirm?
    @State private var pendingUndo: PendingUndo?
    @State private var undoTask: Task<Void, Never>?
    @State private var showingTimeOff = false
    @State private var messaging: MessageSheet?
    /// The rows a push opened (portal.focus), ringed until the next load.
    @State private var focused: Set<String> = []

    /// The tier-2 confirms: what moves, named (UX-14). A decline is
    /// confirmed too (iOS readability round): it can't be taken back, so it
    /// never goes on one stray tap.
    enum Confirm: Identifiable {
        case ask(StaffSwapAsk), offer(StaffShiftOffer), claim(StaffOpenShift)
        case declineAsk(StaffSwapAsk), declineOffer(StaffShiftOffer)

        var id: String {
            switch self {
            case .ask(let a): return "ask\(a.id)"
            case .offer(let o): return "offer\(o.id)"
            case .claim(let s): return "open\(s.id)"
            case .declineAsk(let a): return "decline-ask\(a.id)"
            case .declineOffer(let o): return "decline-offer\(o.id)"
            }
        }

        var title: String {
            switch self {
            case .ask: return "Accept the swap?"
            case .offer(let o): return "Take \(o.shiftLabel)?"
            case .claim(let s): return "Pick up \(s.shiftLabel)?"
            case .declineAsk(let a): return "Decline \(a.employeeName)\u{2019}s swap?"
            case .declineOffer(let o): return "Turn down \(o.shiftLabel)?"
            }
        }

        var message: String {
            switch self {
            case .ask(let a): return a.confirmMessage
            case .offer(let o):
                return ["It goes on your schedule\(o.role.map { " as \($0)" } ?? ""), and your manager hears you said yes.",
                    o.overtimeNote ?? ""].filter { !$0.isEmpty }.joined(separator: " ")
            case .claim(let s):
                return ["It goes on your schedule\(s.role.map { " as \($0)" } ?? "").", s.overtimeNote ?? ""]
                    .filter { !$0.isEmpty }.joined(separator: " ")
            case .declineAsk(let a): return "\(a.employeeName) is told you said no. You keep your shift."
            case .declineOffer: return "Your manager is told you said no."
            }
        }

        var action: String {
            switch self {
            case .ask: return "Accept the swap"
            case .offer: return "Take it"
            case .claim: return "Pick it up"
            case .declineAsk, .declineOffer: return "Decline"
            }
        }

        var isDecline: Bool {
            switch self {
            case .declineAsk, .declineOffer: return true
            default: return false
            }
        }
    }

    struct PendingUndo: Equatable {
        let key: String
        let label: String
        let path: String
    }

    struct MessageSheet: Identifiable {
        let id = UUID()
        let context: StaffMessageContext?
    }

    var body: some View {
        VStack(alignment: .leading, spacing: CavnarSpace.s) {
            waitingSection
            yourRequestsSection
            if askingTimeOff == nil {
                Button {
                    showingTimeOff = true
                } label: {
                    Text("Ask for time off").frame(maxWidth: .infinity)
                }
                .buttonStyle(CavnarPrimaryButtonStyle())
                .padding(.top, 10)
            }
        }
        .task { await reload() }
        .onChange(of: refresh) { _, _ in Task { await reload() } }
        .onChange(of: portal?.focus) { _, _ in takeFocus() }
        .onChange(of: scenePhase) { _, phase in if phase == .active { Task { await reload() } } }
        // Leaving inside the Undo window keeps the request — the safe side.
        .onDisappear { cancelUndo() }
        .sheet(isPresented: timeOffPresented, onDismiss: { Task { await reload(); await portal?.reloadShifts() } }) {
            StaffTimeOffSheet().environment(staff)
        }
        .sheet(item: $messaging) { sheet in
            Group {
                if let portal {
                    StaffMessageThreadView(store: portal, context: sheet.context)
                } else {
                    StaffMessageThreadView(context: sheet.context)
                }
            }
            .environment(staff)
        }
        .confirmationDialog(confirming?.title ?? "", isPresented: Binding(
            get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                            titleVisibility: .visible, presenting: confirming) { c in
            Button(c.action, role: c.isDecline ? .destructive : nil) { Task { await perform(c) } }
            Button("Not now", role: .cancel) {}
        } message: { c in
            Text(c.message)
        }
    }

    // MARK: Waiting on you

    @ViewBuilder
    private var waitingSection: some View {
        switch board {
        case .loading:
            StaffUI.header("Waiting on you")
            StaffUI.loading("Loading your shift requests")
        case .failed(let message):
            StaffUI.header("Waiting on you")
            StaffUI.failedCard(message) { Task { await reload() } }
        case .loaded(let b):
            if b.hasWaiting || postedLabel != nil {
                StaffUI.header("Waiting on you")
                if let postedLabel {
                    CavnarInlinePosted(label: postedLabel) { self.postedLabel = nil }
                }
                ForEach(b.asks) { askRow($0) }
                ForEach(b.offers) { offerRow($0) }
                if !b.open.isEmpty {
                    Text("Open shifts")
                        .cavnarText(.label, color: .cavnarInk2)
                        .padding(.top, 4)
                        .accessibilityAddTraits(.isHeader)
                    ForEach(b.open) { openRow($0) }
                }
            }
        }
    }

    private func askRow(_ ask: StaffSwapAsk) -> some View {
        let key = "ask\(ask.id)"
        return VStack(alignment: .leading, spacing: 6) {
            Text("\(ask.employeeName) asked to swap")
                .cavnarText(.label)
            CavnarMixedText("Their shift: \(ask.theirShift)", role: .secondary)
            CavnarMixedText("Your shift: \(ask.yourShift)", role: .secondary)
            if ask.managerApproved {
                StaffUI.note("Your manager already said yes.")
            }
            // Accept is the primary, in thumb reach on the right; Decline is
            // plain text and confirmed first (it can't be taken back).
            answerRow(key: key, accept: "Accept",
                      decline: { confirming = .declineAsk(ask) },
                      accepting: { confirming = .ask(ask) })
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .staffFocusRing(focused.contains(key))
        .accessibilityElement(children: .contain)
        .id(key)
    }

    private func offerRow(_ offer: StaffShiftOffer) -> some View {
        let key = "offer\(offer.id)"
        return VStack(alignment: .leading, spacing: 6) {
            Text(offer.headline)
                .cavnarText(.label)
            CavnarMixedText(offer.shiftLabel + (offer.role.map { " · \($0)" } ?? ""), role: .secondary)
            if let note = offer.note {
                StaffUI.note("\u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            if offer.forCoverage {
                StaffUI.note("Your manager needs this one covered.")
            }
            if let ot = offer.overtimeNote {
                StaffUI.note(ot, color: .cavnarAmber)
            }
            answerRow(key: key, accept: "Take it",
                      decline: { confirming = .declineOffer(offer) },
                      accepting: { confirming = .offer(offer) })
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .staffFocusRing(focused.contains(key))
        .accessibilityElement(children: .contain)
        .id(key)
    }

    /// Decline as plain text on the left; the yes (Accept / Take it) as the
    /// one primary on the right, in thumb reach. Both confirm first.
    private func answerRow(key: String, accept: String, decline: @escaping () -> Void,
                           accepting: @escaping () -> Void) -> some View {
        HStack(spacing: CavnarSpace.s) {
            StaffTextButton(title: "Decline", tone: .cavnarInk2, disabled: busy.contains(key), action: decline)
                .accessibilityHint("Asks you to confirm before it\u{2019}s sent.")
            Spacer(minLength: 0)
            Button(action: accepting) {
                Group {
                    if busy.contains(key) { StaffShimmerLabel(text: "Answering") } else { Text(accept) }
                }
                .frame(minWidth: 120)
            }
            .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: busy.contains(key)))
        }
        .disabled(busy.contains(key))
        .padding(.top, 2)
    }

    private func openRow(_ shift: StaffOpenShift) -> some View {
        let key = "open\(shift.id)"
        return VStack(alignment: .leading, spacing: 4) {
            CavnarMixedText(shift.shiftLabel + (shift.role.map { " · \($0)" } ?? ""), role: .label)
            Text(shift.postedByManager ? "Posted by your manager"
                                       : (shift.employeeName.map { "\($0) can't work it" } ?? "Open"))
                .cavnarText(.secondary)
            if let ot = shift.overtimeNote {
                StaffUI.note(ot, color: .cavnarAmber)
            }
            if let why = shift.whyNotLine {
                StaffUI.note(why)
            }
            StaffTextButton(title: "Pick this up", busy: busy.contains(key), busyTitle: "Picking it up",
                            disabled: !shift.takeable) {
                confirming = .claim(shift)
            }
            .accessibilityHint(shift.takeable ? "Puts this shift on your schedule after you confirm." : (shift.whyNotLine ?? ""))
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .staffFocusRing(focused.contains(key))
        .accessibilityElement(children: .contain)
        .id(key)
    }

    // MARK: Your requests

    @ViewBuilder
    private var yourRequestsSection: some View {
        StaffUI.header("Your requests")
        let shifts = board.value?.requests
        let offs = timeOff.value
        if board.isLoading || timeOff.isLoading {
            StaffUI.loading("Loading your requests")
        } else {
            let hidden: Set<String> = pendingUndo.map { [$0.key] } ?? []
            let items = StaffYourRequest.merged(timeOff: offs ?? [], shifts: shifts ?? [], hiding: hidden)
            if let p = pendingUndo {
                StaffUndoCapsule(text: p.label) { cancelUndo() }
            }
            ForEach(items) { item in
                switch item {
                case .timeOff(let t): timeOffRow(t)
                case .shift(let s): shiftRow(s)
                }
            }
            if let f = timeOff.failure {
                StaffUI.failedCard("Your time off didn't load. \(f)") { Task { await reload() } }
            } else if board.failure != nil, offs != nil {
                StaffUI.note("Your shift changes didn't load — Try again above.", color: .cavnarRed)
            }
            if let refreshNote {
                StaffUI.note(refreshNote, color: .cavnarAmber)
            }
            if items.isEmpty, pendingUndo == nil, offs != nil, shifts != nil {
                CavnarEmptyHearth(title: "No requests yet",
                                  message: "Can\u{2019}t make a shift? Open it on Today and choose Give up shift or Swap shift. Days away go in a time-off request.")
            }
        }
    }

    private func timeOffRow(_ t: StaffTimeOffRequest) -> some View {
        let key = "to\(t.id)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                CavnarMixedText("Time off \(t.rangeLabel)", role: .label)
                Spacer(minLength: 6)
                TonePill(text: t.chip.text, tone: t.chip.tone)
            }
            if let reason = t.reason, !reason.isEmpty {
                StaffUI.note("You said: \u{201C}\(reason)\u{201D}")
            }
            if let note = t.decisionNote, !note.isEmpty {
                StaffUI.note("Your manager: \u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            if !t.stillScheduled.isEmpty {
                StaffUI.note("Still on your schedule: " + t.stillScheduled.map(\.label).joined(separator: "; ")
                             + ". Your manager moves these — you\u{2019}re on them until they do.",
                             color: .cavnarAmber)
            }
            HStack(spacing: 18) {
                if t.status == "pending" {
                    StaffTextButton(title: "Withdraw") {
                        startUndo(PendingUndo(key: key, label: "Withdrew time off \(t.rangeLabel)",
                                              path: "/staff/api/time-off/\(t.id)/withdraw"))
                    }
                } else if t.status == "approved" && t.canCancel {
                    StaffTextButton(title: "Call it off") {
                        startUndo(PendingUndo(key: key, label: "Called off time off \(t.rangeLabel)",
                                              path: "/staff/api/time-off/\(t.id)/cancel"))
                    }
                }
                if t.isLive {
                    StaffTextButton(title: "Message your manager") {
                        messaging = MessageSheet(context: .timeOff(t))
                    }
                }
            }
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .staffFocusRing(focused.contains(key))
        .accessibilityElement(children: .contain)
        .id(key)
    }

    private func shiftRow(_ r: StaffMyShiftRequest) -> some View {
        let key = "sr\(r.id)"
        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(r.tag)
                        .cavnarText(.kicker, color: .cavnarInk2)
                    CavnarMixedText(r.shiftLabel, role: .label)
                }
                Spacer(minLength: 6)
                TonePill(text: r.chip.text, tone: r.chip.tone)
            }
            if let target = r.targetLabel, let who = r.targetName {
                CavnarMixedText("For \(who)\u{2019}s \(target)", role: .secondary)
            }
            if let detail = r.detail {
                StaffUI.note(detail)
            }
            if let note = r.decisionNote, !note.isEmpty {
                StaffUI.note("Your manager: \u{201C}\(note)\u{201D}", color: .cavnarInk2)
            }
            HStack(spacing: 18) {
                if r.canWithdraw {
                    StaffTextButton(title: "Withdraw") {
                        startUndo(PendingUndo(key: key, label: "Withdrew \(r.tag.lowercased()) \(r.shiftLabel)",
                                              path: "/staff/api/shift-requests/\(r.id)/withdraw"))
                    }
                }
                if r.isLive {
                    StaffTextButton(title: "Message your manager") {
                        messaging = MessageSheet(context: .shiftRequest(r))
                    }
                }
            }
            if let e = rowError[key] { StaffUI.errorLine(e) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .cavnarCard()
        .staffFocusRing(focused.contains(key))
        .accessibilityElement(children: .contain)
        .id(key)
    }

    // MARK: Actions

    private func perform(_ c: Confirm) async {
        switch c {
        case .ask(let a):
            await act("ask\(a.id)", "/staff/api/shift-requests/\(a.id)/respond", body: StaffAcceptBody(accept: true),
                      posted: a.managerApproved ? "Swapped" : "Accepted — waiting on your manager")
        case .offer(let o):
            await act("offer\(o.id)", "/staff/api/offers/\(o.id)/respond", body: StaffAcceptBody(accept: true),
                      posted: "It\u{2019}s on your schedule")
        case .declineAsk(let a):
            await act("ask\(a.id)", "/staff/api/shift-requests/\(a.id)/respond", body: StaffAcceptBody(accept: false),
                      posted: "Declined — \(a.employeeName) is told")
        case .declineOffer(let o):
            await act("offer\(o.id)", "/staff/api/offers/\(o.id)/respond", body: StaffAcceptBody(accept: false),
                      posted: "Declined — your manager is told")
        case .claim(let s):
            await act("open\(s.id)", "/staff/api/open-shifts/\(s.id)/claim", body: StaffEmptyBody(),
                      posted: "It\u{2019}s on your schedule")
        }
    }

    /// One answer on one row: its error stays under that row; success is
    /// the posted check at the top of Waiting on you, never optimistic.
    private func act(_ key: String, _ path: String, body: any Encodable, posted: String) async {
        busy.insert(key)
        rowError[key] = nil
        defer { busy.remove(key) }
        do {
            let r: StaffOKResponse = try await staff.authed(path, method: .post, body: body)
            if r.ok {
                Haptic.success()
                postedLabel = posted
                await reload()
                await portal?.reloadBadges()
                await portal?.reloadShifts()
            } else {
                rowError[key] = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            rowError[key] = StaffErrorText.message(error)
        }
    }

    /// Tier 1: the request leaves the list now and goes to the server only
    /// once the 7-second Undo window ends. A second withdraw sends the
    /// first at once.
    private func startUndo(_ p: PendingUndo) {
        if let earlier = pendingUndo, earlier != p {
            undoTask?.cancel()
            Task { await commit(earlier) }
        }
        rowError[p.key] = nil
        pendingUndo = p
        undoTask?.cancel()
        undoTask = Task { @MainActor in
            try? await Task.sleep(for: .seconds(7))
            guard !Task.isCancelled, pendingUndo == p else { return }
            await commit(p)
        }
    }

    private func cancelUndo() {
        undoTask?.cancel()
        undoTask = nil
        pendingUndo = nil
    }

    private func commit(_ p: PendingUndo) async {
        do {
            let r: StaffOKResponse = try await staff.authed(p.path, method: .post, body: StaffEmptyBody())
            if !r.ok { rowError[p.key] = r.error ?? "That didn\u{2019}t go through." }
        } catch {
            rowError[p.key] = StaffErrorText.message(error)
        }
        if pendingUndo == p { pendingUndo = nil }
        await reload()
        await portal?.reloadShifts()
    }

    // MARK: Loading

    func reload() async {
        async let b: StaffRequestsBoard = staff.authed("/staff/api/shift-requests")
        async let t: StaffTimeOffList = staff.authed("/staff/api/time-off")
        var failed = false
        do {
            board = .loaded(try await b)
        } catch {
            failed = true
            if board.value == nil { board = .failed(StaffErrorText.message(error, fallback: "Your shift requests didn\u{2019}t load.")) }
        }
        do {
            timeOff = .loaded(try await t.requests)
        } catch {
            failed = true
            if timeOff.value == nil { timeOff = .failed(StaffErrorText.message(error, fallback: "Try again.")) }
        }
        // A refresh that failed keeps what loaded before, and says so.
        refreshNote = failed && (board.value != nil || timeOff.value != nil)
            ? "Couldn\u{2019}t refresh just now — this is what loaded earlier." : nil
        takeFocus()
    }

    /// A push that opened Requests (StaffPortalStore.focus): the request it
    /// names is scrolled to and ringed. Taken once the lists are in, then
    /// consumed.
    private func takeFocus() {
        guard let portal, let link = portal.focus, link.tab == .requests else { return }
        guard link.itemID != nil else { portal.consumeFocus(); return }
        guard board.value != nil || timeOff.value != nil else { return }
        portal.consumeFocus()
        let wanted = StaffRequestsFocus.keys(for: link, board: board.value)
        let present = Set(StaffYourRequest.merged(timeOff: timeOff.value ?? [], shifts: board.value?.requests ?? [])
                            .map(\.id))
            .union((board.value?.asks ?? []).map { "ask\($0.id)" })
            .union((board.value?.offers ?? []).map { "offer\($0.id)" })
            .union((board.value?.open ?? []).map { "open\($0.id)" })
        let hits = wanted.filter(present.contains)
        focused = Set(hits)
        if let first = hits.first { scrollTo?(first) }
    }
}

extension View {
    /// The row a notification opened: an ink3 ring on the card (not red,
    /// not ember — it is a pointer, not a state).
    func staffFocusRing(_ on: Bool) -> some View {
        overlay {
            if on {
                RoundedRectangle(cornerRadius: CavnarRadius.card)
                    .strokeBorder(Color.cavnarInk3, lineWidth: 1.5)
                    .accessibilityHidden(true)
            }
        }
    }
}

// MARK: - Ask for time off

/// `StaffTimeOffSheet()` — the dates, an optional reason, one primary.
struct StaffTimeOffSheet: View {
    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var start = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var end = Calendar.current.date(byAdding: .day, value: 1, to: Date()) ?? Date()
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?
    /// Part of the day (schedule audit 10/3/26 D-39): the whole day, off
    /// until a time, from a time, or lunch / dinner.
    @State private var part: Part = .whole
    @State private var untilTime = "4:00pm"
    @State private var fromTime = "5:00pm"

    enum Part: String, CaseIterable {
        case whole, until, from, lunch, dinner
        var label: String {
            switch self {
            case .whole: return "All day"
            case .until: return "Until\u{2026}"
            case .from: return "From\u{2026}"
            case .lunch: return "Lunch"
            case .dinner: return "Dinner"
            }
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    StaffUI.note("Your manager will approve or decline it.")
                    DatePicker("First day off", selection: $start, in: Date()..., displayedComponents: .date)
                        .font(.cavnar(.body))
                        .tint(Color.cavnarEmber)
                        .frame(minHeight: 44)
                    DatePicker("Last day off", selection: $end, in: start..., displayedComponents: .date)
                        .font(.cavnar(.body))
                        .tint(Color.cavnarEmber)
                        .frame(minHeight: 44)
                    partOfDay
                    TextField("Why (optional)", text: $reason, axis: .vertical)
                        .lineLimit(1...4)
                        .cavnarTextFieldStyle()
                    Button {
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { StaffShimmerLabel(text: "Sending") } else { Text("Ask for these days") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending))
                    .disabled(sending)
                    if let error { StaffUI.errorLine(error) }
                }
                .padding(20)
            }
            .accountSheetChrome("Time off")
        }
        .onChange(of: start) { _, new in if end < new { end = new } }
        .cavnarPostedOverlay(posted) { dismiss() }
    }

    /// "Part of the day": all day by default; off until a time (in late),
    /// from a time (gone early), or lunch / dinner only. Each day off in the
    /// range is the same part.
    private var partOfDay: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Part of the day")
                .cavnarText(.body)
            AccountFlowLayout(spacing: 6) {
                ForEach(Part.allCases, id: \.self) { p in
                    Button {
                        Haptic.selection()
                        part = p
                    } label: { AccountChip(text: p.label, muted: part != p) }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(part == p ? .isSelected : [])
                }
            }
            switch part {
            case .until:
                HStack(spacing: 8) {
                    Text("Off until").cavnarText(.secondary)
                    CavnarTimeChip(time: $untilTime, accessibilityName: "Off until")
                    Spacer(minLength: 0)
                }
            case .from:
                HStack(spacing: 8) {
                    Text("Off from").cavnarText(.secondary)
                    CavnarTimeChip(time: $fromTime, accessibilityName: "Off from")
                    Spacer(minLength: 0)
                }
            case .lunch, .dinner, .whole:
                EmptyView()
            }
        }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        var body = StaffTimeOffBody(startDate: StaffDay.iso(start), endDate: StaffDay.iso(end),
                                    reason: reason.trimmingCharacters(in: .whitespacesAndNewlines))
        switch part {
        case .whole: break
        case .until: body.endTime = untilTime
        case .from: body.startTime = fromTime
        case .lunch: body.daypart = "lunch"
        case .dinner: body.daypart = "dinner"
        }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/time-off", method: .post, body: body)
            if r.ok {
                Haptic.success()
                posted = "Sent to your manager"
            } else {
                error = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}

// MARK: - Give up or swap a shift

/// "Give up shift" (a drop) or "Swap shift" (their shift for
/// yours: your manager's yes and theirs). Opened from a shift on Today, or
/// from a conflict after an availability save.
///
/// `StaffShiftChangeSheet(day: StaffWeekDay, mode:)` or, for one leg of a
/// double or a conflict, `StaffShiftChangeSheet(date:shiftStart:shiftEnd:mode:)`.
struct StaffShiftChangeSheet: View {
    enum Mode: Hashable { case drop, swap }

    let date: String
    let shiftStart: String
    let shiftEnd: String?
    let mode: Mode

    init(date: String, shiftStart: String, shiftEnd: String? = nil, mode: Mode) {
        self.date = date
        self.shiftStart = shiftStart
        self.shiftEnd = shiftEnd
        self.mode = mode
    }

    init(day: StaffWeekDay, mode: Mode) {
        self.init(date: day.date, shiftStart: day.shift?.shiftStart ?? "", shiftEnd: day.shift?.shiftEnd, mode: mode)
    }

    @Environment(StaffSessionStore.self) private var staff
    @Environment(\.dismiss) private var dismiss
    @State private var candidates: StaffLoad<StaffSwapCandidates> = .loading
    @State private var colleague: String?
    @State private var theirShiftId: String?
    @State private var pickingColleague = true
    @State private var pickingShift = false
    @State private var reason = ""
    @State private var sending = false
    @State private var error: String?
    @State private var posted: String?

    private var chosen: StaffSwapCandidate? { candidates.value?.colleagues.first { $0.name == colleague } }
    private var theirShift: StaffSwapCandidate.Shift? { chosen?.shifts.first { $0.id == theirShiftId } }
    private var ready: Bool { mode == .drop || theirShift != nil }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    CavnarMixedText(StaffDay.shift(date, shiftStart, shiftEnd), role: .lead)
                    // What happens next, before the button that does it.
                    StaffUI.note(mode == .swap
                                 ? "Both shifts move once your manager approves and your colleague says yes."
                                 : "Your manager decides. If they approve, you\u{2019}re still on it until someone on the team picks it up.")
                    if mode == .swap { swapPickers }
                    TextField(mode == .swap ? "Why (optional)" : "Why can\u{2019}t you work it? (optional)",
                              text: $reason, axis: .vertical)
                        .lineLimit(1...4)
                        .cavnarTextFieldStyle()
                    Button {
                        Task { await send() }
                    } label: {
                        Group {
                            if sending { StaffShimmerLabel(text: "Sending") }
                            else { Text(mode == .swap ? "Ask to swap" : "Ask to give it up") }
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(CavnarPrimaryButtonStyle(isDisabled: sending || !ready))
                    .disabled(sending || !ready)
                    if let error { StaffUI.errorLine(error) }
                }
                .padding(20)
            }
            .accountSheetChrome(mode == .swap ? "Swap shift" : "Give up shift")
        }
        .task { if mode == .swap { await loadCandidates() } }
        .cavnarPostedOverlay(posted) { dismiss() }
    }

    @ViewBuilder
    private var swapPickers: some View {
        switch candidates {
        case .loading:
            StaffUI.loading("Finding who can swap")
        case .failed(let message):
            StaffUI.failedCard(message) { Task { await loadCandidates() } }
        case .loaded(let list):
            if list.colleagues.isEmpty {
                StaffUI.note(list.note ?? "Nobody on the app has a shift that week you could trade for.")
            } else {
                VStack(alignment: .leading, spacing: 10) {
                    CavnarDropdown(title: "Swap with", subtitle: colleague ?? "Choose a colleague",
                                   isExpanded: $pickingColleague) {
                        VStack(spacing: 0) {
                            ForEach(list.colleagues) { c in
                                choiceRow(c.name, selected: c.name == colleague) {
                                    colleague = c.name
                                    theirShiftId = nil          // never preselect their shift (WF-08)
                                    pickingColleague = false
                                    pickingShift = true
                                }
                            }
                        }
                    }
                    if let chosen {
                        CavnarDropdown(title: "For their shift", subtitle: theirShift?.label ?? "Choose their shift",
                                       isExpanded: $pickingShift) {
                            VStack(spacing: 0) {
                                ForEach(chosen.shifts) { s in
                                    choiceRow(s.label + (s.role.map { " · \($0)" } ?? ""), selected: s.id == theirShiftId,
                                              detail: s.checked ? nil : "We\u{2019}ll check this when you ask") {
                                        theirShiftId = s.id
                                        pickingShift = false
                                    }
                                }
                            }
                        }
                    }
                    if let note = list.note { StaffUI.note(note) }
                }
                .cavnarCard()
            }
        }
    }

    private func choiceRow(_ title: String, selected: Bool, detail: String? = nil,
                           action: @escaping () -> Void) -> some View {
        Button {
            Haptic.selection()
            action()
        } label: {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    HomeMixedText.make(title, role: selected ? .label : .body, color: .cavnarInk)
                    if let detail {
                        Text(detail).cavnarText(.caption, color: .cavnarInk2)
                    }
                }
                Spacer(minLength: 8)
                if selected {
                    Image(systemName: "checkmark")
                        .font(.cavnar(.secondary).weight(.bold))
                        .foregroundStyle(Color.cavnarEmber)
                }
            }
            .frame(minHeight: 44)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func loadCandidates() async {
        candidates = .loading
        do {
            candidates = .loaded(try await staff.staffGet("/staff/api/colleagues",
                                                          query: ["shift_date": date, "shift_start": shiftStart]))
        } catch {
            candidates = .failed(StaffErrorText.message(error, fallback: "Couldn\u{2019}t load who can swap."))
        }
    }

    private func send() async {
        sending = true
        error = nil
        defer { sending = false }
        let why = reason.trimmingCharacters(in: .whitespacesAndNewlines)
        var body = StaffShiftChangeBody(date: date, shiftStart: shiftStart, reason: why)
        if mode == .swap {
            guard let theirShift, let colleague else { return }
            body.kind = "swap"
            body.targetName = colleague
            body.targetDate = theirShift.date
            body.targetStart = theirShift.shiftStart
        }
        do {
            let r: StaffOKResponse = try await staff.authed("/staff/api/shift-requests", method: .post, body: body)
            if r.ok {
                Haptic.success()
                posted = mode == .swap ? "Asked — your manager and \(colleague ?? "your colleague") are told"
                                       : "Sent to your manager"
            } else {
                error = r.error ?? "That didn\u{2019}t go through."
            }
        } catch {
            self.error = StaffErrorText.message(error)
        }
    }
}
